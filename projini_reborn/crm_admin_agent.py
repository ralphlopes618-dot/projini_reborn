import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta


DB_NAME = "leads.db"
ENV_FILE = ".env"
OPENROUTER_MODEL = "poolside/laguna-xs-2.1"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_LLM_MAX_TOKENS = 1024         # enough tokens to finish multi-option activity prompts
DEFAULT_LLM_TOOL_RESULT_MAX_CHARS = 3000  # reduced from 6000 to trim tool result payload
DEFAULT_LLM_VALUE_CATALOG_MAX_VALUES = 6  # reduced from 12 to shrink system prompt size
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_VOICE_RESPONSE_MAX_CHARS = 1200
ASSIGNMENT_RULES_FILE = "assignment_rules.json"
PROHIBITED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|"
    r"REINDEX|ANALYZE|PRAGMA)\b",
    flags=re.IGNORECASE,
)
NO_ACTIVITY_SQL = "(ActivityType IS NULL OR ActivityStatus IS NULL OR ActivityNote IS NULL)"
ACTIVITY_TYPES = ("Call", "Meeting", "Task", "Site Visit")
ACTIVITY_STATUSES = ("Pending", "Completed", "Cancelled")
MEETING_RESPONSES = (
    "Interested",
    "Not Interested",
    "Call Back Later",
    "Follow-up Required",
    "Site Visit Scheduled",
    "Proposal Requested",
    "Price Negotiation",
    "No Response",
    "Invalid / Wrong Number",
    "Customer Cancelled",
)
ACTIVITY_EXTRA_COLUMNS = {
    "MeetingResponse": "TEXT",
    "ActivityDateTime": "DATETIME",
    "Feedback": "TEXT",
}


