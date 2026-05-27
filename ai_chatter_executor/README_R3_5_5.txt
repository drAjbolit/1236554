AI Chatter Executor 3.5.5 — provider session reset for Qwen parent_id

Fixes provider-side stale/deleted Qwen chat state. If Qwen returns an error such as
"Invalid input chat parent_id ... is not exist", the worker treats it as
provider_thread_broken, navigates the Qwen tab to https://chat.qwen.ai/, marks the
managed page, and retries the job once.

This is not a Gajim/MUC delivery fix. It prevents old deleted Qwen conversations
from poisoning new jobs after the user manually deletes/renames/opens chats.

Config remains under AI Chatter home/configs/; logs remain under home/logs/.
