#!/usr/bin/env python3
"""AI Chatter Executor 4.0.17 Local Bus ping tool.

Writes a bus.ping request to configs/ai_chatter_bus_requests.jsonl and waits
for bus.pong in logs/ai_chatter_bus_events.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

VERSION = "4.0.17"
BRANCH = "4"
PROTOCOL = "ai_chatter.local_bus.v4"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_home(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("AI_CHATTER_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    if here.parent.name.lower() == "ai_chatter_executor":
        return here.parent.parent
    for candidate in (Path("G:/AI_chatter"), Path("C:/AI_chatter")):
        if candidate.exists():
            return candidate
    return here.parent.parent


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_events_since(path: Path, offset: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    events = []
    with path.open("rb") as f:
        f.seek(offset)
        for raw in f:
            try:
                if raw.strip():
                    events.append(json.loads(raw.decode("utf-8")))
            except Exception:
                pass
        return events, f.tell()


def unwrap_event(row: dict) -> dict:
    if isinstance(row.get("event"), dict):
        return row["event"]
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Branch 4 bus ping")
    parser.add_argument("--home", default=None, help="AI Chatter home directory")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--target", default="chrome_extension")
    args = parser.parse_args()

    home = find_home(args.home)
    configs = home / "configs"
    logs = home / "logs"
    requests = configs / "ai_chatter_bus_requests.jsonl"
    events = logs / "ai_chatter_bus_events.jsonl"
    configs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    correlation_id = f"busping-{uuid4()}"
    event = {
        "id": correlation_id,
        "type": "bus.ping",
        "source": "executor_cli",
        "target": args.target,
        "timestamp": now_iso(),
        "protocol": PROTOCOL,
        "branch": BRANCH,
        "version": VERSION,
        "correlation_id": correlation_id,
        "payload": {
            "reason": "manual_ping",
            "home": str(home),
        },
    }

    offset = events.stat().st_size if events.exists() else 0
    append_jsonl(requests, {"event": event, "ts": now_iso()})

    print("AI Chatter Executor 4.0.17 Local Bus Ping")
    print(f"Home:      {home}")
    print(f"Requests:  {requests}")
    print(f"Events:    {events}")
    print(f"Ping id:   {correlation_id}")
    print("Waiting for bus.pong...")

    deadline = time.time() + args.timeout_ms / 1000.0
    while time.time() < deadline:
        rows, offset = read_events_since(events, offset)
        for row in rows:
            evt = unwrap_event(row)
            if evt.get("type") == "bus.pong" and evt.get("id") == correlation_id:
                print("OK: bus.pong received")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                return 0
            if evt.get("type") == "bus.error" and evt.get("id") == correlation_id:
                print("ERROR: bus.error received")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                return 2
        time.sleep(0.25)

    print("TIMEOUT: no bus.pong received")
    print("Check that Chrome extension is loaded and native host is installed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
