AI Chatter Executor 4.0.17 - mock lifecycle edge-case tests

Adds ai_chatter_bus_job_test.py with scenarios:
- success
- duplicate_request
- duplicate_done
- late_progress
- orphan_done
- timeout
- malformed

This version keeps Branch 4 Local Bus and mock provider only. No real provider worker is enabled yet.
