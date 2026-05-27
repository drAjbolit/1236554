#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archive AI Chatter queue/result files and start clean."""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path


FILES = [
    "ai_chatter_tasks.jsonl",
    "ai_chatter_results.jsonl",
    "ai_chatter_executor_state.json",
    "ai_chatter_delivered_results.json",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive AI Chatter queue/result files")
    parser.add_argument(
        "--plugin-dir",
        default=r"C:\Users\789\AppData\Roaming\Gajim\Plugins\aichatter_bridge",
        help="Path to Gajim aichatter_bridge plugin directory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be moved")
    args = parser.parse_args()

    plugin_dir = Path(args.plugin_dir)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = plugin_dir / "archive" / stamp

    print(f"Plugin dir: {plugin_dir}")
    print(f"Archive dir: {archive_dir}")

    moved = 0
    for name in FILES:
        src = plugin_dir / name
        if not src.exists():
            print(f"skip: {name} not found")
            continue

        dst = archive_dir / name
        print(f"move: {src} -> {dst}")
        if not args.dry_run:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        moved += 1

    print(f"Moved files: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
