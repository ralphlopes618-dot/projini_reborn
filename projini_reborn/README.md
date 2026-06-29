# Projini CRM Admin Agent

Admin chatbot and CLI for lead search, summaries, assignment, activities, daily queues, and health flags.

## Run

```powershell
python crm_admin_agent.py chat
```

One-shot question

```powershell
python crm_admin_agent.py ask "What should I do today?"
```

Useful direct commands:

```powershell
python crm_admin_agent.py schema
python crm_admin_agent.py summarize 1
python crm_admin_agent.py search "Show me hot leads from 99 Acres" --limit 10
python crm_admin_agent.py queue --limit 10
python crm_admin_agent.py flags --limit 20
python crm_admin_agent.py assign 1 --strategy smart
python crm_admin_agent.py activity 1 Call Completed "Called customer and confirmed interest"
python crm_admin_agent.py follow-up 1 --when tomorrow --note "Call back with pricing"
python crm_admin_agent.py reminder 1 --note "Check pending follow-up"
```

Read-only SQL is available for admin analysis across all tables and columns:

```powershell
python crm_admin_agent.py sql "SELECT * FROM Lead_Details LIMIT 5"
```

The SQL command only accepts `SELECT` or `WITH` queries. Writes must go through chatbot actions such as assignment, activity updates, reminders, or schema-validated admin updates.

## LLM

The agent reads `.env` automatically and supports either `API_KEY` or `OPENROUTER_API_KEY`.

Configured model:

```text
nex-agi/nex-n2-pro:free
```

## Assignment Rules

Smart assignment reads `assignment_rules.json`. Edit employee locations, sources, priorities, property interests, properties, or languages there to tune business routing without changing Python.
