#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture AI Chatter click target for a bot.

Usage:
    python ai_chatter_capture_click_target.py --bot deepseek --delay 5

Steps:
1. Open the target AI chat in Chrome.
2. Put the mouse cursor exactly over the Send button.
3. Run the command.
4. Keep the cursor over the button until capture finishes.

The script writes ai_chatter_click_targets.json next to itself.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32 = ctypes.WinDLL("user32", use_last_error=True)


def get_cursor_pos() -> tuple[int, int]:
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(point.x), int(point.y)


def get_foreground_window_rect() -> tuple[int, int, int, int]:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("No foreground window")

    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())

    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def get_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture AHK click target")
    parser.add_argument("--bot", required=True, help="Bot id, e.g. deepseek")
    parser.add_argument("--delay", type=int, default=5, help="Seconds before capture")
    parser.add_argument("--file", default="", help="Target JSON file")
    args = parser.parse_args()

    path = Path(args.file) if args.file else Path(__file__).with_name("ai_chatter_click_targets.json")

    print(f"Bot: {args.bot}")
    print(f"Move mouse to the SEND button. Capture in {args.delay} seconds...")
    for remaining in range(args.delay, 0, -1):
        print(f"{remaining}...")
        time.sleep(1)

    x, y = get_cursor_pos()
    left, top, right, bottom = get_foreground_window_rect()
    width = max(1, right - left)
    height = max(1, bottom - top)

    rel_x = x - left
    rel_y = y - top
    ratio_x = rel_x / width
    ratio_y = rel_y / height

    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data[str(args.bot).lower()] = {
        "mode": "window_ratio",
        "ratioX": ratio_x,
        "ratioY": ratio_y,
        "relX": rel_x,
        "relY": rel_y,
        "screenX": x,
        "screenY": y,
        "window": {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": width,
            "height": height,
            "title": get_window_title(),
        },
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print(f"Saved: {path}")
    print(f"{args.bot}: ratio=({ratio_x:.6f}, {ratio_y:.6f}), rel=({rel_x}, {rel_y}), screen=({x}, {y})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
