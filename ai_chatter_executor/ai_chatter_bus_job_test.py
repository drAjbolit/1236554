#!/usr/bin/env python3
"""AI Chatter Executor 4.0.17 mock lifecycle edge-case tests.

Scenarios run through Branch 4 Local Bus + Native Messaging + Extension mock provider.
No legacy CDP/DOM transport is used.
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
            if not raw.endswith(b"\n"):
                # Ignore partial line; keep offset before it for a future read.
                break
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


def matches(evt: dict, job_id: str, corr_id: str) -> bool:
    return evt.get("job_id") == job_id or evt.get("correlation_id") == corr_id


def make_run_event(job_id: str, corr_id: str, text: str, timeout_ms: int, scenario: str, provider: str = "mock") -> dict:
    metadata: dict = {
        "mock_delay_ms": 800,
        "mock_chunks": 2,
        "mock_reply": f"Pong received. Mock sequence active. Text: {text}",
        "scenario": scenario,
    }
    if scenario == "timeout":
        metadata["simulate_timeout"] = True
    elif scenario == "duplicate_done":
        metadata["simulate_duplicate_done"] = True
    elif scenario == "late_progress":
        metadata["simulate_late_progress"] = True
    elif scenario == "orphan_done":
        metadata["simulate_orphan_done"] = True
    elif scenario == "malformed":
        provider = "not_registered_mock_provider"
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
            "agent_id": "test_agent_v1",
            "text": text,
            "mode": "sync",
            "session_kind": "chat",
            "timeout_ms": timeout_ms,
            "metadata": metadata,
        },
    }


class StateMachine:
    def __init__(self) -> None:
        self.state = "new"
        self.accepted = False
        self.final = False
        self.duplicate_done_count = 0
        self.late_progress_count = 0
        self.orphan_done_count = 0
        self.duplicate_accepted_count = 0
        self.progress_count = 0
        self.done_event: dict | None = None
        self.error_event: dict | None = None

    def handle(self, evt: dict) -> None:
        typ = evt.get("type")
        payload = evt.get("payload") or {}
        if typ == "provider.job.accepted":
            status = payload.get("status", "accepted")
            if self.final:
                print(f"WARN: accepted after final ignored: {status}")
                self.duplicate_accepted_count += 1
                return
            if self.accepted:
                print(f"WARN: duplicate accepted ignored: {status}")
                self.duplicate_accepted_count += 1
                return
            self.accepted = True
            self.state = "accepted"
            print(f"ACCEPTED: queue={payload.get('queue_position')} status={status}")
            return
        if typ == "provider.job.progress":
            if self.final:
                self.late_progress_count += 1
                print("WARN: late progress after final ignored")
                return
            if not self.accepted:
                print("WARN: progress before accepted ignored")
                return
            self.state = "in_progress"
            self.progress_count += 1
            print(f"PROGRESS: {payload.get('percent')}% | {payload.get('step')} | {payload.get('hint')} | chunk={payload.get('chunk')!r}")
            return
        if typ == "provider.job.done":
            if not self.accepted:
                self.orphan_done_count += 1
                print("WARN: orphan done ignored: done without accepted")
                return
            if self.final:
                self.duplicate_done_count += 1
                print("WARN: duplicate done ignored")
                return
            self.final = True
            self.state = "done"
            self.done_event = evt
            print("DONE: provider.job.done received")
            return
        if typ == "provider.job.error" or typ == "bus.error":
            if self.final:
                print("WARN: error after final ignored")
                return
            if not self.accepted and typ == "provider.job.error":
                # Validation errors can arrive before accepted.
                pass
            self.final = True
            self.state = "error"
            self.error_event = evt
            code = payload.get("code") or typ
            print(f"ERROR: {code}: {payload.get('message')}")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Branch 4.0.17 mock lifecycle edge-case tests")
    parser.add_argument("--home", default=None)
    parser.add_argument("--scenario", default="success", choices=["success", "duplicate_request", "duplicate_done", "late_progress", "orphan_done", "timeout", "malformed"])
    parser.add_argument("--text", default="ping404")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--job-id", default=None)
    args = parser.parse_args()

    home = find_home(args.home)
    configs = home / "configs"
    logs = home / "logs"
    requests = configs / "ai_chatter_bus_requests.jsonl"
    events = logs / "ai_chatter_bus_events.jsonl"
    configs.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    job_id = args.job_id or f"job_test_{args.scenario}_{uuid4()}"
    corr_id = f"corr_test_{uuid4()}"
    event = make_run_event(job_id, corr_id, args.text, args.timeout_ms, args.scenario)
    offset = events.stat().st_size if events.exists() else 0

    append_jsonl(requests, {"event": event, "ts": now_iso()})
    if args.scenario == "duplicate_request":
        append_jsonl(requests, {"event": event, "ts": now_iso(), "duplicate": True})

    print(f"AI Chatter Executor {VERSION} Mock Edge Test")
    print(f"Home:      {home}")
    print(f"Scenario:  {args.scenario}")
    print(f"Requests:  {requests}")
    print(f"Events:    {events}")
    print(f"Job id:    {job_id}")
    print(f"Corr id:   {corr_id}")
    print("Waiting for provider.job.* events...")

    sm = StateMachine()
    seen_event_ids: set[str] = set()
    deadline = time.time() + args.timeout_ms / 1000.0
    grace_after_final_until: float | None = None

    while time.time() < deadline or (grace_after_final_until is not None and time.time() < grace_after_final_until):
        rows, offset = read_events_since(events, offset)
        for row in rows:
            evt = unwrap_event(row)
            if not matches(evt, job_id, corr_id):
                continue
            eid = evt.get("id")
            if eid and eid in seen_event_ids:
                print(f"WARN: duplicate event ignored: {eid}")
                continue
            if eid:
                seen_event_ids.add(eid)
            was_final = sm.final
            sm.handle(evt)
            if sm.final and not was_final:
                if args.scenario in ("duplicate_done", "late_progress"):
                    grace_after_final_until = time.time() + 1.2
                else:
                    grace_after_final_until = time.time() + 0.1
        if sm.final and grace_after_final_until is None:
            break
        time.sleep(0.1)

    ok = False
    reason = ""
    if args.scenario in ("success", "duplicate_request"):
        ok = sm.state == "done" and sm.done_event is not None and sm.duplicate_accepted_count == 0
        reason = "expected clean done without duplicate accepted"
    elif args.scenario == "duplicate_done":
        ok = sm.state == "done" and sm.duplicate_done_count >= 1
        reason = "expected duplicate done to be ignored"
    elif args.scenario == "late_progress":
        ok = sm.state == "done" and sm.late_progress_count >= 1
        reason = "expected late progress after final to be ignored"
    elif args.scenario == "orphan_done":
        ok = sm.orphan_done_count >= 1 and sm.done_event is None
        reason = "expected orphan done without accepted to be ignored"
    elif args.scenario == "timeout":
        ok = sm.done_event is None and sm.error_event is None
        reason = "expected no done/error before timeout from mock provider"
    elif args.scenario == "malformed":
        ok = sm.state == "error" and sm.error_event is not None
        reason = "expected validation error"

    summary = {
        "scenario": args.scenario,
        "ok": ok,
        "reason": reason,
        "state": sm.state,
        "progress_count": sm.progress_count,
        "duplicate_accepted_count": sm.duplicate_accepted_count,
        "duplicate_done_count": sm.duplicate_done_count,
        "late_progress_count": sm.late_progress_count,
        "orphan_done_count": sm.orphan_done_count,
        "final_type": (sm.done_event or sm.error_event or {}).get("type"),
    }
    print("SUMMARY:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if ok:
        print("OK: edge-case scenario passed")
        return 0
    print("FAIL: edge-case scenario did not match expectation")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
