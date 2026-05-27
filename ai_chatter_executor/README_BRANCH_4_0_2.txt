AI Chatter Executor 4.0.17

Branch 4 Local Bus mock provider strict dedupe/state machine.
Tools:
- ai_chatter_bus_ping.py
- ai_chatter_bus_job_mock.py

Mock job sends provider.job.run and waits for accepted/progress/done/error through Native Host and Chrome Extension.
Completed finals are appended to logs/ai_chatter_completed_jobs.jsonl for simple idempotency checks.
Archive contains category folder ai_chatter_executor/.
