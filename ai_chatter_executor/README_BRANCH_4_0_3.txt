AI Chatter Executor 4.0.17 - strict dedupe/state machine MVP

Changes:
- Keeps Branch 4 protocol: ai_chatter.local_bus.v4.
- Mock provider CLI now ignores duplicate event.id values during one run.
- Mock provider CLI ignores duplicate accepted/already_processing events after first accepted.
- Progress after final remains ignored.
- Completed jobs are still cached in logs/ai_chatter_completed_jobs.jsonl.

Test:
  "C:\Python314\python.exe" "G:\AI_chatter\ai_chatter_executor\ai_chatter_bus_job_mock.py" --home "G:\AI_chatter" --provider mock --text "ping"

Duplicate job-id test:
  "C:\Python314\python.exe" "G:\AI_chatter\ai_chatter_executor\ai_chatter_bus_job_mock.py" --home "G:\AI_chatter" --provider mock --text "ping" --job-id "job_mock_fixed_001"
