#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

HOST_NAME = "ai_chatter.native_host"
EXTENSION_ID = "kgdgnnlkagfboekpanbjbjiijonhbmhj"

def main() -> int:
    host_dir = Path(__file__).resolve().parent
    host_bat = host_dir / "ai_chatter_native_host.bat"
    host_py = host_dir / "ai_chatter_native_host.py"
    if not host_bat.exists() or not host_py.exists():
        print(f"ERROR: native host files not found in {host_dir}")
        return 1
    manifest = host_dir / f"{HOST_NAME}.json"
    data = {
        "name": HOST_NAME,
        "description": "AI Chatter Branch 4 Native Messaging Host",
        "path": str(host_bat),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    key = rf"HKCU\Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
    cmd = ["reg", "add", key, "/ve", "/t", "REG_SZ", "/d", str(manifest), "/f"]
    print("Writing manifest:", manifest)
    print("Registering:", key)
    rc = subprocess.run(cmd, shell=False).returncode
    if rc != 0:
        print("ERROR: reg add failed", rc)
        return rc
    print("OK: native host registered")
    print("Host name:", HOST_NAME)
    print("Extension ID:", EXTENSION_ID)
    print("Manifest path:", manifest)
    print("Host path:", host_bat)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
