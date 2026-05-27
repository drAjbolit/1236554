#!/usr/bin/env python3
"""AI Chatter Executor 4.0.17 provider job CLI.

Sends provider.job.run through Branch 4 Local Bus and waits for accepted/progress/done/error.
This is the first real-provider MVP. It does not use legacy CDP/Playwright DOM fallback.
"""
from __future__ import annotations

import argparse
import json
import os
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
    rows: list[dict] = []
    with path.open("rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            try:
                if raw.strip():
                    rows.append(json.loads(raw.decode("utf-8")))
            except Exception:
                pass
        return rows, f.tell()


def unwrap_event(row: dict) -> dict:
    if isinstance(row.get("event"), dict):
        return row["event"]
    return row


def matches(evt: dict, job_id: str, corr_id: str) -> bool:
    return evt.get("job_id") == job_id or evt.get("correlation_id") == corr_id


def make_run_event(provider: str, text: str, timeout_ms: int, job_id: str, corr_id: str) -> dict:
    return {
        "id": f"evt_run_{uuid4()}",
        "type": "provider.job.run",
        "source": "executor_cli",
        "target": "chrome_extension",
        "timestamp": now_iso(),
        "protocol": PROTOCOL,
        "branch": BRANCH,
        "version": VERSION,
        "correlation_id": corr_id,
        "job_id": job_id,
        "payload": {
            "provider": provider,
            "agent_id": provider,
            "text": text,
            "mode": "sync",
            "session_kind": "direct",
            "timeout_ms": timeout_ms,
            "metadata": {
                "branch4_real_provider_mvp": True,
                "transport": "extension_native_worker_v417"
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Branch 4.0.17 provider job CLI")
    parser.add_argument("--home", default=None)
    parser.add_argument("--provider", default="qwen", choices=["qwen", "deepseek", "mock"])
    parser.add_argument("--text", required=True)
    parser.add_argument("--timeout-ms", type=int, default=90000)
    parser.add_argument("--job-id", default=None)
    args = parser.parse_args()

    home = find_home(args.home)
    configs = home / "configs"
    logs = home / "logs"
    requests = configs / "ai_chatter_bus_requests.jsonl"
    events = logs / "ai_chatter_bus_events.jsonl"
    configs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    job_id = args.job_id or f"job_provider_{args.provider}_{uuid4()}"
    corr_id = f"corr_provider_{uuid4()}"
    event = make_run_event(args.provider, args.text, args.timeout_ms, job_id, corr_id)
    offset = events.stat().st_size if events.exists() else 0
    append_jsonl(requests, {"event": event, "ts": now_iso()})

    print(f"AI Chatter Executor {VERSION} Provider Job")
    print(f"Home:      {home}")
    print(f"Provider:  {args.provider}")
    print(f"Requests:  {requests}")
    print(f"Events:    {events}")
    print(f"Job id:    {job_id}")
    print(f"Corr id:   {corr_id}")
    print(f"Text:      {args.text}")
    print("Waiting for provider.job.* events...")

    seen: set[str] = set()
    accepted = False
    final = False
    deadline = time.time() + args.timeout_ms / 1000.0
    final_event: dict | None = None
    while time.time() < deadline:
        rows, offset = read_events_since(events, offset)
        for row in rows:
            evt = unwrap_event(row)
            if not matches(evt, job_id, corr_id):
                continue
            eid = evt.get("id")
            if eid and eid in seen:
                print(f"WARN: duplicate event ignored: {eid}")
                continue
            if eid:
                seen.add(eid)
            typ = evt.get("type")
            payload = evt.get("payload") or {}
            if typ == "provider.job.accepted":
                if accepted:
                    print(f"WARN: duplicate accepted ignored: {payload.get('status')}")
                    continue
                accepted = True
                print(f"ACCEPTED: queue={payload.get('queue_position')} status={payload.get('status')} transport={payload.get('transport')}")
            elif typ == "provider.job.progress":
                if final:
                    print("WARN: late progress after final ignored")
                    continue
                print(f"PROGRESS: {payload.get('percent')}% | {payload.get('step')} | {payload.get('hint')} | chunk={payload.get('chunk')!r}")
            elif typ == "provider.job.done":
                if not accepted:
                    print("WARN: orphan done ignored")
                    continue
                final = True
                final_event = evt
                print("DONE: provider.job.done received")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                print("OK: provider job completed")
                return 0
            elif typ == "provider.job.error" or typ == "bus.error":
                final = True
                final_event = evt
                print(f"ERROR: {payload.get('code')}: {payload.get('message')}")
                print(json.dumps(evt, ensure_ascii=False, indent=2))
                return 2
        time.sleep(0.1)

    print("TIMEOUT: no provider.job.done/error received")
    if final_event:
        print(json.dumps(final_event, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
