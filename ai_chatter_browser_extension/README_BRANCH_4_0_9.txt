AI Chatter Browser Extension 4.0.9
Branch 4 Qwen baseline snapshot diff selector.

Changes:
- Before sending a provider prompt, content script captures a baseline snapshot of visible chat text blocks.
- After completion, it compares the after-snapshot against the baseline and selects only newly appeared assistant-like content.
- This is intended to avoid selecting persistent UI labels, reasoning mode labels, footers, disclaimers, sidebars, or old answers.
- Diagnostics include snapshotDiff candidates for debugging.

This is a universal selector pattern planned for all providers, starting with Qwen.
