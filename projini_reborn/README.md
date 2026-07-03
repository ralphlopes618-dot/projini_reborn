# Projini CRM Admin Agent

Admin chatbot and CLI for lead search, summaries, assignment, activities, daily queues, and health flags.

## Setup

Create and activate a project virtual environment before installing voice libraries:

```powershell
cd C:\Users\elson\Downloads\projini_reborn\projini_reborn
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation on your machine, allow it for the current terminal session and activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

When the environment is active, your prompt should show `(.venv)`. To leave it later:

```powershell
deactivate
```

## Run

```powershell
python crm_admin_agent.py chat
```

Voice chat

```powershell
python crm_admin_agent.py voice
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
python crm_admin_agent.py properties
python crm_admin_agent.py assign-properties 1 "Lodha Crown (Residential â€“ 1 & 2 BHK)" "Rustomjee Urbania (Residential â€“ Premium Apartments)"
python crm_admin_agent.py activity 1 Call Completed "Called customer and confirmed interest" --meeting-response "Interested" --feedback "Customer confirmed interest"
python crm_admin_agent.py activity 1 Meeting Pending "Meeting scheduled" --datetime "2026-07-02 15:30" --assigned-to "Ralph Lopes" --feedback "Discuss pricing"
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
z-ai/glm-5.2
```

Token usage is capped by default so OpenRouter does not request a huge completion budget.
Optional `.env` settings:

```text
OPENROUTER_MAX_TOKENS=1024
LLM_TOOL_RESULT_MAX_CHARS=6000
LLM_VALUE_CATALOG_MAX_VALUES=12
```

## Voice Mode

Voice mode uses the same chatbot flow as typed chat. You can speak the same commands you would type, such as:

```text
Show me hot leads
Assign lead 1 to Ralph Lopes
Assign Lodha Crown to lead 1
Create a pending call activity for lead 1 tomorrow at 11 AM assigned to Ralph Lopes
What should I do today?
```

The agent prints the full answer and also speaks it aloud. For database-changing actions, it asks for confirmation; say `yes` to apply the change or `no` to cancel.

Useful options:

```powershell
python crm_admin_agent.py voice --no-speak
python crm_admin_agent.py voice --device-index 1
python crm_admin_agent.py voice --recognizer sphinx
```

The default recognizer is `google`, which uses the SpeechRecognition Google web recognizer. Use `--recognizer sphinx` only after installing an offline Sphinx setup such as `pocketsphinx`.

Optional `.env` settings:

```text
VOICE_RECOGNIZER=google
VOICE_RATE=175
VOICE_RESPONSE_MAX_CHARS=1200
```

## Assignment Rules

Smart assignment reads `assignment_rules.json`. Edit employee locations, sources, priorities, property interests, properties, or languages there to tune business routing without changing Python.

## Property Assignment

Property choices are stored in the `AvailableProperties` table.
Only user-selected properties are stored on leads in `AssignedProperties`.
Use `python crm_admin_agent.py properties` to see the selectable property list.

## Activity Rules

Activity creation requires `ActivityType` and `ActivityStatus`.

Activity types: `Call`, `Meeting`, `Task`, `Site Visit`.
Activity statuses: `Pending`, `Completed`, `Cancelled`.

Pending activities require a future `ActivityDateTime` and `AssignedTo`.
`AssignedTo` must be selected from the current unique values in the database.
Completed or cancelled activities require a `MeetingResponse`.
All activity statuses require `Feedback`.
