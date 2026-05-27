AI Chatter Browser Extension 4.0.7

Branch 4.0.7: Qwen assistant answer selector hotfix.

Changes:
- Do not treat arbitrary prompt fragments as prompt echo. This allows valid one-word answers such as qwen406.
- Keep exact addressed-prompt echo rejection, e.g. "Квен, тест 353" -> "тест 353".
- Blacklist Qwen footer/disclaimer texts such as "Содержимое, созданное ИИ, может быть неточным."
- Transport marker updated to extension_native_worker_v407.

Install:
Reload unpacked extension in chrome://extensions and refresh the Qwen tab.
