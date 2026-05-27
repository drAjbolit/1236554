AI Chatter Executor 4.0.17 - Branch 4 version alignment

This package aligns executor CLI/version labels with Browser Extension 4.0.17.

Current tested stack:
- Local Bus ping/pong OK
- Mock provider lifecycle OK
- Event dedupe/state machine OK
- Mock edge-case tests OK
- Qwen provider worker via extension native transport OK with Browser Extension 4.0.17

Commands:
  python ai_chatter_bus_ping.py --home G:\AI_chatter
  python ai_chatter_bus_job_mock.py --home G:\AI_chatter --provider mock --text ping
  python ai_chatter_bus_job_test.py --home G:\AI_chatter --scenario success
  python ai_chatter_bus_job_provider.py --home G:\AI_chatter --provider qwen --text "ответь одним словом: qwen410"
