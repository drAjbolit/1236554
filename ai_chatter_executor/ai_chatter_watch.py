#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Chatter Watch — Stage 3.11.

Continuously runs the Stage 3.10 run-once cycle:

1. ai_chatter_executor.py --tasks-only
2. ai_chatter_chrome_bridge_worker.py
3. ai_chatter_executor.py --intake-bridge-results

Final delivery to Gajim remains manual:
    .deliver

Stop with Ctrl+C.
"""

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


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


def run_command(command: list[str], cwd: Path) -> int:
    completed = subprocess.run(command, cwd=str(cwd))
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Stage 3.11 watch mode")
    parser.add_argument("--queue", default=default_queue(), help="Path to ai_chatter_tasks.jsonl")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between cycles")
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--timeout-ms", type=int, default=60000)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--idle-log-every", type=int, default=12, help="Print idle message every N idle cycles")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    queue = str(Path(args.queue).expanduser())
    jobs = str(Path(queue).parent / "logs" / "ai_chatter_chrome_jobs.jsonl")

    executor = here / "ai_chatter_executor.py"
    worker = here / "ai_chatter_chrome_bridge_worker.py"

    print("AI Chatter Watch Stage 3.11")
    print(f"Folder:      {here}")
    print(f"Queue:       {queue}")
    print(f"Chrome jobs: {jobs}")
    print(f"CDP:         {args.cdp_url}")
    print(f"Interval:    {args.interval}s")
    print("Stop:        Ctrl+C")
    print("")
    print("Gajim delivery remains manual: .deliver")
    print("")

    idle_cycles = 0
    cycle = 0

    while True:
        cycle += 1
        queue_path = Path(queue)
        before_jobs = count_jsonl(Path(jobs))

        print(f"[AI Chatter Watch] Cycle {cycle}: checking queue...")

        rc1 = run_command(
            [
                sys.executable,
                str(executor),
                "--queue",
                queue,
                "--max-tasks",
                str(args.max_tasks),
                "--tasks-only",
            ],
            here,
        )
        if rc1 != 0:
            print(f"[AI Chatter Watch] tasks-only failed: {rc1}")

        after_jobs = count_jsonl(Path(jobs))
        new_jobs = max(0, after_jobs - before_jobs)

        if new_jobs > 0 or args.once:
            print(f"[AI Chatter Watch] Jobs available: {after_jobs} total, {new_jobs} new in this cycle.")

            rc2 = run_command(
                [
                    sys.executable,
                    str(worker),
                    "--jobs",
                    jobs,
                    "--max-jobs",
                    str(args.max_jobs),
                    "--timeout-ms",
                    str(args.timeout_ms),
                    "--cdp-url",
                    args.cdp_url,
                ],
                here,
            )
            if rc2 != 0:
                print(f"[AI Chatter Watch] worker failed: {rc2}")

            rc3 = run_command(
                [
                    sys.executable,
                    str(executor),
                    "--queue",
                    queue,
                    "--intake-bridge-results",
                ],
                here,
            )
            if rc3 != 0:
                print(f"[AI Chatter Watch] intake failed: {rc3}")

            print("[AI Chatter Watch] Cycle complete. In Gajim use: .deliver")
            idle_cycles = 0
        else:
            idle_cycles += 1
            if args.idle_log_every > 0 and idle_cycles % args.idle_log_every == 0:
                print(f"[AI Chatter Watch] idle; no new jobs. Queue file exists: {queue_path.exists()}")

        if args.once:
            print("[AI Chatter Watch] --once complete.")
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("")
        print("[AI Chatter Watch] stopped by user.")
        raise SystemExit(0)