def load_env(path=ENV_FILE):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_int(name, default, minimum=None, maximum=None):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(name, default, minimum=None, maximum=None):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return default

    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def llm_max_tokens():
    return env_int("OPENROUTER_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS, minimum=128, maximum=8192)


def llm_tool_result_max_chars():
    return env_int("LLM_TOOL_RESULT_MAX_CHARS", DEFAULT_LLM_TOOL_RESULT_MAX_CHARS, minimum=1000, maximum=20000)


def llm_value_catalog_max_values():
    return env_int(
        "LLM_VALUE_CATALOG_MAX_VALUES",
        DEFAULT_LLM_VALUE_CATALOG_MAX_VALUES,
        minimum=3,
        maximum=25,
    )


class CRMAdminAgent:
    def __init__(self, db_name=DB_NAME):
        load_env()
        self.db_name = db_name
        self.confirmation_callback = None

    def connect(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_activity_columns(conn)
        ensure_available_properties_and_assigned_properties(conn)
        return conn

    def schema(self):
        with self.connect() as conn:
            tables = [
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                )
            ]

            result = {}
            for table in tables:
                result[table] = [
                    {
                        "name": col["name"],
                        "type": col["type"],
                        "not_null": bool(col["notnull"]),
                        "default": col["dflt_value"],
                        "primary_key": bool(col["pk"]),
                    }
                    for col in conn.execute(f"PRAGMA table_info({quote_identifier(table)})")
                ]
            return result

    def schema_text(self):
        lines = []
        for table, columns in self.schema().items():
            lines.append(f"Table: {table}")
            for column in columns:
                marker = " PRIMARY KEY" if column["primary_key"] else ""
                lines.append(f"- {column['name']} {column['type']}{marker}")
        return "\n".join(lines)

    def table_names(self):
        return list(self.schema().keys())

    def table_columns(self, table):
        schema = self.schema()
        return [column["name"] for column in schema.get(table, [])]

    def primary_key_column(self, table):
        for column in self.schema().get(table, []):
            if column["primary_key"]:
                return column["name"]
        return None

    def value_catalog(self, max_values=None):
        if max_values is None:
            max_values = llm_value_catalog_max_values()

        catalog = {}
        with self.connect() as conn:
            for table, columns in self.schema().items():
                table_catalog = {}
                for column in columns:
                    column_type = (column["type"] or "").upper()
                    if not any(kind in column_type for kind in ("TEXT", "CHAR", "DATE")):
                        continue
                    column_name = column["name"]
                    rows = conn.execute(
                        f"""
                        SELECT DISTINCT {quote_identifier(column_name)} AS value
                        FROM {quote_identifier(table)}
                        WHERE {quote_identifier(column_name)} IS NOT NULL
                          AND TRIM(CAST({quote_identifier(column_name)} AS TEXT)) != ''
                        ORDER BY value
                        LIMIT ?
                        """,
                        (max_values,),
                    ).fetchall()
                    values = [row["value"] for row in rows]
                    if values:
                        table_catalog[column_name] = values
                if table_catalog:
                    catalog[table] = table_catalog
        return catalog

    def execute_read_sql(self, sql, params=None, limit=DEFAULT_LIMIT):
        params = tuple(params or ())
        limit = clamp_limit(limit)
        safe_sql = make_safe_read_sql(sql, limit)

        with self.connect() as conn:
            conn.set_authorizer(read_only_authorizer)
            rows = [dict(row) for row in conn.execute(safe_sql, params).fetchall()]

        return {
            "sql": safe_sql,
            "params": list(params),
            "row_count": len(rows),
            "rows": rows,
        }

    def get_lead(self, lead_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM Lead_Details WHERE LeadId = ?",
                (lead_id,),
            ).fetchone()
        return dict(row) if row else None

    def summarize_lead(self, lead_id, use_llm=False):
        lead = self.get_lead(lead_id)
        if not lead:
            return {"error": f"No lead found with LeadId {lead_id}"}

        lead["AssignedProperties"] = self.assigned_properties_for_lead(lead_id)
        deterministic_summary = build_lead_summary(lead)
        if not use_llm:
            return deterministic_summary

        prompt = (
            "Summarize this CRM lead for an admin. Include customer requirements, "
            "budget/property interest, source, stage, last activity, pending activities, "
            "and assigned employee. Be concise.\n\n"
            f"Lead JSON:\n{json.dumps(lead, indent=2)}"
        )
        llm_text = self.call_llm(prompt)
        deterministic_summary["ai_summary"] = llm_text
        return deterministic_summary

    def search(self, query, limit=DEFAULT_LIMIT, use_llm=True):
        limit = clamp_limit(limit)
        heuristic = self.query_to_sql(query, limit)
        if heuristic:
            sql, params = heuristic
            result = self.execute_read_sql(sql, params, limit=limit)
            result.update({"query": query, "mode": "heuristic"})
            return result

        if use_llm:
            generated = self.sql_for_natural_language_query(query, limit)
            if "sql" in generated:
                try:
                    result = self.execute_read_sql(generated["sql"], generated.get("params", ()), limit=limit)
                    result.update(
                        {
                            "query": query,
                            "mode": "llm_sql",
                            "reason": generated.get("reason"),
                        }
                    )
                    return result
                except (ValueError, sqlite3.Error) as exc:
                    generated = {"error": f"Generated SQL rejected: {exc}", "generated": generated}

        sql, params = self.keyword_query_to_sql(query, limit)
        result = self.execute_read_sql(sql, params, limit=limit)
        result.update({"query": query, "mode": "keyword_fallback"})
        return result

    def query_to_sql(self, query, limit=DEFAULT_LIMIT):
        lowered = query.lower()

        if "duplicate" in lowered and ("mobile" in lowered or "phone" in lowered or "number" in lowered):
            return (
                """
                SELECT *
                FROM Lead_Details
                WHERE MobileNo IN (
                    SELECT MobileNo
                    FROM Lead_Details
                    WHERE MobileNo IS NOT NULL AND TRIM(MobileNo) != ''
                    GROUP BY MobileNo
                    HAVING COUNT(*) > 1
                )
                ORDER BY MobileNo, LeadId
                LIMIT ?
                """,
                (limit,),
            )

        if "untouched" in lowered:
            conditions = [NO_ACTIVITY_SQL]
            params = []
            if "cold" in lowered:
                conditions.append("Priority = ?")
                params.append("Cold")
            if "hot" in lowered:
                conditions.append("Priority = ?")
                params.append("Hot")
            if "this week" in lowered:
                conditions.append("date(CreatedAt) >= date('now', '-7 days')")
            params.append(limit)
            return (
                f"""
                SELECT *
                FROM Lead_Details
                WHERE {" AND ".join(conditions)}
                ORDER BY CreatedAt DESC, LeadId DESC
                LIMIT ?
                """,
                tuple(params),
            )

        if "pending" in lowered and "activit" in lowered:
            assignee = extract_assignee(query, self.employee_names())
            params = []
            conditions = ["ActivityStatus = ?"]
            params.append("Pending")
            if assignee:
                conditions.append("AssignedTo = ?")
                params.append(assignee)
            params.append(limit)
            return (
                f"""
                SELECT *
                FROM Lead_Details
                WHERE {" AND ".join(conditions)}
                ORDER BY UpdatedAt DESC, LeadId DESC
                LIMIT ?
                """,
                tuple(params),
            )

        params = []
        conditions = []
        for priority in ("Hot", "Warm", "Cold"):
            if priority.lower() in lowered:
                conditions.append("Priority = ?")
                params.append(priority)

        source = extract_source(query, self.sources())
        if source:
            conditions.append("Source = ?")
            params.append(source)

        assignee = extract_assignee(query, self.employee_names())
        if assignee:
            conditions.append("AssignedTo = ?")
            params.append(assignee)

        if not conditions:
            return None

        params.append(limit)
        return (
            f"""
            SELECT *
            FROM Lead_Details
            WHERE {" AND ".join(conditions)}
            ORDER BY UpdatedAt DESC, LeadId DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def keyword_query_to_sql(self, query, limit=DEFAULT_LIMIT):
        text_columns = self.text_columns("Lead_Details")
        like_parts = [f"{quote_identifier(col)} LIKE ?" for col in text_columns]
        params = [f"%{query}%" for _ in text_columns]
        params.append(limit)
        return (
            f"""
            SELECT *
            FROM Lead_Details
            WHERE {" OR ".join(like_parts)}
            ORDER BY UpdatedAt DESC, LeadId DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def sql_for_natural_language_query(self, query, limit=DEFAULT_LIMIT):
        prompt = f"""
Convert the admin's CRM question into one safe SQLite SELECT query.

Current date: {datetime.now().date().isoformat()}
Database schema:
{self.schema_text()}

Known values for matching:
{json.dumps(self.value_catalog(), ensure_ascii=False)}

Question: {query}

Rules:
- Return exactly one JSON object: {{"sql": "SELECT ... LIMIT {limit}", "params": [], "reason": "short reason"}}
- Use any relevant table or column from the schema.
- Only generate read-only SELECT or WITH queries.
- Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, PRAGMA, ATTACH, or multiple statements.
- Prefer SELECT * for lead rows unless aggregation is requested.
- Use case-insensitive matching with lower(column) LIKE lower('%value%') when the user gives partial names.
- Always include LIMIT {limit} unless the query is an aggregate count/grouping query.
""".strip()
        raw_response = self.call_llm(prompt)
        if raw_response.startswith("LLM request failed:") or raw_response.startswith("LLM unavailable:"):
            return {"error": raw_response}
        parsed = parse_json_object(raw_response)
        if not parsed or "sql" not in parsed:
            return {"error": "LLM did not return a valid SQL JSON object.", "raw": raw_response}
        return parsed

    def employee_names(self):
        with self.connect() as conn:
            return [
                row["AssignedTo"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT AssignedTo
                    FROM Lead_Details
                    WHERE AssignedTo IS NOT NULL AND TRIM(AssignedTo) != ''
                    ORDER BY AssignedTo
                    """
                )
            ]

    def sources(self):
        with self.connect() as conn:
            return [
                row["Source"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT Source
                    FROM Lead_Details
                    WHERE Source IS NOT NULL AND TRIM(Source) != ''
                    ORDER BY Source
                    """
                )
            ]

    def property_names(self, active_only=False):
        where = "WHERE IsActive = 1" if active_only else ""
        with self.connect() as conn:
            return [
                row["PropertyName"]
                for row in conn.execute(
                    f"""
                    SELECT PropertyName
                    FROM AvailableProperties
                    {where}
                    ORDER BY PropertyName
                    """
                )
            ]

    def assigned_properties_for_lead(self, lead_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT AssignedProperties FROM Lead_Details WHERE LeadId = ?",
                (lead_id,),
            ).fetchone()
        return parse_property_list(row["AssignedProperties"]) if row else []

    def set_lead_properties(self, lead_id, properties):
        if isinstance(properties, str):
            properties = [properties]
        if not isinstance(properties, list) or not properties:
            return {
                "error": "At least one property must be selected.",
                "field": "properties",
                "options": self.property_names(),
            }

        allowed_properties = self.property_names()
        normalized = normalize_property_selection(properties, allowed_properties)
        if "error" in normalized:
            return normalized
        selected_properties = normalized["properties"]

        with self.connect() as conn:
            lead = conn.execute(
                "SELECT LeadId FROM Lead_Details WHERE LeadId = ?",
                (lead_id,),
            ).fetchone()
            if not lead:
                return {"error": f"No lead found with LeadId {lead_id}"}

            conn.execute(
                """
                UPDATE Lead_Details
                SET AssignedProperties = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE LeadId = ?
                """,
                (json.dumps(selected_properties, ensure_ascii=False), lead_id),
            )
            conn.commit()

        return {
            "lead_id": lead_id,
            "assigned_properties": selected_properties,
            "message": "Lead properties assigned successfully.",
        }

    def text_columns(self, table):
        return [
            col["name"]
            for col in self.schema().get(table, [])
            if "TEXT" in (col["type"] or "").upper() or col["type"] == ""
        ]

    def assign_lead(self, lead_id, strategy="smart", employee=None):
        if employee is None:
            lead = self.get_lead(lead_id)
            if not lead:
                return {"error": f"No lead found with LeadId {lead_id}"}
            employee = self.pick_employee(strategy, lead)

        if not employee:
            return {"error": "No employee could be selected for assignment"}

        with self.connect() as conn:
            before = conn.execute(
                "SELECT LeadId, AssignedTo FROM Lead_Details WHERE LeadId = ?",
                (lead_id,),
            ).fetchone()
            if not before:
                return {"error": f"No lead found with LeadId {lead_id}"}

            conn.execute(
                """
                UPDATE Lead_Details
                SET AssignedTo = ?, UpdatedAt = CURRENT_TIMESTAMP
                WHERE LeadId = ?
                """,
                (employee, lead_id),
            )
            conn.commit()

        return {
            "lead_id": lead_id,
            "previous_assignee": before["AssignedTo"],
            "assigned_to": employee,
            "strategy": strategy,
        }

    def pick_employee(self, strategy="least_workload", lead=None):
        employees = self.employee_names()
        if not employees:
            return None

        if strategy == "smart":
            selected = self.pick_employee_by_rules(employees, lead or {})
            if selected:
                return selected
            strategy = "least_workload"

        if strategy == "round_robin":
            with self.connect() as conn:
                last = conn.execute(
                    """
                    SELECT AssignedTo
                    FROM Lead_Details
                    WHERE AssignedTo IS NOT NULL AND TRIM(AssignedTo) != ''
                    ORDER BY UpdatedAt DESC, LeadId DESC
                    LIMIT 1
                    """
                ).fetchone()
            if not last or last["AssignedTo"] not in employees:
                return employees[0]
            return employees[(employees.index(last["AssignedTo"]) + 1) % len(employees)]

        with self.connect() as conn:
            workloads = {
                row["AssignedTo"]: row["LeadCount"]
                for row in conn.execute(
                    """
                    SELECT AssignedTo, COUNT(*) AS LeadCount
                    FROM Lead_Details
                    WHERE AssignedTo IS NOT NULL AND TRIM(AssignedTo) != ''
                    GROUP BY AssignedTo
                    """
                )
            }
        return min(employees, key=lambda name: workloads.get(name, 0))

    def pick_employee_by_rules(self, employees, lead):
        rules = self.assignment_rules()
        employee_profiles = rules.get("employees", {})
        if isinstance(employee_profiles, list):
            employee_profiles = {
                profile.get("name"): profile
                for profile in employee_profiles
                if isinstance(profile, dict) and profile.get("name")
            }

        if not employee_profiles:
            return None

        workloads = self.employee_workloads()
        scored = []
        for employee in employees:
            profile = employee_profiles.get(employee, {})
            score = 0
            score += match_rule_value(lead.get("AssignedProperties"), profile.get("properties"), 4)
            score += match_rule_value(lead.get("Requirements"), profile.get("locations"), 3)
            score += match_rule_value(lead.get("Requirements"), profile.get("property_interests"), 3)
            score += match_rule_value(lead.get("Source"), profile.get("sources"), 2)
            score += match_rule_value(lead.get("Priority"), profile.get("priorities"), 2)
            score += match_rule_value(lead.get("Requirements"), profile.get("languages"), 1)
            if score:
                scored.append((score, workloads.get(employee, 0), employee))

        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return scored[0][2]

    def employee_workloads(self):
        with self.connect() as conn:
            return {
                row["AssignedTo"]: row["LeadCount"]
                for row in conn.execute(
                    """
                    SELECT AssignedTo, COUNT(*) AS LeadCount
                    FROM Lead_Details
                    WHERE AssignedTo IS NOT NULL AND TRIM(AssignedTo) != ''
                    GROUP BY AssignedTo
                    """
                )
            }

    def assignment_rules(self):
        if not os.path.exists(ASSIGNMENT_RULES_FILE):
            return {}
        try:
            with open(ASSIGNMENT_RULES_FILE, "r", encoding="utf-8") as rules_file:
                return json.load(rules_file)
        except (OSError, json.JSONDecodeError):
            return {}

    def update_activity(
        self,
        lead_id,
        activity_type,
        status,
        note=None,
        assigned_to=None,
        activity_datetime=None,
        meeting_response=None,
        feedback=None,
    ):
        validation = validate_activity_payload(
            activity_type=activity_type,
            status=status,
            assigned_to=assigned_to,
            activity_datetime=activity_datetime,
            meeting_response=meeting_response,
            feedback=feedback,
            allowed_assignees=self.employee_names(),
        )
        if "error" in validation:
            return validation

        normalized_activity_datetime = validation.get("activity_datetime")
        normalized_feedback = validation.get("feedback")
        normalized_assigned_to = validation.get("assigned_to")
        normalized_note = note or normalized_feedback

        with self.connect() as conn:
            exists = conn.execute(
                "SELECT LeadId FROM Lead_Details WHERE LeadId = ?",
                (lead_id,),
            ).fetchone()
            if not exists:
                return {"error": f"No lead found with LeadId {lead_id}"}

            conn.execute(
                """
                UPDATE Lead_Details
                SET ActivityType = ?,
                    ActivityStatus = ?,
                    ActivityNote = ?,
                    MeetingResponse = ?,
                    ActivityDateTime = ?,
                    Feedback = ?,
                    AssignedTo = COALESCE(?, AssignedTo),
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE LeadId = ?
                """,
                (
                    activity_type,
                    status,
                    normalized_note,
                    meeting_response,
                    normalized_activity_datetime,
                    normalized_feedback,
                    normalized_assigned_to,
                    lead_id,
                ),
            )
            conn.commit()

        return {
            "lead_id": lead_id,
            "activity_type": activity_type,
            "activity_status": status,
            "activity_note": normalized_note,
            "activity_datetime": normalized_activity_datetime,
            "assigned_to": normalized_assigned_to,
            "meeting_response": meeting_response,
            "feedback": normalized_feedback,
            "message": "Activity created successfully.",
        }

    def schedule_follow_up(self, lead_id, when_text="tomorrow", note="Follow up with lead"):
        lead = self.get_lead(lead_id)
        if not lead:
            return {"error": f"No lead found with LeadId {lead_id}"}

        return self.update_activity(
            lead_id,
            "Task",
            "Pending",
            note=f"{note}. Scheduled for {when_text}.",
            assigned_to=lead.get("AssignedTo"),
            activity_datetime=when_text,
            feedback=note,
        )

    def remind_next_day(self, lead_id, note="Remind assigned employee to follow up"):
        lead = self.get_lead(lead_id)
        if not lead:
            return {"error": f"No lead found with LeadId {lead_id}"}

        reminder_date = (datetime.now() + timedelta(days=1)).date().isoformat()
        assignee = lead.get("AssignedTo") or "assigned employee"
        return self.update_activity(
            lead_id,
            "Task",
            "Pending",
            note=f"Reminder for {assignee} on {reminder_date}: {note}",
            assigned_to=lead.get("AssignedTo"),
            activity_datetime=f"{reminder_date} 09:00",
            feedback=note,
        )

    def mark_call_completed(self, lead_id, note="Call completed"):
        return self.update_activity(
            lead_id,
            "Call",
            "Completed",
            note=note,
            meeting_response="Follow-up Required",
            feedback=note,
        )

    def leads_without_activity(self, limit=50):
        with self.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM Lead_Details
                    WHERE ActivityType IS NULL
                       OR ActivityStatus IS NULL
                       OR ActivityNote IS NULL
                    ORDER BY CreatedAt DESC, LeadId DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]
        return rows

    def daily_work_queue(self, limit=25):
        with self.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT *
                    FROM Lead_Details
                    ORDER BY
                      CASE
                        WHEN Priority = 'Hot'
                         AND (ActivityType IS NULL OR ActivityStatus IS NULL OR ActivityNote IS NULL)
                        THEN 1
                        WHEN ActivityStatus = 'Pending' THEN 2
                        WHEN Stage IN ('Interested', 'In Follow-up', 'Ready for Site Visit')
                         AND datetime(UpdatedAt) <= datetime('now', '-3 days')
                        THEN 3
                        WHEN MobileNo IN (
                            SELECT MobileNo
                            FROM Lead_Details
                            WHERE MobileNo IS NOT NULL AND TRIM(MobileNo) != ''
                            GROUP BY MobileNo
                            HAVING COUNT(*) > 1
                        )
                        THEN 4
                        WHEN AssignedTo IS NULL OR TRIM(AssignedTo) = '' THEN 5
                        ELSE 6
                      END,
                      UpdatedAt ASC,
                      LeadId ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ]

        return [
            {
                "lead": row,
                "flags": self.health_flags_for_lead(row),
                "recommended_action": recommend_action(row),
            }
            for row in rows
        ]

    def health_flags(self, limit=100):
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM Lead_Details LIMIT ?", (limit,))]
            duplicate_mobiles = {
                row["MobileNo"]
                for row in conn.execute(
                    """
                    SELECT MobileNo
                    FROM Lead_Details
                    WHERE MobileNo IS NOT NULL AND TRIM(MobileNo) != ''
                    GROUP BY MobileNo
                    HAVING COUNT(*) > 1
                    """
                )
            }

        flagged = []
        for row in rows:
            row["AssignedProperties"] = self.assigned_properties_for_lead(row["LeadId"])
            flags = self.health_flags_for_lead(row, duplicate_mobiles)
            if flags:
                flagged.append({"lead": row, "flags": flags})
        return flagged

    def health_flags_for_lead(self, lead, duplicate_mobiles=None):
        if duplicate_mobiles is None:
            with self.connect() as conn:
                duplicate_mobiles = {
                    row["MobileNo"]
                    for row in conn.execute(
                        """
                        SELECT MobileNo
                        FROM Lead_Details
                        WHERE MobileNo IS NOT NULL AND TRIM(MobileNo) != ''
                        GROUP BY MobileNo
                        HAVING COUNT(*) > 1
                        """
                    )
                }

        flags = []
        if lead.get("MobileNo") in duplicate_mobiles:
            flags.append("Duplicate lead / same mobile number")
        if not lead.get("ActivityType") or not lead.get("ActivityStatus") or not lead.get("ActivityNote"):
            flags.append("No activity recorded")
        if not lead.get("AssignedTo"):
            flags.append("No employee assigned")
        assigned_properties = lead.get("AssignedProperties")
        if assigned_properties is None and lead.get("LeadId"):
            assigned_properties = self.assigned_properties_for_lead(lead["LeadId"])
        if not assigned_properties:
            flags.append("No property assigned")
        if lead.get("Priority") == "Hot" and (
            not lead.get("ActivityStatus") or lead.get("ActivityStatus") == "Pending"
        ):
            flags.append("High-priority lead needs attention")
        if lead.get("ActivityStatus") == "Pending" and is_older_than_days(lead.get("UpdatedAt"), 1):
            flags.append("Overdue pending activity")
        return flags

    def update_record(self, table, pk_value, values, pk_column=None):
        if table not in self.table_names():
            return {"error": f"Unknown table: {table}"}
        if not isinstance(values, dict) or not values:
            return {"error": "values must be a non-empty object"}

        pk_column = pk_column or self.primary_key_column(table)
        if not pk_column:
            return {"error": f"No primary key found for table: {table}"}

        columns = set(self.table_columns(table))
        invalid_columns = sorted(set(values) - columns)
        if invalid_columns:
            return {"error": f"Unknown columns for {table}: {invalid_columns}"}
        if pk_column in values:
            return {"error": f"Refusing to update primary key column: {pk_column}"}

        update_values = dict(values)
        if "UpdatedAt" in columns and "UpdatedAt" not in update_values:
            update_values["UpdatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        set_clause = ", ".join(f"{quote_identifier(column)} = ?" for column in update_values)
        sql = (
            f"UPDATE {quote_identifier(table)} "
            f"SET {set_clause} "
            f"WHERE {quote_identifier(pk_column)} = ?"
        )
        params = list(update_values.values()) + [pk_value]

        with self.connect() as conn:
            before = conn.execute(
                f"SELECT * FROM {quote_identifier(table)} WHERE {quote_identifier(pk_column)} = ?",
                (pk_value,),
            ).fetchone()
            if not before:
                return {"error": f"No row found in {table} where {pk_column} = {pk_value}"}

            conn.execute(sql, params)
            conn.commit()
            after = conn.execute(
                f"SELECT * FROM {quote_identifier(table)} WHERE {quote_identifier(pk_column)} = ?",
                (pk_value,),
            ).fetchone()

        return {
            "table": table,
            "primary_key": pk_column,
            "primary_key_value": pk_value,
            "updated_columns": sorted(update_values),
            "before": dict(before),
            "after": dict(after),
        }

    def call_llm(self, prompt):
        api_key = os.environ.get("API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return "LLM unavailable: API_KEY is not set."

        payload = {
            "model": OPENROUTER_MODEL,
            "max_tokens": llm_max_tokens(),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an admin CRM assistant. Use all available schema/data context, "
                        "be concise, and never invent database values."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "Projini CRM Admin Agent",
            },
            method="POST",
        )

        return send_llm_request(request)

    def call_llm_messages(self, messages):
        api_key = os.environ.get("API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return "LLM unavailable: API_KEY is not set."

        payload = {
            "model": OPENROUTER_MODEL,
            "max_tokens": llm_max_tokens(),
            "messages": messages,
        }
        request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost",
                "X-Title": "Projini CRM Admin Chatbot",
            },
            method="POST",
        )

        return send_llm_request(request)

    def chatbot_system_prompt(self):
        return f"""
You are the Projini CRM Admin chatbot. You are connected to the SQLite CRM database.
You can inspect schema dynamically and operate on all available CRM data through tools.

Current database schema:
{self.schema_text()}

Current date/time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

Current AssignedTo options from the database:
{json.dumps(self.employee_names(), ensure_ascii=False)}

Current property options from the database:
{json.dumps(self.property_names(), ensure_ascii=False)}

Available tool actions:
- schema: show database schema. Args: {{}}
- sql: run safe read-only SQL against any table/column. Args: {{"sql": "SELECT * FROM Lead_Details LIMIT 10"}}
- summarize: summarize one lead. Args: {{"lead_id": 1}}
- search: natural language lead search. Args: {{"query": "hot leads from 99 Acres", "limit": 10}}
- queue: daily prioritized work queue. Args: {{"limit": 10}}
- flags: lead health warnings. Args: {{"limit": 20}}
- assign: assign a lead. Args: {{"lead_id": 1, "employee": "Ralph Lopes"}} or {{"lead_id": 1, "strategy": "smart"}}
- assign_properties: assign one or more existing properties to a lead. Args: {{"lead_id": 1, "properties": ["Lodha Crown (Residential â€“ 1 & 2 BHK)", "Rustomjee Urbania (Residential â€“ Premium Apartments)"]}}
- activity: create/update activity. Args: {{"lead_id": 1, "activity_type": "Call", "status": "Completed", "note": "Called customer", "meeting_response": "Follow-up Required", "feedback": "Customer wants a callback"}}
- follow_up: schedule follow-up. Args: {{"lead_id": 1, "when": "tomorrow", "note": "Call back"}}
- reminder: remind assigned employee next day. Args: {{"lead_id": 1, "note": "Call customer"}}
- no_activity: find leads with no activity. Args: {{"limit": 20}}
- update_record: admin update to any valid table/column by primary key. Args: {{"table": "Lead_Details", "pk_value": 1, "values": {{"Stage": "Interested"}}}}
- final: answer the user directly. Args: {{"answer": "your response"}}

Rules:
- Return exactly one JSON object and no markdown.
- JSON format: {{"thought": "Your step-by-step reasoning about the user's query, intent, context, and plan of action", "action": "search", "args": {{"query": "...", "limit": 10}}}}
- Every response MUST be a valid JSON object in the specified format. NEVER output plain text or markdown directly. Even if the conversation history has plain text assistant responses, you must always respond in the JSON format with "thought" and "action" keys.
- Every response must include the "thought" field with your detailed reasoning. Use this to analyze the conversation history, identify what the user is trying to achieve, check for any negation or cancellation intent, and reason about the correct tool call or answer for all types of questions and answers.
- If the user indicates they want to cancel, abort, stop the current flow, or not perform/apply any activity/change, acknowledge this request, stop collecting fields, do not make any database updates, and respond with a simple confirmation using the "final" action (e.g., "Okay, the request has been cancelled.").
- Use a tool when database information or database action is needed.
- For analysis/search across arbitrary columns, prefer sql with a safe SELECT query.
- Use final only when you can answer without another tool, or after tool results are provided.
- When the user asks to set/create an activity, collect missing required fields before calling activity.
- ActivityType options: {", ".join(ACTIVITY_TYPES)}.
- ActivityStatus options: {", ".join(ACTIVITY_STATUSES)}.
- If the user clearly says meeting, call, site visit, or task in the first activity request, infer ActivityType and do not ask for it again.
- If ActivityStatus is Pending, ask for ActivityDateTime and AssignedTo. ActivityDateTime must be later than the current date/time. Include these as activity_datetime and assigned_to.
- AssignedTo must be selected from the current database AssignedTo options only. Display those current database values as a numbered list when asking the user to choose AssignedTo.
- A lead can be assigned multiple properties, and a property can be assigned to multiple leads.
- Properties must be selected from the current database property options only. Display those current database values as a numbered list when asking the user to choose properties.
- If ActivityStatus is Completed or Cancelled, ask for MeetingResponse. MeetingResponse options: {", ".join(MEETING_RESPONSES)}.
- Always collect feedback for activity requests and pass it as feedback. You may also use it as note.
- Present option fields as short selectable numbered lists in final answers.
- For updates, choose the proper action but do not claim success until the tool result confirms it.
- Never use update_record for activity workflow when activity/follow_up/reminder fits better.
- Keep final answers concise and useful for an Admin user.
""".strip()

    def chat_once(self, user_message, history):
        pending_update = pending_duplicate_update(user_message, history, self.table_columns("Lead_Details"))
        if pending_update:
            return self.apply_pending_duplicate_update(pending_update)

        messages = [{"role": "system", "content": self.chatbot_system_prompt()}]
        for msg in history[-8:]:
            if msg["role"] == "assistant":
                content_str = msg["content"].strip()
                if not (content_str.startswith("{") and content_str.endswith("}")):
                    wrapped_content = json.dumps({
                        "thought": "Providing a final response to the user.",
                        "action": "final",
                        "args": {
                            "answer": msg["content"]
                        }
                    }, ensure_ascii=False)
                    messages.append({"role": "assistant", "content": wrapped_content})
                else:
                    messages.append(msg)
            else:
                messages.append(msg)
        messages.append({"role": "user", "content": user_message})

        for _ in range(4):
            raw_response = self.call_llm_messages(messages)
            if raw_response.startswith("LLM request failed:"):
                return self.fallback_chat_once(user_message, raw_response, history)

            action_request = parse_action_request(raw_response)

            if not action_request:
                # The LLM returned text that is not a valid action JSON.
                # Try to salvage a readable answer before falling back to the raw text.
                return extract_readable_answer(raw_response)

            thought = action_request.get("thought")
            if thought:
                print(f"Thought: {thought}")

            action = action_request.get("action")
            args = action_request.get("args") or {}

            if action == "final":
                return args.get("answer", "")

            if is_mutating_action(action):
                confirmed = self.confirm_mutating_action(action, args)
                if not confirmed:
                    return "Cancelled. I did not make any database changes."

            tool_result = self.execute_chat_tool(action, args)
            messages.append({"role": "assistant", "content": json.dumps(action_request)})
            messages.append(
                {
                    "role": "user",
                    "content": "Tool result:\n" + compact_json(
                        tool_result,
                        max_chars=llm_tool_result_max_chars(),
                    ),
                }
            )

        return "I ran out of tool steps while handling that request. Try making the request a little more specific."

    def confirm_mutating_action(self, action, args):
        if callable(self.confirmation_callback):
            return self.confirmation_callback(action, args)
        return ask_confirmation(action, args)

    def is_cancellation_intent(self, user_message, history=None):
        lowered = user_message.lower().strip()
        cancel_phrases = [
            "cancel", "abort", "nevermind", "never mind", "stop", "forget it", "forget that",
            "dont want to", "don't want to", "dont apply", "don't apply",
            "no activity", "no activities", "no properties", "no property", "no no",
            "dont do", "don't do", "do not assign", "dont assign", "don't assign",
            "dont want any", "don't want any", "dont want to apply", "don't want to apply"
        ]
        if any(phrase in lowered for phrase in cancel_phrases):
            return True
        if history and len(history) > 0:
            last_assistant = history[-1].get("content", "").lower()
            if any(keyword in last_assistant for keyword in ["missing required fields", "please choose", "would you like to", "i need some information"]):
                if lowered in {"no", "no thank you", "no thanks", "none", "nothing", "neither", "stop"}:
                    return True
        return False

    def fallback_chat_once(self, user_message, llm_error, history=None):
        lowered = user_message.lower()

        if self.is_cancellation_intent(user_message, history):
            if history:
                last_assistant = history[-1].get("content", "").lower()
                if "propert" in last_assistant:
                    return "Okay, I've cancelled the property assignment request."
                if any(k in last_assistant for k in ["activit", "meeting", "call", "task", "site visit"]):
                    return "Okay, I've cancelled the activity request."
            return "Okay, cancelled."

        if any(word in lowered for word in ["activity", "meeting", "call", "site visit", "task"]):
            return format_activity_collection_prompt(user_message, self.employee_names())

        if "propert" in lowered and any(word in lowered for word in ["assign", "set", "select", "add"]):
            return format_property_assignment_prompt(self.property_names())

        if "schema" in lowered or "table" in lowered or "column" in lowered:
            return self.schema_text()

        lead_id = extract_lead_id(user_message)
        if ("summar" in lowered or "open" in lowered or "lead" in lowered) and lead_id:
            return format_lead_summary(self.summarize_lead(lead_id))

        if "queue" in lowered or "today" in lowered or "what should i do" in lowered:
            return format_work_queue(self.daily_work_queue(limit=10))

        if "flag" in lowered or "health" in lowered or "warning" in lowered:
            return format_health_flags(self.health_flags(limit=20))

        if "no activit" in lowered:
            return format_search_rows(self.leads_without_activity(limit=20), "Leads with no activity")

        if any(word in lowered for word in ["show", "find", "search", "lead", "duplicate", "hot", "cold", "pending", "untouched"]):
            return format_search_result(self.search(user_message, limit=10, use_llm=False))

        return (
            "The LLM connection failed, but the local CRM tools are available. "
            f"Details: {llm_error}"
        )

    def execute_chat_tool(self, action, args):
        try:
            if action == "schema":
                return self.schema()
            if action == "sql":
                return self.execute_read_sql(
                    args["sql"],
                    params=args.get("params", ()),
                    limit=int(args.get("limit", DEFAULT_LIMIT)),
                )
            if action == "summarize":
                return self.summarize_lead(int(args["lead_id"]), use_llm=False)
            if action == "search":
                return self.search(args["query"], limit=int(args.get("limit", 10)))
            if action == "queue":
                return self.daily_work_queue(limit=int(args.get("limit", 10)))
            if action == "flags":
                return self.health_flags(limit=int(args.get("limit", 20)))
            if action == "assign":
                return self.assign_lead(
                    int(args["lead_id"]),
                    strategy=args.get("strategy", "least_workload"),
                    employee=args.get("employee"),
                )
            if action == "assign_properties":
                return self.set_lead_properties(
                    int(args["lead_id"]),
                    args.get("properties", []),
                )
            if action == "activity":
                return self.update_activity(
                    int(args["lead_id"]),
                    args["activity_type"],
                    args["status"],
                    note=args.get("note"),
                    assigned_to=args.get("assigned_to"),
                    activity_datetime=args.get("activity_datetime"),
                    meeting_response=args.get("meeting_response"),
                    feedback=args.get("feedback"),
                )
            if action == "follow_up":
                return self.schedule_follow_up(
                    int(args["lead_id"]),
                    when_text=args.get("when", "tomorrow"),
                    note=args.get("note", "Follow up with lead"),
                )
            if action == "reminder":
                return self.remind_next_day(
                    int(args["lead_id"]),
                    note=args.get("note", "Remind assigned employee to follow up"),
                )
            if action == "no_activity":
                return self.leads_without_activity(limit=int(args.get("limit", 20)))
            if action == "update_record":
                return self.update_record(
                    args["table"],
                    args["pk_value"],
                    args["values"],
                    pk_column=args.get("pk_column"),
                )
            return {"error": f"Unknown action: {action}"}
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            return {"error": f"Invalid tool arguments for {action}: {exc}", "args": args}

    def chat(self):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        print("Projini CRM Admin Chatbot")
        print("Ask about leads, summaries, work queue, flags, assignments, or activities.")
        print("Type 'exit' to quit.\n")

        history = []
        while True:
            user_message = input("You: ").strip()
            if user_message.lower() in {"exit", "quit", "q"}:
                print("Agent: Bye.")
                break
            if not user_message:
                continue

            answer = self.chat_once(user_message, history)
            print(f"Agent: {answer}\n")
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": answer})

    def voice_chat(
        self,
        recognizer_backend="google",
        device_index=None,
        listen_timeout=8,
        phrase_time_limit=25,
        language="en-IN",
        pause_threshold=1.4,
        speak=True,
    ):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")

        try:
            voice = VoiceIO(
                recognizer_backend=recognizer_backend,
                device_index=device_index,
                language=language,
                pause_threshold=pause_threshold,
                speak=speak,
            )
        except VoiceUnavailableError as exc:
            print(str(exc))
            return 2

        print("Projini CRM Voice Assistant")
        print("Speak naturally about leads, properties, assignments, activities, queue, or flags.")
        print("Say 'exit', 'quit', or 'stop voice' to quit.\n")

        history = []
        original_confirmation_callback = self.confirmation_callback

        try:
            with voice.microphone as source:
                print("Calibrating microphone for background noise...")
                voice.recognizer.adjust_for_ambient_noise(source, duration=0.7)
                voice.say("Projini CRM voice assistant is ready.")

                def confirm_with_voice(action, args):
                    return voice.confirm_action(source, action, args, listen_timeout=listen_timeout)

                self.confirmation_callback = confirm_with_voice

                while True:
                    print("Listening...")
                    try:
                        user_message = voice.listen(
                            source,
                            timeout=listen_timeout,
                            phrase_time_limit=phrase_time_limit,
                        )
                    except voice.wait_timeout_error:
                        print("No speech detected. Listening again...\n")
                        continue
                    except voice.unknown_value_error:
                        message = "I could not understand that. Please say it again."
                        print(f"Agent: {message}\n")
                        voice.say(message)
                        continue
                    except voice.request_error as exc:
                        message = f"Speech recognition failed: {exc}"
                        print(f"Agent: {message}\n")
                        voice.say("Speech recognition failed. Please check the recognizer setup.")
                        continue

                    corrected_message = normalize_voice_command(user_message, history)
                    if corrected_message != user_message:
                        print(f"You: {user_message}")
                        print(f"Heard as: {corrected_message}")
                        user_message = corrected_message
                    else:
                        print(f"You: {user_message}")
                    if user_message.lower().strip() in {"exit", "quit", "q", "stop voice", "stop listening"}:
                        print("Agent: Bye.")
                        voice.say("Bye.")
                        break

                    answer = self.chat_once(user_message, history)
                    print(f"Agent: {answer}\n")
                    voice.say(voice_response_text(answer))
                    history.append({"role": "user", "content": user_message})
                    history.append({"role": "assistant", "content": answer})
        except KeyboardInterrupt:
            print("\nAgent: Bye.")
            voice.say("Bye.")
        finally:
            self.confirmation_callback = original_confirmation_callback

        return 0

    def apply_pending_duplicate_update(self, pending_update):
        results = []
        for lead_id in pending_update["lead_ids"]:
            results.append(
                self.update_record(
                    "Lead_Details",
                    lead_id,
                    {pending_update["column"]: pending_update["new_value"]},
                    pk_column="LeadId",
                )
            )

        errors = [item["error"] for item in results if isinstance(item, dict) and "error" in item]
        updated_ids = [
            item["primary_key_value"]
            for item in results
            if isinstance(item, dict) and "primary_key_value" in item
        ]
        if errors and not updated_ids:
            return "I could not apply that update: " + "; ".join(errors)

        message = (
            f"Updated {pending_update['column']} to '{pending_update['new_value']}' "
            f"for LeadIds: {', '.join(str(lead_id) for lead_id in updated_ids)}."
        )
        if errors:
            message += " Some rows were skipped: " + "; ".join(errors)
        return message


