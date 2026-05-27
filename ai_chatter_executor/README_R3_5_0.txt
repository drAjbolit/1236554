AI Chatter Executor 3.5.0 R3.5.0
Browser extension resident transport experimental.

Install:
1. Backup C:\AI_chatter\ai_chatter_executor.
2. Replace it with this ai_chatter_executor folder.
3. Install/reload the matching Chrome extension transport 0.3.5.

Runtime flow:
- run_once converts Gajim jobs into provider jobs as before.
- chrome worker connects to Chrome CDP only to select provider tab and post a message to the already resident extension content script.
- content script inserts prompt, sends, waits for answer, and returns answer + diagnostics.
- old Playwright/CDP observer remains as fallback.

Diagnostics:
- ai_chatter_chrome_results.jsonl -> runtime.kind should be extension_resident when new transport succeeds.
- runtime.diagnostics includes inputFound, inputSet, sendClicked, answerStarted, answerDone, replyCollected.
