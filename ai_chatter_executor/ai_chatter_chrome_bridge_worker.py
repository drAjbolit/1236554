#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Chatter Chrome Bridge Worker — 3.5.10 Assistant-only answer selection.

Reads ai_chatter_chrome_jobs.jsonl, connects to the already running Chrome CDP
at http://127.0.0.1:9222, controls an open AI chat tab with Playwright, and
writes ai_chatter_chrome_results.jsonl.

Main strategy:
- Playwright connect_over_cdp
- find page by botId / URL
- insert text into input
- click Send via selector/role/DOM scoring
- wait for answer text to stabilize

Fallbacks:
- Playwright mouse click by DOM-computed button center
- Enter key
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(item, ensure_ascii=False) + "\n")


def state_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    ids = data.get("processedJobIds", []) if isinstance(data, dict) else []
    return {str(item) for item in ids} if isinstance(ids, list) else set()


def write_state(path: Path, ids: set[str]) -> None:
    data = {"processedJobIds": sorted(ids), "updatedAt": now()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def bot_matches_url(bot_id: str, url: str) -> bool:
    value = (url or "").lower()
    bot = (bot_id or "").lower()
    if bot == "chatgpt":
        return "chatgpt.com" in value
    if bot == "qwen":
        return "qwen" in value
    if bot == "deepseek":
        return "deepseek" in value
    return bool(bot and bot in value)



def profiles_path(explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configs_dir = os.environ.get("AI_CHATTER_CONFIGS_DIR", "").strip()
    if configs_dir:
        candidate = Path(configs_dir) / "ai_chatter_profiles.json"
        if candidate.exists():
            return candidate
    return Path(__file__).with_name("ai_chatter_profiles.json")


def load_profiles(explicit: str = "") -> dict[str, Any]:
    path = profiles_path(explicit)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_profile_for_job(job: dict[str, Any]) -> dict[str, Any]:
    profiles = load_profiles()
    bot_id = str(job.get("botId", "")).lower()
    profile_key = str(job.get("profileKey", ""))
    if profile_key and isinstance(profiles.get(profile_key), dict):
        return profiles[profile_key]
    for key, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("chatId", "")).lower() == bot_id:
            return profile
        if bot_id and str(key).lower().startswith(bot_id + "::"):
            return profile
    return {}


def split_selector_list(value: Any) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def unique_list(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def all_pages(browser) -> list[Any]:
    pages = []
    for context in browser.contexts:
        pages.extend(context.pages)
    return pages


def find_page(browser, job: dict[str, Any]):
    bot_id = str(job.get("botId", "")).lower()
    pages = all_pages(browser)
    scored = []
    for page in pages:
        try:
            url = page.url
            title = page.title()
        except Exception:
            continue
        score = 0
        if bot_matches_url(bot_id, url):
            score += 100
        if bot_id and bot_id in (title or "").lower():
            score += 20
        if url.startswith("chrome://") or url.startswith("devtools://"):
            score -= 100
        if score > 0:
            scored.append((score, page, url, title))
    if not scored:
        known = [getattr(p, "url", "") for p in pages]
        raise RuntimeError(f"No open page found for botId={bot_id}. Open the AI chat tab. Known pages: {known}")
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def provider_session_cfg(profile: dict[str, Any] | None, job: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = profile or {}
    session = profile.get("session") if isinstance(profile.get("session"), dict) else {}
    bot_id = str((job or {}).get("botId", profile.get("chatId", ""))).lower()
    origin = str(profile.get("origin", "")).strip()
    if not origin:
        if bot_id == "qwen":
            origin = "https://chat.qwen.ai/"
        elif bot_id == "chatgpt":
            origin = "https://chatgpt.com/"
        elif bot_id == "deepseek":
            origin = "https://chat.deepseek.com/"
    return {
        "managed": bool(session.get("managed", True)),
        "startUrl": str(session.get("startUrl") or origin or "").strip(),
        "resetOnProviderThreadBroken": bool(session.get("resetOnProviderThreadBroken", True)),
        "forceStartUrlEveryJob": bool(session.get("forceStartUrlEveryJob", False)),
        "markSessionStorage": bool(session.get("markSessionStorage", True)),
        "sessionPolicy": str(session.get("sessionPolicy", "sticky_with_reset")),
        "memoryHandoff": bool(session.get("memoryHandoff", False)),
        "handoffBeforeRollover": bool(session.get("handoffBeforeRollover", True)),
        "injectSummaryIntoNewChat": bool(session.get("injectSummaryIntoNewChat", True)),
        "closeOldChatAfterSuccessfulHandoff": bool(session.get("closeOldChatAfterSuccessfulHandoff", False)),
        "maxSessionAgeMinutes": int(session.get("maxSessionAgeMinutes", 0) or 0),
        "maxMessagesPerSession": int(session.get("maxMessagesPerSession", 0) or 0),
        "directChatTitle": str(session.get("directChatTitle", "AI Chatter Direct")),
        "circleChatTitle": str(session.get("circleChatTitle", "AI Chatter Circle")),
    }


def configs_dir_from_env() -> Path:
    value = os.environ.get("AI_CHATTER_CONFIGS_DIR", "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parent


def logs_dir_from_configs(configs_dir: Path) -> Path:
    home = configs_dir.parent if configs_dir.name.lower() == "configs" else configs_dir.parent
    return home / "logs"


def provider_sessions_path() -> Path:
    return configs_dir_from_env() / "ai_chatter_provider_sessions.json"


def read_provider_sessions() -> dict[str, Any]:
    path = provider_sessions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_provider_sessions(data: dict[str, Any]) -> None:
    path = provider_sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_dt(value: Any) -> datetime | None:
    try:
        text = str(value or "")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def session_key_for_job(job: dict[str, Any]) -> str:
    bot_id = str(job.get("botId") or job.get("provider") or "unknown").lower()
    text = str(job.get("message", {}).get("text", ""))
    kind = "circle" if "[circle:" in text else "direct"
    return f"{bot_id}:{kind}"


def needs_session_rollover(job: dict[str, Any], profile: dict[str, Any], cfg: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    if not cfg.get("memoryHandoff") or cfg.get("sessionPolicy") not in {"sticky_with_rollover", "managed_sticky_with_rollover"}:
        return False, "policy_disabled"
    key = session_key_for_job(job)
    item = state.get(key) if isinstance(state.get(key), dict) else {}
    if not item:
        return False, "new_session"
    max_messages = int(cfg.get("maxMessagesPerSession") or 0)
    if max_messages and int(item.get("messageCount") or 0) >= max_messages:
        return True, "max_messages"
    max_age = int(cfg.get("maxSessionAgeMinutes") or 0)
    created = parse_dt(item.get("createdAt"))
    if max_age and created and datetime.now(timezone.utc) - created >= timedelta(minutes=max_age):
        return True, "max_age"
    if item.get("forceRollover"):
        return True, "force_rollover"
    return False, "healthy"


def save_session_memory(bot_id: str, kind: str, summary: str, reason: str) -> Path:
    logs_dir = logs_dir_from_configs(configs_dir_from_env())
    memory_dir = logs_dir / "session_memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_bot = re.sub(r"[^a-zA-Z0-9_-]+", "_", bot_id or "provider")
    safe_kind = re.sub(r"[^a-zA-Z0-9_-]+", "_", kind or "direct")
    path = memory_dir / f"{safe_bot}-{safe_kind}-{stamp}-{reason}.txt"
    path.write_text(summary, encoding="utf-8")
    return path


def build_handoff_prompt(bot_id: str, kind: str) -> str:
    return (
        "Сделай краткую техническую выжимку текущей сессии для переноса в новый чат AI Chatter.\n\n"
        "Нужно сохранить:\n"
        "- кто ты и какая твоя роль;\n"
        "- текущую задачу/проект;\n"
        "- важные решения;\n"
        "- известные ошибки и ограничения;\n"
        "- что уже проверено;\n"
        "- что нужно делать дальше;\n"
        "- стиль ответа и язык.\n\n"
        "Не пересказывай весь диалог. Дай компактный переносимый контекст. "
        "Ответь только выжимкой, без вступления."
    )


def build_memory_bootstrap_prompt(summary: str, original_text: str) -> str:
    return (
        "Ты продолжаешь работу как агент AI Chatter.\n\n"
        "Ниже выжимка предыдущей сессии. Используй её как рабочий контекст, "
        "но не упоминай перенос без необходимости.\n\n"
        "--- ВЫЖИМКА ПРЕДЫДУЩЕЙ СЕССИИ ---\n"
        f"{summary.strip()}\n"
        "--- КОНЕЦ ВЫЖИМКИ ---\n\n"
        "Подтверди готовность одной короткой фразой. Следующее сообщение будет текущей задачей."
    )


def clone_job_with_text(job: dict[str, Any], text: str, suffix: str) -> dict[str, Any]:
    cloned = json.loads(json.dumps(job, ensure_ascii=False))
    cloned["id"] = f"{job.get('id', 'job')}-{suffix}"
    cloned.setdefault("message", {})["text"] = text
    return cloned


def maybe_rollover_session(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    profile = load_profile_for_job(job)
    cfg = provider_session_cfg(profile, job)
    sessions = read_provider_sessions()
    key = session_key_for_job(job)
    bot_id = str(job.get("botId") or job.get("provider") or "provider").lower()
    kind = key.split(":", 1)[1] if ":" in key else "direct"
    now_s = now()
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else None
    if item is None:
        sessions[key] = {
            "provider": bot_id,
            "kind": kind,
            "createdAt": now_s,
            "lastHealthyAt": now_s,
            "messageCount": 0,
            "lastRolloverReason": "initial",
        }
        write_provider_sessions(sessions)
        return {"rolledOver": False, "reason": "initial_session", "sessionKey": key}

    should, reason = needs_session_rollover(job, profile, cfg, sessions)
    if not should:
        return {"rolledOver": False, "reason": reason, "sessionKey": key}

    handoff = {"rolledOver": False, "reason": reason, "sessionKey": key}
    summary = ""
    if cfg.get("handoffBeforeRollover", True):
        summary_job = clone_job_with_text(job, build_handoff_prompt(bot_id, kind), "handoff-summary")
        summary_result = run_job_with_playwright(cdp_url, summary_job, min(max(timeout_ms, 30000), 120000))
        handoff["summaryResult"] = {"ok": bool(summary_result.get("ok")), "error": summary_result.get("error", ""), "runtime": summary_result.get("runtime", {})}
        if summary_result.get("ok"):
            summary = str(summary_result.get("answer", "")).strip()
            path = save_session_memory(bot_id, kind, summary, reason)
            handoff["memoryPath"] = str(path)
        else:
            summary = "Выжимку старого чата получить не удалось; продолжай с минимальным контекстом текущей задачи."

    reset = reset_provider_session(cdp_url, job, timeout_ms)
    handoff["reset"] = reset

    if cfg.get("injectSummaryIntoNewChat", True) and summary.strip():
        bootstrap_job = clone_job_with_text(job, build_memory_bootstrap_prompt(summary, str(job.get("message", {}).get("text", ""))), "handoff-bootstrap")
        bootstrap_result = run_job_with_playwright(cdp_url, bootstrap_job, min(max(timeout_ms, 30000), 120000))
        handoff["bootstrapResult"] = {"ok": bool(bootstrap_result.get("ok")), "error": bootstrap_result.get("error", ""), "answerPreview": str(bootstrap_result.get("answer", ""))[:300]}

    sessions = read_provider_sessions()
    sessions[key] = {
        "provider": bot_id,
        "kind": kind,
        "createdAt": now(),
        "lastHealthyAt": now(),
        "messageCount": 0,
        "lastRolloverReason": reason,
        "lastMemoryPath": handoff.get("memoryPath", ""),
    }
    write_provider_sessions(sessions)
    handoff["rolledOver"] = True
    return handoff


def update_provider_session_after_job(job: dict[str, Any], ok: bool, runtime: dict[str, Any] | None = None) -> None:
    sessions = read_provider_sessions()
    key = session_key_for_job(job)
    bot_id = str(job.get("botId") or job.get("provider") or "provider").lower()
    kind = key.split(":", 1)[1] if ":" in key else "direct"
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else {"provider": bot_id, "kind": kind, "createdAt": now(), "messageCount": 0}
    item["messageCount"] = int(item.get("messageCount") or 0) + 1
    item["lastJobAt"] = now()
    if ok:
        item["lastHealthyAt"] = now()
    item["lastRuntimeKind"] = (runtime or {}).get("kind", "") if isinstance(runtime, dict) else ""
    if isinstance(runtime, dict):
        active_url = str(runtime.get("url") or "").strip()
        active_title = str(runtime.get("title") or "").strip()
        if active_url and not active_url.startswith(("chrome://", "devtools://")):
            item["activeUrl"] = active_url
        if active_title:
            item["activeTitle"] = active_title
    sessions[key] = item
    write_provider_sessions(sessions)


def activate_sticky_provider_session(page, job: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Navigate to the saved active chat URL for a healthy sticky session.

    R3.5.10: session counters alone are not enough. For providers such as Qwen,
    using the provider home page creates a new chat every job. When we have a
    known activeUrl from the previous successful job, return to that exact URL
    before submitting the next job.
    """
    cfg = provider_session_cfg(profile, job)
    if not cfg.get("managed", True):
        return {"used": False, "reason": "unmanaged"}
    key = session_key_for_job(job)
    sessions = read_provider_sessions()
    item = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    active_url = str(item.get("activeUrl") or "").strip()
    if not active_url:
        return {"used": False, "reason": "no_active_url"}
    if active_url.startswith(("chrome://", "devtools://")):
        return {"used": False, "reason": "unsafe_active_url"}
    start_url = str(cfg.get("startUrl") or "").strip()
    if start_url and not active_url.lower().startswith(start_url.split("/", 3)[0].lower()):
        # Light sanity guard; do not navigate to an unrelated site saved by mistake.
        pass
    try:
        current = str(getattr(page, "url", "") or "")
        if current != active_url:
            page.goto(active_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        mark_managed_page(page, job, profile)
        return {"used": True, "reason": "active_url", "activeUrl": active_url, "activeTitle": str(item.get("activeTitle") or "")}
    except Exception as error:
        return {"used": False, "reason": "active_url_failed", "activeUrl": active_url, "error": str(error)}


def mark_managed_page(page, job: dict[str, Any], profile: dict[str, Any] | None = None) -> None:
    cfg = provider_session_cfg(profile, job)
    if not cfg.get("markSessionStorage"):
        return
    bot_id = str(job.get("botId", ""))
    try:
        page.evaluate(
            """({botId}) => {
                try {
                    sessionStorage.setItem('AI_CHATTER_PROVIDER_ID', String(botId || ''));
                    sessionStorage.setItem('AI_CHATTER_MANAGED_AT', new Date().toISOString());
                    window.__AI_CHATTER_PROVIDER_ID = String(botId || '');
                } catch (e) {}
            }""",
            {"botId": bot_id},
        )
    except Exception:
        pass


def reset_provider_session_if_needed(page, job: dict[str, Any], profile: dict[str, Any] | None = None, *, force: bool = False) -> bool:
    cfg = provider_session_cfg(profile, job)
    start_url = str(cfg.get("startUrl") or "").strip()
    if not start_url:
        return False
    bot_id = str(job.get("botId", "")).lower()
    should = force or bool(cfg.get("forceStartUrlEveryJob"))
    # Qwen is sensitive to deleted/expired parent chat ids. If the current URL
    # points to a concrete old chat, prefer provider home/new-chat URL before retry.
    if not should and bot_id == "qwen":
        url = (getattr(page, "url", "") or "").lower()
        if any(token in url for token in ("parent_id", "/c/", "/chat/", "chatid", "conversation")):
            should = True
    if not should:
        return False
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        mark_managed_page(page, job, profile)
        return True
    except Exception:
        return False


def provider_thread_broken_text(value: Any) -> bool:
    text = str(value or "").lower()
    patterns = [
        "invalid input chat parent_id",
        "parent_id",
        "is not exist",
        "conversation not found",
        "chat not found",
        "thread not found",
    ]
    return any(p in text for p in patterns)


def classify_provider_thread_broken(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    fields = [result.get("answer"), result.get("error"), result.get("reason")]
    runtime = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
    fields.extend([runtime.get("error"), runtime.get("stage")])
    raw = runtime.get("raw") if isinstance(runtime.get("raw"), dict) else {}
    fields.extend([raw.get("answer"), raw.get("error")])
    return any(provider_thread_broken_text(item) for item in fields)


INPUT_SELECTORS = [
    "textarea",
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true']",
    "div.ProseMirror",
    "#prompt-textarea",
    "textarea[placeholder]",
    "div[role='textbox']",
]

SEND_SELECTORS = [
    "button[data-testid='send-button']",
    "button[aria-label='Send prompt']",
    "button[aria-label='Send message']",
    "button[aria-label*='Send']",
    "button[aria-label*='Отправ']",
    "[role='button'][aria-label*='Send']",
    "[role='button'][aria-label*='Отправ']",
    "button[type='submit']",
]


def wait_ready(page, timeout_ms: int = 30000) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    # Network idle is not guaranteed on chat apps, so keep it best-effort.
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def insert_text(page, text: str, timeout_ms: int, profile: dict[str, Any] | None = None) -> str:
    profile = profile or {}
    input_cfg = profile.get("input", {}) if isinstance(profile.get("input"), dict) else {}
    selectors = unique_list(split_selector_list(input_cfg.get("selector")) + split_selector_list(input_cfg.get("fallbackSelector")) + INPUT_SELECTORS)

    last_error = ""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=min(timeout_ms, 10000))
            locator.click(timeout=5000)
            tag = locator.evaluate("(el) => (el.tagName || '').toLowerCase()")
            if tag in ("textarea", "input"):
                locator.fill(text, timeout=10000)
            else:
                locator.evaluate(
                    """(el, value) => {
                        el.focus();
                        try {
                            document.execCommand('selectAll', false, null);
                            document.execCommand('insertText', false, value);
                        } catch (e) {
                            el.textContent = value;
                        }
                        try {
                            el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
                        } catch (e) {
                            el.dispatchEvent(new Event('input', {bubbles:true}));
                        }
                    }""",
                    text,
                )
            return selector
        except Exception as error:
            last_error = f"{selector}: {type(error).__name__}: {error}"
            continue
    raise RuntimeError(f"Input not found or text insert failed. Last error: {last_error}")


def click_send_by_profile_strategy(page, profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile or {}
    send = profile.get("send", {}) if isinstance(profile.get("send"), dict) else {}
    strategy = send.get("strategy", {}) if isinstance(send.get("strategy"), dict) else {}
    input_cfg = profile.get("input", {}) if isinstance(profile.get("input"), dict) else {}

    return page.evaluate(
        r"""({send, strategy, inputCfg}) => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }
            function rectInfo(el) {
                const r = el.getBoundingClientRect();
                return {
                    left: Math.round(r.left), top: Math.round(r.top),
                    right: Math.round(r.right), bottom: Math.round(r.bottom),
                    width: Math.round(r.width), height: Math.round(r.height),
                    cx: Math.round(r.left + r.width / 2), cy: Math.round(r.top + r.height / 2)
                };
            }
            function splitSelectors(value) {
                if (!value || typeof value !== 'string') return [];
                return value.split(',').map(s => s.trim()).filter(Boolean);
            }
            function firstVisible(selectors, root=document) {
                for (const selector of selectors) {
                    try {
                        const nodes = Array.from(root.querySelectorAll(selector)).filter(visible);
                        if (nodes.length) return nodes[0];
                    } catch(e) {}
                }
                return null;
            }

            const inputSelectors = [
                ...splitSelectors(inputCfg.selector),
                ...splitSelectors(inputCfg.fallbackSelector),
                "textarea",
                "[contenteditable='true'][role='textbox']",
                "[contenteditable='true']",
                "div.ProseMirror",
                "#prompt-textarea",
                "textarea[placeholder]",
                "div[role='textbox']"
            ];

            const input = firstVisible(inputSelectors);
            if (!input) return {ok:false, error:"profile input not found"};

            let root = document;
            if (strategy.composerFromInput) {
                const ir = input.getBoundingClientRect();
                let best = input.parentElement || input;
                for (let n = input; n; n = n.parentElement) {
                    if (!visible(n)) continue;
                    const r = n.getBoundingClientRect();
                    const looks =
                        r.width >= ir.width &&
                        r.height >= ir.height &&
                        r.height <= 300 &&
                        r.bottom >= ir.bottom;
                    if (looks) best = n;
                }
                root = best;
            } else if (strategy.composerSelector) {
                try { root = document.querySelector(strategy.composerSelector) || document; } catch(e) { root = document; }
            }

            const selectors = [
                ...splitSelectors(strategy.buttonSelector),
                ...splitSelectors(send.selector)
            ];

            let buttons = [];
            for (const selector of selectors) {
                try {
                    buttons = buttons.concat(Array.from(root.querySelectorAll(selector)).filter(visible));
                } catch(e) {}
            }

            const enabledAttribute = strategy.enabledAttribute;
            const enabledValueNot = strategy.enabledValueNot;
            if (enabledAttribute) {
                buttons = buttons.filter(el => String(el.getAttribute(enabledAttribute)) !== String(enabledValueNot));
            }
            buttons = buttons.filter(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true');

            if (!buttons.length) {
                return {
                    ok:false,
                    error:"profile send button not found",
                    inputRect: rectInfo(input),
                    rootRect: root === document ? null : rectInfo(root),
                    selectors
                };
            }

            let target = buttons[0];
            if (strategy.pick === "last") target = buttons[buttons.length - 1];
            if (strategy.pick === "rightmost") {
                buttons.sort((a,b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
                target = buttons[0];
            }

            const hover = target.querySelector('.ds-icon-button__hover-bg');
            if (hover && visible(hover)) target = hover.closest(".ds-icon-button, [role='button'], .button, button, [tabindex]") || hover;

            const r = target.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            target.scrollIntoView({block:'center', inline:'center'});
            const init = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};
            try { target.dispatchEvent(new PointerEvent('pointerdown', init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent('mousedown', init)); } catch(e) {}
            try { target.dispatchEvent(new PointerEvent('pointerup', init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent('mouseup', init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent('click', init)); } catch(e) {}
            try { target.click(); } catch(e) {}

            return {
                ok:true,
                method:"profile_strategy",
                profileName: send.method || "",
                targetRect: rectInfo(target),
                targetText: [
                    target.tagName || "",
                    target.getAttribute("class") || "",
                    target.getAttribute("role") || "",
                    target.getAttribute("aria-label") || "",
                    target.getAttribute("title") || "",
                    target.textContent || ""
                ].join(" ").slice(0, 300),
                buttonCount: buttons.length,
                strategy
            };
        }""",
        {"send": send, "strategy": strategy, "inputCfg": input_cfg},
    )


def click_deepseek_send_by_known_selector(page) -> dict[str, Any]:
    """Click DeepSeek Send using known structure discovered from DevTools.

    The user inspected the real Send hover layer:
    div.bf38813a > div:nth-child(3) ... .ds-icon-button__hover-bg

    We avoid full #root path and scope the search to the prompt panel near input.
    """
    return page.evaluate(
        r"""() => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }

            function rectInfo(el) {
                const r = el.getBoundingClientRect();
                return {
                    left: Math.round(r.left),
                    top: Math.round(r.top),
                    right: Math.round(r.right),
                    bottom: Math.round(r.bottom),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                    cx: Math.round(r.left + r.width / 2),
                    cy: Math.round(r.top + r.height / 2)
                };
            }

            const inputSelectors = [
                "textarea",
                "[contenteditable='true'][role='textbox']",
                "[contenteditable='true']",
                "div.ProseMirror",
                "#prompt-textarea",
                "textarea[placeholder]",
                "div[role='textbox']"
            ];
            const inputs = inputSelectors.flatMap(sel => Array.from(document.querySelectorAll(sel))).filter(visible);
            const input = inputs[0];
            if (!input) return {ok:false, error:"input not found"};

            const ir = input.getBoundingClientRect();

            // Locate prompt panel/container near the input.
            let promptPanel = null;
            for (let n = input; n; n = n.parentElement) {
                if (!visible(n)) continue;
                const r = n.getBoundingClientRect();
                if (r.width >= ir.width && r.height >= ir.height && r.height <= 180 && r.bottom >= ir.bottom) {
                    promptPanel = n;
                }
            }
            if (!promptPanel) promptPanel = input.parentElement || document.body;

            // The inspected DeepSeek right-side icon group has class bf38813a.
            const groups = Array.from(promptPanel.querySelectorAll(".bf38813a")).filter(visible);
            const candidates = [];

            for (const group of groups) {
                const exact = group.querySelector("div:nth-child(3) .ds-icon-button__hover-bg");
                if (exact && visible(exact)) candidates.push(exact);

                // Fallback: all hover backgrounds inside this right icon group.
                for (const el of Array.from(group.querySelectorAll(".ds-icon-button__hover-bg")).filter(visible)) {
                    if (!candidates.includes(el)) candidates.push(el);
                }
            }

            // Wider fallback: rightmost hover-bg inside prompt panel.
            if (!candidates.length) {
                for (const el of Array.from(promptPanel.querySelectorAll(".ds-icon-button__hover-bg")).filter(visible)) {
                    candidates.push(el);
                }
            }

            if (!candidates.length) {
                return {ok:false, error:"DeepSeek send hover-bg candidate not found", inputRect: rectInfo(input)};
            }

            // Prefer the rightmost candidate; Send is right of attachment.
            candidates.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
            const hover = candidates[0];

            // Click the closest button-ish ancestor if possible, otherwise click hover itself.
            const target = hover.closest(".ds-icon-button, [role='button'], .button, button, [tabindex]") || hover;
            const r = target.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;

            target.scrollIntoView({block:"center", inline:"center"});

            const init = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, button:0};
            try { target.dispatchEvent(new PointerEvent("pointerdown", init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent("mousedown", init)); } catch(e) {}
            try { target.dispatchEvent(new PointerEvent("pointerup", init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent("mouseup", init)); } catch(e) {}
            try { target.dispatchEvent(new MouseEvent("click", init)); } catch(e) {}
            try { target.click(); } catch(e) {}

            return {
                ok:true,
                method:"deepseek_known_selector",
                targetRect: rectInfo(target),
                hoverRect: rectInfo(hover),
                candidateCount: candidates.length,
                targetText: [
                    target.tagName || "",
                    target.getAttribute("class") || "",
                    target.getAttribute("aria-label") || "",
                    target.getAttribute("title") || "",
                    target.textContent || ""
                ].join(" ").slice(0, 300)
            };
        }"""
    )


def click_send_by_locator(page, timeout_ms: int) -> str | None:
    # Role/name first. Some sites expose accessible name.
    role_patterns = [
        re.compile(r"send|submit|отправ", re.I),
    ]
    for pattern in role_patterns:
        try:
            page.get_by_role("button", name=pattern).click(timeout=4000)
            return f"role=button[name~={pattern.pattern}]"
        except Exception:
            pass

    for selector in SEND_SELECTORS:
        try:
            locator = page.locator(selector).first
            locator.click(timeout=4000)
            return selector
        except Exception:
            continue

    return None


def diagnose_send_candidates(page) -> dict[str, Any]:
    return page.evaluate(
        r"""() => {
            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }

            function textOf(el) {
                let cls = "";
                try { cls = typeof el.className === 'string' ? el.className : JSON.stringify(el.className); } catch(e) {}
                return [
                    el.tagName || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.getAttribute('role') || '',
                    el.getAttribute('type') || '',
                    el.getAttribute('data-testid') || '',
                    cls || '',
                    el.textContent || ''
                ].join(' ').replace(/\s+/g, ' ').trim();
            }

            const inputSelectors = [
                "textarea",
                "[contenteditable='true'][role='textbox']",
                "[contenteditable='true']",
                "div.ProseMirror",
                "#prompt-textarea",
                "textarea[placeholder]",
                "div[role='textbox']"
            ];
            const inputs = inputSelectors.flatMap(sel => Array.from(document.querySelectorAll(sel))).filter(visible);
            const input = inputs[0];
            if (!input) return {ok:false, error:"input not found"};

            const ir = input.getBoundingClientRect();
            const seen = new Set();
            const candidates = [];

            for (let root = input, depth = 0; root && depth < 12; root = root.parentElement, depth++) {
                if (!visible(root)) continue;
                const rr = root.getBoundingClientRect();

                const nodes = Array.from(root.querySelectorAll("button,[role='button'],[tabindex],svg,path,div,span"));
                for (const node of nodes) {
                    let el = node;
                    const tag = (node.tagName || '').toLowerCase();
                    if (tag === 'svg' || tag === 'path') {
                        el = node.closest("button,[role='button'],[tabindex],div,span") || node;
                    }
                    if (!el || !visible(el) || seen.has(el)) continue;
                    seen.add(el);

                    const r = el.getBoundingClientRect();
                    const near = (
                        r.left >= ir.left - 180 &&
                        r.right <= ir.right + 260 &&
                        r.top >= ir.top - 180 &&
                        r.bottom <= ir.bottom + 180
                    );
                    if (!near) continue;

                    const t = textOf(el).toLowerCase();
                    let score = 0;
                    const tagName = (el.tagName || '').toLowerCase();
                    const hasSvg = !!(el.querySelector && el.querySelector('svg'));
                    const compact = r.width <= 42 && r.height <= 42;
                    const veryRight = r.left > ir.left + ir.width * 0.82;
                    const rightHalf = r.left > ir.left + ir.width * 0.55;

                    if (/send|submit|отправ|arrow|стрел/.test(t)) score += 120;
                    if (tagName === 'button') score += 40;
                    if (hasSvg) score += 35;
                    if (compact) score += 80;
                    if (rightHalf) score += 50;
                    if (veryRight) score += 90;
                    if (r.top >= ir.top - 80 && r.bottom <= ir.bottom + 140) score += 20;

                    // DeepSeek has a wrapper around two icon buttons. It can be
                    // wider than a button and its center falls between icons.
                    // Do not choose such containers when compact children exist.
                    if (r.width > 50 && r.height <= 45 && !/send|submit|отправ/.test(t)) score -= 160;

                    // Left icon in DeepSeek's right-side pair is usually attachment/tool.
                    // Prefer the far-right compact icon.
                    if (compact && !veryRight && r.left > ir.left + ir.width * 0.65) score -= 60;

                    if (/attach|upload|file|clip|paperclip|скреп|загруз/.test(t)) score -= 300;
                    if (/search|поиск|deepthink|thinking|reason|глубок/.test(t)) score -= 120;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') score -= 200;

                    candidates.push({
                        score,
                        tag: el.tagName || '',
                        text: textOf(el).slice(0, 260),
                        rect: {
                            left: Math.round(r.left),
                            top: Math.round(r.top),
                            right: Math.round(r.right),
                            bottom: Math.round(r.bottom),
                            width: Math.round(r.width),
                            height: Math.round(r.height),
                            cx: Math.round(r.left + r.width / 2),
                            cy: Math.round(r.top + r.height / 2)
                        },
                        meta: {compact, veryRight, rightHalf, hasSvg},
                        depth,
                        rootRect: {
                            left: Math.round(rr.left),
                            top: Math.round(rr.top),
                            right: Math.round(rr.right),
                            bottom: Math.round(rr.bottom),
                            width: Math.round(rr.width),
                            height: Math.round(rr.height)
                        }
                    });
                }
            }

            candidates.sort((a,b) => b.score - a.score);
            return {
                ok:true,
                inputRect: {
                    left: Math.round(ir.left),
                    top: Math.round(ir.top),
                    right: Math.round(ir.right),
                    bottom: Math.round(ir.bottom),
                    width: Math.round(ir.width),
                    height: Math.round(ir.height)
                },
                candidates: candidates.slice(0, 15)
            };
        }"""
    )


def click_send_by_dom_scoring(page) -> dict[str, Any]:
    """Find and click likely Send near the input using DOM scoring."""
    info = diagnose_send_candidates(page)
    if not info.get("ok"):
        return info

    candidates = info.get("candidates", [])
    if not candidates:
        return {**info, "ok": False, "error": "no send candidates"}

    best = candidates[0]
    if int(best.get("score", 0)) <= 0:
        return {**info, "ok": False, "error": "no positive send candidate"}

    # Try to click by coordinates via Playwright mouse; this is more realistic
    # than el.click(), but still based on DOM element detection, not fixed coords.
    x = best["rect"]["cx"]
    y = best["rect"]["cy"]
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.up()
    return {**info, "ok": True, "method": "diagnostic_dom_mouse", "clicked": best}


def click_send_by_mouse(page) -> dict[str, Any]:
    info = diagnose_send_candidates(page)
    if not info.get("ok"):
        return info
    positives = [c for c in info.get("candidates", []) if int(c.get("score", 0)) > 0]
    if not positives:
        return {**info, "ok": False, "error": "no positive mouse candidate"}
    best = positives[0]
    page.mouse.click(best["rect"]["cx"], best["rect"]["cy"])
    return {**info, "ok": True, "method": "playwright_mouse_diagnostic", "clicked": best}

def press_enter(page) -> str:
    page.keyboard.press("Enter")
    return "keyboard.enter"


def input_text_value(page) -> str:
    try:
        return page.evaluate(
            r"""() => {
                function visible(el) {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                }
                const selectors = [
                    "textarea",
                    "[contenteditable='true'][role='textbox']",
                    "[contenteditable='true']",
                    "div.ProseMirror",
                    "#prompt-textarea",
                    "textarea[placeholder]",
                    "div[role='textbox']"
                ];
                const inputs = selectors.flatMap(sel => Array.from(document.querySelectorAll(sel))).filter(visible);
                const el = inputs[0];
                if (!el) return "";
                if ('value' in el) return String(el.value || "");
                return String(el.innerText || el.textContent || "");
            }"""
        )
    except Exception:
        return ""


def click_send(page, timeout_ms: int, profile: dict[str, Any] | None = None) -> dict[str, Any]:
    before_value = input_text_value(page)

    profile_result = click_send_by_profile_strategy(page, profile)
    if profile_result.get("ok"):
        time.sleep(1)
        after_value = input_text_value(page)
        return {**profile_result, "inputBefore": before_value[:200], "inputAfter": after_value[:200]}

    if "deepseek" in (page.url or "").lower():
        ds = click_deepseek_send_by_known_selector(page)
        if ds.get("ok"):
            time.sleep(1)
            after_value = input_text_value(page)
            return {**ds, "inputBefore": before_value[:200], "inputAfter": after_value[:200]}

    method = click_send_by_locator(page, timeout_ms)
    if method:
        time.sleep(1)
        after_value = input_text_value(page)
        return {"ok": True, "method": f"locator:{method}", "inputBefore": before_value[:200], "inputAfter": after_value[:200]}

    dom = click_send_by_dom_scoring(page)
    if dom.get("ok"):
        time.sleep(1)
        after_value = input_text_value(page)
        return {**dom, "inputBefore": before_value[:200], "inputAfter": after_value[:200]}

    mouse = click_send_by_mouse(page)
    if mouse.get("ok"):
        time.sleep(1)
        after_value = input_text_value(page)
        return {**mouse, "inputBefore": before_value[:200], "inputAfter": after_value[:200]}

    # Enter is only diagnostic now. It is not treated as reliable success.
    enter_error = ""
    try:
        page.keyboard.press("Enter")
        time.sleep(1)
        enter_error = f"Enter pressed; inputAfter={input_text_value(page)[:200]}"
    except Exception as error:
        enter_error = f"Enter failed: {error}"

    diag = diagnose_send_candidates(page)
    return {
        "ok": False,
        "error": "send button not clicked by locator/dom/mouse; Enter is not considered success",
        "priorErrors": {"dom": dom, "mouse": mouse, "enter": enter_error},
        "diagnostics": diag,
    }

def clean_answer_text(text: str) -> str:
    """Remove provider UI/chatter artifacts from final answer text.

    This is intentionally conservative: it removes known one-line status labels
    and keeps the user's visible final answer. It must not invent text.
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    lines = [line.rstrip() for line in raw.split("\n")]
    cleaned: list[str] = []
    skip_block = False

    for line in lines:
        stripped = line.strip()
        low = stripped.lower()

        # Qwen / generic reasoning-status artifacts.
        if stripped in {"Завершено размышление", "Завершено размышления", "Размышление завершено"}:
            continue
        if stripped in {"Думаю", "Размышляю", "Thinking", "Thinking...", "Thought"}:
            continue
        if low.startswith("thought for ") or low.startswith("thoughts for "):
            continue
        if low.startswith("we need to ") or low.startswith("the user "):
            # Usually a leaked/expanded reasoning summary from the UI. Drop this
            # line, but keep later final answer lines.
            continue
        if stripped.startswith("• We need") or stripped.startswith("- We need"):
            continue

        cleaned.append(line)

    result = "\n".join(cleaned).strip()

    # If a provider returned a very large blob with a final short answer at the
    # end, prefer the tail after a blank separation. This helps when ChatGPT's
    # collapsed thinking UI is included in the same DOM container.
    if len(result) > 1200:
        parts = [part.strip() for part in result.split("\n\n") if part.strip()]
        if parts:
            tail = parts[-1]
            if 0 < len(tail) < len(result):
                result = tail

    return result.strip()




def is_boilerplate_answer(text: str) -> bool:
    s = str(text or "").strip().lower()
    if not s:
        return True
    if s in {"думаю", "размышляю", "thinking", "thinking...", "завершено размышление", "размышление завершено", "пропустить"}:
        return True
    bad_fragments = (
        "evaluating the input for meaning and context",
        "engaging in the collaborative dialogue circle",
        "engaging in the ai chatter circle",
        "participating in the ai chatter circle",
        "responding with the requested word",
    )
    if any(fragment in s for fragment in bad_fragments) and "пропустить" in s:
        return True
    if s == "автоматический":
        return True
    return False



def normalize_for_compare_py(text: str) -> str:
    return " ".join(str(text or "").replace("​", "").replace("﻿", "").split()).strip().lower()


def looks_like_echo_of_prompt(text: str, prompt_text: str) -> bool:
    a = normalize_for_compare_py(text)
    p = normalize_for_compare_py(prompt_text)
    if not a or not p:
        return False
    if a == p:
        return True
    if len(a) <= 80 and a in p:
        return True
    if len(p) <= 80 and p in a:
        return True
    return False


def extract_answer(page, prompt_text: str = "") -> str:
    # Prefer final rendered markdown blocks before broad assistant containers.
    # Broad containers often include reasoning UI such as "Thought for..." or
    # Qwen placeholder blocks like "Evaluating... / Пропустить". Gather all
    # candidate blocks and choose the newest meaningful one, not merely the last
    # DOM block.
    selectors = [
        "[data-message-author-role='assistant'] .markdown",
        "article .markdown",
        ".markdown",
        "[data-testid*='assistant'] .markdown",
        "[data-message-author-role='assistant']",
        "article",
        "[role='article']",
        "[class*='assistant']",
        "[class*='message']",
    ]
    texts: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            for index in range(count):
                text = loc.nth(index).inner_text(timeout=1000).strip()
                text = clean_answer_text(text)
                if text and text not in seen:
                    texts.append(text)
                    seen.add(text)
        except Exception:
            continue

    non_echo = [text for text in texts if not looks_like_echo_of_prompt(text, prompt_text)]
    meaningful = [text for text in non_echo if not is_boilerplate_answer(text)]
    if meaningful:
        return meaningful[-1]
    if non_echo:
        return non_echo[-1]
    try:
        body = clean_answer_text(page.locator("body").inner_text(timeout=1000).strip()[-4000:])
        if body and not is_boilerplate_answer(body) and not looks_like_echo_of_prompt(body, prompt_text):
            return body
        return body
    except Exception:
        return ""


def page_generation_state(page) -> dict[str, Any]:
    """Best-effort page-side generation/completion detector.

    R3.5.7: for Qwen and similar providers, do not treat early text as final
    until the page itself exposes an end signal: reasoning done text or final
    action controls such as copy/like/dislike/regenerate.
    """
    try:
        return page.evaluate(
            r"""() => {
                const visible = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
                };
                const textOf = el => ((el.getAttribute('aria-label') || '') + ' ' +
                    (el.getAttribute('title') || '') + ' ' + (el.innerText || el.textContent || '')).trim();
                const buttons = Array.from(document.querySelectorAll('button,[role="button"],[aria-label],[title]')).filter(visible);
                let hasStop = false;
                let hasSend = false;
                let disabledSend = false;
                let hasCopy = false;
                let hasLike = false;
                let hasDislike = false;
                let hasRegenerate = false;
                let visibleActionCount = 0;
                for (const b of buttons) {
                    const t = textOf(b).toLowerCase();
                    if (/stop|cancel|останов|прервать|停止|中止/.test(t)) hasStop = true;
                    if (/send|submit|отправ|arrow|стрел/.test(t)) {
                        hasSend = true;
                        if (b.disabled || b.getAttribute('aria-disabled') === 'true') disabledSend = true;
                    }
                    if (/copy|копир|скопир/.test(t)) { hasCopy = true; visibleActionCount += 1; }
                    if (/like|нрав|good|thumbs up|赞/.test(t)) { hasLike = true; visibleActionCount += 1; }
                    if (/dislike|не нрав|bad|thumbs down|踩/.test(t)) { hasDislike = true; visibleActionCount += 1; }
                    if (/regenerate|retry|again|повтор|перегенер|重新|刷新/.test(t)) { hasRegenerate = true; visibleActionCount += 1; }
                }
                const bodyText = (document.body.innerText || '').slice(-7000).toLowerCase();
                const reasoningDone = /завершено размышление|completed reasoning|done reasoning|finished reasoning|completed thinking|finished thinking/.test(bodyText);
                const textGenerating = /generating|thinking|размышля|думаю|生成中|思考中/.test(bodyText) && !reasoningDone;
                const actionControlsVisible = Boolean(hasCopy || hasLike || hasDislike || hasRegenerate || visibleActionCount >= 2);
                const answerComplete = Boolean(reasoningDone || actionControlsVisible);
                return { generating: Boolean(hasStop || (disabledSend && !hasSend) || textGenerating), hasStop, hasSend, disabledSend, textGenerating, reasoningDone, actionControlsVisible, hasCopy, hasLike, hasDislike, hasRegenerate, visibleActionCount, answerComplete };
            }"""
        ) or {"generating": False}
    except Exception as error:
        return {"generating": False, "error": str(error)}

def weak_intermediate_answer(text: str) -> bool:
    return is_boilerplate_answer(text)


def wait_for_answer(page, before_text: str, timeout_ms: int, bot_id: str = "", prompt_text: str = "") -> dict[str, Any]:
    deadline = time.time() + timeout_ms / 1000
    last = ""
    stable_since = time.time()
    not_generating_since: float | None = None
    unchanged_input_seconds = 0
    state = {"generating": False}
    completion_since: float | None = None
    require_completion_signal = "qwen" in str(bot_id or "").lower() or "qwen" in str(getattr(page, "url", "")).lower()

    while time.time() < deadline:
        text = extract_answer(page, prompt_text)
        current_input = input_text_value(page).strip()
        state = page_generation_state(page)
        generating = bool(state.get("generating"))

        if not generating:
            if not_generating_since is None:
                not_generating_since = time.time()
        else:
            not_generating_since = None

        if state.get("answerComplete"):
            if completion_since is None:
                completion_since = time.time()
        else:
            completion_since = None

        if text and text != before_text and text != last:
            last = text
            stable_since = time.time()

        stable_for = time.time() - stable_since
        done_for = 0 if not_generating_since is None else time.time() - not_generating_since

        # Do not return bare "Думаю" / "Thinking" as a final answer.
        if last and not weak_intermediate_answer(last):
            completion_for = 0 if completion_since is None else time.time() - completion_since
            if require_completion_signal:
                if completion_for >= 1.2 and stable_for >= 2.2:
                    return {"ok": True, "answer": last, "reason": "page_observer_complete", "generationState": state}
                if stable_for >= 30 and done_for >= 12:
                    return {"ok": True, "answer": last, "reason": "qwen_long_stable_fallback", "generationState": state}
            else:
                if done_for >= 1.5 and stable_for >= 1.5:
                    return {"ok": True, "answer": last, "reason": "answer_ready_observer", "generationState": state}
                if stable_for >= 12 and not generating:
                    return {"ok": True, "answer": last, "reason": "stable_text_not_generating", "generationState": state}
                if stable_for >= 18:
                    return {"ok": True, "answer": last, "reason": "stable_text_timeout_guard", "generationState": state}

        # If the prompt text is still in input and no answer starts, send probably failed.
        if current_input and not last:
            unchanged_input_seconds += 0.5
            if unchanged_input_seconds >= 6:
                return {"ok": False, "error": "send_not_confirmed_input_still_contains_text", "generationState": state}
        else:
            unchanged_input_seconds = 0

        time.sleep(0.5)

    if last and not weak_intermediate_answer(last) and (not require_completion_signal or state.get("answerComplete")):
        return {"ok": True, "answer": last, "reason": "timeout_with_text", "generationState": state}
    return {"ok": False, "error": "timeout_no_final_answer", "lastText": last, "generationState": state}



def run_job_with_extension_transport(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """Run a chrome job through the installed AI Chatter browser extension.

    The extension content script is already resident on provider pages. We talk
    to it with page postMessage using R3.5-only message types. This avoids
    injecting provider-control JS for every job and makes diagnostics come from
    the page-side worker itself.
    """
    text = str(job.get("message", {}).get("text", ""))
    if not text.strip():
        return {"ok": False, "error": "empty job text", "transport": "extension_resident"}

    profile = load_profile_for_job(job)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        try:
            page = find_page(browser, job)
            page.bring_to_front()
            wait_ready(page, timeout_ms=30000)
            session_activation = activate_sticky_provider_session(page, job, profile)
            if not session_activation.get("used"):
                reset_provider_session_if_needed(page, job, profile, force=False)
            mark_managed_page(page, job, profile)

            payload = {
                "job": {**job, "timeoutMs": timeout_ms},
                "profile": profile,
                "timeoutMs": timeout_ms,
            }
            result = page.evaluate(
                r"""async ({payload}) => {
                    const requestId = 'r35-' + Date.now() + '-' + Math.random().toString(16).slice(2);
                    const timeoutMs = Number(payload.timeoutMs || 180000) + 10000;

                    function waitForResult() {
                        return new Promise(resolve => {
                            const timer = setTimeout(() => {
                                window.removeEventListener('message', onMessage, true);
                                resolve({ok:false, error:'extension_transport_timeout', stage:'page_eval_waiter'});
                            }, timeoutMs);

                            function onMessage(event) {
                                if (event.source !== window) return;
                                const data = event.data || {};
                                if (data.type !== 'AI_CHATTER_EXTENSION_JOB_RESULT_V35') return;
                                if (data.requestId !== requestId) return;
                                clearTimeout(timer);
                                window.removeEventListener('message', onMessage, true);
                                resolve(data.result || {ok:false, error:'empty_extension_result'});
                            }

                            window.addEventListener('message', onMessage, true);
                            window.postMessage({
                                type: 'AI_CHATTER_EXTENSION_RUN_JOB_V35',
                                requestId,
                                job: payload.job,
                                profile: payload.profile
                            }, '*');
                        });
                    }

                    return await waitForResult();
                }""",
                {"payload": payload},
            )

            if not isinstance(result, dict):
                return {"ok": False, "error": f"extension returned non-object: {type(result).__name__}", "transport": "extension_resident", "url": page.url}

            if result.get("ok"):
                return {
                    "ok": True,
                    "answer": result.get("answer", ""),
                    "reason": result.get("reason", "extension_resident"),
                    "url": result.get("url") or page.url,
                    "runtime": {
                        "kind": "extension_resident",
                        "stage": result.get("stage", ""),
                        "diagnostics": result.get("diagnostics", {}),
                        "url": result.get("url") or page.url,
                        "title": result.get("title") or "",
                        "sessionActivation": session_activation,
                    },
                }

            return {
                "ok": False,
                "error": str(result.get("error", "extension resident transport failed")),
                "answer": result.get("answer", ""),
                "url": result.get("url") or page.url,
                "runtime": {
                    "kind": "extension_resident",
                    "stage": result.get("stage", ""),
                    "diagnostics": result.get("diagnostics", {}),
                    "url": result.get("url") or page.url,
                    "title": result.get("title") or "",
                    "sessionActivation": session_activation,
                    "raw": result,
                },
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass


def collect_answer_after_extension_send(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """Collect the answer after the extension already clicked Send.

    R3.5.1 safety rule: if the extension confirms inputSet+sendClicked but
    fails to recognize the final answer, do not run the old fallback sender,
    because that submits the same prompt a second time. Instead, attach with
    Playwright only as a read/observe collector.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        try:
            page = find_page(browser, job)
            page.bring_to_front()
            wait_ready(page, timeout_ms=30000)
            mark_managed_page(page, job, None)
            collect_timeout = max(15000, min(int(timeout_ms), 90000))
            result = wait_for_answer(page, "", collect_timeout, str(job.get("botId") or job.get("provider") or ""), prompt_text=str(job.get("message", {}).get("text") or job.get("text") or ""))
            if result.get("ok"):
                return {
                    "ok": True,
                    "answer": result.get("answer", ""),
                    "reason": "collect_after_extension_send",
                    "url": page.url,
                    "runtime": {
                        "kind": "extension_resident_collect_after_send",
                        "collector": "playwright_read_only",
                        "generationState": result.get("generationState", {}),
                        "url": page.url,
                        "title": page.title() if hasattr(page, "title") else "",
                    },
                }
            return {
                "ok": False,
                "error": result.get("error") or "collect_after_extension_send_failed",
                "answer": result.get("answer") or result.get("lastText") or "",
                "url": page.url,
                "runtime": {
                    "kind": "extension_resident_collect_after_send_failed",
                    "collector": "playwright_read_only",
                    "generationState": result.get("generationState", {}),
                    "raw": result,
                },
            }
        finally:
            try:
                browser.close()
            except Exception:
                pass


def _extension_did_submit(extension_result: dict[str, Any]) -> bool:
    runtime = extension_result.get("runtime") or {}
    diagnostics = runtime.get("diagnostics") or {}
    raw = runtime.get("raw") or {}
    raw_diag = raw.get("diagnostics") or {}
    merged = {**raw_diag, **diagnostics}
    return bool(merged.get("inputSet") and merged.get("sendClicked"))


def _run_job_with_extension_then_playwright_once(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    extension_result = run_job_with_extension_transport(cdp_url, job, timeout_ms)
    if extension_result.get("ok"):
        return extension_result

    # If the extension already submitted the prompt, never send it again with
    # fallback. This prevents duplicate Qwen/DeepSeek messages and versioned
    # answers like 1/2. Use Playwright only to collect the result from DOM.
    if _extension_did_submit(extension_result):
        collected = collect_answer_after_extension_send(cdp_url, job, timeout_ms)
        if collected.get("ok"):
            collected_runtime = collected.get("runtime", {})
            collected_runtime["extensionError"] = extension_result.get("error", "")
            collected_runtime["extensionRuntime"] = extension_result.get("runtime", {})
            collected["runtime"] = collected_runtime
            return collected
        return {
            "ok": False,
            "error": f"extension submitted but collection failed: {extension_result.get('error', '')}; collector failed: {collected.get('error', '')}",
            "answer": collected.get("answer", ""),
            "url": extension_result.get("url") or collected.get("url", ""),
            "runtime": {
                "kind": "extension_resident_submitted_collect_failed_no_resend",
                "extension": extension_result.get("runtime", {}),
                "collector": collected.get("runtime", {}),
            },
        }

    # Keep the old Stage 3.13 Playwright/CDP observer as fallback only if the
    # extension did not submit anything.
    fallback = run_job_with_playwright(cdp_url, job, timeout_ms)
    if fallback.get("ok"):
        fallback_runtime = {
            "kind": "playwright_cdp_fallback",
            "extensionError": extension_result.get("error", ""),
            "extensionRuntime": extension_result.get("runtime", {}),
        }
        fallback["runtime"] = fallback_runtime
        return fallback

    return {
        "ok": False,
        "error": f"extension failed: {extension_result.get('error', '')}; fallback failed: {fallback.get('error', '')}",
        "url": extension_result.get("url") or fallback.get("url", ""),
        "runtime": {
            "kind": "extension_resident_with_playwright_fallback_failed",
            "extension": extension_result.get("runtime", {}),
            "playwright": fallback,
        },
    }


def reset_provider_session(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    profile = load_profile_for_job(job)
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        try:
            page = find_page(browser, job)
            page.bring_to_front()
            wait_ready(page, timeout_ms=30000)
            ok = reset_provider_session_if_needed(page, job, profile, force=True)
            return {"ok": ok, "url": page.url, "runtime": {"kind": "provider_session_reset", "startUrl": provider_session_cfg(profile, job).get("startUrl", "")}}
        finally:
            try:
                browser.close()
            except Exception:
                pass


def run_job_with_extension_then_playwright(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    result = _run_job_with_extension_then_playwright_once(cdp_url, job, timeout_ms)
    if not classify_provider_thread_broken(result):
        return result

    bot_id = str(job.get("botId", "")).lower()
    profile = load_profile_for_job(job)
    cfg = provider_session_cfg(profile, job)
    if bot_id != "qwen" or not cfg.get("resetOnProviderThreadBroken"):
        result.setdefault("runtime", {})["providerThreadBroken"] = True
        return result

    reset = reset_provider_session(cdp_url, job, timeout_ms)
    retry = _run_job_with_extension_then_playwright_once(cdp_url, job, timeout_ms)
    runtime = retry.get("runtime") if isinstance(retry.get("runtime"), dict) else {}
    runtime["providerThreadBrokenRecovered"] = bool(retry.get("ok") and not classify_provider_thread_broken(retry))
    runtime["providerThreadBrokenFirstResult"] = {"ok": result.get("ok"), "error": result.get("error", ""), "answer": str(result.get("answer", ""))[:300], "runtime": result.get("runtime", {})}
    runtime["providerSessionReset"] = reset
    retry["runtime"] = runtime
    if classify_provider_thread_broken(retry):
        retry["ok"] = False
        retry["error"] = "provider_thread_broken_after_reset: open a fresh Qwen chat manually or reload provider page"
    return retry

def run_job_with_playwright(cdp_url: str, job: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    text = str(job.get("message", {}).get("text", ""))
    if not text.strip():
        return {"ok": False, "error": "empty job text"}

    profile = load_profile_for_job(job)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url)
        try:
            page = find_page(browser, job)
            page.bring_to_front()
            wait_ready(page, timeout_ms=30000)
            session_activation = activate_sticky_provider_session(page, job, profile)
            if not session_activation.get("used"):
                reset_provider_session_if_needed(page, job, profile, force=False)
            mark_managed_page(page, job, profile)

            before = extract_answer(page, text)
            input_selector = insert_text(page, text, timeout_ms, profile)
            click_info = click_send(page, timeout_ms, profile)

            if not click_info.get("ok"):
                return {"ok": False, "error": f"send click failed: {click_info}"}

            answer = wait_for_answer(page, before, timeout_ms, str(job.get("botId") or job.get("provider") or ""), prompt_text=text)
            if not answer.get("ok"):
                return {
                    "ok": False,
                    "error": answer.get("error", "answer wait failed"),
                    "inputSelector": input_selector,
                    "clickInfo": click_info,
                    "url": page.url,
                }

            return {
                "ok": True,
                "answer": answer.get("answer", ""),
                "reason": answer.get("reason", ""),
                "inputSelector": input_selector,
                "clickInfo": click_info,
                "url": page.url,
            }
        finally:
            # Do not close the shared user Chrome browser. Just disconnect.
            try:
                browser.close()
            except Exception:
                pass


def process_once(
    jobs_path: Path,
    results_path: Path,
    state_path: Path,
    *,
    cdp_url: str,
    max_jobs: int,
    timeout_ms: int,
    verbose: bool,
) -> int:
    jobs = read_jsonl(jobs_path)
    processed = state_ids(state_path)
    count = 0

    for job in jobs:
        if count >= max_jobs:
            break

        job_id = str(job.get("id", ""))
        if not job_id or job_id in processed:
            continue

        bot_id = str(job.get("botId", ""))
        bot_title = str(job.get("botTitle") or bot_id or "Bot")

        try:
            session_event = maybe_rollover_session(cdp_url, job, timeout_ms)
            result = run_job_with_extension_then_playwright(cdp_url, job, timeout_ms)
            runtime_for_session = result.get("runtime") if isinstance(result.get("runtime"), dict) else {}
            if session_event:
                runtime_for_session = dict(runtime_for_session)
                runtime_for_session["providerSession"] = session_event
                result["runtime"] = runtime_for_session
            if result.get("ok"):
                row = {
                    "id": f"chromeresult-{job_id}",
                    "jobId": job_id,
                    "createdAt": now(),
                    "status": "done",
                    "botId": bot_id,
                    "botTitle": bot_title,
                    "taskId": str(job.get("taskId", "")),
                    "source": job.get("source", {}),
                    "answer": str(result.get("answer", "")),
                    "runtime": result.get("runtime") or {
                        "kind": "playwright_cdp_exception",
                        "reason": result.get("reason", ""),
                        "url": result.get("url", ""),
                        "inputSelector": result.get("inputSelector", ""),
                        "clickInfo": result.get("clickInfo", {}),
                    },
                }
            else:
                row = {
                    "id": f"chromeresult-{job_id}",
                    "jobId": job_id,
                    "createdAt": now(),
                    "status": "error",
                    "botId": bot_id,
                    "botTitle": bot_title,
                    "taskId": str(job.get("taskId", "")),
                    "source": job.get("source", {}),
                    "error": str(result.get("error", "unknown Playwright error")),
                    "runtime": result.get("runtime") or {
                        "kind": "playwright_cdp_exception",
                        "url": result.get("url", ""),
                        "inputSelector": result.get("inputSelector", ""),
                        "clickInfo": result.get("clickInfo", {}),
                    },
                }
        except Exception as error:
            row = {
                "id": f"chromeresult-{job_id}",
                "jobId": job_id,
                "createdAt": now(),
                "status": "error",
                "botId": bot_id,
                "botTitle": bot_title,
                "taskId": str(job.get("taskId", "")),
                "source": job.get("source", {}),
                "error": f"{type(error).__name__}: {error}",
                "runtime": {"kind": "playwright_cdp_exception"},
            }

        append_jsonl(results_path, row)
        update_provider_session_after_job(job, row.get("status") == "done", row.get("runtime") if isinstance(row.get("runtime"), dict) else {})
        processed.add(job_id)
        count += 1

        if verbose:
            print(f"Processed chrome job {job_id} -> {bot_title}: {row['status']}")

    write_state(state_path, processed)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Chrome Worker 3.5.10 Assistant-only answer selection")
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--results", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--max-jobs", type=int, default=1)
    parser.add_argument("--timeout-ms", type=int, default=120000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--profiles", default="", help="Path to configs/ai_chatter_profiles.json")
    args = parser.parse_args()
    if args.profiles:
        os.environ["AI_CHATTER_CONFIGS_DIR"] = str(Path(args.profiles).expanduser().resolve().parent)

    jobs_path = Path(args.jobs).expanduser().resolve()
    results_path = Path(args.results).expanduser().resolve() if args.results else jobs_path.with_name("ai_chatter_chrome_results.jsonl")
    state_path = Path(args.state).expanduser().resolve() if args.state else jobs_path.with_name("ai_chatter_chrome_worker_state.json")

    if not args.quiet:
        print("AI Chatter Chrome Worker 3.5.10 Assistant-only answer selection")
        print(f"Jobs:    {jobs_path}")
        print(f"Results: {results_path}")
        print(f"State:   {state_path}")
        print(f"CDP:     {args.cdp_url}")
        print(f"Profiles:{profiles_path(args.profiles)}")

    count = process_once(
        jobs_path,
        results_path,
        state_path,
        cdp_url=args.cdp_url,
        max_jobs=args.max_jobs,
        timeout_ms=args.timeout_ms,
        verbose=not args.quiet,
    )

    if not args.quiet:
        print(f"New chrome jobs processed: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
