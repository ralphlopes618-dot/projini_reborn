import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta


DB_NAME = "leads.db"
ENV_FILE = ".env"
OPENROUTER_MODEL = "z-ai/glm-5.2"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_LLM_MAX_TOKENS = 1024
DEFAULT_LLM_TOOL_RESULT_MAX_CHARS = 6000
DEFAULT_LLM_VALUE_CATALOG_MAX_VALUES = 12
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ASSIGNMENT_RULES_FILE = "assignment_rules.json"
PROHIBITED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|VACUUM|"
    r"REINDEX|ANALYZE|PRAGMA)\b",
    flags=re.IGNORECASE,
)
NO_ACTIVITY_SQL = "(ActivityType IS NULL OR ActivityStatus IS NULL OR ActivityNote IS NULL)"


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

    def connect(self):
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
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
            score += match_rule_value(lead.get("AssignedProperty"), profile.get("properties"), 4)
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

    def update_activity(self, lead_id, activity_type, status, note):
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
                    UpdatedAt = CURRENT_TIMESTAMP
                WHERE LeadId = ?
                """,
                (activity_type, status, note, lead_id),
            )
            conn.commit()

        return {
            "lead_id": lead_id,
            "activity_type": activity_type,
            "activity_status": status,
            "activity_note": note,
        }

    def schedule_follow_up(self, lead_id, when_text="tomorrow", note="Follow up with lead"):
        return self.update_activity(
            lead_id,
            "Task",
            "Pending",
            f"{note}. Scheduled for {when_text}.",
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
            f"Reminder for {assignee} on {reminder_date}: {note}",
        )

    def mark_call_completed(self, lead_id, note="Call completed"):
        return self.update_activity(lead_id, "Call", "Completed", note)

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
        if not lead.get("AssignedProperty"):
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

Available tool actions:
- schema: show database schema. Args: {{}}
- sql: run safe read-only SQL against any table/column. Args: {{"sql": "SELECT * FROM Lead_Details LIMIT 10"}}
- summarize: summarize one lead. Args: {{"lead_id": 1}}
- search: natural language lead search. Args: {{"query": "hot leads from 99 Acres", "limit": 10}}
- queue: daily prioritized work queue. Args: {{"limit": 10}}
- flags: lead health warnings. Args: {{"limit": 20}}
- assign: assign a lead. Args: {{"lead_id": 1, "employee": "Ralph Lopes"}} or {{"lead_id": 1, "strategy": "smart"}}
- activity: update activity. Args: {{"lead_id": 1, "activity_type": "Call", "status": "Completed", "note": "Called customer"}}
- follow_up: schedule follow-up. Args: {{"lead_id": 1, "when": "tomorrow", "note": "Call back"}}
- reminder: remind assigned employee next day. Args: {{"lead_id": 1, "note": "Call customer"}}
- no_activity: find leads with no activity. Args: {{"limit": 20}}
- update_record: admin update to any valid table/column by primary key. Args: {{"table": "Lead_Details", "pk_value": 1, "values": {{"Stage": "Interested"}}}}
- final: answer the user directly. Args: {{"answer": "your response"}}

Rules:
- Return exactly one JSON object and no markdown.
- JSON format: {{"action": "search", "args": {{"query": "...", "limit": 10}}}}
- Use a tool when database information or database action is needed.
- For analysis/search across arbitrary columns, prefer sql with a safe SELECT query.
- Use final only when you can answer without another tool, or after tool results are provided.
- For updates, choose the proper action but do not claim success until the tool result confirms it.
- Never use update_record for activity workflow when activity/follow_up/reminder fits better.
- Keep final answers concise and useful for an Admin user.
""".strip()

    def chat_once(self, user_message, history):
        pending_update = pending_duplicate_update(user_message, history, self.table_columns("Lead_Details"))
        if pending_update:
            return self.apply_pending_duplicate_update(pending_update)

        messages = [{"role": "system", "content": self.chatbot_system_prompt()}]
        messages.extend(history[-8:])
        messages.append({"role": "user", "content": user_message})

        for _ in range(4):
            raw_response = self.call_llm_messages(messages)
            if raw_response.startswith("LLM request failed:"):
                return self.fallback_chat_once(user_message, raw_response)

            action_request = parse_action_request(raw_response)

            if not action_request:
                return raw_response

            action = action_request.get("action")
            args = action_request.get("args") or {}

            if action == "final":
                return args.get("answer", "")

            if is_mutating_action(action):
                confirmed = ask_confirmation(action, args)
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

    def fallback_chat_once(self, user_message, llm_error):
        lowered = user_message.lower()

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
            if action == "activity":
                return self.update_activity(
                    int(args["lead_id"]),
                    args["activity_type"],
                    args["status"],
                    args["note"],
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


def send_llm_request(request):
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return f"LLM request failed: HTTP {exc.code} {extract_llm_error(body)}"
    except urllib.error.URLError as exc:
        return f"LLM request failed: {exc}"

    try:
        data = json.loads(body)
        return extract_llm_content(data)
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        return f"LLM request failed: {exc}"


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
    activity = "No activity recorded"
    if lead.get("ActivityType") or lead.get("ActivityStatus") or lead.get("ActivityNote"):
        activity = (
            f"{lead.get('ActivityType') or 'Activity'} - "
            f"{lead.get('ActivityStatus') or 'Unknown status'}"
        )
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
            "assigned_property": lead.get("AssignedProperty") or "Not assigned",
        },
        "lead_source": lead.get("Source") or "Not provided",
        "current_stage": lead.get("Stage") or "Not provided",
        "last_activity": activity,
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
    if not lead.get("AssignedProperty"):
        return "Assign a matching property."
    return "Review and update the lead stage after next contact."


def parse_action_request(raw_response):
    if not raw_response or raw_response.startswith("LLM request failed:"):
        return None

    parsed = parse_json_object(raw_response)
    if not isinstance(parsed, dict) or "action" not in parsed:
        return None
    if "args" not in parsed or parsed["args"] is None:
        parsed["args"] = {}
    return parsed


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
    return action in {"assign", "activity", "follow_up", "reminder", "update_record"}


def ask_confirmation(action, args):
    print(f"Agent wants to run database update: {action} {json.dumps(args, ensure_ascii=False)}")
    answer = input("Allow this change? Type yes to confirm: ").strip().lower()
    return answer == "yes"


def print_json(data):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Projini CRM Admin Agent")
    parser.add_argument("--db", default=DB_NAME, help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("chat")
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

    activity_parser = subparsers.add_parser("activity")
    activity_parser.add_argument("lead_id", type=int)
    activity_parser.add_argument("activity_type", choices=["Call", "Meeting", "Task", "Site Visit"])
    activity_parser.add_argument("status", choices=["Pending", "Completed", "Cancelled"])
    activity_parser.add_argument("note")

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
    elif args.command == "activity":
        print_json(agent.update_activity(args.lead_id, args.activity_type, args.status, args.note))
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
