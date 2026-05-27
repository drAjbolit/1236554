AI Chatter Executor 4.0.0
Branch 4 Local Bus ping MVP.

This is not a provider executor yet. It tests the new communication line:
executor -> local bus jsonl -> native host -> Chrome extension -> native host -> bus events -> executor.

Test:
  python G:\AI_chatter\ai_chatter_executor\ai_chatter_bus_ping.py --home G:\AI_chatter

Expected:
  OK: bus.pong received
