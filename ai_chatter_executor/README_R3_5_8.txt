AI Chatter Executor 3.5.8

Managed provider sessions + memory rollover.

Adds configs/ai_chatter_provider_sessions.json and logs/session_memory/.
Before session rollover, executor asks the old provider chat for a compact handoff summary, saves it, opens/resets the provider session, injects the summary into the new chat, then runs the original job.

Default rollover policy in ai_chatter_profiles.json:
- Qwen: maxSessionAgeMinutes=120, maxMessagesPerSession=30
- ChatGPT/DeepSeek: maxSessionAgeMinutes=180, maxMessagesPerSession=40

Rollover is sticky, not every job.
