AI Chatter Bridge Stage 2.8.8 R3.4.1b circle session path fix + dynamic MUC agent batch hotfix

Hotfix:
- adds canonical circle commands to the early command whitelist: .круг, .круг-статус, .круг-дальше, .круг-ответ, .круг-стоп;
- computes executor --max-tasks/--max-jobs dynamically from live enabled AI agents in the current MUC, with fallback to available registry agents;
- keeps the existing provider pipeline, Chrome/CDP observer rules, registry, broadcast/multi-target syntax and XMPP send layer unchanged.

AI Chatter Bridge Stage 2.8.4 R3.2.1 env sys-import hotfix

Fixes missing sys import in .env/.env-check/settings environment check.

# AI Chatter Bridge

Stage 2.8.1 R3.1 pending-aware autorun and delivery.

R3.1 keeps the working multi-agent XMPP provider pipeline from 2.8.0 and adds pending-aware automatic executor runs plus automatic delivery. The plugin counts unfinished agent provider jobs, runs `ai_chatter_run_once.py` with matching `--max-tasks/--max-jobs` limits, delivers ready provider results from the correct XMPP agent account, and reruns while jobs remain pending.

Commands added/updated:

- `.agent-provider` — provider mode and autorun settings
- `.agent-pending` — pending provider jobs / executor status
- `.autorun-on` / `.autorun-off` — quick autorun toggle

XMPP send layer from 2.7.15 is intentionally unchanged.


Stage 2.8.6 R3.3.1: добавлены единые русские команды: .хелп, .версия, .окружение, .проверка-окружения, .агент-регистр, .агент-задачи, .агент-исходящие и т.д. Английские команды сохранены для совместимости.
