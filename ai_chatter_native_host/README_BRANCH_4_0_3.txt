AI Chatter Native Host 4.0.17 - request event dedupe

Changes:
- Tracks processed request event IDs in logs/ai_chatter_native_host_state.json.
- Duplicate bus request event.id is skipped and logged as duplicate_bus_request_skipped.
- Keeps cursor/offset in native host state.

This is still JSONL/state-file MVP. SQLite is deferred to a later Branch 4 version.
