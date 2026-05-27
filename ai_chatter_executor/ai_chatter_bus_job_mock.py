#!/usr/bin/env python3
"""AI Chatter Executor 4.0.17 mock provider strict dedupe/state machine test.

Writes provider.job.run to configs/ai_chatter_bus_requests.jsonl and waits for
provider.job.accepted/progress/done/error in logs/ai_chatter_bus_events.jsonl.
Adds strict client-side state checks: duplicate accepted/progress-after-final are ignored.
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


def now_ms() -> int:
    return int(time.time() * 1000)


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


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def read_events_since(path: Path, offset: int) -> tuple[list[dict], int]:
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    events: list[dict] = []
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


def completed_lookup(path: Path, job_id: str) -> dict | None:
    for row in reversed(read_jsonl(path)):
        if row.get("job_id") == job_id and row.get("final") is True:
            return row
    return None


def event_matches(evt: dict, job_id: str, correlation_id: str) -> bool:
    if evt.get("job_id") == job_id:
        return True
    if evt.get("correlation_id") == correlation_id:
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Branch 4 mock provider strict dedupe/state machine test")
    parser.add_argument("--home", default=None, help="AI Chatter home directory")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--agent-id", default="test_agent_v1")
    parser.add_argument("--text", default="ping")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--job-id", default=None, help="Optional stable job_id for duplicate/idempotency tests")
    parser.add_argument("--mock-delay-ms", type=int, default=1200)
    parser.add_argument("--simulate-timeout", action="store_true")
    parser.add_argument("--simulate-error", action="store_true")
    parser.add_argument("--target", default="chrome_extension")
    args = parser.parse_args()

    home = find_home(args.home)
    configs = home / "configs"
    logs = home / "logs"
    requests = configs / "ai_chatter_bus_requests.jsonl"
    events = logs / "ai_chatter_bus_events.jsonl"
    completed = logs / "ai_chatter_completed_jobs.jsonl"
    configs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    job_id = args.job_id or f"job_mock_{uuid4()}"
    existing = completed_lookup(completed, job_id)
    if existing:
        print("AI Chatter Executor 4.0.17 Mock Job strict dedupe")
        print(f"Home:      {home}")
        print(f"Job id:    {job_id}")
        print("Already completed; returning stored final result")
        print(json.dumps(existing, ensure_ascii=False, indent=2))
        return 0 if existing.get("status") == "done" else 2

    correlation_id = f"corr_mock_{uuid4()}"
    event = {
        "id": f"evt_run_{uuid4()}",
        "type": "provider.job.run",
        "source": "executor_cli",
        "target": args.target,
        "timestamp": now_iso(),
        "protocol": PROTOCOL,
        "branch": BRANCH,
        "version": VERSION,
        "correlation_id": correlation_id,
        "job_id": job_id,
        "payload": {
            "provider": args.provider,
            "agent_id": args.agent_id,
            "text": args.text,
            "mode": "sync",
            "session_kind": "chat",
            "timeout_ms": args.timeout_ms,
            "metadata": {
                "mock_delay_ms": args.mock_delay_ms,
                "mock_chunks": 2,
                "simulate_error": bool(args.simulate_error),
                "simulate_timeout": bool(args.simulate_timeout),
                "mock_reply": f"Pong received. Mock sequence active. Text: {args.text}",
            },
        },
    }

    offset = events.stat().st_size if events.exists() else 0
    append_jsonl(requests, {"event": event, "ts": now_iso()})

    print("AI Chatter Executor 4.0.17 Mock Job strict dedupe")
    print(f"Home:      {home}")
    print(f"Requests:  {requests}")
    print(f"Events:    {events}")
    print(f"Completed: {completed}")
    print(f"Job id:    {job_id}")
    print(f"Corr id:   {correlation_id}")
    print(f"Provider:  {args.provider}")
    print(f"Text:      {args.text}")
    print("Waiting for provider.job.* events...")

    accepted = False
    in_progress = False
    final_seen = False
    seen_event_ids = set()
    start = now_ms()
    deadline = time.time() + args.timeout_ms / 1000.0
    last_event_type = None

    while time.time() < deadline:
        rows, offset = read_events_since(events, offset)
        for row in rows:
            evt = unwrap_event(row)
            if not event_matches(evt, job_id, correlation_id):
                continue
            event_id = evt.get("id")
            if event_id and event_id in seen_event_ids:
                print(f"WARN: duplicate event ignored: {event_id}")
                continue
            if event_id:
                seen_event_ids.add(event_id)
            typ = evt.get("type")
            last_event_type = typ
            payload = evt.get("payload") or {}
            if typ == "provider.job.accepted":
                status = payload.get("status", "accepted")
                if accepted and status == "accepted":
                    print("WARN: duplicate accepted ignored")
                    continue
                if accepted and status == "already_processing":
                    print("WARN: duplicate already_processing ignored")
                    continue
                accepted = True
                print(f"ACCEPTED: queue={payload.get('queue_position')} status={status}")
            elif typ == "provider.job.progress":
                if final_seen:
                    print("WARN: late progress after final ignored")
                    continue
                if not accepted:
                    print("WARN: progress before accepted ignored")
                    continue
                in_progress = True
                print(f"PROGRESS: {payload.get('percent')}% | {payload.get('step')} | {payload.get('hint')} | chunk={payload.get('chunk')!r}")
            elif typ == "provider.job.done":
                if not accepted:
                    print("ERROR: done received without accepted; ignored")
                    continue
                if final_seen:
                    print("WARN: duplicate done ignored")
                    continue
                final_seen = True
                elapsed = now_ms() - start
                record = {
                    "ts": now_iso(),
                    "final": True,
                    "status": "done",
                    "job_id": job_id,
                    "correlation_id": correlation_id,
                    "elapsed_ms": elapsed,
                    "event": evt,
                }
                append_jsonl(completed, record)
                print("DONE: provider.job.done received")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                print("OK: mock job lifecycle completed")
                return 0
            elif typ == "provider.job.error" or typ == "bus.error":
                final_seen = True
                elapsed = now_ms() - start
                record = {
                    "ts": now_iso(),
                    "final": True,
                    "status": "error",
                    "job_id": job_id,
                    "correlation_id": correlation_id,
                    "elapsed_ms": elapsed,
                    "event": evt,
                }
                append_jsonl(completed, record)
                print(f"ERROR: {typ} received")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                return 2
        time.sleep(0.2)

    timeout_evt = {
        "id": f"evt_timeout_{uuid4()}",
        "type": "provider.job.error",
        "source": "executor_cli",
        "target": "local_bus",
        "timestamp": now_iso(),
        "protocol": PROTOCOL,
        "branch": BRANCH,
        "version": VERSION,
        "correlation_id": correlation_id,
        "job_id": job_id,
        "payload": {
            "code": "JOB_TIMEOUT",
            "message": "No provider.job.done received within timeout_ms",
            "recoverable": True,
            "retry_recommended": False,
            "details": {"elapsed_ms": args.timeout_ms, "last_event_type": last_event_type},
        },
    }
    append_jsonl(events, timeout_evt)
    append_jsonl(completed, {
        "ts": now_iso(),
        "final": True,
        "status": "error",
        "job_id": job_id,
        "correlation_id": correlation_id,
        "elapsed_ms": args.timeout_ms,
        "event": timeout_evt,
    })
    print("TIMEOUT: no provider.job.done received")
    print(json.dumps(timeout_evt, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
