# AI Chatter Executor — Stage 3.11 Watch Mode

Stable folder name: `ai_chatter_executor`.

This version keeps Stage 3.10 `run_once` and adds:

```text
ai_chatter_watch.py
ai_chatter_watch.bat
```

Watch mode continuously runs:

```text
tasks-only → Playwright CDP worker → intake
```

Then delivery is still manual in Gajim:

```text
.deliver
.results
```

## Install

Unpack to:

```text
G:\AI_chatter\
```

with replacement of:

```text
G:\AI_chatter\ai_chatter_executor
```

## Run watch mode

```powershell
cd G:\AI_chatter\ai_chatter_executor

python .\ai_chatter_watch.py --queue "$env:APPDATA\Gajim\Plugins\aichatter_bridge\ai_chatter_tasks.jsonl" --interval 5 --max-tasks 1 --max-jobs 1 --timeout-ms 60000
```

Or double-click:

```text
G:\AI_chatter\ai_chatter_executor\ai_chatter_watch.bat
```

Stop with:

```text
Ctrl+C
```

## Requirements

- Chrome must be running with CDP port 9222.
- Target AI chat tabs must be open in the CDP Chrome.
- Bot routing comes from Gajim plugin checkboxes.
- `ai_chatter_profiles.json` remains the source of truth for selectors.


## Stage 3.12

Adds lock-safe event-driven `run_once`:
- `ai_chatter_run_once.py` creates `ai_chatter_run_once.lock` next to the queue.
- If another run is active, a new run exits safely.
- Stale locks older than 1800 seconds are removed automatically.
- Intended to be launched by the Gajim plugin when a new task is queued.
