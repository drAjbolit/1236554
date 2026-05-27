#!/usr/bin/env python3
"""AI Chatter Native Host 4.0.17.

Chrome Native Messaging host + file-based Local Bus MVP.
The host bridges:
- Chrome extension native messages
- local JSONL bus requests from executor/tools
"""
from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

VERSION = "4.0.17"
BRANCH = "4"
PROTOCOL = "ai_chatter.local_bus.v4"
HOST_NAME = "ai_chatter.native_host"

_write_lock = threading.Lock()
_outgoing_to_extension: "queue.Queue[dict]" = queue.Queue()
_stop = threading.Event()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_home() -> Path:
    env = os.environ.get("AI_CHATTER_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # Expected: <home>/ai_chatter_native_host/ai_chatter_native_host.py
    if here.parent.name.lower() == "ai_chatter_native_host":
        return here.parent.parent
    for candidate in (Path("G:/AI_chatter"), Path("C:/AI_chatter")):
        if candidate.exists():
            return candidate
    return here.parent.parent


HOME = find_home()
CONFIGS_DIR = HOME / "configs"
LOGS_DIR = HOME / "logs"
BUS_REQUESTS = CONFIGS_DIR / "ai_chatter_bus_requests.jsonl"
BUS_EVENTS = LOGS_DIR / "ai_chatter_bus_events.jsonl"
STATE_FILE = LOGS_DIR / "ai_chatter_native_host_state.json"
HOST_LOG = LOGS_DIR / "ai_chatter_native_host.log"


def ensure_dirs() -> None:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    ensure_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def log(message: str, **data: Any) -> None:
    ensure_dirs()
    row = {"ts": now_iso(), "message": message, **data}
    with HOST_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_event(type_: str, payload: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    return {
        "id": extra.get("id") or f"native-{int(time.time()*1000)}-{os.getpid()}",
        "type": type_,
        "source": extra.get("source", "native_host"),
        "target": extra.get("target", "extension"),
        "timestamp": now_iso(),
        "protocol": PROTOCOL,
        "branch": BRANCH,
        "version": VERSION,
        "payload": payload or {},
        **{k: v for k, v in extra.items() if k not in {"id", "source", "target"}},
    }


def read_native_message() -> Optional[Dict[str, Any]]:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None
    if len(raw_len) < 4:
        return None
    message_len = struct.unpack("<I", raw_len)[0]
    if message_len <= 0 or message_len > 50_000_000:
        raise ValueError(f"Invalid native message length: {message_len}")
    data = sys.stdin.buffer.read(message_len)
    if len(data) != message_len:
        raise EOFError("Native message body truncated")
    return json.loads(data.decode("utf-8"))


def write_native_message(message: Dict[str, Any]) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _write_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def load_state() -> Dict[str, Any]:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log("state_load_failed", traceback=traceback.format_exc())
    return {"requestOffset": 0, "processedEventIds": []}


def save_state(state: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)




def _processed_set(state: Dict[str, Any]) -> set[str]:
    ids = state.get("processedEventIds")
    if not isinstance(ids, list):
        ids = []
    return set(str(x) for x in ids if x)

def mark_processed_event(state: Dict[str, Any], event_id: str) -> None:
    if not event_id:
        return
    ids = state.get("processedEventIds")
    if not isinstance(ids, list):
        ids = []
    if event_id not in ids:
        ids.append(event_id)
    # Keep only recent IDs in JSON state. This is MVP JSONL/state-file dedupe.
    state["processedEventIds"] = ids[-10000:]

def handle_extension_message(message: Dict[str, Any]) -> None:
    append_jsonl(BUS_EVENTS, {"direction": "extension_to_bus", "event": message, "ts": now_iso()})
    if message.get("type") == "extension.hello":
        log("extension_hello", event=message)
    elif message.get("type") == "bus.pong":
        log("bus_pong", event=message)
    elif message.get("type") == "bus.error":
        log("bus_error_from_extension", event=message)


def reader_loop() -> None:
    while not _stop.is_set():
        try:
            message = read_native_message()
            if message is None:
                _stop.set()
                break
            handle_extension_message(message)
        except Exception:
            log("native_reader_error", traceback=traceback.format_exc())
            _stop.set()
            break


def writer_loop() -> None:
    while not _stop.is_set():
        try:
            message = _outgoing_to_extension.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            write_native_message(message)
            append_jsonl(BUS_EVENTS, {"direction": "bus_to_extension", "event": message, "ts": now_iso()})
        except Exception:
            log("native_writer_error", traceback=traceback.format_exc(), event=message)
            _stop.set()
            break


def iter_new_requests(state: Dict[str, Any]) -> list[Dict[str, Any]]:
    ensure_dirs()
    if not BUS_REQUESTS.exists():
        return []
    offset = int(state.get("requestOffset") or 0)
    size = BUS_REQUESTS.stat().st_size
    if offset > size:
        offset = 0
    out = []
    with BUS_REQUESTS.open("rb") as f:
        f.seek(offset)
        for raw in f:
            try:
                if raw.strip():
                    out.append(json.loads(raw.decode("utf-8")))
            except Exception:
                log("bad_bus_request_line", raw=raw.decode("utf-8", "replace"))
        state["requestOffset"] = f.tell()
    return out


def poll_bus_loop() -> None:
    state = load_state()
    while not _stop.is_set():
        try:
            for request in iter_new_requests(state):
                if not isinstance(request, dict):
                    continue
                event = request.get("event") if isinstance(request.get("event"), dict) else request
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("id") or "")
                if event_id and event_id in _processed_set(state):
                    log("duplicate_bus_request_skipped", event_id=event_id, job_id=event.get("job_id"))
                    continue
                event.setdefault("source", "local_bus")
                event.setdefault("target", "chrome_extension")
                event.setdefault("timestamp", now_iso())
                event.setdefault("protocol", PROTOCOL)
                event.setdefault("branch", BRANCH)
                _outgoing_to_extension.put(event)
                if event_id:
                    mark_processed_event(state, event_id)
                    save_state(state)
            save_state(state)
        except Exception:
            log("bus_poll_error", traceback=traceback.format_exc())
        time.sleep(0.5)


def main() -> int:
    ensure_dirs()
    log("native_host_start", home=str(HOME), version=VERSION, branch=BRANCH)
    append_jsonl(BUS_EVENTS, make_event("native_host.started", {
        "ok": True,
        "home": str(HOME),
        "configs": str(CONFIGS_DIR),
        "logs": str(LOGS_DIR),
    }, target="local_bus"))

    _outgoing_to_extension.put(make_event("bus.ping", {
        "reason": "native_host_startup",
        "host": HOST_NAME,
    }))

    threads = [
        threading.Thread(target=reader_loop, name="native-reader", daemon=True),
        threading.Thread(target=writer_loop, name="native-writer", daemon=True),
        threading.Thread(target=poll_bus_loop, name="bus-poller", daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        while not _stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _stop.set()
    log("native_host_stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
