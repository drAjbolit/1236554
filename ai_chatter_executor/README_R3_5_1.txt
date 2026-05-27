R3.5.1
- If extension confirms inputSet+sendClicked, executor no longer sends the same prompt again through Playwright fallback.
- It attaches read-only and collects the answer from DOM.
- Prevents duplicated Qwen/DeepSeek prompts and 1/2 answer variants after extension answer collection timeout.
