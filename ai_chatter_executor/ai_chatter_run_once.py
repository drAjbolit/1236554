#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Chatter Run Once — 3.5.10 assistant-only answer selection."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def default_queue() -> str:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return str(Path(appdata) / "Gajim" / "Plugins" / "aichatter_bridge" / "ai_chatter_tasks.jsonl")
    return "ai_chatter_tasks.jsonl"


def lock_path_for_queue(queue: str, custom_lock: str = "") -> Path:
    if custom_lock:
        return Path(custom_lock).expanduser()
    return Path(queue).expanduser().with_name("ai_chatter_run_once.lock")


def relative_to_queue(queue: str, name: str) -> str:
    raw = str(name or "").strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return str(candidate)
    q = Path(queue).expanduser()
    if len(candidate.parts) > 1:
        return str(q.parent / candidate)
    return str(q.with_name(candidate.name))


def default_config_for_queue(queue: str) -> str:
    q = Path(queue).expanduser()
    return str(q.parent / "configs" / "ai_chatter_executor_config.json")


def configs_dir_for_queue(queue: str, config: str = "") -> str:
    if config:
        return str(Path(config).expanduser().parent)
    return str(Path(default_config_for_queue(queue)).parent)

def acquire_lock(lock_path: Path, stale_seconds: int) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except Exception:
            age = 0

        if stale_seconds > 0 and age > stale_seconds:
            try:
                lock_path.unlink()
            except Exception:
                return False
        else:
            return False

    payload = (
        "{"
        f"'pid': {os.getpid()}, "
        f"'createdAt': '{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}'"
        "}"
    )
    try:
        lock_path.write_text(payload, encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def run_step(name: str, command: list[str], *, cwd: Path, keep_going: bool = False) -> int:
    print("")
    print("=" * 78)
    print(f"[AI Chatter] {name}")
    print("=" * 78)
    print(" ".join(f'"{x}"' if " " in x else x for x in command))
    print("")

    completed = subprocess.run(command, cwd=str(cwd))
    if completed.returncode != 0:
        print("")
        print(f"[AI Chatter] Step failed: {name} | exit code {completed.returncode}")
        if not keep_going:
            raise SystemExit(completed.returncode)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Stage 3.12 lock-safe run-once wrapper")
    parser.add_argument("--queue", default=default_queue(), help="Path to ai_chatter_tasks.jsonl")
    parser.add_argument("--config", default="", help="Path to configs/ai_chatter_executor_config.json")
    parser.add_argument("--max-tasks", type=int, default=1, help="How many Gajim tasks to convert into chrome jobs")
    parser.add_argument("--max-jobs", type=int, default=1, help="How many chrome jobs to process")
    parser.add_argument("--timeout-ms", type=int, default=60000, help="AI answer wait timeout for worker")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="Chrome CDP URL")
    parser.add_argument("--skip-tasks", action="store_true", help="Skip tasks-only step")
    parser.add_argument("--skip-worker", action="store_true", help="Skip Chrome worker step")
    parser.add_argument("--skip-intake", action="store_true", help="Skip intake step")
    parser.add_argument("--keep-going", action="store_true", help="Continue to next steps even if one step fails")
    parser.add_argument("--lock-file", default="", help="Optional lock file path")
    parser.add_argument("--stale-lock-seconds", type=int, default=1800, help="Remove lock if older than this many seconds; 0 disables stale removal")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    queue = str(Path(args.queue).expanduser())
    config = str(Path(args.config).expanduser()) if args.config else default_config_for_queue(queue)
    configs_dir = configs_dir_for_queue(queue, config)
    lock_path = lock_path_for_queue(queue, args.lock_file)

    if not acquire_lock(lock_path, args.stale_lock_seconds):
        print("AI Chatter Run Once 3.5.10 Assistant-only answer selection: another run is already active; exiting.")
        print(f"Lock: {lock_path}")
        return 0

    try:
        executor = here / "ai_chatter_executor.py"
        worker = here / "ai_chatter_chrome_bridge_worker.py"

        if not executor.exists():
            print(f"ERROR: missing {executor}")
            return 2
        if not worker.exists():
            print(f"ERROR: missing {worker}")
            return 2

        print("AI Chatter Run Once 3.5.10 Assistant-only answer selection")
        print(f"Folder:     {here}")
        print(f"Queue:      {queue}")
        print(f"Config:     {config}")
        print(f"Configs:    {configs_dir}")
        print(f"CDP:        {args.cdp_url}")
        print(f"Max tasks:  {args.max_tasks}")
        print(f"Max jobs:   {args.max_jobs}")
        print(f"Timeout:    {args.timeout_ms} ms")
        print(f"Lock:       {lock_path}")

        os.environ["AI_CHATTER_CONFIGS_DIR"] = configs_dir
        os.environ["AI_CHATTER_HOME"] = str(Path(queue).expanduser().parent)

        if not args.skip_tasks:
            run_step(
                "1/3 Convert Gajim tasks to provider jobs",
                [
                    sys.executable,
                    str(executor),
                    "--queue",
                    queue,
                    "--config",
                    config,
                    "--max-tasks",
                    str(args.max_tasks),
                    "--tasks-only",
                ],
                cwd=here,
                keep_going=args.keep_going,
            )

        if not args.skip_worker:
            jobs = relative_to_queue(queue, "logs/ai_chatter_chrome_jobs.jsonl")
            run_step(
                "2/3 Process provider jobs via extension resident transport",
                [
                    sys.executable,
                    str(worker),
                    "--jobs",
                    jobs,
                    "--results",
                    str(Path(queue).expanduser().parent / "logs" / "ai_chatter_chrome_results.jsonl"),
                    "--state",
                    str(Path(queue).expanduser().parent / "logs" / "ai_chatter_chrome_worker_state.json"),
                    "--profiles",
                    str(Path(configs_dir) / "ai_chatter_profiles.json"),
                    "--max-jobs",
                    str(args.max_jobs),
                    "--timeout-ms",
                    str(args.timeout_ms),
                    "--cdp-url",
                    args.cdp_url,
                ],
                cwd=here,
                keep_going=args.keep_going,
            )

        if not args.skip_intake:
            run_step(
                "3/3 Intake Chrome results into Gajim result queue",
                [
                    sys.executable,
                    str(executor),
                    "--queue",
                    queue,
                    "--config",
                    config,
                    "--results",
                    relative_to_queue(queue, "logs/ai_chatter_results.jsonl"),
                    "--intake-bridge-results",
                ],
                cwd=here,
                keep_going=args.keep_going,
            )

        print("")
        print("=" * 78)
        print("[AI Chatter] Run once complete.")
        print("Next in Gajim: .deliver")
        print("Then check:    .results")
        print("=" * 78)
        return 0
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