def send_llm_request(request, _retries=3, _backoff=15):
    """Send an LLM request, retrying up to _retries times on HTTP 429 rate-limit errors."""
    for attempt in range(_retries + 1):
        # urllib.Request can only be read once, so we must rebuild it each attempt
        retry_request = urllib.request.Request(
            request.full_url,
            data=request.data,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(retry_request, timeout=60) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < _retries:
                wait = _backoff * (2 ** attempt)  # 5s, 10s, 20s
                print(f"[Rate limited] Waiting {wait}s before retry {attempt + 1}/{_retries}...")
                time.sleep(wait)
                continue
            body = exc.read().decode("utf-8", errors="replace")
            return f"LLM request failed: HTTP {exc.code} {extract_llm_error(body)}"
        except urllib.error.URLError as exc:
            return f"LLM request failed: {exc}"

        try:
            data = json.loads(body)
            return extract_llm_content(data)
        except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return f"LLM request failed: {exc}"

    return "LLM request failed: rate limit persisted after retries."


def ensure_activity_columns(conn):
    table = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'Lead_Details'
        """
    ).fetchone()
    if not table:
        return

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(Lead_Details)")}
    for column, column_type in ACTIVITY_EXTRA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE Lead_Details ADD COLUMN {quote_identifier(column)} {column_type}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_activity_datetime "
        "ON Lead_Details(ActivityDateTime)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_meeting_response "
        "ON Lead_Details(MeetingResponse)"
    )
    conn.commit()


def ensure_available_properties_and_assigned_properties(conn):
    lead_table = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'Lead_Details'
        """
    ).fetchone()
    property_table = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = 'Properties'
        """
    ).fetchone()
    if not lead_table or not property_table:
        return

    property_names = [
        row["PropertyName"]
        for row in conn.execute(
            """
            SELECT PropertyName
            FROM Properties
            WHERE PropertyName IS NOT NULL AND TRIM(PropertyName) != ''
            ORDER BY PropertyName
            """
        )
    ]
    full_property_set = set(property_names)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS AvailableProperties (
            AvailablePropertyId INTEGER PRIMARY KEY AUTOINCREMENT,
            PropertyName TEXT NOT NULL UNIQUE,
            IsActive INTEGER NOT NULL CHECK (IsActive IN (0, 1)) DEFAULT 1,
            CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO AvailableProperties (PropertyName, IsActive)
        SELECT PropertyName, IsActive
        FROM Properties
        WHERE PropertyName IS NOT NULL AND TRIM(PropertyName) != ''
        """
    )

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(Lead_Details)")}

    if "AssignedProperty" in columns or "PropertiesAvailable" in columns:
        rows = [dict(row) for row in conn.execute("SELECT * FROM Lead_Details")]
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP INDEX IF EXISTS idx_lead_details_assigned_property")
        conn.execute("DROP INDEX IF EXISTS idx_lead_details_properties_available")
        conn.execute("DROP INDEX IF EXISTS idx_lead_properties_property_name")
        conn.execute("DROP TABLE IF EXISTS Lead_Properties")
        conn.execute("ALTER TABLE Lead_Details RENAME TO Lead_Details_old")
        create_lead_details_table(conn)

        insert_columns = [
            "LeadId",
            "FirstName",
            "LastName",
            "MobileNo",
            "EmailId",
            "Address",
            "Requirements",
            "AssignedTo",
            "Stage",
            "Source",
            "Priority",
            "AssignedProperties",
            "ActivityType",
            "ActivityStatus",
            "ActivityNote",
            "MeetingResponse",
            "ActivityDateTime",
            "Feedback",
            "CreatedAt",
            "UpdatedAt",
        ]
        placeholders = ", ".join("?" for _ in insert_columns)
        for row in rows:
            row["AssignedProperties"] = migrated_assigned_properties(row, full_property_set)
            conn.execute(
                f"""
                INSERT INTO Lead_Details ({", ".join(insert_columns)})
                VALUES ({placeholders})
                """,
                [row.get(column) for column in insert_columns],
            )

        conn.execute("DROP TABLE Lead_Details_old")
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        if "AssignedProperties" not in columns:
            conn.execute("ALTER TABLE Lead_Details ADD COLUMN AssignedProperties TEXT")
        conn.execute("DROP INDEX IF EXISTS idx_lead_properties_property_name")
        conn.execute("DROP INDEX IF EXISTS idx_lead_details_properties_available")
        conn.execute("DROP TABLE IF EXISTS Lead_Properties")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_details_mobile ON Lead_Details(MobileNo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_details_email ON Lead_Details(EmailId)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_details_assigned ON Lead_Details(AssignedTo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_details_stage ON Lead_Details(Stage)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lead_details_priority ON Lead_Details(Priority)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_assigned_properties "
        "ON Lead_Details(AssignedProperties)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_activity_type "
        "ON Lead_Details(ActivityType)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_activity_status "
        "ON Lead_Details(ActivityStatus)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_activity_datetime "
        "ON Lead_Details(ActivityDateTime)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lead_details_meeting_response "
        "ON Lead_Details(MeetingResponse)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_available_properties_property_name "
        "ON AvailableProperties(PropertyName)"
    )
    conn.commit()


def migrated_assigned_properties(row, full_property_set):
    existing_assigned = parse_property_list(row.get("AssignedProperties"))
    if existing_assigned:
        return json.dumps(existing_assigned, ensure_ascii=False)

    mistaken_available = parse_property_list(row.get("PropertiesAvailable"))
    if mistaken_available and set(mistaken_available) != full_property_set:
        return json.dumps(mistaken_available, ensure_ascii=False)

    legacy_single = row.get("AssignedProperty")
    if legacy_single and str(legacy_single).strip():
        return json.dumps([str(legacy_single).strip()], ensure_ascii=False)

    return None


def create_lead_details_table(conn):
    conn.execute(
        """
        CREATE TABLE Lead_Details (
            LeadId INTEGER PRIMARY KEY AUTOINCREMENT,
            FirstName TEXT NOT NULL,
            LastName TEXT,
            MobileNo TEXT,
            EmailId TEXT,
            Address TEXT,
            Requirements TEXT,
            AssignedTo TEXT,
            Stage TEXT CHECK(Stage IN (
                'Fresh Lead',
                'Contact Attempted',
                'In Follow-up',
                'Interested',
                'Ready for Site Visit',
                'Site Visit Completed',
                'Proposal Presented',
                'In Negotiation',
                'Booking Confirmed',
                'Booking Complete â€“ Won',
                'Booking Complete – Won',
                'Future Prospect',
                'Not Interested',
                'Invalid / Not Qualified',
                'Closed â€“ Lost',
                'Closed – Lost'
            )) DEFAULT 'Fresh Lead',
            Source TEXT,
            Priority TEXT CHECK(Priority IN ('Hot', 'Warm', 'Cold')) DEFAULT 'Warm',
            AssignedProperties TEXT,
            ActivityType TEXT CHECK(ActivityType IN ('Call', 'Meeting', 'Task', 'Site Visit')),
            ActivityStatus TEXT CHECK(ActivityStatus IN ('Pending', 'Completed', 'Cancelled')),
            ActivityNote TEXT,
            MeetingResponse TEXT CHECK(MeetingResponse IN (
                'Interested',
                'Not Interested',
                'Call Back Later',
                'Follow-up Required',
                'Site Visit Scheduled',
                'Proposal Requested',
                'Price Negotiation',
                'No Response',
                'Invalid / Wrong Number',
                'Customer Cancelled'
            )),
            ActivityDateTime DATETIME,
            Feedback TEXT,
            CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
            UpdatedAt DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def extract_llm_content(data):
    if not isinstance(data, dict):
        raise ValueError("LLM response was not a JSON object.")

    if data.get("error"):
        raise ValueError(extract_llm_error(data))

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"LLM response did not include choices. {extract_llm_error(data)}")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("LLM response choice was not a JSON object.")

    message = first_choice.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(first_choice.get("text"), str):
        return first_choice["text"]

    raise ValueError(f"LLM response did not include message content. {extract_llm_error(data)}")


def extract_llm_error(raw):
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip()[:500] or "No error body returned."
    if isinstance(raw, dict):
        error = raw.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error.get("type")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
        message = raw.get("message") or raw.get("detail")
        if message:
            return str(message)
        return json.dumps(raw, ensure_ascii=False)[:500]
    return str(raw)[:500]


def pending_duplicate_update(user_message, history, lead_columns):
    if not history or len(history) < 2:
        return None

    last_assistant = history[-1].get("content", "")
    previous_user = history[-2].get("content", "")
    offered_ids = extract_offered_lead_ids(last_assistant)
    if not offered_ids:
        return None

    selected_ids = selected_pending_lead_ids(user_message, offered_ids)
    if not selected_ids:
        return None

    update_request = parse_lead_update_request(previous_user, lead_columns)
    if not update_request:
        return None

    update_request["lead_ids"] = selected_ids
    return update_request


def extract_offered_lead_ids(text):
    if not isinstance(text, str) or not re.search(r"\blead\s*ids?\b", text, flags=re.IGNORECASE):
        return []

    match = re.search(r"Lead\s*Ids?\s*:\s*([0-9,\sand]+)", text, flags=re.IGNORECASE)
    if not match:
        return []
    return [int(value) for value in re.findall(r"\d+", match.group(1))]


def selected_pending_lead_ids(user_message, offered_ids):
    lowered = user_message.lower().strip()
    if lowered in {"confirm", "confirmed", "yes", "y", "all", "all four", "update all", "confirm all"}:
        return offered_ids

    requested_ids = [int(value) for value in re.findall(r"\d+", user_message)]
    selected = [lead_id for lead_id in requested_ids if lead_id in offered_ids]
    return selected


def parse_lead_update_request(text, lead_columns):
    if not isinstance(text, str):
        return None

    column = extract_update_column(text, lead_columns)
    if not column:
        return None

    new_value = extract_update_value(text, column)
    if not new_value:
        return None

    return {"column": column, "new_value": new_value}


def extract_update_column(text, lead_columns):
    for column in sorted(lead_columns, key=len, reverse=True):
        if column in {"LeadId", "CreatedAt", "UpdatedAt"}:
            continue
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return column
    return None


def extract_update_value(text, column):
    pattern = rf"{re.escape(column)}\s+(?:to|as|=)\s+(.+)$"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"\b(?:to|as|=)\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return None

    value = match.group(1).strip()
    value = re.sub(r"[.?!]\s*$", "", value).strip()
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        value = value[1:-1].strip()
    return value or None


def quote_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def clamp_limit(limit):
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def make_safe_read_sql(sql, limit=DEFAULT_LIMIT):
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a non-empty string")

    cleaned = sql.strip()
    cleaned = re.sub(r";+\s*$", "", cleaned).strip()
    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not allowed")
    if not re.match(r"^(SELECT|WITH)\b", cleaned, flags=re.IGNORECASE):
        raise ValueError("Only SELECT/WITH read queries are allowed")
    if PROHIBITED_SQL.search(cleaned):
        raise ValueError("Only read-only SELECT queries are allowed")
    if not re.search(r"\bLIMIT\b", cleaned, flags=re.IGNORECASE):
        cleaned = f"{cleaned}\nLIMIT {clamp_limit(limit)}"
    return cleaned


def read_only_authorizer(action, arg1, arg2, db_name, trigger_name):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def parse_json_object(raw_response):
    if not raw_response:
        return None

    text = raw_response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    return parsed if isinstance(parsed, dict) else None


def extract_source(query, sources):
    lowered = query.lower()
    for source in sources:
        if source and source.lower() in lowered:
            return source
    return None


def extract_assignee(query, employees):
    lowered = query.lower()
    for employee in employees:
        if employee and employee.lower() in lowered:
            return employee

    assigned_match = re.search(
        r"assigned to\s+([a-zA-Z ]+?)(?:\s+with\b|\s+having\b|\s+who\b|\s+where\b|$)",
        query,
        flags=re.IGNORECASE,
    )
    if assigned_match:
        requested = assigned_match.group(1).strip()
        for employee in employees:
            if employee and requested.lower() in employee.lower():
                return employee
        return requested
    return None


def match_rule_value(value, options, weight):
    if not value or not options:
        return 0
    if isinstance(options, str):
        options = [options]
    text = str(value).lower()
    for option in options:
        if option and str(option).lower() in text:
            return weight
    return 0


def validate_activity_payload(
    activity_type,
    status,
    assigned_to=None,
    activity_datetime=None,
    meeting_response=None,
    feedback=None,
    allowed_assignees=None,
):
    if activity_type not in ACTIVITY_TYPES:
        return {
            "error": "ActivityType is required.",
            "field": "ActivityType",
            "options": list(ACTIVITY_TYPES),
        }
    if status not in ACTIVITY_STATUSES:
        return {
            "error": "ActivityStatus is required.",
            "field": "ActivityStatus",
            "options": list(ACTIVITY_STATUSES),
        }

    if not feedback or not str(feedback).strip():
        return {"error": "Feedback is required for every activity.", "field": "Feedback"}

    normalized = {"feedback": str(feedback).strip()}
    if status == "Pending":
        if not activity_datetime:
            return {
                "error": "ActivityDateTime is required when ActivityStatus is Pending.",
                "field": "ActivityDateTime",
            }
        parsed_datetime = parse_activity_datetime(activity_datetime)
        if not parsed_datetime:
            return {
                "error": "ActivityDateTime must be a valid future date and time.",
                "field": "ActivityDateTime",
                "accepted_formats": ["YYYY-MM-DD HH:MM", "YYYY-MM-DDTHH:MM", "tomorrow"],
            }
        if parsed_datetime <= datetime.now():
            return {
                "error": "ActivityDateTime must be later than the current date and time.",
                "field": "ActivityDateTime",
            }
        normalized_assignee = normalize_assignee(assigned_to, allowed_assignees)
        if "error" in normalized_assignee:
            return normalized_assignee
        normalized["activity_datetime"] = parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")
        normalized["assigned_to"] = normalized_assignee["assigned_to"]
        return normalized

    if not meeting_response or meeting_response not in MEETING_RESPONSES:
        return {
            "error": "MeetingResponse is required when ActivityStatus is Completed or Cancelled.",
            "field": "MeetingResponse",
            "options": list(MEETING_RESPONSES),
        }
    normalized["activity_datetime"] = None
    normalized["assigned_to"] = None
    return normalized


def normalize_assignee(assigned_to, allowed_assignees=None):
    options = [name for name in (allowed_assignees or []) if name and str(name).strip()]
    if not assigned_to or not str(assigned_to).strip():
        return {
            "error": "AssignedTo is required when ActivityStatus is Pending.",
            "field": "AssignedTo",
            "options": options,
        }

    requested = str(assigned_to).strip()
    for option in options:
        if requested.lower() == option.lower():
            return {"assigned_to": option}

    return {
        "error": "AssignedTo must be selected from the current database values.",
        "field": "AssignedTo",
        "provided": requested,
        "options": options,
    }


def normalize_property_selection(properties, allowed_properties):
    options = [name for name in (allowed_properties or []) if name and str(name).strip()]
    if not options:
        return {
            "error": "No properties are available in the database.",
            "field": "properties",
            "options": [],
        }

    requested_values = []
    for value in properties:
        if value and str(value).strip():
            requested_values.append(str(value).strip())

    if not requested_values:
        return {
            "error": "At least one property must be selected.",
            "field": "properties",
            "options": options,
        }

    selected = []
    invalid = []
    for requested in requested_values:
        match = next((option for option in options if requested.lower() == option.lower()), None)
        if match:
            if match not in selected:
                selected.append(match)
        else:
            invalid.append(requested)

    if invalid:
        return {
            "error": "Properties must be selected from the current database values.",
            "field": "properties",
            "invalid": invalid,
            "options": options,
        }

    return {"properties": selected}


def parse_property_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if item and str(item).strip()]

    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if item and str(item).strip()]
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_activity_datetime(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)

    text = str(value).strip()
    lowered = text.lower()
    if lowered == "tomorrow":
        return (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d/%m/%Y %H:%M",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def extract_lead_id(text):
    match = re.search(r"\blead(?:\s*id)?\s*#?\s*(\d+)\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\b#(\d+)\b", text)
    if match:
        return int(match.group(1))
    return None


def build_lead_summary(lead):
    requirements = lead.get("Requirements") or "Not provided"
    budget = extract_budget(requirements)
    assigned_properties = lead.get("AssignedProperties")
    if assigned_properties is None:
        assigned_properties = parse_property_list(lead.get("AssignedProperties"))
    activity = "No activity recorded"
    if lead.get("ActivityType") or lead.get("ActivityStatus") or lead.get("ActivityNote"):
        activity = (
            f"{lead.get('ActivityType') or 'Activity'} - "
            f"{lead.get('ActivityStatus') or 'Unknown status'}"
        )
        if lead.get("ActivityDateTime"):
            activity += f" at {lead['ActivityDateTime']}"
        if lead.get("MeetingResponse"):
            activity += f" ({lead['MeetingResponse']})"
        if lead.get("ActivityNote"):
            activity += f": {lead['ActivityNote']}"

    pending = []
    if lead.get("ActivityStatus") == "Pending":
        pending.append(activity)

    return {
        "lead_id": lead.get("LeadId"),
        "customer": " ".join(
            part for part in [lead.get("FirstName"), lead.get("LastName")] if part
        ),
        "customer_requirements": requirements,
        "budget_and_property_interest": {
            "budget": budget or "Not found",
            "assigned_property": ", ".join(assigned_properties) if assigned_properties else "Not assigned",
            "assigned_properties": assigned_properties,
        },
        "lead_source": lead.get("Source") or "Not provided",
        "current_stage": lead.get("Stage") or "Not provided",
        "last_activity": activity,
        "activity_datetime": lead.get("ActivityDateTime"),
        "meeting_response": lead.get("MeetingResponse"),
        "feedback": lead.get("Feedback"),
        "pending_activities": pending,
        "assigned_employee": lead.get("AssignedTo") or "Unassigned",
    }


def extract_budget(requirements):
    match = re.search(r"(Rs\.?\s*\d+(?:\.\d+)?\s*(?:Lac|Lakh|Cr|Crore)?)", requirements, re.IGNORECASE)
    return match.group(1) if match else None


def is_older_than_days(value, days):
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed <= datetime.now(parsed.tzinfo) - timedelta(days=days)


def recommend_action(lead):
    if lead.get("Priority") == "Hot" and not lead.get("ActivityStatus"):
        return "Call immediately and create a follow-up activity."
    if lead.get("ActivityStatus") == "Pending":
        return "Complete or reschedule the pending activity."
    if not lead.get("AssignedTo"):
        return "Assign this lead to an employee."
    assigned_properties = lead.get("AssignedProperties")
    if assigned_properties is None:
        assigned_properties = parse_property_list(lead.get("AssignedProperties"))
    if not assigned_properties:
        return "Assign a matching property."
    return "Review and update the lead stage after next contact."


def infer_activity_type(text):
    lowered = text.lower()
    if "site visit" in lowered:
        return "Site Visit"
    if re.search(r"\bmeeting\b", lowered):
        return "Meeting"
    if re.search(r"\bcall\b", lowered):
        return "Call"
    if re.search(r"\btask\b", lowered):
        return "Task"
    return None


def format_activity_collection_prompt(user_message, assigned_to_options=None):
    inferred_type = infer_activity_type(user_message)
    assigned_to_options = [name for name in (assigned_to_options or []) if name]
    lines = ["I can create that activity. Please provide the missing required fields."]

    if not inferred_type:
        lines.append("ActivityType:")
        for index, option in enumerate(ACTIVITY_TYPES, start=1):
            lines.append(f"{index}. {option}")
    else:
        lines.append(f"ActivityType: {inferred_type}")

    lines.append("ActivityStatus:")
    for index, option in enumerate(ACTIVITY_STATUSES, start=1):
        lines.append(f"{index}. {option}")

    lines.append("If Pending, include a future date/time and choose AssignedTo from this list:")
    if assigned_to_options:
        for index, option in enumerate(assigned_to_options, start=1):
            lines.append(f"{index}. {option}")
    else:
        lines.append("No AssignedTo values are available in the database yet.")
    lines.append("If Completed or Cancelled, choose a MeetingResponse:")
    for index, option in enumerate(MEETING_RESPONSES, start=1):
        lines.append(f"{index}. {option}")
    lines.append("Feedback is required for every activity.")
    return "\n".join(lines)


def format_property_assignment_prompt(property_options):
    lines = [
        "Please choose one or more properties from the current database list.",
        "Property options:",
    ]
    if property_options:
        for index, option in enumerate(property_options, start=1):
            lines.append(f"{index}. {option}")
    else:
        lines.append("No properties are available in the database yet.")
    lines.append("A lead can have multiple properties, and the same property can be used for multiple leads.")
    return "\n".join(lines)


def extract_readable_answer(raw_response):
    """Return the most human-readable text from an LLM response that is not a
    valid action JSON.  Tries (in order):
    1. Parse a complete JSON and return args.answer or root answer (final action).
    2. Regex-extract the "answer" string from a truncated / partial JSON.
    3. Strip leading JSON scaffolding and return the remaining prose.
    4. Return the raw response as-is.
    """
    if not raw_response:
        return ""

    text = raw_response.strip()

    # Try to extract and print thought if present in JSON or via regex
    thought_match = re.search(
        r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)',
        text,
        flags=re.DOTALL,
    )
    if thought_match:
        raw_thought = thought_match.group(1)
        raw_thought = raw_thought.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        print(f"Thought: {raw_thought.strip()}")

    # 1. Try full JSON parse first (model sometimes wraps answer in final).
    parsed = parse_json_object(text)
    if isinstance(parsed, dict):
        action = parsed.get("action")
        args = parsed.get("args") or {}
        answer = args.get("answer") or parsed.get("answer")
        if action == "final" and answer:
            return answer
        # Any other well-formed JSON we can't interpret — fall through.

    # 2. Regex-extract "answer" from a truncated JSON fragment.
    answer_match = re.search(
        r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)',
        text,
        flags=re.DOTALL,
    )
    if answer_match:
        raw_answer = answer_match.group(1)
        # Unescape basic JSON escape sequences.
        raw_answer = raw_answer.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        return raw_answer.strip()

    # 3. Strip leading JSON boilerplate like {"action":"final","args":{"answer":
    stripped = re.sub(
        r'^\s*\{[^}]*"action"\s*:\s*"[^"]*"[^}]*"args"\s*:\s*\{[^}]*"answer"\s*:\s*"?',
        "",
        text,
        flags=re.DOTALL,
    ).strip().strip('"').strip()
    if stripped and stripped != text:
        return stripped

    # 4. Last resort: return the raw text so the user sees *something*.
    return text


def parse_action_request(raw_response):
    if not raw_response or raw_response.startswith("LLM request failed:"):
        return None

    # Try parsing XML-style <tool_call>
    xml_parsed = parse_tool_call_xml_or_text(raw_response)
    if xml_parsed:
        return xml_parsed

    parsed = parse_json_object(raw_response)
    if not isinstance(parsed, dict) or "action" not in parsed:
        return None
    if "args" not in parsed or parsed["args"] is None:
        parsed["args"] = {}
        
    # If the action is final and answer is in the root, move/copy it to args
    if parsed.get("action") == "final" and "answer" in parsed and "answer" not in parsed["args"]:
        parsed["args"]["answer"] = parsed["answer"]
        
    return parsed


def parse_tool_call_xml_or_text(text):
    # Find "<tool_call>" (case-insensitive)
    match = re.search(r"<tool_call>(.*)", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    
    content = match.group(1)
    # Strip closing tag if present
    content = re.sub(r"</tool_call>.*", "", content, flags=re.IGNORECASE | re.DOTALL).strip()
    
    # 1. Check if the content is a JSON object or contains a JSON object
    json_obj = parse_json_object(content)
    if json_obj and isinstance(json_obj, dict):
        if "action" in json_obj:
            return {
                "action": json_obj.get("action"),
                "args": json_obj.get("args") or {}
            }
    
    # 2. Check if we have XML-like arg_key and arg_value tags:
    # <arg_key>sql</arg_key>\s*<arg_value>SELECT * ...</arg_value>
    # First line of content might contain the action name
    first_line = content.split("\n")[0].strip()
    action = first_line.split("<")[0].strip()
    if not action:
        # check if there's any text before the first tag
        pre_tag = content.split("<")[0].strip()
        if pre_tag:
            action = pre_tag
        else:
            action = "sql" # default fallback

    # Extract all arg_key / arg_value pairs
    args = {}
    keys = re.findall(r"<arg_key>(.*?)</arg_key>", content, flags=re.IGNORECASE | re.DOTALL)
    values = re.findall(r"<arg_value>(.*?)</arg_value>", content, flags=re.IGNORECASE | re.DOTALL)
    for k, v in zip(keys, values):
        args[k.strip()] = v.strip()
        
    if keys and values:
        return {
            "action": action.lower().strip(),
            "args": args
        }
        
    # 3. Simple text style like: "summarize lead_id 1" or "sql SELECT ..."
    # Let's clean the content by stripping any remaining XML tags
    clean_content = re.sub(r"<[^>]+>", "", content).strip()
    
    # Let's extract the action: it's the first word
    words = clean_content.split(None, 1)
    if not words:
        return None
    action = words[0].strip().lower()
    rest = words[1].strip() if len(words) > 1 else ""
    
    # Map cleaned action name to standard action names if needed
    action_map = {
        "schema": "schema",
        "sql": "sql",
        "summarize": "summarize",
        "search": "search",
        "queue": "queue",
        "flags": "flags",
        "assign": "assign",
        "assign_properties": "assign_properties",
        "activity": "activity",
        "follow_up": "follow_up",
        "reminder": "reminder",
        "no_activity": "no_activity",
        "update_record": "update_record",
        "final": "final"
    }
    
    matched_action = None
    for k, v in action_map.items():
        if action == k:
            matched_action = v
            break
    
    if not matched_action:
        return None
        
    args = {}
    if matched_action == "sql":
        query = rest.strip()
        if (query.startswith("'") and query.endswith("'")) or (query.startswith('"') and query.endswith('"')):
            query = query[1:-1].strip()
        args["sql"] = query
    elif matched_action == "summarize":
        num_match = re.search(r"\d+", rest)
        if num_match:
            args["lead_id"] = int(num_match.group(0))
    elif matched_action == "search":
        args["query"] = rest
    elif matched_action in ("queue", "flags", "no_activity"):
        num_match = re.search(r"\d+", rest)
        if num_match:
            args["limit"] = int(num_match.group(0))
    elif matched_action == "assign":
        num_match = re.search(r"\d+", rest)
        if num_match:
            args["lead_id"] = int(num_match.group(0))
        if "strategy" in rest.lower():
            strat_match = re.search(r"strategy\s*[:=]?\s*(\w+)", rest, flags=re.IGNORECASE)
            if strat_match:
                args["strategy"] = strat_match.group(1).strip()
        if "employee" in rest.lower():
            emp_match = re.search(r"employee\s*[:=]?\s*([a-zA-Z\s]+)", rest, flags=re.IGNORECASE)
            if emp_match:
                args["employee"] = emp_match.group(1).strip()
    
    return {
        "action": matched_action,
        "args": args
    }


def compact_json(data, max_chars=12000):
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def lead_display_name(lead):
    name = " ".join(part for part in [lead.get("FirstName"), lead.get("LastName")] if part)
    return name or "Unnamed lead"


def format_lead_summary(summary):
    if "error" in summary:
        return summary["error"]
    pending = summary.get("pending_activities") or []
    pending_text = "; ".join(pending) if pending else "None"
    budget_property = summary.get("budget_and_property_interest") or {}
    return "\n".join(
        [
            f"Lead #{summary.get('lead_id')}: {summary.get('customer')}",
            f"Requirements: {summary.get('customer_requirements')}",
            f"Budget/property: {budget_property.get('budget')} / {budget_property.get('assigned_property')}",
            f"Source: {summary.get('lead_source')}",
            f"Stage: {summary.get('current_stage')}",
            f"Last activity: {summary.get('last_activity')}",
            f"Activity date/time: {summary.get('activity_datetime') or 'Not scheduled'}",
            f"Meeting response: {summary.get('meeting_response') or 'Not provided'}",
            f"Feedback: {summary.get('feedback') or 'Not provided'}",
            f"Pending activities: {pending_text}",
            f"Assigned employee: {summary.get('assigned_employee')}",
        ]
    )


def format_search_result(result):
    title = f"Found {result.get('row_count', len(result.get('rows', [])))} lead(s)"
    return format_search_rows(result.get("rows", []), title)


def format_search_rows(rows, title):
    if not rows:
        return f"{title}: no matching records found."

    lines = [f"{title}:"]
    for index, lead in enumerate(rows[:10], start=1):
        lines.append(
            f"{index}. Lead #{lead.get('LeadId')}: {lead_display_name(lead)} | "
            f"{lead.get('Priority') or 'No priority'} | {lead.get('Stage') or 'No stage'} | "
            f"Source: {lead.get('Source') or 'Not provided'} | "
            f"Assigned: {lead.get('AssignedTo') or 'Unassigned'}"
        )
    if len(rows) > 10:
        lines.append(f"...and {len(rows) - 10} more.")
    return "\n".join(lines)


def format_work_queue(queue):
    if not queue:
        return "There is no prioritized work queued right now."

    lines = ["Today, start with:"]
    for index, item in enumerate(queue[:10], start=1):
        lead = item.get("lead", {})
        flags = item.get("flags") or []
        lines.append(
            f"{index}. Lead #{lead.get('LeadId')}: {lead_display_name(lead)} | "
            f"{lead.get('Priority') or 'No priority'} | {lead.get('Stage') or 'No stage'} | "
            f"Assigned: {lead.get('AssignedTo') or 'Unassigned'}"
        )
        if flags:
            lines.append(f"   Flags: {', '.join(flags)}")
        lines.append(f"   Action: {item.get('recommended_action')}")
    return "\n".join(lines)


def format_health_flags(flagged):
    if not flagged:
        return "No lead health warnings found."

    lines = ["Lead health warnings:"]
    for index, item in enumerate(flagged[:15], start=1):
        lead = item.get("lead", {})
        lines.append(
            f"{index}. Lead #{lead.get('LeadId')}: {lead_display_name(lead)} | "
            f"{', '.join(item.get('flags') or [])}"
        )
    if len(flagged) > 15:
        lines.append(f"...and {len(flagged) - 15} more.")
    return "\n".join(lines)


def is_mutating_action(action):
    return action in {"assign", "assign_properties", "activity", "follow_up", "reminder", "update_record"}


def ask_confirmation(action, args):
    print(f"Agent wants to run database update: {action} {json.dumps(args, ensure_ascii=False)}")
    answer = input("Allow this change? Type yes to confirm: ").strip().lower()
    return answer == "yes"


class VoiceUnavailableError(RuntimeError):
    pass


VOICE_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "tree": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
    "ten": "10",
}

VOICE_PHRASE_REPLACEMENTS = (
    (r"\bmeating\b", "meeting"),
    (r"\bmeting\b", "meeting"),
    (r"\bsight visit\b", "site visit"),
    (r"\bside visit\b", "site visit"),
    (r"\bsite visual\b", "site visit"),
    (r"\bpanting\b", "pending"),
    (r"\bpainting\b", "pending"),
    (r"\bending\b", "pending"),
    (r"\bpanding\b", "pending"),
    (r"\bpen ding\b", "pending"),
    (r"\bcancel\b", "cancelled"),
    (r"\bcanceled\b", "cancelled"),
    (r"\bcomplete\b", "completed"),
    (r"\blid\b", "lead"),
    (r"\bleed\b", "lead"),
    (r"\bleads id\b", "lead"),
    (r"\bassigned 2\b", "assigned to"),
    (r"\bassign 2\b", "assign to"),
)


def best_google_transcript(result):
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""

    alternatives = result.get("alternative") or []
    if not alternatives:
        return ""

    best = max(
        alternatives,
        key=lambda item: item.get("confidence", 0) if isinstance(item, dict) else 0,
    )
    if isinstance(best, dict):
        return best.get("transcript", "")
    return ""


def google_transcript_alternatives(result):
    if isinstance(result, str):
        transcript = result.strip()
        return [transcript] if transcript else []
    if not isinstance(result, dict):
        return []

    transcripts = []
    seen = set()
    for alternative in result.get("alternative") or []:
        if isinstance(alternative, dict):
            transcript = str(alternative.get("transcript") or "").strip()
        else:
            transcript = str(alternative or "").strip()
        transcript_key = transcript.lower()
        if transcript and transcript_key not in seen:
            transcripts.append(transcript)
            seen.add(transcript_key)
    return transcripts


CONFIRMATION_YES_PATTERNS = (
    r"\byes\b",
    r"\byeah\b",
    r"\byep\b",
    r"\byup\b",
    r"\bya\b",
    r"\byah\b",
    r"\byas\b",
    r"\byesh\b",
    r"\bsure\b",
    r"\bok\b",
    r"\bokay\b",
    r"\bconfirm\b",
    r"\bconfirmed\b",
    r"\ballow\b",
    r"\bproceed\b",
    r"\bgo ahead\b",
    r"\bdo it\b",
    r"\bcorrect\b",
    r"\baffirmative\b",
)

CONFIRMATION_NO_PATTERNS = (
    r"\bno\b",
    r"\bnope\b",
    r"\bnah\b",
    r"\bcancel\b",
    r"\bcancelled\b",
    r"\bcanceled\b",
    r"\bstop\b",
    r"\bdeny\b",
    r"\bnegative\b",
    r"\bdo not\b",
    r"\bdon't\b",
)


def parse_confirmation_response(text):
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    if not normalized:
        return None
    normalized = re.sub(r"[^\w\s']", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    matches = []
    for pattern in CONFIRMATION_YES_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            matches.append((match.start(), True))
    for pattern in CONFIRMATION_NO_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            matches.append((match.start(), False))

    if not matches:
        return None
    return min(matches, key=lambda item: item[0])[1]


def normalize_voice_command(text, history=None):
    corrected = re.sub(r"\s+", " ", text or "").strip()
    if not corrected:
        return corrected

    lower_context = " ".join(
        msg.get("content", "")
        for msg in (history or [])[-4:]
        if msg.get("role") == "assistant"
    ).lower()
    activity_context = bool(
        re.search(r"\b(activity|meeting|call|task|site visit|status|feedback|assigned to)\b", corrected.lower())
        or re.search(r"\b(activity|meeting|call|task|site visit|status|feedback|assigned to)\b", lower_context)
    )

    for pattern, replacement in VOICE_PHRASE_REPLACEMENTS:
        corrected = re.sub(pattern, replacement, corrected, flags=re.IGNORECASE)

    corrected = normalize_lead_number_words(corrected)

    if activity_context:
        corrected = normalize_activity_words(corrected)

    return re.sub(r"\s+", " ", corrected).strip()


def normalize_lead_number_words(text):
    def replace_match(match):
        number_word = match.group(1).lower()
        return f"lead {VOICE_NUMBER_WORDS.get(number_word, number_word)}"

    return re.sub(
        r"\blead\s+(" + "|".join(re.escape(word) for word in VOICE_NUMBER_WORDS) + r")\b",
        replace_match,
        text,
        flags=re.IGNORECASE,
    )


def normalize_activity_words(text):
    words = text.split()
    valid_terms = {
        "pending": ("pending", "painting", "panding", "panting", "ending"),
        "completed": ("completed", "complete", "competed"),
        "cancelled": ("cancelled", "canceled", "cancel", "cancels"),
        "meeting": ("meeting", "meating", "meting"),
        "call": ("call", "called"),
        "task": ("task", "tasks"),
    }
    reverse_terms = {
        variant: canonical
        for canonical, variants in valid_terms.items()
        for variant in variants
    }

    normalized = []
    for word in words:
        prefix = re.match(r"^\W*", word).group(0)
        suffix = re.search(r"\W*$", word).group(0)
        core = word[len(prefix) : len(word) - len(suffix) if suffix else len(word)]
        lowered = core.lower()
        replacement = reverse_terms.get(lowered)
        if not replacement:
            match = difflib.get_close_matches(lowered, valid_terms.keys(), n=1, cutoff=0.84)
            replacement = match[0] if match else None
        normalized.append(f"{prefix}{replacement or core}{suffix}")

    result = " ".join(normalized)
    if re.search(r"\bstatus\b", result, flags=re.IGNORECASE):
        result = re.sub(r"\bstarted\b", "pending", result, flags=re.IGNORECASE)
    return result


class VoiceIO:
    def __init__(
        self,
        recognizer_backend="google",
        device_index=None,
        language="en-IN",
        pause_threshold=1.4,
        speak=True,
    ):
        try:
            import speech_recognition as speech_recognition_module
        except ImportError as exc:
            raise VoiceUnavailableError(
                "Voice mode needs SpeechRecognition and PyAudio. Install them with:\n"
                "python -m pip install SpeechRecognition PyAudio pyttsx3"
            ) from exc

        self.sr = speech_recognition_module
        self.wait_timeout_error = self.sr.WaitTimeoutError
        self.unknown_value_error = self.sr.UnknownValueError
        self.request_error = self.sr.RequestError
        self.recognizer_backend = recognizer_backend
        self.language = language
        self.recognizer = self.sr.Recognizer()
        self.recognizer.pause_threshold = pause_threshold
        self.recognizer.non_speaking_duration = 0.6
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.energy_threshold = env_int("VOICE_ENERGY_THRESHOLD", 300, minimum=50, maximum=4000)

        try:
            self.microphone = self.sr.Microphone(device_index=device_index)
        except (AttributeError, OSError) as exc:
            raise VoiceUnavailableError(
                "Could not open a microphone. Make sure PyAudio is installed and a microphone is connected."
            ) from exc

        self.engine = None
        if speak:
            try:
                import pyttsx3

                self.engine = pyttsx3.init()
                self.engine.setProperty("rate", env_int("VOICE_RATE", 175, minimum=80, maximum=260))
            except Exception as exc:
                print(f"Text-to-speech is unavailable, so I will print responses only: {exc}")

    def listen(self, source, timeout=8, phrase_time_limit=25):
        audio = self.recognizer.listen(
            source,
            timeout=timeout,
            phrase_time_limit=phrase_time_limit,
        )
        if self.recognizer_backend == "sphinx":
            return self.recognizer.recognize_sphinx(audio).strip()
        result = self.recognizer.recognize_google(
            audio,
            language=self.language,
            show_all=True,
        )
        transcript = best_google_transcript(result)
        if not transcript:
            raise self.unknown_value_error()
        return transcript.strip()

    def listen_confirmation(self, source, timeout=6, phrase_time_limit=5):
        audio = self.recognizer.listen(
            source,
            timeout=timeout,
            phrase_time_limit=phrase_time_limit,
        )
        if self.recognizer_backend == "sphinx":
            transcript = self.recognizer.recognize_sphinx(audio).strip()
            return transcript, parse_confirmation_response(transcript)

        candidates = []
        seen = set()
        languages = [self.language, "en-US", "en-GB"]
        for language in dict.fromkeys(lang for lang in languages if lang):
            result = self.recognizer.recognize_google(
                audio,
                language=language,
                show_all=True,
            )
            for transcript in google_transcript_alternatives(result):
                transcript_key = transcript.lower()
                if transcript_key in seen:
                    continue
                candidates.append(transcript)
                seen.add(transcript_key)

                decision = parse_confirmation_response(transcript)
                if decision is not None:
                    return transcript, decision

        if not candidates:
            raise self.unknown_value_error()
        return candidates[0], None

    def say(self, text):
        if not self.engine or not text:
            return
        self.engine.say(text)
        self.engine.runAndWait()

    def confirm_action(self, source, action, args, listen_timeout=6):
        prompt = (
            f"Agent wants to run database update: {action} "
            f"{json.dumps(args, ensure_ascii=False)}"
        )
        print(prompt)
        print("Say 'yes' to confirm, or 'no' to cancel.")
        self.say("This needs a database update. Say yes to confirm, or no to cancel.")

        for _ in range(3):
            try:
                response, decision = self.listen_confirmation(
                    source,
                    timeout=listen_timeout,
                    phrase_time_limit=5,
                )
            except self.wait_timeout_error:
                print("No confirmation heard.")
                self.say("I did not hear a confirmation. Please say yes or no.")
                continue
            except self.unknown_value_error:
                print("Could not understand the confirmation.")
                self.say("I could not understand that. Please say yes or no.")
                continue
            except self.request_error as exc:
                print(f"Speech recognition failed during confirmation: {exc}")
                return False

            print(f"You confirmed: {response}")
            if decision is True:
                return True
            if decision is False:
                return False

            self.say("Please say yes to confirm, or no to cancel.")

        return False


def voice_response_text(answer):
    text = re.sub(r"\s+", " ", str(answer or "")).strip()
    max_chars = env_int(
        "VOICE_RESPONSE_MAX_CHARS",
        DEFAULT_VOICE_RESPONSE_MAX_CHARS,
        minimum=200,
        maximum=5000,
    )
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + ". I printed the full response on screen."


def print_json(data):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    load_env()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Projini CRM Admin Agent")
    parser.add_argument("--db", default=DB_NAME, help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("chat")
    voice_parser = subparsers.add_parser("voice")
    voice_parser.add_argument(
        "--recognizer",
        choices=["google", "sphinx"],
        default=os.environ.get("VOICE_RECOGNIZER", "google"),
        help="Speech recognition backend. google is easiest; sphinx works offline if pocketsphinx is installed.",
    )
    voice_parser.add_argument("--device-index", type=int, help="Microphone device index")
    voice_parser.add_argument("--timeout", type=float, default=8, help="Seconds to wait for speech to start")
    voice_parser.add_argument(
        "--phrase-time-limit",
        type=float,
        default=25,
        help="Maximum seconds for each spoken command",
    )
    voice_parser.add_argument(
        "--language",
        default=os.environ.get("VOICE_LANGUAGE", "en-IN"),
        help="Recognition language code for Google recognizer, for example en-IN or en-US",
    )
    voice_parser.add_argument(
        "--pause-threshold",
        type=float,
        default=env_float("VOICE_PAUSE_THRESHOLD", 1.4, minimum=0.4, maximum=4.0),
        help="Seconds of silence before a phrase is considered complete",
    )
    voice_parser.add_argument("--no-speak", action="store_true", help="Listen by voice but print responses only")
    subparsers.add_parser("schema")

    ask_parser = subparsers.add_parser("ask")
    ask_parser.add_argument("message")

    summarize_parser = subparsers.add_parser("summarize")
    summarize_parser.add_argument("lead_id", type=int)
    summarize_parser.add_argument("--llm", action="store_true")

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=50)

    sql_parser = subparsers.add_parser("sql")
    sql_parser.add_argument("sql")
    sql_parser.add_argument("--limit", type=int, default=50)

    assign_parser = subparsers.add_parser("assign")
    assign_parser.add_argument("lead_id", type=int)
    assign_parser.add_argument("--employee")
    assign_parser.add_argument("--strategy", choices=["smart", "least_workload", "round_robin"], default="smart")

    subparsers.add_parser("properties")

    assign_properties_parser = subparsers.add_parser("assign-properties")
    assign_properties_parser.add_argument("lead_id", type=int)
    assign_properties_parser.add_argument("properties", nargs="+")

    activity_parser = subparsers.add_parser("activity")
    activity_parser.add_argument("lead_id", type=int)
    activity_parser.add_argument("activity_type", choices=["Call", "Meeting", "Task", "Site Visit"])
    activity_parser.add_argument("status", choices=["Pending", "Completed", "Cancelled"])
    activity_parser.add_argument("note")
    activity_parser.add_argument("--assigned-to")
    activity_parser.add_argument("--datetime", dest="activity_datetime")
    activity_parser.add_argument("--meeting-response", choices=list(MEETING_RESPONSES))
    activity_parser.add_argument("--feedback")

    follow_up_parser = subparsers.add_parser("follow-up")
    follow_up_parser.add_argument("lead_id", type=int)
    follow_up_parser.add_argument("--when", default="tomorrow")
    follow_up_parser.add_argument("--note", default="Follow up with lead")

    reminder_parser = subparsers.add_parser("reminder")
    reminder_parser.add_argument("lead_id", type=int)
    reminder_parser.add_argument("--note", default="Remind assigned employee to follow up")

    queue_parser = subparsers.add_parser("queue")
    queue_parser.add_argument("--limit", type=int, default=25)

    flags_parser = subparsers.add_parser("flags")
    flags_parser.add_argument("--limit", type=int, default=100)

    args = parser.parse_args()
    agent = CRMAdminAgent(args.db)

    if args.command == "schema":
        print(agent.schema_text())
    elif args.command == "ask":
        print(agent.chat_once(args.message, []))
    elif args.command == "chat":
        agent.chat()
    elif args.command == "voice":
        sys.exit(
            agent.voice_chat(
                recognizer_backend=args.recognizer,
                device_index=args.device_index,
                listen_timeout=args.timeout,
                phrase_time_limit=args.phrase_time_limit,
                language=args.language,
                pause_threshold=args.pause_threshold,
                speak=not args.no_speak,
            )
        )
    elif args.command == "summarize":
        print_json(agent.summarize_lead(args.lead_id, use_llm=args.llm))
    elif args.command == "search":
        print_json(agent.search(args.query, limit=args.limit))
    elif args.command == "sql":
        try:
            print_json(agent.execute_read_sql(args.sql, limit=args.limit))
        except (ValueError, sqlite3.Error) as exc:
            print_json({"error": str(exc)})
            sys.exit(2)
    elif args.command == "assign":
        print_json(agent.assign_lead(args.lead_id, strategy=args.strategy, employee=args.employee))
    elif args.command == "properties":
        print_json(agent.property_names())
    elif args.command == "assign-properties":
        print_json(agent.set_lead_properties(args.lead_id, args.properties))
    elif args.command == "activity":
        print_json(
            agent.update_activity(
                args.lead_id,
                args.activity_type,
                args.status,
                note=args.note,
                assigned_to=args.assigned_to,
                activity_datetime=args.activity_datetime,
                meeting_response=args.meeting_response,
                feedback=args.feedback or args.note,
            )
        )
    elif args.command == "follow-up":
        print_json(agent.schedule_follow_up(args.lead_id, when_text=args.when, note=args.note))
    elif args.command == "reminder":
        print_json(agent.remind_next_day(args.lead_id, note=args.note))
    elif args.command == "queue":
        print_json(agent.daily_work_queue(limit=args.limit))
    elif args.command == "flags":
        print_json(agent.health_flags(limit=args.limit))


if __name__ == "__main__":
    main()
