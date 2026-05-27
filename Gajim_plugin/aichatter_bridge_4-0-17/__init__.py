# SPDX-License-Identifier: GPL-3.0-or-later
"""AI Chatter Bridge plugin for Gajim.

AI Chatter Gajim Plugin 4.0.17 Branch 4 addressed Qwen + DeepSeek direct
- injects a compact AI bot panel into the right side of the active chat window;
- keeps settings window as fallback;
- keeps text commands for mobile clients / diagnostics;
- adds event-driven online/offline bot status by Chrome CDP;
- skips offline bots silently when creating tasks.
"""

from __future__ import annotations

import json
import re
import logging
import os
import sys
import subprocess
import shutil
import urllib.request
import uuid
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gi.repository import GLib
from gi.repository import Gtk

from gajim.common import app
from gajim.common import ged
from gajim.common.modules.contacts import BareContact
from gajim.common.modules.contacts import GroupchatContact
from gajim.common.modules.contacts import GroupchatParticipant
from gajim.common.structs import OutgoingMessage
from gajim.plugins.gajimplugin import GajimPlugin

log = logging.getLogger("gajim.p.aichatter_bridge")


class AIChatterBridgePlugin(GajimPlugin):
    config_default_values = {
        "room_jid": ("kuhetaje@chat.yax.im", "Main shared MUC room"),
        "respond_only_in_room": (True, "Respond only in the configured room"),
        "routing_enabled": (True, "Route normal user messages to active bots"),
        "routing_echo_enabled": (True, "Echo routing decisions to the room"),
        "route_any_non_command_in_room": (True, "Route non-command messages even if Gajim does not mark them as outgoing"),
        "queue_enabled": (True, "Write routed tasks to a JSONL queue"),
        "queue_file_name": ("ai_chatter_tasks.jsonl", "Task queue JSONL file name"),
        "results_file_name": ("logs/ai_chatter_results.jsonl", "Result JSONL file name"),
        "result_delivery_batch_size": (3, "Maximum results to deliver per .deliver command"),
        "chrome_cdp_url": ("http://127.0.0.1:9222", "Chrome DevTools Protocol URL for online/offline status"),
        "chrome_executable": ("", "Full path to Chrome executable. Empty = auto-detect"),
        "chrome_user_data_dir": ("", "Chrome CDP profile/user-data-dir. Empty = {home}/chrome-cdp-profile"),
        "offline_behavior": ("silent", "How to handle checked but offline bots: silent/notice"),
        "python_executable": ("", "Full path to Python executable. Empty = auto-detect"),
        "ai_chatter_home": ("", "AI Chatter home folder. Empty = auto-detect C:/G:/AI_chatter"),
        "executor_workdir": ("", "Executor working directory. Empty = auto-detect"),
        "auto_run_executor": (True, "Launch ai_chatter_run_once.py automatically after queuing a task"),
        "executor_command": (
            "",
            "Command template for event-driven executor launch. Empty = auto-build. Supports {queue}, {config}, {configs_dir}, {plugin_dir}, {local_dir}",
        ),
        "executor_log_file_name": ("logs/ai_chatter_event_executor.log", "Log file for event-driven executor launch"),

        "always_show_me": (True, "Always show user's own messages"),
        "hidden_bots_can_notify": (True, "Hidden bots may show priority notifications"),
        "priority_markers_text": (
            "DONE, NEED_USER, ERROR, IMPORTANT, REPORT",
            "Markers that bypass visibility filters",
        ),
        "free_bot_conversation": (True, "Bots may talk to each other"),
        "only_within_active_task": (True, "Bots talk only within active tasks"),
        "max_bot_only_turns": (6, "Max bot-only turns before cooldown"),
        "active_bot_ids_text": ("chatgpt,qwen,deepseek", "Visible/target bot ids"),
        "can_speak_bot_ids_text": ("chatgpt,qwen,deepseek", "Bots allowed to speak"),
        "chat_panel_enabled": (True, "Show compact AI panel in chat window"),
        "agent_accounts_text": ("vova_gpt@yax.im=chatgpt\nai_chatter_bot@yax.im=chatgpt", "Real XMPP agent JIDs, one per line or comma-separated; optional =bot_id suffix"),
        "agent_echo_enabled": (True, "R2/R3: answer addressed messages from real XMPP agent accounts"),
        "agent_provider_enabled": (True, "R3: create Chrome/provider jobs instead of local echo replies for enabled agents"),
        "agent_provider_queue_notice": (True, "R3: show a short diagnostic when an agent provider job is queued"),
        "auto_deliver_results": (True, "R3.1: deliver provider results automatically after executor finishes"),
        "max_auto_jobs_per_run": (5, "R3.1: maximum provider jobs/tasks processed per automatic executor run"),
        "auto_rerun_pending": (True, "R3.1: rerun executor while provider jobs remain pending"),
        "agent_outbox_file_name": ("logs/ai_chatter_agent_outbox.jsonl", "R2.1 agent outbox/delivery-state JSONL file name"),
        "agent_jobs_file_name": ("logs/ai_chatter_agent_jobs.jsonl", "R2.2 agent job queue JSONL file name"),
        "agent_registry_file_name": ("configs/ai_chatter_agent_registry.json", "R2.3 file-based agent registry JSON file name"),
        "agent_registry_text": ("", "Legacy fallback only: agent_jid=alias1,alias2 per line"),
        "stage": ("stage_2_5_3_manual_results", "Internal plugin stage"),
    }

    def init(self) -> None:
        self._profiles: dict[str, Any] = {}
        self._bots: list[dict[str, str]] = []
        self._controller_identity_keys: set[str] = set()
        self._settings_window: Gtk.Window | None = None
        self._recent_agent_outputs: list[dict[str, object]] = []
        self._executor_process: subprocess.Popen | None = None
        self._executor_needs_rerun: bool = False
        self._executor_last_limit: int = 1
        self._registry_auto_repair_running: bool = False

        self._panel_box: Gtk.Box | None = None
        self._injected_box: Gtk.Box | None = None
        self._original_roster: Gtk.Widget | None = None
        self._paned: Gtk.Paned | None = None

        self.config_dialog = self._show_config_dialog
        self.events_handlers = {
            "message-received": (ged.POSTGUI, self._on_message_event),
            "message-sent": (ged.POSTGUI, self._on_message_event),
        }

    def activate(self) -> None:
        self._load_profiles()
        self._bots = self._build_bot_registry(self._profiles)
        self._ensure_defaults()
        self._ensure_agent_registry_file()
        self._auto_repair_agent_registry_from_live_accounts()
        self._update_bot_statuses()
        # R1 room-scope rule: this plugin must be active only in the configured MUC.
        # Existing saved configs from 2.6.5 may still contain False, so force it here.
        self.config["respond_only_in_room"] = True
        self.config["stage"] = "stage_4_0_15_branch4_muc_self_dedupe"
        self.save_config()
        GLib.idle_add(self._inject_chat_panel)
        log.warning("AI Chatter Bridge AI Chatter Gajim Plugin 4.0.17 Branch 4 addressed Qwen + DeepSeek direct")

    def deactivate(self) -> None:
        self._remove_chat_panel()
        if self._settings_window is not None:
            self._settings_window.close()
            self._settings_window = None
        log.warning("AI Chatter Bridge deactivated")

    def _ensure_defaults(self) -> None:
        if not self._string_list_config("active_bot_ids_text"):
            self.config["active_bot_ids_text"] = ",".join(bot["id"] for bot in self._bots)
        if not self._string_list_config("can_speak_bot_ids_text"):
            self.config["can_speak_bot_ids_text"] = ",".join(bot["id"] for bot in self._bots)

    def _load_profiles(self) -> None:
        path = self._home_configs_path("ai_chatter_profiles.json")
        if not path.exists():
            # Migrate/copy the bundled default profile config into project configs/.
            try:
                bundled = Path(self.local_file_path("ai_chatter_profiles.json"))
                if bundled.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(bundled.read_text(encoding="utf-8-sig"), encoding="utf-8")
            except Exception:
                log.exception("Could not initialize AI Chatter profiles in configs/")
        try:
            self._profiles = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            log.exception("Could not load AI Chatter profiles from %s", path)
            self._profiles = {}

    def _build_bot_registry(self, profiles: dict[str, Any]) -> list[dict[str, str]]:
        bots: list[dict[str, str]] = []
        for profile_key, profile in profiles.items():
            if not profile.get("enabled", True):
                continue
            chat_id = str(profile.get("chatId") or profile_key.split("::", 1)[0])
            title = str(profile.get("chatName") or profile.get("profileName") or chat_id)
            origin = str(profile.get("origin", ""))
            adapter = str(profile.get("runtime", {}).get("adapter", chat_id))
            bots.append({
                "id": chat_id,
                "title": title,
                "profileKey": str(profile_key),
                "origin": origin,
                "adapter": adapter,
                "status": "configured",
            })
        return bots

    def _bot_ids(self) -> list[str]:
        if not self._bots:
            self._load_profiles()
            self._bots = self._build_bot_registry(self._profiles)
        return [bot["id"] for bot in self._bots]

    def _bot_title(self, bot_id: str) -> str:
        for bot in self._bots:
            if bot["id"] == bot_id:
                return bot["title"]
        return bot_id

    def _string_list_config(self, key: str) -> list[str]:
        value = self.config[key]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return []

    def _set_string_list_config(self, key: str, values: list[str]) -> None:
        known = set(self._bot_ids())
        filtered = [v for v in values if v in known]
        self.config[key] = ",".join(filtered)

    def _bot_profile(self, bot_id: str) -> dict[str, Any]:
        for profile in self._profiles.values():
            if not isinstance(profile, dict):
                continue
            if str(profile.get("chatId", "")).lower() == bot_id.lower():
                return profile
        return {}

    def _cdp_pages(self) -> list[dict[str, Any]]:
        url = str(self.config["chrome_cdp_url"]).rstrip("/") + "/json/list"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _bot_online_statuses(self) -> dict[str, str]:
        """Return bot online/offline by checking open CDP tabs.

        No timers are used. This is called only on panel refresh, .status, .autodetect/.bots,
        and before routing a new task.
        """
        if not self._profiles:
            self._load_profiles()

        pages = self._cdp_pages()
        page_urls = [
            str(page.get("url", "")).lower()
            for page in pages
            if str(page.get("type", "")) == "page"
        ]

        statuses: dict[str, str] = {}
        for bot_id in self._bot_ids():
            profile = self._bot_profile(bot_id)
            origin = str(profile.get("origin", "")).lower().rstrip("/")
            url_match = str(profile.get("urlMatch", "")).lower().replace("*", "").rstrip("/")
            needles = [value for value in (origin, url_match) if value]

            online = False
            for page_url in page_urls:
                if any(needle and page_url.startswith(needle) for needle in needles):
                    online = True
                    break

            statuses[bot_id] = "online" if online else "offline"

        return statuses

    def _update_bot_statuses(self) -> dict[str, str]:
        statuses = self._bot_online_statuses()
        for bot in self._bots:
            bot["status"] = statuses.get(bot["id"], "offline")
        return statuses

    def _status_text(self, bot: dict[str, str]) -> str:
        if bot.get("status") == "online":
            return "●"
        return "●"

    def _status_tooltip(self, bot: dict[str, str]) -> str:
        if bot.get("status") == "online":
            return "online: вкладка бота открыта в CDP Chrome"
        return "offline: подходящая вкладка не найдена в CDP Chrome"

    def _status_markup(self, bot: dict[str, str]) -> str:
        if bot.get("status") == "online":
            return '<span foreground="#30d158">●</span>'
        return '<span foreground="#ffd60a">●</span>'

    # ------------------------------------------------------------------
    # Right-side chat panel
    # ------------------------------------------------------------------
    def _active_control_jid(self, control: object) -> str:
        """Best-effort JID detection for the currently selected chat control."""
        candidates: list[object] = []
        for attr in ("jid", "room_jid", "contact_jid"):
            try:
                value = getattr(control, attr, "")
                if value:
                    candidates.append(value)
            except Exception:
                pass

        try:
            contact = getattr(control, "contact", None)
            if contact is not None:
                for attr in ("jid", "bare_jid", "address"):
                    value = getattr(contact, attr, "")
                    if value:
                        candidates.append(value)
        except Exception:
            pass

        try:
            get_contact = getattr(control, "get_contact", None)
            if callable(get_contact):
                contact = get_contact()
                for attr in ("jid", "bare_jid", "address"):
                    value = getattr(contact, attr, "")
                    if value:
                        candidates.append(value)
        except Exception:
            pass

        for value in candidates:
            norm = self._normalize_jid(value)
            if norm:
                return norm
        return ""

    def _inject_chat_panel(self) -> bool:
        if not bool(self.config["chat_panel_enabled"]):
            return False

        try:
            control = app.window.get_control()
            configured_room = self._normalize_jid(self.config["room_jid"])
            active_jid = self._active_control_jid(control)
            # In some Gajim 2.4.x windows get_control() does not expose a stable JID.
            # Do not fail panel injection merely because active_jid is empty; strict
            # message routing is still room-scoped by event.jid. If Gajim does expose
            # a JID and it is not the configured room, remove the panel.
            if active_jid and active_jid != configured_room:
                # Do not change ordinary private chats or unrelated MUCs when known.
                if self._injected_box is not None:
                    self._remove_chat_panel()
                return False

            paned = control._ui.conv_view_paned
            roster = control.get_group_chat_roster()

            # Already injected
            if self._injected_box is not None:
                return False

            self._paned = paned
            self._original_roster = roster

            panel = self._build_compact_panel()
            panel.set_margin_top(8)
            panel.set_margin_bottom(8)
            panel.set_margin_start(8)
            panel.set_margin_end(8)

            wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            wrapper.set_size_request(245, -1)

            # Detach roster from paned before placing it into wrapper.
            paned.set_end_child(None)
            wrapper.append(panel)
            wrapper.append(roster)
            paned.set_end_child(wrapper)

            self._panel_box = panel
            self._injected_box = wrapper
            return False
        except Exception:
            log.exception("Could not inject AI Chatter panel into chat window")
            return False

    def _remove_chat_panel(self) -> None:
        try:
            if self._paned is not None and self._original_roster is not None:
                self._paned.set_end_child(None)
                self._paned.set_end_child(self._original_roster)
        except Exception:
            log.exception("Could not restore original Gajim roster panel")
        finally:
            self._panel_box = None
            self._injected_box = None
            self._original_roster = None
            self._paned = None

    def _build_compact_panel(self) -> Gtk.Box:
        self._load_profiles()
        self._bots = self._build_bot_registry(self._profiles)
        self._ensure_defaults()
        self._ensure_agent_registry_file()
        self._auto_repair_agent_registry_from_live_accounts()
        self._update_bot_statuses()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("card")

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.append(title_row)

        title = Gtk.Label(label="AI Chatter")
        title.set_xalign(0)
        title.set_hexpand(True)
        title.add_css_class("heading")
        title_row.append(title)

        refresh = Gtk.Button(label="↻")
        refresh.set_tooltip_text("Обновить статус и панель")
        title_row.append(refresh)

        hint = Gtk.Label(label="✓ видно/адресую   ● online/offline")
        hint.set_xalign(0)
        hint.add_css_class("caption")
        box.append(hint)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(list_box)

        active_ids = set(self._string_list_config("active_bot_ids_text"))

        for bot in self._bots:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.set_hexpand(True)

            toggle = Gtk.CheckButton()
            toggle.set_active(bot["id"] in active_ids)
            toggle.set_tooltip_text("Показать слой этого бота и адресовать ему мои новые сообщения")
            row.append(toggle)

            status = Gtk.Label()
            try:
                status.set_markup(self._status_markup(bot))
            except Exception:
                status.set_text("●")
            status.set_tooltip_text(self._status_tooltip(bot))
            row.append(status)

            name = Gtk.Label(label=bot["title"])
            name.set_xalign(0)
            name.set_hexpand(True)
            name.set_tooltip_text(self._status_tooltip(bot))
            row.append(name)

            def on_toggle_toggled(check: Gtk.CheckButton, bot_id: str = bot["id"]) -> None:
                ids = set(self._string_list_config("active_bot_ids_text"))
                if check.get_active():
                    ids.add(bot_id)
                else:
                    ids.discard(bot_id)

                # One checkbox is now the source of truth for both visual focus
                # and default addressing. Keep the legacy can_speak field in sync
                # so older routing code keeps working.
                sorted_ids = sorted(ids)
                self._set_string_list_config("active_bot_ids_text", sorted_ids)
                self._set_string_list_config("can_speak_bot_ids_text", sorted_ids)
                self.save_config()

            toggle.connect("toggled", on_toggle_toggled)
            list_box.append(row)

        quick = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.append(quick)

        all_button = Gtk.Button(label="Все")
        none_button = Gtk.Button(label="Никого")
        quick.append(all_button)
        quick.append(none_button)

        def set_all(active: bool) -> None:
            ids = self._bot_ids() if active else []
            self._set_string_list_config("active_bot_ids_text", ids)
            self._set_string_list_config("can_speak_bot_ids_text", ids)
            self.save_config()
            self._refresh_panel()

        all_button.connect("clicked", lambda _b: set_all(True))
        none_button.connect("clicked", lambda _b: set_all(False))
        refresh.connect("clicked", lambda _b: self._refresh_panel())

        return box

    def _refresh_panel(self) -> None:
        if self._injected_box is None:
            GLib.idle_add(self._inject_chat_panel)
            return

        try:
            wrapper = self._injected_box
            old_panel = self._panel_box
            if old_panel is not None:
                wrapper.remove(old_panel)
            panel = self._build_compact_panel()
            panel.set_margin_top(8)
            panel.set_margin_bottom(8)
            panel.set_margin_start(8)
            panel.set_margin_end(8)
            wrapper.prepend(panel)
            self._panel_box = panel
        except Exception:
            log.exception("Could not refresh AI Chatter panel")

    # ------------------------------------------------------------------
    # Message routing and commands
    # ------------------------------------------------------------------
    def _normalize_jid(self, jid: object) -> str:
        text = str(jid).strip()
        if text.startswith("xmpp:"):
            text = text[5:]
        if "/" in text:
            text = text.split("/", 1)[0]
        return text.lower()

    def _event_text(self, event: object) -> str:
        try:
            message = event.message
            text = getattr(message, "text", None)
            if text is None:
                return ""
            return str(text).strip()
        except Exception:
            log.exception("Could not read event message text")
            return ""

    def _is_outgoing_event(self, event: object) -> bool:
        try:
            message = event.message
            direction = str(getattr(message, "direction", "")).lower()
            return "out" in direction or "sent" in direction
        except Exception:
            return False

    def _on_message_event(self, event: object) -> None:
        try:
            if getattr(event, "from_mam", False):
                return

            text = self._event_text(event)
            if not text:
                return
            # Ignore every diagnostic/service message produced by this plugin.
            # Older guards only matched "AI Chatter Bridge:" and missed strings like
            # "AI Chatter Bridge R2.1:", which could be routed back into ChatGPT.
            if self._is_ai_chatter_delivered_message(text):
                return

            event_jid = self._normalize_jid(getattr(event, "jid", ""))
            configured_room = self._normalize_jid(self.config["room_jid"])
            # Hard room scope: ignore every event outside the configured MUC,
            # including commands, private chats, and unrelated group chats.
            if event_jid != configured_room:
                return

            account = str(getattr(event, "account", ""))

            # Gajim delivers the same MUC message separately to every joined account.
            # Agent accounts such as vova_gpt@yax.im must be output identities only.
            # They must not answer commands and must not route incoming human text.
            if self._is_agent_account_context(account):
                return

            # R3.4.1a failsafe: circle commands must be handled before any
            # ordinary routing. This protects .круг-статус/.круг-* even if the
            # general command whitelist misses a localized token.
            if self._looks_like_circle_command(text):
                self._remember_controller_identity(account, event)
                try:
                    response = self._handle_command(text, account=account, room_jid=event_jid)
                except Exception as exc:
                    log.exception("Circle command handling failed")
                    response = f"AI Chatter Bridge R3.4.1b: ошибка обработки команды круга: {exc}"
                if response:
                    self._send_reply(account, event_jid, response)
                else:
                    self._send_reply(account, event_jid, "AI Chatter Bridge R3.4.1b: команда круга распознана, но обработчик не вернул ответ.")
                return

            # Commands are handled only from the human/controller account context.
            if self._looks_like_command(text):
                self._remember_controller_identity(account, event)
                try:
                    response = self._handle_command(text, account=account, room_jid=event_jid)
                except Exception as exc:
                    log.exception("Command handling failed")
                    response = f"AI Chatter Bridge R3.4.1b: ошибка обработки команды: {exc}"
                if response:
                    self._send_reply(account, event_jid, response)
                return

            # R1 real-agent experiment: messages sent by agent accounts
            # are already conference messages and must not be routed as new user tasks.
            if self._is_agent_sender(event):
                return

            self._remember_controller_identity(account, event)

            # When the plugin sends a message through an agent account, Gajim also
            # delivers that MUC message to the human/controller account context.
            # In that copy event.account is the human account and event.jid is the room,
            # so sender checks may not reveal the agent. Suppress exact recent agent
            # output texts before they can be routed back into ChatGPT.
            if self._is_recent_agent_output(event_jid, text):
                return

            # Own diagnostic/service output must not become tasks.
            if self._is_ai_chatter_delivered_message(text):
                return

            # R2 real-XMPP agent echo: addressed human messages are answered
            # directly from the matching agent account and must not enter
            # the Chrome/CDP queue yet. This keeps R2 XMPP-only.
            if bool(self.config["agent_echo_enabled"]):
                # R3.3: multi-address / broadcast syntax, e.g.
                #   Вова, Qwen, Сирожа: <task>
                #   всем! <task>
                # This must run before single-agent parsing, otherwise "Вова, Qwen, ..."
                # would be stolen by the first single-agent alias.
                if self._try_agent_multi_addressing(event_jid, text, account):
                    return
                if self._try_agent_echo(event_jid, text, account):
                    return

            # Safety: no dot/bang-prefixed service-like message can enter the AI task queue.
            if self._is_service_like_message(text):
                account = str(getattr(event, "account", ""))
                self._send_reply(
                    account,
                    event_jid,
                    "AI Chatter Bridge: служебная команда не распознана и не поставлена в очередь. Используйте .help",
                )
                return

            if bool(self.config["routing_enabled"]):
                # Some Gajim 2.4.x events do not expose direction in the way we expected.
                # Commands work, but normal outgoing user text may not be marked as outgoing.
                # For the configured bot-room workflow, lenient mode treats ordinary
                # non-command messages in the room as user tasks. Bridge-generated replies
                # are still ignored above by the "AI Chatter Bridge:" guard.
                if self._is_outgoing_event(event) or bool(self.config["route_any_non_command_in_room"]):
                    account = str(getattr(event, "account", ""))
                    self._simulate_routing(account, event_jid, text)
        except Exception:
            log.exception("AI Chatter Bridge message handling failed")

    def _is_service_like_message(self, text: str) -> bool:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            return False
        return lines[0].startswith(".") or lines[0].startswith("!")

    def _command_alias_map(self) -> dict[str, str]:
        """Single Russian alias for each user-facing dot command.

        English commands stay supported for compatibility, but Russian commands are
        the preferred everyday UI so the user does not need to switch keyboard
        layouts on mobile.
        """
        return {
            "!хелп": "!help",
            "!версия": "!version",
            "!пинг": "!ping",
            "!комната": "!room",
            "!конфиг": "!config",
            "!статус": "!status",
            "!боты": "!bots",
            "!окружение": "!env",
            "!пути": "!paths",
            "!проверка-окружения": "!env-check",
            "!агент-регистр": "!agent-registry",
            "!агент-регистр-шаблон": "!agent-registry-template",
            "!агент-регистр-задать": "!agent-registry-set",
            "!агент-исходящие": "!agent-outbox",
            "!агент-задачи": "!agent-jobs",
            "!агент-провайдер": "!agent-provider",
            "!агент-ожидание": "!agent-pending",
            "!агент-аккаунты": "!agent-accounts",
            "!агент-цели": "!agent-targets",
            "!участники-комнаты": "!muc-participants",
            "!участники": "!muc-participants",
            "!агент-тест": "!agent-test",
            "!агент-тест-отправки": "!agent-send-test",
            "!результаты": "!results",
            "!доставить": "!deliver",
            "!автозапуск-вкл": "!autorun-on",
            "!автозапуск-выкл": "!autorun-off",
            "!панель": "!panel",
            "!панель-вкл": "!panel-on",
            "!круг": "!circle",
            "!круг-статус": "!circle-status",
            "!круг-дальше": "!circle-next",
            "!круг-стоп": "!circle-stop",
            "!круг-ответ": "!circle-answer",
            "!шина-статус": "!bus-status",
            "!шина-пинг": "!bus-ping",
            "!шина-квен": "!bus-qwen-test",
            "!шина-дипсик": "!bus-deepseek-test",
            "!bus-status": "!bus-status",
            "!bus-ping": "!bus-ping",
            "!bus-qwen-test": "!bus-qwen-test",
            "!bus-deepseek-test": "!bus-deepseek-test",
        }

    def _known_command_tokens(self) -> set[str]:
        base = {
            "!ping", "!bots", "!room", "!config", "!env", "!paths", "!env-check", "!help", "!aichatter",
            "!show", "!speak", "!all", "!only", "!mute", "!unmute",
            "!routing", "!queue", "!results", "!deliver", "!version", "!status",
            "!agent-accounts", "!agent-targets", "!muc-participants", "!agent-test", "!agent-send-test", "!agent-registry", "!agent-registry-set", "!agent-registry-template", "!agent-outbox", "!agent-jobs", "!agent-provider", "!agent-pending", "!autorun-on", "!autorun-off",
            "!circle", "!circle-status", "!circle-next", "!circle-answer", "!circle-stop",
            "!bus-status", "!bus-ping", "!bus-qwen-test", "!bus-deepseek-test",
            "!panel", "!panel-on",
        }
        return base | set(self._command_alias_map().keys())

    def _looks_like_circle_command(self, text: str) -> bool:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            return False
        first = lines[0].lower()
        # Normalize common command prefix variants to the internal bang form.
        if first.startswith("."):
            first = "!" + first[1:]
        token = first.split(maxsplit=1)[0]
        return token in (
            "!круг", "!круг-статус", "!круг-дальше", "!круг-ответ", "!круг-стоп",
            "!circle", "!circle-status", "!circle-next", "!circle-answer", "!circle-stop",
        )

    def _looks_like_command(self, text: str) -> bool:
        lines = [line.strip() for line in str(text).splitlines() if line.strip()]
        if not lines:
            return False
        first = lines[0].lower()
        if first.startswith("."):
            first = "!" + first[1:]
        cmd = first.split(maxsplit=1)[0]
        return cmd in self._known_command_tokens()

    def _canonical_command(self, text: str) -> str:
        raw = str(text or "")
        lines = raw.splitlines()
        # Preserve multiline command bodies, especially .agent-registry-set.
        first_index = None
        for i, line in enumerate(lines):
            if line.strip():
                first_index = i
                break
        if first_index is None:
            return ""
        first_line = lines[first_index].strip()
        if first_line.startswith("."):
            first_line = "!" + first_line[1:]
        if not first_line.startswith("!"):
            return first_line
        token, sep, rest = first_line.partition(" ")
        mapped = self._command_alias_map().get(token.lower(), token)
        new_first = mapped + (sep + rest if sep else "")
        lines[first_index] = new_first
        return "\n".join(lines[first_index:])

    def _parse_bot_args(self, text: str) -> list[str]:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return []
        raw = parts[1].replace(";", ",").replace(" ", ",")
        return [item.strip().lower() for item in raw.split(",") if item.strip()]

    def _agent_jids(self) -> set[str]:
        out: set[str] = set()

        # R2.3 primary source: persistent agent registry file.
        try:
            for entry in self._agent_registry():
                jid = self._normalize_jid(str(entry.get("jid", "")))
                if jid:
                    out.add(jid)
        except Exception:
            pass

        # Legacy/output-only guard source kept for compatibility with older configs.
        try:
            raw = str(self.config["agent_accounts_text"] or "")
        except Exception:
            raw = "vova_gpt@yax.im=chatgpt\nai_chatter_bot@yax.im=chatgpt"
        for item in raw.replace("\n", ",").split(","):
            item = item.strip()
            if not item:
                continue
            jid = item.split("=", 1)[0].strip()
            if jid:
                out.add(self._normalize_jid(jid))
        return out

    def _is_agent_account_context(self, account: str) -> bool:
        """True when this event is being processed in an agent account context.

        In MUCs, one human message is delivered once per joined Gajim account.
        If the plugin handles that copy for vova_gpt@yax.im, Vova replies to
        commands and routes tasks again. This guard makes real agents output-only.
        """
        if not account:
            return False
        agent_jids = self._agent_jids()
        if not agent_jids:
            return False

        account_norm = self._normalize_jid(account)
        account_jid = self._normalize_jid(self._account_jid(account))
        candidates = [value for value in (account_norm, account_jid) if value]

        for value in candidates:
            if value in agent_jids:
                return True
            local = value.split("@", 1)[0] if "@" in value else value
            for agent_jid in agent_jids:
                agent_local = agent_jid.split("@", 1)[0]
                if local == agent_local:
                    return True
        return False

    def _is_agent_sender(self, event: object) -> bool:
        candidates: list[str] = []

        # For outgoing MUC messages sent by a secondary Gajim account, event.jid is
        # usually the room JID, not the sender JID. The reliable signal is event.account
        # -> account bare JID, so check it before nick/from-style fields.
        try:
            account = str(getattr(event, "account", "") or "")
            if account:
                candidates.append(account)
                account_jid = self._account_jid(account)
                if account_jid:
                    candidates.append(account_jid)
        except Exception:
            pass

        for attr in ("jid", "from_", "from_jid", "remote_jid", "fjid", "nick", "resource"):
            try:
                value = getattr(event, attr, None)
                if value:
                    candidates.append(str(value))
            except Exception:
                pass

        try:
            stanza = getattr(event, "stanza", None)
            if stanza is not None:
                value = stanza.getFrom()
                if value:
                    candidates.append(str(value))
        except Exception:
            pass

        agent_jids = self._agent_jids()
        if not agent_jids:
            return False

        for value in candidates:
            norm = self._normalize_jid(value)
            # MUC sender may look like room@conference/nick or full JID.
            if norm in agent_jids:
                return True
            resource = norm.rsplit("/", 1)[-1] if "/" in norm else norm
            if resource in agent_jids:
                return True
            local = resource.split("@", 1)[0] if "@" in resource else resource
            for agent_jid in agent_jids:
                agent_local = agent_jid.split("@", 1)[0]
                if local == agent_local or resource == agent_local:
                    return True
        return False

    def _available_accounts(self) -> list[str]:
        accounts: list[str] = []
        try:
            accounts.extend(list(app.settings.get_accounts()))
        except Exception:
            pass
        try:
            accounts.extend(list(app.get_accounts()))
        except Exception:
            pass

        # preserve order, remove duplicates
        out: list[str] = []
        seen: set[str] = set()
        for item in accounts:
            value = str(item)
            if value and value not in seen:
                out.append(value)
                seen.add(value)
        return out

    def _account_jid(self, account: str) -> str:
        """Best-effort bare JID for a Gajim account identifier.

        Gajim exposes different account identifiers on different machines
        (for example yax.im1/yax.im2, display names, or the bare JID itself).
        Keep this resolver broad so agent registry records do not need to store
        local internal account names by hand.
        """
        account = str(account or "").strip()
        if not account:
            return ""
        for getter in (
            lambda: app.get_jid_from_account(account),
            lambda: app.settings.get_account_setting(account, "address"),
            lambda: app.settings.get_account_setting(account, "jid"),
            lambda: app.settings.get_account_setting(account, "name"),
        ):
            try:
                value = getter()
                if value:
                    return self._normalize_jid(str(value))
            except Exception:
                pass
        try:
            client = app.get_client(account)
        except Exception:
            client = None
        if client is not None:
            for attr in ("jid", "boundjid", "bound_jid", "own_jid"):
                try:
                    value = getattr(client, attr, None)
                    if value:
                        text = str(getattr(value, "bare", value))
                        if text:
                            return self._normalize_jid(text)
                except Exception:
                    pass
            for meth in ("get_own_jid", "get_bound_jid"):
                try:
                    fn = getattr(client, meth, None)
                    if callable(fn):
                        value = fn()
                        text = str(getattr(value, "bare", value))
                        if text:
                            return self._normalize_jid(text)
                except Exception:
                    pass
        if "@" in account:
            return self._normalize_jid(account)
        return ""

    def _find_account_for_jid_or_name(self, needle: str) -> str:
        return self._find_account_for_agent_ref(needle, None)

    def _agent_ref_match_values(self, entry: dict[str, object] | None, extra: str = "") -> set[str]:
        values: set[str] = set()

        def add(value: object) -> None:
            raw = str(value or "").strip().lstrip("@").strip()
            if not raw:
                return
            values.add(raw.casefold())
            norm = self._normalize_jid(raw)
            if norm:
                values.add(norm.casefold())
                if "@" in norm:
                    values.add(norm.split("@", 1)[0].casefold())

        add(extra)
        if entry:
            add(entry.get("id"))
            add(entry.get("jid"))
            add(entry.get("account"))
            add(entry.get("provider"))
            for alias in entry.get("aliases", []) or []:
                add(alias)
        return values

    def _find_account_for_agent_ref(self, needle: str = "", entry: dict[str, object] | None = None) -> str:
        """Resolve an agent registry entry to the best local Gajim account.

        R3.4.3: do not return the first fuzzy alias match.  Prefer exact
        registry JID/account matches, then prefer available/usable accounts.
        This avoids a home registry entry like qwen_agent@yax.im accidentally
        resolving to an old/offline qwen_qwen@yax.im account merely because both
        contain the word "qwen".
        """
        entry = entry or {}
        needle_norm = self._normalize_jid(str(needle or ""))
        registry_jid = self._normalize_jid(str(entry.get("jid") or needle_norm or ""))
        account_hint = str(entry.get("account") or "").strip()

        # 1) Exact pass: account id or account JID must equal the requested JID/hint.
        exact_targets = {x.casefold() for x in (needle_norm, registry_jid, account_hint) if x}
        for account in self._available_accounts():
            account_text = str(account or "")
            account_jid = self._account_jid(account_text)
            account_candidates = {
                account_text.casefold(),
                self._normalize_jid(account_text).casefold(),
                self._normalize_jid(account_jid).casefold(),
            }
            if exact_targets & {x for x in account_candidates if x}:
                return account_text

        # 2) Scored fuzzy pass.  Provider/id/alias/localpart matches are allowed,
        # but an available/usable account beats an unavailable stale match.
        targets = self._agent_ref_match_values(entry, needle)
        if not targets:
            return str(needle or "")

        best_account = ""
        best_score = -1
        for account in self._available_accounts():
            account_text = str(account or "")
            account_jid = self._account_jid(account_text)
            account_values: set[str] = set()
            account_values.update(self._agent_ref_match_values(None, account_text))
            account_values.update(self._agent_ref_match_values(None, account_jid))

            score = 0
            account_norm = self._normalize_jid(account_text).casefold()
            jid_norm = self._normalize_jid(account_jid).casefold()
            reg_norm = self._normalize_jid(registry_jid).casefold()

            if reg_norm and jid_norm == reg_norm:
                score += 1000
            if account_hint and account_norm == self._normalize_jid(account_hint).casefold():
                score += 900
            if targets & account_values:
                score += 100
            if self._account_is_usable(account_text):
                score += 50
            else:
                score -= 25

            # Mild localpart similarity fallback.
            haystack = " ".join([account_norm, jid_norm])
            for target in targets:
                if target and target in haystack:
                    score += 5

            if score > best_score:
                best_score = score
                best_account = account_text

        if best_account and best_score > 0:
            return best_account
        return str(needle or entry.get("jid") or "")

    def _account_is_usable(self, account: str) -> bool:
        """Best-effort account usability check for Gajim API differences.

        app.account_is_available() is authoritative when it returns True, but
        on some installations it can be False for accounts that still have a
        client object and can be used.  Treat a visible account with a client/JID
        as usable for routing diagnostics and job creation; actual send errors
        are still reported by _send_reply_detailed.
        """
        account = str(account or "").strip()
        if not account:
            return False
        try:
            if app.account_is_available(account):
                return True
        except Exception:
            pass
        try:
            if account in self._available_accounts() and self._account_jid(account):
                try:
                    client = app.get_client(account)
                    if client is not None:
                        return True
                except Exception:
                    return True
        except Exception:
            pass
        return False

    def _format_agent_accounts_probe(self) -> str:
        lines = ["AI Chatter Bridge R3.4.3: XMPP accounts visible to plugin"]
        accounts = self._available_accounts()
        if not accounts:
            return "AI Chatter Bridge R1: аккаунты через app.settings/app.get_accounts не найдены."

        for account in accounts:
            jid = self._account_jid(account)
            try:
                available = self._account_is_usable(account)
            except Exception:
                available = False
            lines.append(f"- account={account}; jid={jid or '?'}; available={available}")

        lines.append("")
        lines.append("Тест отправки:")
        lines.append(".agent-test vova_gpt@yax.im Привет от агента")
        return "\n".join(lines)

    def _remember_agent_output(
        self,
        room_jid: str,
        text: str,
        *,
        job_id: str = "",
        stage: str = "",
        kind: str = "",
        agent_id: str = "",
        provider: str = "",
        agent_jid: str = "",
        agent_account: str = "",
        input_text: str = "",
    ) -> None:
        """Remember an outgoing agent message so its MUC echo can be verified.

        Gajim reports API send success before the user can necessarily see the
        message in the room. R3.1.1 records send_called first and then appends
        visible_in_muc only after the same text returns through the room event
        stream. This also preserves the old loop guard behaviour.
        """
        room = self._normalize_jid(room_jid)
        normalized_text = " ".join((text or "").strip().split())
        if not room or not normalized_text:
            return
        now = time.time()
        kept: list[dict[str, object]] = []
        for item in getattr(self, "_recent_agent_outputs", []):
            try:
                if now - float(item.get("ts", 0)) <= 60.0:
                    kept.append(item)
            except Exception:
                continue
        kept.append({
            "ts": now,
            "room": room,
            "text": normalized_text,
            "jobId": job_id,
            "stage": stage,
            "kind": kind,
            "agentId": agent_id,
            "provider": provider,
            "agentJid": agent_jid,
            "agentAccount": agent_account,
            "inputText": input_text,
            "replyText": text,
        })
        self._recent_agent_outputs = kept

    def _is_recent_agent_output(self, room_jid: str, text: str) -> bool:
        room = self._normalize_jid(room_jid)
        normalized_text = " ".join((text or "").strip().split())
        if not room or not normalized_text:
            return False
        now = time.time()
        kept: list[dict[str, object]] = []
        matched_item: dict[str, object] | None = None
        for item in getattr(self, "_recent_agent_outputs", []):
            try:
                ts = float(item.get("ts", 0))
            except Exception:
                continue
            if now - ts > 60.0:
                # If send was called but no room echo was observed within the window,
                # leave a diagnostic outbox record when we have metadata.
                if item.get("jobId"):
                    try:
                        stale = dict(item)
                        stale.update({
                            "id": str(item.get("jobId") or ""),
                            "status": "send_called_no_muc_echo",
                            "checkedAt": datetime.now(timezone.utc).isoformat(),
                            "roomJid": str(item.get("room") or room),
                        })
                        self._append_agent_outbox_record(stale)
                    except Exception:
                        pass
                continue
            if str(item.get("room") or "") == room and str(item.get("text") or "") == normalized_text:
                matched_item = item
                # one-shot consume: this is the agent's echoed MUC event
                continue
            kept.append(item)
        self._recent_agent_outputs = kept
        if matched_item is not None:
            if matched_item.get("jobId"):
                try:
                    visible = {
                        "id": str(matched_item.get("jobId") or ""),
                        "stage": str(matched_item.get("stage") or "r3.1.1"),
                        "kind": str(matched_item.get("kind") or "agent_visible_delivery_verification"),
                        "status": "visible_in_muc",
                        "visibleAt": datetime.now(timezone.utc).isoformat(),
                        "roomJid": room,
                        "agentId": str(matched_item.get("agentId") or ""),
                        "provider": str(matched_item.get("provider") or ""),
                        "agentJid": str(matched_item.get("agentJid") or ""),
                        "agentAccount": str(matched_item.get("agentAccount") or ""),
                        "inputText": str(matched_item.get("inputText") or ""),
                        "replyText": str(matched_item.get("replyText") or text),
                    }
                    self._append_agent_outbox_record(visible)
                    job = dict(visible)
                    job["kind"] = "agent_visible_delivery_verification"
                    self._append_agent_job_record(job)
                except Exception:
                    log.exception("Could not append visible_in_muc verification record")
            return True
        return False

    def _send_agent_probe_message(self, agent_ref: str, room_jid: str, text: str) -> str:
        agent_account = self._find_account_for_jid_or_name(agent_ref)
        resolved_jid = self._account_jid(agent_account)

        if not self._account_is_usable(agent_account):
            return (
                "AI Chatter Bridge R1: agent account недоступен.\n"
                f"- requested: {agent_ref}\n"
                f"- resolved account: {agent_account}\n"
                f"- resolved jid: {resolved_jid or '?'}"
            )

        room = room_jid or str(self.config["room_jid"]) or str(self.config["agent_probe_default_room"])
        self._remember_agent_output(room, text)
        ok = self._send_reply(agent_account, room, text)
        return (
            "AI Chatter Bridge R1: agent-test выполнен.\n"
            f"- requested: {agent_ref}\n"
            f"- resolved account: {agent_account}\n"
            f"- resolved jid: {resolved_jid or '?'}\n"
            f"- room: {room}\n"
            f"- sent: {ok}\n"
            "Если всё хорошо, сообщение выше/ниже должно появиться от agent-аккаунта, а не от основного пользователя."
        )

    def _configs_dir_path(self) -> Path:
        """Return the single project-level configs directory.

        R3.4.6 rule: all AI Chatter settings shared by the Gajim plugin,
        executor, and bridge live under <AI_CHATTER_HOME>/configs.  Chrome
        extension settings remain inside Chrome/extension storage.
        """
        try:
            home = Path(self._detect_ai_chatter_home())
            if str(home).strip():
                return home / "configs"
        except Exception:
            pass
        return Path(self.local_file_path("configs"))

    def _home_configs_path(self, name: str, *, migrate_from_root: bool = True) -> Path:
        raw = str(name or "").strip()
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        configs_dir = self._configs_dir_path()
        target = configs_dir / (candidate.name if len(candidate.parts) <= 1 else Path(*candidate.parts[1:]) if candidate.parts[0].casefold() == "configs" else candidate)
        if migrate_from_root:
            try:
                home = Path(self._detect_ai_chatter_home())
                old = home / candidate.name
                if old.exists() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(old.read_text(encoding="utf-8-sig"), encoding="utf-8")
                    log.warning("AI Chatter R3.4.6 migrated config %s -> %s", old, target)
            except Exception:
                log.exception("Could not migrate config file %s into configs/", raw)
        return target

    def _agent_registry_path(self) -> Path:
        name = str(self.config["agent_registry_file_name"]).strip() or "configs/ai_chatter_agent_registry.json"
        return self._home_configs_path(name)

    def _default_agent_registry_payload(self) -> dict[str, object]:
        return {
            "schema": "ai_chatter.agent_registry.v1",
            "stage": "r2.6",
            "agents": {
                "vova": {
                    "jid": "vova_gpt@yax.im",
                    "aliases": ["Вова", "vova", "chatgpt", "vova_gpt"],
                    "enabled": True,
                    "provider": "chatgpt",
                },
                "qwen": {
                    "jid": "qwen_qwen@yax.im",
                    "aliases": ["Qwen", "Квен", "qwen", "qwen_qwen", "qwen_agent"],
                    "enabled": True,
                    "provider": "qwen",
                },
                "deepseek": {
                    "jid": "siroza_deepseek@yax.im",
                    "aliases": ["DeepSeek", "Дипсик", "Сирожа", "deepseek", "siroza_deepseek", "deepseek_agent"],
                    "enabled": True,
                    "provider": "deepseek",
                },
            },
        }

    def _ensure_agent_registry_file(self) -> None:
        path = self._agent_registry_path()
        default_payload = self._default_agent_registry_payload()
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(default_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                log.exception("Could not create agent registry file at %s", path)
            return

        # R2.4 migration: preserve the user's existing registry and only add
        # missing disabled placeholders for future agents. Do not overwrite Vova.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            agents = data.get("agents")
            if not isinstance(agents, dict):
                return
            changed = False
            for agent_id in ("qwen", "deepseek"):
                if agent_id not in agents:
                    agents[agent_id] = default_payload["agents"][agent_id]
                    changed = True
            if data.get("stage") != "r2.6":
                data["stage"] = "r2.6"
                changed = True
            if changed:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.exception("Could not migrate agent registry file at %s", path)


    def _auto_repair_agent_registry_from_live_accounts(self) -> None:
        """R3.4.5: keep the registry from being reset to stale disabled rows.

        Older settings dialogs and copied home/work registries could leave qwen
        and deepseek as enabled=false or with stale JIDs such as
        qwen_agent@yax.im.  If Gajim currently exposes a usable account that
        matches an agent by id/provider/alias/JID, treat that as the source of
        truth and persist the resolved JID.  This makes moving between home and
        work profiles stable without hunting for internal account names.
        """
        if getattr(self, "_registry_auto_repair_running", False):
            return
        self._registry_auto_repair_running = True
        path = self._agent_registry_path()
        try:
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            agents = data.get("agents")
            if not isinstance(agents, dict):
                return

            changed = False
            for agent_id, item in list(agents.items()):
                if not isinstance(item, dict):
                    continue
                entry = {
                    "id": str(agent_id),
                    "jid": str(item.get("jid") or ""),
                    "aliases": list(item.get("aliases") or []),
                    "provider": str(item.get("provider") or agent_id),
                    "account": str(item.get("account") or ""),
                    "enabled": bool(item.get("enabled", True)),
                }

                account = self._find_account_for_agent_ref(str(entry.get("jid") or agent_id), entry)
                resolved_jid = self._account_jid(account) if account else ""
                usable = bool(account and self._account_is_usable(account))
                if not usable:
                    continue

                # If an agent is present/usable, do not keep it disabled because
                # of a stale copied registry or old settings-window template.
                if item.get("enabled") is not True:
                    item["enabled"] = True
                    changed = True

                # Prefer the real local account JID over stale copied JIDs.
                if resolved_jid and self._normalize_jid(str(item.get("jid") or "")) != resolved_jid:
                    item["jid"] = resolved_jid
                    changed = True

                # Store account as a cache only; resolver will still re-check.
                if account and str(item.get("account") or "") != str(account):
                    item["account"] = str(account)
                    changed = True

                aliases = list(item.get("aliases") or [])
                local = resolved_jid.split("@", 1)[0] if resolved_jid else ""
                for alias in (str(agent_id), str(entry.get("provider") or ""), local):
                    alias = alias.strip()
                    if alias and alias.casefold() not in [str(x).casefold() for x in aliases]:
                        aliases.append(alias)
                        changed = True
                item["aliases"] = aliases

            if changed:
                data["stage"] = "r3.4.5-auto-repaired"
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                log.warning("AI Chatter Bridge R3.4.5 auto-repaired agent registry at %s", path)
        except Exception:
            log.exception("Could not auto-repair agent registry from live accounts")
        finally:
            self._registry_auto_repair_running = False

    def _agent_registry_editor_text(self) -> str:
        """Return a compact editable text form for the settings dialog.

        Format per line:
        id | enabled | jid | provider | alias1, alias2, alias3
        """
        try:
            entries = self._agent_registry_from_file(include_disabled=True)
        except Exception:
            entries = []
        if not entries:
            defaults = self._default_agent_registry_payload().get("agents", {})
            entries = []
            if isinstance(defaults, dict):
                for key, item in defaults.items():
                    if isinstance(item, dict):
                        aliases = item.get("aliases", [])
                        if isinstance(aliases, list):
                            alias_text = ", ".join(str(x) for x in aliases)
                        else:
                            alias_text = str(aliases or "")
                        entries.append({
                            "id": key,
                            "enabled": bool(item.get("enabled", True)),
                            "jid": str(item.get("jid", "")),
                            "account": str(item.get("account", "")),
                            "provider": str(item.get("provider", key)),
                            "aliases": [x.strip() for x in alias_text.split(",") if x.strip()],
                        })

        lines = [
            "# Формат: id | enabled | jid | provider | aliases через запятую | optional account",
            "# Пример: vova | true | vova_gpt@yax.im | chatgpt | Вова, vova, chatgpt | yax.im1",
        ]
        for entry in entries:
            aliases = ", ".join(str(x) for x in entry.get("aliases", []))
            enabled = "true" if bool(entry.get("enabled", True)) else "false"
            account = str(entry.get("account", "") or "")
            suffix = f" | {account}" if account else ""
            lines.append(
                f"{entry.get('id', '')} | {enabled} | {entry.get('jid', '')} | {entry.get('provider', '')} | {aliases}{suffix}"
            )
        return "\n".join(lines)

    def _parse_bool_text(self, value: str, default: bool = True) -> bool:
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "on", "да", "вкл", "enabled"}:
            return True
        if text in {"0", "false", "no", "n", "off", "нет", "выкл", "disabled"}:
            return False
        return default

    def _save_agent_registry_from_editor_text(self, text: str) -> tuple[bool, str]:
        """Persist the agent registry edited in the plugin settings dialog."""
        agents: dict[str, dict[str, object]] = {}
        errors: list[str] = []

        for lineno, raw_line in enumerate(str(text or "").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|", 5)]
            if len(parts) < 5:
                errors.append(f"line {lineno}: expected 5 or 6 pipe-separated fields")
                continue
            agent_id, enabled_text, jid, provider, aliases_text = parts[:5]
            account_hint = parts[5].strip() if len(parts) >= 6 else ""
            agent_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", agent_id.strip())
            jid = self._normalize_jid(jid)
            provider = provider.strip() or agent_id
            if not agent_id:
                errors.append(f"line {lineno}: empty agent id")
                continue
            if not jid or "@" not in jid:
                errors.append(f"line {lineno}: invalid jid")
                continue
            aliases: list[str] = []
            for alias in re.split(r"[,|/]+", aliases_text):
                alias = alias.strip().lstrip("@").strip()
                if alias and alias.lower() not in [x.lower() for x in aliases]:
                    aliases.append(alias)
            local = jid.split("@", 1)[0]
            if local and local.lower() not in [x.lower() for x in aliases]:
                aliases.append(local)

            agent_record = {
                "jid": jid,
                "aliases": aliases,
                "enabled": self._parse_bool_text(enabled_text, default=True),
                "provider": provider,
            }
            if account_hint:
                agent_record["account"] = account_hint
            agents[agent_id] = agent_record

        if not agents:
            return False, "agent registry not saved: no valid agents"

        payload = {
            "schema": "ai_chatter.agent_registry.v1",
            "stage": "r2.6",
            "agents": agents,
        }
        path = self._agent_registry_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.exception("Could not save agent registry file at %s", path)
            return False, f"agent registry not saved: {exc}"

        if errors:
            return True, "agent registry saved with skipped lines: " + "; ".join(errors[:3])
        return True, f"agent registry saved: {path}"

    def _agent_registry_from_file(self, include_disabled: bool = False) -> list[dict[str, object]]:
        if not getattr(self, "_registry_auto_repair_running", False):
            self._auto_repair_agent_registry_from_live_accounts()
        path = self._agent_registry_path()
        if not path.exists():
            self._ensure_agent_registry_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        agents = data.get("agents", {}) if isinstance(data, dict) else {}
        entries: list[dict[str, object]] = []

        if isinstance(agents, dict):
            iterable = agents.items()
        elif isinstance(agents, list):
            iterable = [(str(i), item) for i, item in enumerate(agents)]
        else:
            iterable = []

        for key, item in iterable:
            if not isinstance(item, dict):
                continue
            enabled = bool(item.get("enabled", True))
            if not enabled and not include_disabled:
                continue
            jid = self._normalize_jid(str(item.get("jid", "")))
            if not jid or "@" not in jid:
                continue
            aliases_raw = item.get("aliases", [])
            aliases: list[str] = []
            if isinstance(aliases_raw, str):
                aliases_iter = re.split(r"[,|/]+", aliases_raw)
            elif isinstance(aliases_raw, list):
                aliases_iter = aliases_raw
            else:
                aliases_iter = []
            for alias_item in aliases_iter:
                alias = str(alias_item).strip().lstrip("@").strip()
                if alias and alias.lower() not in [x.lower() for x in aliases]:
                    aliases.append(alias)
            local = jid.split("@", 1)[0]
            if local and local.lower() not in [x.lower() for x in aliases]:
                aliases.append(local)
            registry_id = str(item.get("id") or key or local)
            provider = str(item.get("provider") or registry_id or local)
            entries.append({
                "id": registry_id,
                "jid": jid,
                "account": str(item.get("account") or ""),
                "aliases": aliases,
                "provider": provider,
                "enabled": enabled,
                "source": str(path),
            })
        return entries

    def _agent_registry_from_legacy_text(self) -> list[dict[str, object]]:
        try:
            raw = str(self.config["agent_registry_text"] or "")
        except Exception:
            raw = ""
        if not raw.strip():
            return []

        entries: list[dict[str, object]] = []
        for line in raw.replace(";", "\n").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "=" in line:
                left, right = [part.strip() for part in line.split("=", 1)]
                if "@" in left:
                    jid = left
                    aliases_raw = right
                else:
                    jid = right
                    aliases_raw = left
            else:
                jid = line
                aliases_raw = line.split("@", 1)[0]

            jid_norm = self._normalize_jid(jid)
            if not jid_norm or "@" not in jid_norm:
                continue

            aliases: list[str] = []
            for alias in re.split(r"[,|/]+", aliases_raw):
                alias = alias.strip().lstrip("@").strip()
                if alias and alias.lower() not in [item.lower() for item in aliases]:
                    aliases.append(alias)

            local = jid_norm.split("@", 1)[0]
            if local and local.lower() not in [item.lower() for item in aliases]:
                aliases.append(local)

            entries.append({"id": local, "jid": jid_norm, "aliases": aliases, "provider": local, "enabled": True, "source": "legacy agent_registry_text"})
        return entries

    def _agent_registry(self) -> list[dict[str, object]]:
        """Return enabled R2.6 real-XMPP agents from persistent JSON registry.

        Primary file: C:\AI_chatter\ai_chatter_agent_registry.json
        Fallback: legacy agent_registry_text for compatibility.
        """
        try:
            entries = self._agent_registry_from_file()
            if entries:
                return entries
        except Exception:
            log.exception("Could not load file agent registry")
        return self._agent_registry_from_legacy_text()

    def _format_agent_registry(self) -> str:
        path = self._agent_registry_path()
        lines = ["AI Chatter Bridge R2.6: configurable file agent registry", f"- файл: {path}"]
        try:
            entries = self._agent_registry_from_file(include_disabled=True)
        except Exception:
            entries = self._agent_registry()
        if not entries:
            return "AI Chatter Bridge R2.6: agent registry пуст. Настройте ai_chatter_agent_registry.json или настройки плагина."
        for entry in entries:
            jid = str(entry.get("jid", ""))
            aliases = ", ".join(str(x) for x in entry.get("aliases", []))
            provider = str(entry.get("provider", ""))
            account = self._find_account_for_agent_ref(jid, entry)
            resolved_jid = self._account_jid(account)
            try:
                available = self._account_is_usable(account)
            except Exception:
                available = False
            lines.append(
                f"- id={entry.get('id')}; enabled={entry.get('enabled')}; jid={jid}; provider={provider}; aliases={aliases}; "
                f"account={account}; resolved_jid={resolved_jid or '?'}; available={available}"
            )
        lines.append("")
        lines.append("Команды: .agent-outbox, .agent-jobs, .agent-registry, .agent-registry-set, .agent-accounts")
        lines.append("R3.4.6: configs/ — единая папка настроек проекта; registry/executor config/profiles лежат в AI Chatter home/configs/.")
        return "\n".join(lines)


    def _agent_registry_template_text(self) -> str:
        return "\n".join([
            "# Формат: id | enabled | jid | provider | aliases через запятую | optional account",
            "vova | true | vova_gpt@yax.im | chatgpt | Вова, vova, chatgpt, vova_gpt",
            "qwen | true | qwen_qwen@yax.im | qwen | Qwen, Квен, qwen_agent",
            "deepseek | true | siroza_deepseek@yax.im | deepseek | Сирожа, DeepSeek, Дипсик, deepseek_agent",
        ])

    def _format_agent_registry_set_help(self) -> str:
        return (
            "AI Chatter Bridge R2.6.3: используйте многострочную команду:\n"
            ".agent-registry-set\n"
            + self._agent_registry_template_text()
        )

    def _handle_agent_registry_set_command(self, cmd: str) -> str:
        # Accept a multiline chat command. This bypasses the settings-window save path
        # and writes the same JSON registry file used by runtime matching.
        text = str(cmd or "")
        parts = text.splitlines()
        if not parts:
            return self._format_agent_registry_set_help()
        first = parts[0]
        rest_lines = parts[1:]
        if not rest_lines:
            # Also allow one-line payload after the command name, but the intended
            # workflow is multiline because there are several agents.
            bits = first.split(maxsplit=1)
            if len(bits) > 1:
                rest_lines = [bits[1]]
        payload = "\n".join(rest_lines).strip()
        if not payload:
            return self._format_agent_registry_set_help()
        ok, message = self._save_agent_registry_from_editor_text(payload)
        # Persist the raw editor text too, so reopening the plugin settings has a
        # fallback source even if the JSON file was deleted later.
        try:
            self.config["agent_registry_text"] = payload
            self.save_config()
        except Exception:
            log.exception("Could not persist agent_registry_text after .agent-registry-set")
        if not ok:
            return "AI Chatter Bridge R2.6.3: " + message
        return (
            "AI Chatter Bridge R2.6.3: agent registry сохранён.\n"
            f"- файл: {self._agent_registry_path()}\n"
            + self._format_agent_registry()
        )

    def _match_agent_addressing(self, text: str, include_disabled: bool = False) -> tuple[dict[str, object], str] | None:
        """Return (agent_entry, remainder) when text starts with a known agent alias.

        R2.6 intentionally looks at disabled agents too, so the plugin can
        acknowledge "Qwen, ..." / "DeepSeek, ..." without creating jobs.
        """
        raw = (text or "").strip()
        if not raw:
            return None

        try:
            entries = self._agent_registry_from_file(include_disabled=include_disabled)
        except Exception:
            entries = self._agent_registry()

        for entry in entries:
            aliases = [str(alias).strip() for alias in entry.get("aliases", []) if str(alias).strip()]
            # Longer aliases first prevents "vova" from stealing "vova_gpt".
            aliases.sort(key=len, reverse=True)
            for alias in aliases:
                pattern = r"^\s*@?" + re.escape(alias) + r"(?:\s*[,;:：\-—–]\s*|\s+)(.*)$"
                match = re.match(pattern, raw, flags=re.IGNORECASE | re.UNICODE | re.DOTALL)
                if match:
                    remainder = match.group(1).strip()
                    return entry, remainder

                # Allow a bare alias as a liveness test.
                bare_pattern = r"^\s*@?" + re.escape(alias) + r"\s*$"
                if re.match(bare_pattern, raw, flags=re.IGNORECASE | re.UNICODE):
                    return entry, ""
        return None

    def _agent_alias_key(self, value: str) -> str:
        return str(value or "").strip().lstrip("@").casefold()

    def _agent_alias_lookup(self, include_disabled: bool = True) -> dict[str, dict[str, object]]:
        try:
            entries = self._agent_registry_from_file(include_disabled=include_disabled)
        except Exception:
            entries = self._agent_registry()
        lookup: dict[str, dict[str, object]] = {}
        for entry in entries:
            keys = [str(entry.get("id", "")), str(entry.get("jid", ""))]
            keys.extend(str(a) for a in entry.get("aliases", []) if str(a).strip())
            for key in keys:
                normalized = self._agent_alias_key(key)
                if normalized:
                    lookup[normalized] = entry
        return lookup

    def _enabled_agent_entries(self) -> list[dict[str, object]]:
        try:
            entries = self._agent_registry_from_file(include_disabled=True)
        except Exception:
            entries = self._agent_registry()
        return [entry for entry in entries if bool(entry.get("enabled", True))]

    def _agent_entry_match_keys(self, entry: dict[str, object]) -> set[str]:
        """Keys used to match registry agents to live MUC occupants.

        Gajim may expose a participant as a real JID, bare JID, account name,
        resource, or just a visible nick. Keep this matcher deliberately broad,
        but only match against enabled registry agents so the human/controller
        occupant is never counted as an executor target.
        """
        keys: set[str] = set()

        def add(value: object) -> None:
            raw = str(value or "").strip().lstrip("@").strip()
            if not raw:
                return
            keys.add(raw.casefold())
            norm = self._normalize_jid(raw)
            if norm:
                keys.add(norm.casefold())
                if "@" in norm:
                    keys.add(norm.split("@", 1)[0].casefold())

        add(entry.get("id"))
        add(entry.get("jid"))
        add(entry.get("provider"))
        for alias in entry.get("aliases", []) or []:
            add(alias)
        account = self._find_account_for_agent_ref(str(entry.get("jid") or ""), entry)
        if account:
            add(account)
            add(self._account_jid(account))
        return keys

    def _occupant_match_keys(self, occupant: object) -> set[str]:
        keys: set[str] = set()

        def add(value: object) -> None:
            raw = str(value or "").strip().lstrip("@").strip()
            if not raw:
                return
            keys.add(raw.casefold())
            norm = self._normalize_jid(raw)
            if norm:
                keys.add(norm.casefold())
                if "@" in norm:
                    keys.add(norm.split("@", 1)[0].casefold())

        if isinstance(occupant, dict):
            values = occupant.values()
        else:
            values = []
        for value in values:
            if isinstance(value, (str, int)):
                add(value)

        for attr in (
            "jid", "real_jid", "bare_jid", "address", "name", "nick", "nickname",
            "resource", "jid_resource", "account", "pk", "id",
        ):
            try:
                value = getattr(occupant, attr, None)
            except Exception:
                value = None
            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None
            add(value)

        try:
            add(str(occupant))
        except Exception:
            pass
        return keys

    def _remember_controller_identity(self, account: str = "", event: object | None = None) -> None:
        """Remember keys that identify the human/controller in the current MUC.

        Some Gajim APIs do not expose the user's own bare JID through account
        settings, while MUC occupants still expose the real JID/nick.  Record
        identity hints from actual human events so duplicate resources such as
        Gajim + Miranda are treated as the same owner.
        """
        def add(value: object) -> None:
            raw = str(value or "").strip().lstrip("@").strip()
            if not raw:
                return
            norm = self._normalize_jid(raw)
            candidates = [raw.casefold()]
            if norm:
                candidates.append(norm.casefold())
                if "@" in norm:
                    candidates.append(norm.split("@", 1)[0].casefold())
            for item in candidates:
                # Avoid poisoning self keys with generic domain/account names such
                # as "yax.im" that appear in every occupant string.
                if item and item not in {"yax.im", "chat.yax.im"}:
                    self._controller_identity_keys.add(item)

        if account and not self._is_agent_account_context(account):
            add(account)
            try:
                add(self._account_jid(account))
            except Exception:
                pass
        if event is not None:
            for attr in ("from_", "from_jid", "remote_jid", "fjid", "nick", "resource"):
                try:
                    value = getattr(event, attr, None)
                    if value:
                        add(value)
                except Exception:
                    pass
            try:
                stanza = getattr(event, "stanza", None)
                if stanza is not None:
                    value = stanza.getFrom()
                    if value:
                        add(value)
            except Exception:
                pass

    def _own_identity_keys(self) -> set[str]:
        """Return normalized keys for all local/controller accounts.

        In a MUC the same bare JID can appear more than once when the user is
        connected from Gajim and another client (for example Miranda NG).  Gajim
        exposes those as separate occupants/resources/nicks, but for routing and
        circle participant calculations they are the same human owner.  Keep a
        broad key set so both "Гусаров М (You)" and "gusarov.m" can be marked
        as self when their real/bare JID maps to one of the user's accounts.
        """
        keys: set[str] = set()

        def add(value: object) -> None:
            raw = str(value or "").strip().lstrip("@").strip()
            if not raw:
                return
            keys.add(raw.casefold())
            norm = self._normalize_jid(raw)
            if norm:
                keys.add(norm.casefold())
                if "@" in norm:
                    keys.add(norm.split("@", 1)[0].casefold())

        try:
            for item in getattr(self, "_controller_identity_keys", set()) or set():
                add(item)
        except Exception:
            pass

        try:
            accounts = self._available_accounts()
        except Exception:
            accounts = []
        for account in accounts:
            try:
                if self._is_agent_account_context(str(account)):
                    continue
            except Exception:
                pass
            # Prefer resolved bare JID. Avoid generic local account IDs such as
            # yax.im/yax.im1 because those appear in every occupant description.
            try:
                jid = self._account_jid(str(account))
                if jid:
                    add(jid)
                    continue
            except Exception:
                pass
            raw = str(account or "").strip()
            if "@" in raw:
                add(raw)
        return keys

    def _occupant_is_self(self, occupant: object) -> bool:
        keys = self._occupant_match_keys(occupant)
        own = self._own_identity_keys()
        if not keys or not own:
            return False
        return bool(keys & own)

    def _iter_current_muc_occupants(self, room_jid: str = "") -> list[object]:
        """Best-effort live MUC occupant extraction across Gajim 2.4 APIs."""
        room = self._normalize_jid(room_jid or self.config["room_jid"])
        out: list[object] = []

        def extend_from_container(container: object) -> None:
            if container is None:
                return
            for meth in ("get_participants", "get_users", "get_occupants", "iter_participants"):
                try:
                    fn = getattr(container, meth, None)
                except Exception:
                    fn = None
                if callable(fn):
                    try:
                        value = fn()
                        if isinstance(value, dict):
                            out.extend(value.values())
                        elif value is not None:
                            out.extend(list(value))
                    except Exception:
                        pass
            for attr in ("participants", "users", "occupants"):
                try:
                    value = getattr(container, attr, None)
                except Exception:
                    value = None
                if isinstance(value, dict):
                    out.extend(value.values())
                elif value is not None and not isinstance(value, (str, bytes)):
                    try:
                        out.extend(list(value))
                    except Exception:
                        pass

        try:
            control = app.window.get_control()
            if control is not None:
                active = self._active_control_jid(control)
                if not active or active == room:
                    get_contact = getattr(control, "get_contact", None)
                    if callable(get_contact):
                        try:
                            extend_from_container(get_contact())
                        except Exception:
                            pass
        except Exception:
            pass

        try:
            for account in app.get_accounts():
                try:
                    client = app.get_client(account)
                    contact = self._resolve_contact(client, room)
                    extend_from_container(contact)
                except Exception:
                    pass
        except Exception:
            pass

        # De-duplicate by broad match key set / string representation.
        seen: set[str] = set()
        unique: list[object] = []
        for item in out:
            keys = self._occupant_match_keys(item)
            key = next(iter(sorted(keys)), "") or repr(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _format_muc_participants(self, room_jid: str = "") -> str:
        room = self._normalize_jid(room_jid or self.config["room_jid"])
        occupants = self._iter_current_muc_occupants(room)
        enabled = self._enabled_agent_entries()
        self_count = sum(1 for occupant in occupants if self._occupant_is_self(occupant))
        lines = [
            "AI Chatter Bridge R3.4.4: MUC participants probe",
            f"- room: {room or '?'}",
            f"- raw occupants found: {len(occupants)}",
            f"- self occupants by bare-JID: {self_count}",
            f"- self keys: {', '.join(sorted(self._own_identity_keys())[:12]) or '-'}",
            "- occupants:",
        ]
        if not occupants:
            lines.append("  (Gajim API did not expose occupants; sidebar may still show them)")
        for index, occupant in enumerate(occupants, 1):
            keys = sorted(self._occupant_match_keys(occupant))
            preview = ", ".join(keys[:10])
            matched_ids: list[str] = []
            for entry in enabled:
                if self._agent_entry_match_keys(entry) & set(keys):
                    matched_ids.append(str(entry.get("id") or entry.get("jid") or "agent"))
            try:
                text = str(occupant)
            except Exception:
                text = repr(occupant)
            if len(text) > 180:
                text = text[:177] + "..."
            self_mark = "yes" if self._occupant_is_self(occupant) else "no"
            lines.append(f"  {index}. {text}; self={self_mark}; keys={preview or '?'}; registry_match={', '.join(matched_ids) or '-'}")
        lines.append("- active agents by resolver:")
        for entry in self._active_muc_agent_entries(room):
            jid = str(entry.get("jid") or "")
            account = self._find_account_for_agent_ref(jid, entry)
            resolved = self._account_jid(account)
            lines.append(f"  {entry.get('id')}: registry_jid={jid}; account={account or '?'}; resolved_jid={resolved or '?'}; usable={self._account_is_usable(account) if account else False}")
        return "\n".join(lines)

    def _active_muc_agent_entries(self, room_jid: str = "") -> list[dict[str, object]]:
        """Return enabled registry agents currently visible in the configured MUC.

        If Gajim does not expose roster occupants in the current API/context, fall
        back to enabled agents whose Gajim account is available. This preserves the
        existing stable pipeline instead of dropping to zero targets.
        """
        enabled = self._enabled_agent_entries()
        if not enabled:
            return []

        occupants = self._iter_current_muc_occupants(room_jid)
        occupant_keys: set[str] = set()
        for occupant in occupants:
            if self._occupant_is_self(occupant):
                continue
            occupant_keys.update(self._occupant_match_keys(occupant))

        matched: list[dict[str, object]] = []
        if occupant_keys:
            for entry in enabled:
                if self._agent_entry_match_keys(entry) & occupant_keys:
                    matched.append(entry)

        fallback: list[dict[str, object]] = []
        for entry in enabled:
            account = self._find_account_for_agent_ref(str(entry.get("jid") or ""), entry)
            try:
                if account and self._account_is_usable(account):
                    fallback.append(entry)
            except Exception:
                pass

        # R3.4.1c: live MUC roster extraction is API-dependent in Gajim 2.4.x.
        # On some systems it can return only a partial occupant set even though
        # the roster sidebar visibly contains all agent accounts (for example
        # vova + deepseek but not qwen_qwen).  Do not let a partial occupant
        # match silently drop an available registry agent from broadcast/circle.
        # Prefer exact MUC matches, then fill missing agents from available
        # Gajim accounts.  If neither source works, keep the old safe fallback.
        combined = self._dedupe_agent_entries(matched + fallback)
        if combined:
            return combined
        return self._dedupe_agent_entries(enabled)

    def _active_muc_agent_count(self, room_jid: str = "") -> int:
        return max(1, len(self._active_muc_agent_entries(room_jid)))

    def _agent_entry_label(self, entry: dict[str, object]) -> str:
        aliases = [str(a).strip() for a in entry.get("aliases", []) if str(a).strip()]
        if aliases:
            return aliases[0]
        return str(entry.get("id") or entry.get("jid") or "agent")

    def _dedupe_agent_entries(self, entries: list[dict[str, object]]) -> list[dict[str, object]]:
        seen: set[str] = set()
        out: list[dict[str, object]] = []
        for entry in entries:
            key = str(entry.get("id") or entry.get("jid") or id(entry)).strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
        return out

    def _parse_multi_agent_addressing(self, text: str) -> tuple[list[dict[str, object]], str, str] | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        broadcast = re.match(r"^\s*(?:всем|all)\s*!\s*(.+)$", raw, flags=re.IGNORECASE | re.UNICODE | re.DOTALL)
        if broadcast:
            payload = broadcast.group(1).strip()
            targets = self._active_muc_agent_entries(str(self.config["room_jid"]))
            if payload and targets:
                return targets, payload, "broadcast"
            return None

        lookup = self._agent_alias_lookup(include_disabled=True)

        for delimiter in (":", "："):
            if delimiter in raw:
                head, payload = raw.split(delimiter, 1)
                refs = [x.strip().lstrip("@") for x in re.split(r"[,;]", head) if x.strip()]
                if len(refs) >= 2 and payload.strip():
                    entries: list[dict[str, object]] = []
                    for ref in refs:
                        entry = lookup.get(self._agent_alias_key(ref))
                        if entry is None:
                            entries = []
                            break
                        entries.append(entry)
                    if len(entries) >= 2:
                        return self._dedupe_agent_entries(entries), payload.strip(), "multi"

        parts = [x.strip() for x in raw.split(",")]
        if len(parts) >= 3:
            entries: list[dict[str, object]] = []
            stop_at = 0
            for idx, part in enumerate(parts):
                key = self._agent_alias_key(part)
                entry = lookup.get(key)
                if entry is None:
                    stop_at = idx
                    break
                entries.append(entry)
                stop_at = idx + 1
            if len(entries) >= 2 and stop_at < len(parts):
                payload = ",".join(parts[stop_at:]).strip()
                if payload:
                    return self._dedupe_agent_entries(entries), payload, "multi"
        return None

    def _try_agent_multi_addressing(self, room_jid: str, text: str, controller_account: str = "") -> bool:
        parsed = self._parse_multi_agent_addressing(text)
        if parsed is None:
            return False
        targets, payload, mode = parsed
        if not targets or not payload:
            return False

        queued = 0
        labels = []
        for entry in targets:
            label = self._agent_entry_label(entry)
            labels.append(str(entry.get("id") or label))
            synthetic = f"{label}, {payload}"
            try:
                if self._try_agent_echo(room_jid, synthetic, controller_account):
                    queued += 1
            except Exception:
                log.exception("R3.3 multi-target dispatch failed for %s", label)
        self._send_reply(
            controller_account,
            room_jid,
            "AI Chatter Bridge R3.3: multi-target dispatch.\n"
            f"- mode: {mode}\n"
            f"- targets: {', '.join(labels)}\n"
            f"- queued: {queued}\n"
            f"- prompt: {payload}",
        )
        return True

    def _format_agent_targets(self) -> str:
        lines = ["AI Chatter Bridge R3.3: agent targets"]
        try:
            entries = self._agent_registry_from_file(include_disabled=True)
        except Exception:
            entries = self._agent_registry()
        for entry in entries:
            jid = str(entry.get("jid", ""))
            provider = str(entry.get("provider", ""))
            aliases = ", ".join(str(a) for a in entry.get("aliases", []) if str(a).strip())
            lines.append(f"- {entry.get('id')}: enabled={entry.get('enabled')}; provider={provider}; jid={jid}; aliases={aliases}")
        lines.append("")
        lines.append("Синтаксис:")
        lines.append("- Вова, Qwen, Сирожа: <задание>")
        lines.append("- Вова, Qwen, Сирожа, <задание>")
        lines.append("- всем! <задание>")
        lines.append("- all! <task>")
        return "\n".join(lines)


    def _find_agent_entry_by_ref(self, ref: str) -> dict[str, object] | None:
        target = str(ref or "").strip().lower()
        if not target:
            return None
        target_jid = self._normalize_jid(target)
        for entry in self._agent_registry_from_file(include_disabled=True):
            entry_id = str(entry.get("id", "")).strip().lower()
            jid = self._normalize_jid(str(entry.get("jid", "")))
            aliases = [str(a).strip().lower() for a in entry.get("aliases", []) if str(a).strip()]
            if target in (entry_id, jid) or target_jid in (entry_id, jid):
                return entry
            if target in aliases:
                return entry
        # Last chance: resolve direct JID/account to a synthetic enabled entry.
        account = self._find_account_for_jid_or_name(ref)
        if account and self._normalize_jid(account) != self._normalize_jid(ref):
            return {
                "id": self._normalize_jid(ref).split("@", 1)[0],
                "jid": self._account_jid(account) or ref,
                "provider": "manual",
                "enabled": True,
                "aliases": [ref],
            }
        return None

    def _format_agent_send_test(self, cmd: str, room_jid: str) -> str:
        parts = cmd.split(maxsplit=2)
        if len(parts) < 3:
            return "AI Chatter Bridge R2.6.7: используйте .agent-send-test <agent-id|jid> текст"
        ref = parts[1].strip()
        text = parts[2].strip()
        entry = self._find_agent_entry_by_ref(ref)
        if entry is None:
            return (
                "AI Chatter Bridge R2.6.7: agent not found.\n"
                f"- requested: {ref}\n"
                "- hint: проверь .agent-registry"
            )
        agent_id = str(entry.get("id") or ref)
        agent_jid = str(entry.get("jid") or ref)
        agent_account = self._find_account_for_agent_ref(agent_jid, entry)
        if not agent_account or self._normalize_jid(agent_account) == self._normalize_jid(agent_jid):
            return (
                "AI Chatter Bridge R2.6.7: agent account not found.\n"
                f"- id: {agent_id}\n"
                f"- jid: {agent_jid}\n"
                f"- resolved account: {agent_account}"
            )
        self._remember_agent_output(room_jid, text)
        sent, error = self._send_reply_detailed(agent_account, room_jid, text)
        return (
            "AI Chatter Bridge R2.6.7: agent send test.\n"
            f"- id: {agent_id}\n"
            f"- jid: {agent_jid}\n"
            f"- account: {agent_account}\n"
            f"- room: {room_jid}\n"
            f"- sent: {sent}\n"
            f"- error: {error or '-'}"
        )

    def _home_logs_path(self, name: str) -> Path:
        """Return a path under AI Chatter home/logs for diagnostic JSONL/log files.

        R3.4.4 keeps noisy lifecycle logs out of the project root while leaving
        durable config/queue files in the root.  Existing absolute paths and
        explicit subdirectories are respected.
        """
        raw = str(name or "").strip()
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
        try:
            home = Path(self._detect_ai_chatter_home())
            if str(home).strip():
                # If caller already supplied a subdirectory such as logs/foo, use it.
                if len(candidate.parts) > 1:
                    return home / candidate
                return home / "logs" / candidate.name
        except Exception:
            pass
        return Path(self.local_file_path(raw))

    def _agent_jobs_path(self) -> Path:
        name = str(self.config["agent_jobs_file_name"]).strip() or "logs/ai_chatter_agent_jobs.jsonl"
        return self._home_logs_path(name)

    def _append_agent_job_record(self, record: dict[str, object]) -> None:
        path = self._agent_jobs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_agent_jobs_jsonl(self) -> list[dict[str, object]]:
        """Read agent jobs JSONL records from the shared AI Chatter state file.

        R3.4.1d: circle status/transcript already used this helper, but older
        builds did not define it. The resulting AttributeError was swallowed in
        _format_circle_status(), so .круг-статус reported zero counters even
        when ai_chatter_agent_jobs.jsonl contained circle jobs.
        """
        path = self._agent_jobs_path()
        if not path.exists():
            return []
        records: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    records.append(item)
        except Exception:
            log.exception("Could not read agent jobs JSONL")
            return []
        return records

    def _format_agent_jobs_status(self) -> str:
        path = self._agent_jobs_path()
        if not path.exists():
            return f"AI Chatter Bridge R2.2: agent jobs пуст.\nФайл: {path}"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = lines[-8:]
        result = [
            "AI Chatter Bridge R2.2: agent jobs",
            f"- файл: {path}",
            f"- записей: {len(lines)}",
            "- последние:",
        ]
        for line in recent:
            try:
                item = json.loads(line)
                result.append(
                    f"  {item.get('id')} {item.get('status')} "
                    f"{item.get('agentJid')} <- {item.get('roomJid')} "
                    f"input={item.get('inputText')!r} reply={item.get('replyText')!r}"
                )
            except Exception:
                result.append(f"  {line[:180]}")
        return "\n".join(result)

    def _agent_outbox_path(self) -> Path:
        name = str(self.config["agent_outbox_file_name"]).strip() or "logs/ai_chatter_agent_outbox.jsonl"
        return self._home_logs_path(name)

    def _append_agent_outbox_record(self, record: dict[str, object]) -> None:
        path = self._agent_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _format_agent_outbox_status(self) -> str:
        path = self._agent_outbox_path()
        if not path.exists():
            return f"AI Chatter Bridge R2.1: agent outbox пуст.\nФайл: {path}"
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = lines[-5:]
        result = [
            "AI Chatter Bridge R2.1: agent outbox",
            f"- файл: {path}",
            f"- записей: {len(lines)}",
            "- последние:",
        ]
        for line in recent:
            try:
                item = json.loads(line)
                result.append(
                    f"  {item.get('id')} {item.get('status')} "
                    f"{item.get('agentJid')} -> {item.get('roomJid')} "
                    f"text={item.get('replyText')!r}"
                )
            except Exception:
                result.append(f"  {line[:160]}")
        return "\n".join(result)




    def _latest_agent_job_statuses(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        path = self._agent_jobs_path()
        if not path.exists():
            return statuses
        try:
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    job_id = str(item.get("id", ""))
                    status = str(item.get("status", ""))
                    if job_id and status:
                        statuses[job_id] = status
        except Exception:
            log.exception("Could not read agent job statuses")
        return statuses

    def _pending_agent_provider_count(self) -> int:
        """Count R3 agent jobs that still need provider processing/delivery."""
        state = self._read_agent_provider_state()
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            return 0
        latest = self._latest_agent_job_statuses()
        pending = 0
        final_statuses = {"delivered", "send_called", "visible_in_muc", "delivery_failed", "provider_queue_failed", "provider_timeout"}
        for item in tasks.values():
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("jobId", ""))
            if not job_id:
                continue
            status = latest.get(job_id, "")
            if status not in final_statuses:
                pending += 1
        return pending

    def _auto_run_limit(self, pending_count: int | None = None) -> int:
        try:
            limit = int(self.config["max_auto_jobs_per_run"])
        except Exception:
            limit = 5
        if limit < 1:
            limit = 1
        if limit > 20:
            limit = 20
        if pending_count is None:
            pending_count = self._pending_agent_provider_count()
        try:
            pending_count = int(pending_count)
        except Exception:
            pending_count = 1
        if pending_count < 1:
            pending_count = 1

        # R3.4.1: do not let a stale executor command with --max-tasks 1 /
        # --max-jobs 1 collapse broadcast/circle/multi-target delivery to one
        # agent. Use the live MUC AI-agent count as a dynamic lower bound.
        try:
            muc_agents = self._active_muc_agent_count(str(self.config["room_jid"]))
        except Exception:
            muc_agents = 1
        batch = max(pending_count, muc_agents, 1)
        return min(limit, batch)

    def _format_agent_pending_status(self) -> str:
        pending = self._pending_agent_provider_count()
        running = self._executor_process is not None and self._executor_process.poll() is None
        return (
            "AI Chatter Bridge R3.1: pending-aware autorun\n"
            f"- provider pending jobs: {pending}\n"
            f"- executor running: {running}\n"
            f"- needs next run: {self._executor_needs_rerun}\n"
            f"- auto-run executor: {self.config['auto_run_executor']}\n"
            f"- auto-deliver results: {self.config['auto_deliver_results']}\n"
            f"- max auto jobs per run: {self.config['max_auto_jobs_per_run']}\n"
            f"- live MUC AI agents: {self._active_muc_agent_count(str(self.config['room_jid']))}"
        )

    def _agent_provider_state_path(self) -> Path:
        try:
            home = Path(self._detect_ai_chatter_home())
            if str(home).strip():
                return home / "ai_chatter_agent_provider_state.json"
        except Exception:
            pass
        return Path(self.local_file_path("ai_chatter_agent_provider_state.json"))

    def _read_agent_provider_state(self) -> dict[str, object]:
        path = self._agent_provider_state_path()
        if not path.exists():
            return {"tasks": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                tasks = data.get("tasks")
                if isinstance(tasks, dict):
                    return data
        except Exception:
            pass
        return {"tasks": {}}

    def _write_agent_provider_state(self, data: dict[str, object]) -> None:
        path = self._agent_provider_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data["updatedAt"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remember_agent_provider_task(self, task_id: str, agent_delivery: dict[str, object]) -> None:
        state = self._read_agent_provider_state()
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
            state["tasks"] = tasks
        tasks[str(task_id)] = agent_delivery
        self._write_agent_provider_state(state)

    def _lookup_agent_provider_delivery(self, result: dict[str, object]) -> dict[str, object] | None:
        candidates = []
        for key in ("taskId", "task_id", "sourceTaskId", "source_task_id", "requestId"):
            value = result.get(key)
            if value:
                candidates.append(str(value))
        source = result.get("source")
        if isinstance(source, dict):
            for key in ("taskId", "task_id", "id"):
                value = source.get(key)
                if value:
                    candidates.append(str(value))
        state = self._read_agent_provider_state()
        tasks = state.get("tasks")
        if not isinstance(tasks, dict):
            return None
        for candidate in candidates:
            item = tasks.get(candidate)
            if isinstance(item, dict):
                return item
        return None

    def _append_agent_provider_task(
        self,
        *,
        job_id: str,
        controller_account: str,
        room_jid: str,
        agent_id: str,
        provider: str,
        agent_jid: str,
        agent_account: str,
        input_text: str,
        payload_text: str,
    ) -> dict[str, object]:
        """Create an executor-compatible queue task for an addressed XMPP agent.

        The executor/Chrome bridge still consumes the normal ai_chatter_tasks.jsonl
        format. R3 adds enough metadata under source.agentDelivery so .deliver can
        send the provider answer back through the real XMPP agent account instead
        of through the controller account.
        """
        bot_id = (provider or agent_id or "chatgpt").strip().lower()
        task = {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "status": "queued",
            "source": {
                "type": "gajim-agent-r3",
                "account": controller_account,
                "roomJid": room_jid,
                "agentDelivery": {
                    "jobId": job_id,
                    "agentId": agent_id,
                    "provider": provider,
                    "agentJid": agent_jid,
                    "agentAccount": agent_account,
                    "roomJid": room_jid,
                    "inputText": input_text,
                    "payloadText": payload_text,
                },
            },
            "targets": [
                {
                    "botId": bot_id,
                    "botTitle": self._bot_title(bot_id),
                }
            ],
            "message": {
                "text": payload_text,
            },
            "routing": {
                "activeBotIds": [bot_id],
                "canSpeakBotIds": [bot_id],
                "r3AgentProvider": True,
            },
        }
        queue_path = self._queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with queue_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(task, ensure_ascii=False) + "\n")
        try:
            agent_delivery = task.get("source", {}).get("agentDelivery", {})
            if isinstance(agent_delivery, dict):
                self._remember_agent_provider_task(str(task.get("id", "")), agent_delivery)
        except Exception:
            log.exception("Could not persist R3 agent provider task metadata")
        return task

    def _deliver_agent_reply(
        self,
        *,
        job_id: str,
        stage: str,
        kind: str,
        room_jid: str,
        agent_id: str,
        provider: str,
        agent_jid: str,
        agent_account: str,
        input_text: str,
        reply_text: str,
    ) -> bool:
        base_record = {
            "id": job_id,
            "stage": stage,
            "kind": kind,
            "status": "created",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "roomJid": room_jid,
            "agentId": agent_id,
            "provider": provider,
            "agentJid": agent_jid,
            "agentAccount": agent_account,
            "inputText": input_text,
            "replyText": reply_text,
        }
        self._append_agent_outbox_record(base_record)
        self._remember_agent_output(
            room_jid,
            reply_text,
            job_id=job_id,
            stage=stage,
            kind=kind,
            agent_id=agent_id,
            provider=provider,
            agent_jid=agent_jid,
            agent_account=agent_account,
            input_text=input_text,
        )
        sent, send_error = self._send_reply_detailed(agent_account, room_jid, reply_text)
        delivered_record = dict(base_record)
        delivered_record["status"] = "send_called" if sent else "send_failed"
        delivered_record["sentAt"] = datetime.now(timezone.utc).isoformat()
        delivered_record["sent"] = bool(sent)
        if send_error:
            delivered_record["error"] = send_error
        self._append_agent_outbox_record(delivered_record)

        final_job_record = {
            "id": job_id,
            "stage": stage,
            "kind": "agent_provider_job" if stage.startswith("r3") else "agent_reply_echo",
            "status": "send_called" if sent else "delivery_failed",
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "roomJid": room_jid,
            "agentId": agent_id,
            "provider": provider,
            "agentJid": agent_jid,
            "agentAccount": agent_account,
            "inputText": input_text,
            "replyText": reply_text,
            "sent": bool(sent),
        }
        if send_error:
            final_job_record["error"] = send_error
        self._append_agent_job_record(final_job_record)
        return bool(sent)

    def _try_agent_echo(self, room_jid: str, text: str, controller_account: str = "") -> bool:
        match = self._match_agent_addressing(text, include_disabled=True)
        if match is None:
            return False

        entry, remainder = match
        agent_id = str(entry.get("id") or "agent")
        agent_jid = str(entry.get("jid") or "")
        provider = str(entry.get("provider") or agent_id)
        enabled = bool(entry.get("enabled", True))

        # R2.6 disabled-agent behavior: recognize configured disabled agents,
        # but do not create agent jobs and do not create outbox records.
        if not enabled:
            self._send_reply(
                controller_account,
                room_jid,
                "AI Chatter Bridge R2.6: agent disabled.\n"
                f"- id: {agent_id}\n"
                f"- jid: {agent_jid}\n"
                f"- provider: {provider}\n"
                "- job: not created\n"
                "- outbox: not created",
            )
            return True

        agent_account = self._find_account_for_agent_ref(agent_jid, entry)
        if not agent_account or self._normalize_jid(agent_account) == self._normalize_jid(agent_jid):
            # _find_account_for_jid_or_name returns the input unchanged when not found.
            self._send_reply(
                controller_account,
                room_jid,
                "AI Chatter Bridge R2.6: agent configured but Gajim account not found.\n"
                f"- id: {agent_id}\n"
                f"- jid: {agent_jid}\n"
                "- job: not created\n"
                "- outbox: not created",
            )
            return True
        try:
            if not self._account_is_usable(agent_account):
                self._send_reply(
                    controller_account,
                    room_jid,
                    "AI Chatter Bridge R2.6: agent account unavailable.\n"
                    f"- id: {agent_id}\n"
                    f"- jid: {agent_jid}\n"
                    f"- account: {agent_account}\n"
                    "- job: not created\n"
                    "- outbox: not created",
                )
                return True
        except Exception:
            self._send_reply(
                controller_account,
                room_jid,
                "AI Chatter Bridge R2.6: could not check agent account availability.\n"
                f"- id: {agent_id}\n"
                f"- jid: {agent_jid}\n"
                "- job: not created\n"
                "- outbox: not created",
            )
            return True

        resolved_agent_jid = self._account_jid(agent_account) or agent_jid
        # R3.4.4: store and deliver using the real resolved Gajim account JID,
        # not the stale registry JID.  At home qwen/deepseek registry JIDs can be
        # qwen_agent/deepseek_agent while the active MUC accounts resolve to
        # qwen_qwen/siroza_deepseek.  Using the stale JID makes send_called look
        # successful but prevents reliable MUC visibility checks.

        payload = remainder or "(пусто)"

        # Branch 4 rollout: ordinary addressed Qwen/DeepSeek messages go through
        # Local Bus + Native Messaging + provider content worker. This bypasses
        # the legacy ai_chatter_tasks.jsonl provider queue for these providers only.
        provider_l = str(provider).strip().lower()
        if provider_l in ("qwen", "deepseek") and self._branch4_provider_enabled_for_addressed(provider_l):
            return self._send_branch4_provider_addressed(
                controller_account=controller_account,
                room_jid=room_jid,
                agent_account=agent_account,
                payload=payload,
                provider=provider_l,
            )

        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        if bool(self.config["agent_provider_enabled"]):
            reply_placeholder = ""
            job_record = {
                "id": job_id,
                "stage": "r3.1",
                "kind": "agent_provider_job",
                "status": "created",
                "createdAt": now,
                "roomJid": room_jid,
                "agentId": agent_id,
                "provider": provider,
                "agentJid": resolved_agent_jid,
                "agentAccount": agent_account,
                "controllerAccount": controller_account,
                "inputText": text,
                "payloadText": payload,
                "replyText": reply_placeholder,
            }
            self._append_agent_job_record(job_record)

            try:
                task = self._append_agent_provider_task(
                    job_id=job_id,
                    controller_account=controller_account,
                    room_jid=room_jid,
                    agent_id=agent_id,
                    provider=provider,
                    agent_jid=resolved_agent_jid,
                    agent_account=agent_account,
                    input_text=text,
                    payload_text=payload,
                )
                queued_record = dict(job_record)
                queued_record["status"] = "provider_queued"
                queued_record["taskId"] = str(task.get("id", ""))
                queued_record["queuedAt"] = datetime.now(timezone.utc).isoformat()
                self._append_agent_job_record(queued_record)
                self._ensure_executor_runtime_config()
                self._trigger_executor_async(pending_count=self._pending_agent_provider_count())
                if bool(self.config["agent_provider_queue_notice"]):
                    self._send_reply(
                        controller_account,
                        room_jid,
                        "AI Chatter Bridge R3.1: agent provider job queued.\n"
                        f"- agent: {agent_id}\n"
                        f"- provider: {provider}\n"
                        f"- task: {task.get('id')}\n"
                        "Executor запускается автоматически; готовый результат будет доставлен автоматически, если auto-deliver включён.",
                    )
            except Exception as error:
                failed_record = dict(job_record)
                failed_record["status"] = "provider_queue_failed"
                failed_record["error"] = str(error)
                failed_record["finishedAt"] = datetime.now(timezone.utc).isoformat()
                self._append_agent_job_record(failed_record)
                self._send_reply(
                    controller_account,
                    room_jid,
                    "AI Chatter Bridge R3.0: provider job не создан.\n"
                    f"- agent: {agent_id}\n"
                    f"- provider: {provider}\n"
                    f"- error: {error}",
                )
            return True

        reply = f"echo: {payload}"
        # R2 fallback: local echo delivery, kept for diagnostics / rollback.
        job_record = {
            "id": job_id,
            "stage": "r2.6",
            "kind": "agent_reply_echo",
            "status": "created",
            "createdAt": now,
            "roomJid": room_jid,
            "agentId": agent_id,
            "provider": provider,
            "agentJid": resolved_agent_jid,
            "agentAccount": agent_account,
            "inputText": text,
            "payloadText": payload,
            "replyText": reply,
        }
        self._append_agent_job_record(job_record)

        processing_record = dict(job_record)
        processing_record["status"] = "ready_for_delivery"
        processing_record["readyAt"] = datetime.now(timezone.utc).isoformat()
        self._append_agent_job_record(processing_record)

        self._deliver_agent_reply(
            job_id=job_id,
            stage="r2.6",
            kind="agent_reply_delivery",
            room_jid=room_jid,
            agent_id=agent_id,
            provider=provider,
            agent_jid=resolved_agent_jid,
            agent_account=agent_account,
            input_text=text,
            reply_text=reply,
        )
        return True

    def _circle_sessions_path(self) -> Path:
        name = str(self._cfg("circle_sessions_file_name", "ai_chatter_circle_sessions.jsonl")).strip() or "ai_chatter_circle_sessions.jsonl"
        # R3.4.1b: keep circle session storage in the same AI Chatter home
        # as registry/jobs/outbox. Older circle draft code called _home_path(),
        # but this plugin uses _detect_ai_chatter_home() instead.
        return Path(self._detect_ai_chatter_home()) / name

    def _append_circle_record(self, record: dict[str, object]) -> None:
        path = self._circle_sessions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_circle_records(self) -> list[dict[str, object]]:
        path = self._circle_sessions_path()
        if not path.exists():
            return []
        out: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        out.append(item)
                except Exception:
                    continue
        except Exception:
            log.exception("Could not read circle session records")
        return out

    def _latest_circle_session_id(self) -> str:
        for record in reversed(self._read_circle_records()):
            sid = str(record.get("circleId") or record.get("id") or "")
            if sid and str(record.get("status", "")) != "stopped":
                return sid
        return ""

    def _circle_records_for(self, circle_id: str) -> list[dict[str, object]]:
        if not circle_id:
            return []
        return [r for r in self._read_circle_records() if str(r.get("circleId") or r.get("id") or "") == circle_id]

    def _circle_session_info(self, circle_id: str) -> dict[str, object]:
        info: dict[str, object] = {}
        for rec in self._circle_records_for(circle_id):
            if rec.get("kind") == "circle_session":
                info.update(rec)
        return info

    def _circle_job_reply_records(self, circle_id: str) -> list[dict[str, object]]:
        """Return one latest visible/delivered reply record per circle provider job.

        R3.4.1e: the circle needs a shared transcript, not just messages visible
        to the human in MUC.  We build it from ai_chatter_agent_jobs.jsonl by
        circle marker and de-duplicate multiple lifecycle records for one job
        (created -> queued -> send_called -> visible_in_muc).
        """
        if not circle_id:
            return []
        marker = f"[circle:{circle_id}]"
        priority = {"visible_in_muc": 4, "delivered": 3, "send_called": 2}
        by_job: dict[str, tuple[int, int, dict[str, object]]] = {}
        try:
            for index, rec in enumerate(self._read_agent_jobs_jsonl()):
                input_text = str(rec.get("inputText") or "")
                if marker not in input_text:
                    continue
                reply = str(rec.get("replyText") or "").strip()
                if not reply:
                    continue
                status = str(rec.get("status") or "")
                rank = priority.get(status, 0)
                if rank <= 0:
                    continue
                job_id = str(rec.get("id") or rec.get("jobId") or f"row-{index}")
                old = by_job.get(job_id)
                if old is None or (rank, index) >= (old[0], old[1]):
                    by_job[job_id] = (rank, index, rec)
        except Exception:
            log.exception("Could not read circle job reply records")
            return []
        ordered = sorted(by_job.values(), key=lambda item: item[1])
        return [item[2] for item in ordered]

    def _circle_transcript_for(self, circle_id: str) -> str:
        lines: list[str] = []
        # Human clarifications saved in circle session log.
        for rec in self._circle_records_for(circle_id):
            if rec.get("kind") == "circle_human_answer":
                text = str(rec.get("text") or "").strip()
                if text:
                    lines.append(f"Михаил: {text}")
        # Delivered/visible agent/provider messages saved in agent jobs file.
        try:
            for rec in self._circle_job_reply_records(circle_id):
                agent_id = str(rec.get("agentId") or "agent")
                role = str(rec.get("circleRole") or "").strip()
                round_no = str(rec.get("roundNo") or "?")
                reply = str(rec.get("replyText") or "").strip()
                if reply:
                    label = agent_id
                    if role:
                        label = f"{agent_id} ({role}, раунд {round_no})"
                    lines.append(f"{label}: {reply}")
        except Exception:
            log.exception("Could not build circle transcript")
        if not lines:
            return "(пока нет доставленных ответов)"
        return "\n\n".join(lines[-30:])

    def _circle_participant_entries(self, info: dict[str, object], room_jid: str) -> list[dict[str, object]]:
        """Resolve stored circle participant ids back to registry entries.

        If an old session has no participantIds, fall back to current active MUC
        agents and keep the chancellor first.
        """
        participants: list[dict[str, object]] = []
        raw_ids = info.get("participantIds") or []
        if isinstance(raw_ids, list):
            for item in raw_ids:
                entry = self._find_agent_entry_by_ref(str(item))
                if entry:
                    participants.append(entry)
        participants = self._dedupe_agent_entries(participants)
        if not participants:
            participants = self._active_muc_agent_entries(room_jid)
        ch_ref = str(info.get("chancellorId") or info.get("chancellorJid") or "")
        ch = self._find_agent_entry_by_ref(ch_ref) if ch_ref else None
        if ch:
            ch_id = str(ch.get("id") or "").casefold()
            participants = [ch] + [p for p in participants if str(p.get("id") or "").casefold() != ch_id]
        return self._dedupe_agent_entries(participants)

    def _circle_parse_start(self, cmd: str) -> tuple[str, str] | None:
        text = str(cmd or "").strip()
        if text.lower().startswith("!circle"):
            text = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        text = text.strip()
        if not text:
            return None
        # Preferred: .круг Вова: тема
        for sep in (":", "："):
            if sep in text:
                left, right = text.split(sep, 1)
                left = left.strip()
                right = right.strip()
                if left and right:
                    return left, right
        # Fallback: .круг Вова, тема
        if "," in text:
            left, right = text.split(",", 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                return left, right
        return None

    def _circle_queue_provider_job(
        self,
        *,
        circle_id: str,
        round_no: int,
        role: str,
        entry: dict[str, object],
        controller_account: str,
        room_jid: str,
        topic: str,
        prompt: str,
    ) -> str:
        agent_id = str(entry.get("id") or "agent")
        agent_jid = str(entry.get("jid") or "")
        provider = str(entry.get("provider") or agent_id)
        agent_account = self._find_account_for_agent_ref(agent_jid, entry)
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        input_text = f"[circle:{circle_id}] round={round_no}; role={role}; topic={topic}"
        job_record = {
            "id": job_id,
            "stage": "r3.4",
            "kind": "circle_agent_provider_job",
            "status": "created",
            "createdAt": now,
            "circleId": circle_id,
            "roundNo": round_no,
            "circleRole": role,
            "roomJid": room_jid,
            "agentId": agent_id,
            "provider": provider,
            "agentJid": agent_jid,
            "agentAccount": agent_account,
            "controllerAccount": controller_account,
            "inputText": input_text,
            "payloadText": prompt,
            "replyText": "",
        }
        self._append_agent_job_record(job_record)
        task = self._append_agent_provider_task(
            job_id=job_id,
            controller_account=controller_account,
            room_jid=room_jid,
            agent_id=agent_id,
            provider=provider,
            agent_jid=agent_jid,
            agent_account=agent_account,
            input_text=input_text,
            payload_text=prompt,
        )
        queued_record = dict(job_record)
        queued_record["status"] = "provider_queued"
        queued_record["taskId"] = str(task.get("id", ""))
        queued_record["queuedAt"] = datetime.now(timezone.utc).isoformat()
        self._append_agent_job_record(queued_record)
        return str(task.get("id", ""))

    def _handle_circle_start(self, cmd: str, account: str, room_jid: str) -> str:
        parsed = self._circle_parse_start(cmd)
        if parsed is None:
            return "AI Chatter Bridge R3.4.1: используйте .круг <ведущий>: <тема>"
        chancellor_ref, topic = parsed
        chancellor = self._find_agent_entry_by_ref(chancellor_ref)
        if not chancellor:
            return f"AI Chatter Bridge R3.4.1: ведущий не найден в agent registry: {chancellor_ref}"
        agents = self._active_muc_agent_entries(room_jid)
        if not agents:
            return "AI Chatter Bridge R3.4.1: нет enabled agents в registry."
        # Put chancellor first, then the rest.
        ch_id = str(chancellor.get("id") or "").casefold()
        participants = [chancellor] + [a for a in agents if str(a.get("id") or "").casefold() != ch_id]
        circle_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._append_circle_record({
            "id": circle_id,
            "circleId": circle_id,
            "kind": "circle_session",
            "status": "active",
            "createdAt": now,
            "roomJid": room_jid,
            "controllerAccount": account,
            "topic": topic,
            "chancellorId": str(chancellor.get("id") or ""),
            "chancellorJid": str(chancellor.get("jid") or ""),
            "participantIds": [str(a.get("id") or "") for a in participants],
            "roundNo": 1,
            "maxRounds": 5,
        })
        tasks: list[str] = []
        ch_name = self._agent_entry_label(chancellor)
        ch_prompt = (
            "Ты канцлер и полноценный участник управляемого круга AI Chatter.\n"
            f"Тема: {topic}\n\n"
            "Твоя роль: предложить свои идеи, задать рамку обсуждения, следить за галлюцинациями, "
            "останавливать уход в сторону, отделять факты от предположений и в конце помогать Михаилу получить практический вывод.\n"
            "Сейчас открой круг: дай своё первое мнение и коротко обозначь, на что участникам нужно обратить внимание. "
            "Пиши по-русски, структурно и без лишней воды."
        )
        try:
            tasks.append(self._circle_queue_provider_job(circle_id=circle_id, round_no=1, role="chancellor_open", entry=chancellor, controller_account=account, room_jid=room_jid, topic=topic, prompt=ch_prompt))
        except Exception as exc:
            log.exception("Could not queue circle chancellor opening")
            return f"AI Chatter Bridge R3.4.1: не удалось создать задачу канцлера: {exc}"
        for entry in participants[1:]:
            participant_name = self._agent_entry_label(entry)
            prompt = (
                "Ты участник управляемого круга AI Chatter.\n"
                f"Тема: {topic}\n"
                f"Канцлер круга: {ch_name}.\n\n"
                "Дай своё мнение по теме. Не выдумывай факты; если не уверен — явно скажи, что это предположение. "
                "Предложи практичные шаги и укажи риски. Пиши по-русски и достаточно коротко."
            )
            try:
                tasks.append(self._circle_queue_provider_job(circle_id=circle_id, round_no=1, role=f"participant:{participant_name}", entry=entry, controller_account=account, room_jid=room_jid, topic=topic, prompt=prompt))
            except Exception:
                log.exception("Could not queue circle participant %s", participant_name)
        self._ensure_executor_runtime_config()
        self._trigger_executor_async(pending_count=self._pending_agent_provider_count())
        return (
            "AI Chatter Bridge R3.4.1: круг создан.\n"
            f"- id: {circle_id}\n"
            f"- канцлер: {ch_name}\n"
            f"- тема: {topic}\n"
            f"- участники: {', '.join(str(a.get('id') or self._agent_entry_label(a)) for a in participants)}\n"
            f"- provider jobs: {len(tasks)}\n"
            "После ответов используйте .круг-дальше для канцлерского продолжения/итога, .круг-статус для статуса или .круг-стоп для остановки."
        )

    def _format_circle_status(self) -> str:
        circle_id = self._latest_circle_session_id()
        if not circle_id:
            return f"AI Chatter Bridge R3.4.1: активных кругов нет.\n- файл: {self._circle_sessions_path()}"
        info = self._circle_session_info(circle_id)
        marker = f"[circle:{circle_id}]"
        total = queued = done = visible = 0
        seen_created: set[str] = set()
        seen_queued: set[str] = set()
        seen_done: set[str] = set()
        seen_visible: set[str] = set()
        read_error = ""
        try:
            for rec in self._read_agent_jobs_jsonl():
                if marker not in str(rec.get("inputText") or ""):
                    continue
                job_id = str(rec.get("id") or rec.get("jobId") or "")
                status = str(rec.get("status") or "")
                if status == "created" and job_id not in seen_created:
                    seen_created.add(job_id)
                    total += 1
                if status == "provider_queued" and job_id not in seen_queued:
                    seen_queued.add(job_id)
                    queued += 1
                if status in ("delivered", "send_called") and job_id not in seen_done:
                    seen_done.add(job_id)
                    done += 1
                if status == "visible_in_muc" and job_id not in seen_visible:
                    seen_visible.add(job_id)
                    visible += 1
        except Exception as exc:
            log.exception("Could not count circle job records")
            read_error = str(exc)
        return (
            "AI Chatter Bridge R3.4.1: круг-статус\n"
            f"- id: {circle_id}\n"
            f"- тема: {info.get('topic', '')}\n"
            f"- канцлер: {info.get('chancellorId', '')}\n"
            f"- раунд: {info.get('roundNo', '?')}\n"
            f"- provider jobs created: {total}\n"
            f"- provider queued: {queued}\n"
            f"- delivered/send_called: {done}\n"
            f"- visible_in_muc: {visible}\n"
            f"- transcript replies: {len(self._circle_job_reply_records(circle_id))}\n"
            f"- jobs file: {self._agent_jobs_path()}\n"
            f"- file read error: {read_error or '-'}\n"
            f"- файл: {self._circle_sessions_path()}"
        )

    def _handle_circle_answer(self, cmd: str) -> str:
        circle_id = self._latest_circle_session_id()
        if not circle_id:
            return "AI Chatter Bridge R3.4.1: активного круга нет."
        text = cmd.split(maxsplit=1)[1].strip() if len(cmd.split(maxsplit=1)) > 1 else ""
        if not text:
            return "AI Chatter Bridge R3.4.1: используйте .круг-ответ <текст>"
        self._append_circle_record({
            "circleId": circle_id,
            "kind": "circle_human_answer",
            "status": "recorded",
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "text": text,
        })
        return "AI Chatter Bridge R3.4.1: ответ человека добавлен в контекст круга. Используйте .круг-дальше для продолжения."

    def _handle_circle_stop(self) -> str:
        circle_id = self._latest_circle_session_id()
        if not circle_id:
            return "AI Chatter Bridge R3.4.1: активного круга нет."
        self._append_circle_record({
            "circleId": circle_id,
            "kind": "circle_session",
            "status": "stopped",
            "stoppedAt": datetime.now(timezone.utc).isoformat(),
        })
        return f"AI Chatter Bridge R3.4.1: круг остановлен.\n- id: {circle_id}"

    def _handle_circle_next(self, account: str, room_jid: str) -> str:
        circle_id = self._latest_circle_session_id()
        if not circle_id:
            return "AI Chatter Bridge R3.4.1: активного круга нет. Сначала используйте .круг <ведущий>: <тема>"
        info = self._circle_session_info(circle_id)
        topic = str(info.get("topic") or "")
        chancellor_ref = str(info.get("chancellorId") or info.get("chancellorJid") or "")
        chancellor = self._find_agent_entry_by_ref(chancellor_ref)
        if not chancellor:
            return "AI Chatter Bridge R3.4.1: канцлер текущего круга не найден в registry."
        participants = self._circle_participant_entries(info, room_jid)
        if not participants:
            return "AI Chatter Bridge R3.4.1: нет участников круга для следующего хода."
        try:
            round_no = int(info.get("roundNo") or 1) + 1
        except Exception:
            round_no = 2
        if round_no > 5:
            return "AI Chatter Bridge R3.4.1: достигнут максимум 5 кругов. Создайте новый .круг или завершите .круг-стоп."
        transcript = self._circle_transcript_for(circle_id)
        ch_name = self._agent_entry_label(chancellor)
        task_ids: list[str] = []
        ch_id = str(chancellor.get("id") or "").casefold()
        for entry in participants:
            agent_id = str(entry.get("id") or "").casefold()
            agent_name = self._agent_entry_label(entry)
            if agent_id == ch_id:
                role = "chancellor_next"
                prompt = (
                    "Ты канцлер управляемого круга AI Chatter и полноценный участник обсуждения.\n"
                    f"Тема: {topic}\n\n"
                    "Ниже общий протокол круга: ответы всех участников и уточнения Михаила. "
                    "Это общий контекст, который также получат остальные участники на этом controlled step.\n"
                    f"{transcript}\n\n"
                    "Сделай следующий канцлерский ход: отдели факты от предположений, отфильтруй возможные галлюцинации, "
                    "кратко сопоставь позиции участников и предложи следующий практический шаг. "
                    "Не запускай бесконечную дискуссию; новые ходы делает только команда Михаила. Пиши по-русски."
                )
            else:
                role = f"participant_next:{agent_name}"
                prompt = (
                    "Ты участник управляемого круга AI Chatter.\n"
                    f"Тема: {topic}\n"
                    f"Канцлер круга: {ch_name}.\n\n"
                    "Ниже общий протокол круга: ответы всех участников и уточнения Михаила. "
                    "Ты видишь позиции других агентов, но не должен сам запускать новый круг или уходить в бесконечный спор.\n"
                    f"{transcript}\n\n"
                    "Дай короткую следующую реплику: что поддерживаешь, с чем не согласен, где возможны галлюцинации, "
                    "и какой практический вывод или следующий шаг предлагаешь. Пиши по-русски, структурно и без лишней воды."
                )
            try:
                task_ids.append(self._circle_queue_provider_job(circle_id=circle_id, round_no=round_no, role=role, entry=entry, controller_account=account, room_jid=room_jid, topic=topic, prompt=prompt))
            except Exception:
                log.exception("Could not queue shared circle next task for %s", agent_name)
        self._append_circle_record({
            "id": circle_id,
            "circleId": circle_id,
            "kind": "circle_session",
            "status": "active",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "roomJid": room_jid,
            "controllerAccount": account,
            "topic": topic,
            "chancellorId": str(chancellor.get("id") or ""),
            "chancellorJid": str(chancellor.get("jid") or ""),
            "participantIds": [str(a.get("id") or "") for a in participants],
            "roundNo": round_no,
            "maxRounds": 5,
        })
        self._ensure_executor_runtime_config()
        self._trigger_executor_async(pending_count=self._pending_agent_provider_count())
        return (
            "AI Chatter Bridge R3.4.1: следующий общий ход круга поставлен в очередь.\n"
            f"- id: {circle_id}\n"
            f"- раунд: {round_no}\n"
            f"- канцлер: {self._agent_entry_label(chancellor)}\n"
            f"- участники: {', '.join(str(a.get('id') or self._agent_entry_label(a)) for a in participants)}\n"
            f"- provider jobs: {len(task_ids)}"
        )

    def _format_help(self) -> str:
        return "\n".join([
            "AI Chatter Bridge: справка",
            "",
            "Русские команды — основные. Английские аналоги работают для совместимости.",
            "",
            "Основное:",
            ".хелп — справка (.help)",
            ".версия — версия плагина (.version)",
            ".пинг — проверка активности (.ping)",
            ".комната — настроенная конференция (.room)",
            ".конфиг — сводка настроек (.config)",
            "",
            "Окружение:",
            ".окружение — все ключевые пути: home, Python, Chrome, executor, registry, jobs, outbox (.env)",
            ".пути — то же самое, что .окружение (.paths)",
            ".проверка-окружения — проверка Python, Chrome, CDP, executor и файлов состояния (.env-check)",
            "",
            "Агенты:",
            ".агент-регистр — registry агентов, JID, aliases, provider, account, available (.agent-registry)",
            ".агент-цели — кто попадёт под 'всем!' и примеры multi-target синтаксиса (.agent-targets)",
            ".участники-комнаты — кого Gajim реально видит в MUC и как они матчятся с registry (.muc-participants)",
            ".агент-аккаунты — XMPP-аккаунты, видимые Gajim-плагину (.agent-accounts)",
            ".агент-тест-отправки <agent> <текст> — тест отправки от agent account (.agent-send-test)",
            ".агент-регистр-задать — сохранить registry из многострочного сообщения (.agent-registry-set)",
            "",
            "Очереди и доставка:",
            ".агент-задачи — agent job lifecycle (.agent-jobs)",
            ".агент-исходящие — agent outbox/delivery-state (.agent-outbox)",
            ".агент-ожидание — pending provider jobs и autorun status (.agent-pending)",
            ".результаты — обычные provider results (.results)",
            ".доставить — ручная доставка provider results, если auto-deliver выключен (.deliver)",
            "",
            "Provider/autorun:",
            ".агент-провайдер — provider mode и autorun status (.agent-provider)",
            ".автозапуск-вкл — включить auto-run executor и auto-deliver (.autorun-on)",
            ".автозапуск-выкл — выключить auto-run executor (.autorun-off)",
            "",
            "Панель:",
            ".панель — переинициализировать AI-панель (.panel)",
            ".панель-вкл — то же самое (.panel-on)",
            "",
            "Обращение к одному агенту:",
            "Вова, <задание>",
            "Qwen, <задание>",
            "Сирожа, <задание>",
            "",
            "Обращение к нескольким агентам:",
            "Вова, Qwen, Сирожа: <одно общее задание>",
            "Вова, Qwen, Сирожа, <одно общее задание>",
            "всем! <одно общее задание> — разложить всем enabled agents",
            "",
            "Круг с канцлером:",
            ".круг <ведущий>: <тема> — создать управляемый круг; ведущий является полноценным участником и канцлером",
            ".круг-статус — статус текущего круга",
            ".круг-дальше — следующий канцлерский ход по накопленному контексту",
            ".круг-ответ <текст> — добавить уточнение человека в контекст круга",
            ".круг-стоп — остановить текущий круг",
            "",
            "Важно: сообщения от agent accounts не запускают новые задачи, чтобы агенты не зацикливались.",
        ])

    def _handle_command(self, text: str, account: str = "", room_jid: str = "") -> str | None:
        cmd = self._canonical_command(text)
        cmd_l = cmd.lower()

        if cmd_l == "!ping":
            return (
                "AI Chatter Bridge: активен.\n"
                f"Комната в настройках: {self.config['room_jid']}\n"
                "AI Chatter Gajim Plugin 4.0.17 Branch 4 addressed Qwen + DeepSeek direct"
            )

        if cmd_l == "!room":
            return f"AI Chatter Bridge: основная конференция — {self.config['room_jid']}"

        if cmd_l == "!bots":
            return self._format_bots()

        if cmd_l == "!status":
            return self._format_status()

        if cmd_l == "!agent-accounts":
            return self._format_agent_accounts_probe()

        if cmd_l == "!muc-participants":
            self._remember_controller_identity(account, None)
            return self._format_muc_participants(room_jid)

        if cmd_l == "!agent-registry":
            return self._format_agent_registry()

        if cmd_l == "!agent-registry-template":
            return self._format_agent_registry_set_help()

        if cmd_l.startswith("!agent-registry-set"):
            return self._handle_agent_registry_set_command(cmd)

        if cmd_l == "!agent-outbox":
            return self._format_agent_outbox_status()

        if cmd_l == "!agent-jobs":
            return self._format_agent_jobs_status()

        if cmd_l == "!agent-provider":
            return (
                "AI Chatter Bridge R3.1: agent provider mode\n"
                f"- enabled: {self.config['agent_provider_enabled']}\n"
                f"- queue: {self._queue_path()}\n"
                f"- results: {self._results_path()}\n"
                f"- executor auto-run: {self.config['auto_run_executor']}\n"
                f"- auto-deliver results: {self.config['auto_deliver_results']}\n"
                f"- max auto jobs per run: {self.config['max_auto_jobs_per_run']}\n"
                "- flow: addressed agent message -> provider task -> pending-aware executor -> auto-delivery -> XMPP agent outbox"
            )

        if cmd_l == "!agent-pending":
            return self._format_agent_pending_status()

        if cmd_l == "!autorun-on":
            self.config["auto_run_executor"] = True
            self.config["auto_deliver_results"] = True
            self.save_config()
            return "AI Chatter Bridge R3.1: auto-run и auto-deliver включены."

        if cmd_l == "!autorun-off":
            self.config["auto_run_executor"] = False
            self.save_config()
            return "AI Chatter Bridge R3.1: auto-run executor выключен."

        if cmd_l.startswith("!agent-test"):
            parts = cmd.split(maxsplit=2)
            if len(parts) < 3:
                return "AI Chatter Bridge R1: используйте .agent-test vova_gpt@yax.im текст"
            agent_ref = parts[1].strip()
            probe_text = parts[2].strip()
            return self._send_agent_probe_message(agent_ref, room_jid, probe_text)

        if cmd_l.startswith("!agent-send-test"):
            return self._format_agent_send_test(cmd, room_jid)

        if cmd_l == "!bus-status":
            return self._format_branch4_bus_status()

        if cmd_l == "!bus-ping":
            return self._run_branch4_bus_ping()

        if cmd_l.startswith("!bus-qwen-test"):
            parts = cmd.split(maxsplit=1)
            probe_text = parts[1].strip() if len(parts) > 1 else "ответь одним словом: qwen-gajim"
            return self._run_branch4_provider_test("qwen", probe_text)

        if cmd_l.startswith("!bus-deepseek-test"):
            parts = cmd.split(maxsplit=1)
            probe_text = parts[1].strip() if len(parts) > 1 else "answer one word: deepseek-gajim"
            return self._run_branch4_provider_test("deepseek", probe_text)

        if cmd_l in ("!env", "!paths"):
            return self._format_env_status()

        if cmd_l == "!env-check":
            return self._format_env_check()

        if cmd_l == "!config":
            return self._format_config()

        if cmd_l in ("!help", "!хелп", "!aichatter"):
            return self._format_help()

        if cmd_l.startswith("!circle-answer"):
            return self._handle_circle_answer(cmd)

        if cmd_l == "!circle-status":
            return self._format_circle_status()

        if cmd_l == "!circle-stop":
            return self._handle_circle_stop()

        if cmd_l == "!circle-next":
            return self._handle_circle_next(account, room_jid)

        if cmd_l.startswith("!circle"):
            return self._handle_circle_start(cmd, account, room_jid)

        if cmd_l == "!agent-targets":
            return self._format_agent_targets()

        if cmd_l in ("!panel", "!panel-on"):
            try:
                self._refresh_panel()
            except Exception:
                log.exception("Panel refresh failed")
            return "AI Chatter Bridge: AI-панель переинициализирована."

        if cmd_l.startswith("!show"):
            bots = self._parse_bot_args(cmd)
            if not bots:
                return "AI Chatter Bridge: укажите ботов, например .show chatgpt,qwen"
            self._set_string_list_config("active_bot_ids_text", bots)
            self.save_config()
            self._refresh_panel()
            return f"AI Chatter Bridge: адресовать — {self.config['active_bot_ids_text']}"

        if cmd_l.startswith("!speak"):
            bots = self._parse_bot_args(cmd)
            if not bots:
                return "AI Chatter Bridge: укажите ботов, например .speak chatgpt,qwen"
            self._set_string_list_config("can_speak_bot_ids_text", bots)
            self.save_config()
            self._refresh_panel()
            return f"AI Chatter Bridge: могут говорить — {self.config['can_speak_bot_ids_text']}"

        if cmd_l.startswith("!only"):
            bots = self._parse_bot_args(cmd)
            if len(bots) != 1:
                return "AI Chatter Bridge: укажите одного бота, например .only chatgpt"
            self._set_string_list_config("active_bot_ids_text", bots)
            self._set_string_list_config("can_speak_bot_ids_text", bots)
            self.save_config()
            self._refresh_panel()
            return f"AI Chatter Bridge: режим беседы только с {bots[0]}"

        if cmd_l in ("!all",):
            all_ids = self._bot_ids()
            self._set_string_list_config("active_bot_ids_text", all_ids)
            self._set_string_list_config("can_speak_bot_ids_text", all_ids)
            self.save_config()
            self._refresh_panel()
            return "AI Chatter Bridge: включены все боты."

        if cmd_l.startswith("!mute"):
            bots = set(self._parse_bot_args(cmd))
            current = [b for b in self._string_list_config("can_speak_bot_ids_text") if b not in bots]
            self._set_string_list_config("can_speak_bot_ids_text", current)
            self.save_config()
            self._refresh_panel()
            return f"AI Chatter Bridge: могут говорить — {self.config['can_speak_bot_ids_text']}"

        if cmd_l.startswith("!unmute"):
            bots = self._parse_bot_args(cmd)
            current = set(self._string_list_config("can_speak_bot_ids_text"))
            current.update(bots)
            self._set_string_list_config("can_speak_bot_ids_text", sorted(current))
            self.save_config()
            self._refresh_panel()
            return f"AI Chatter Bridge: могут говорить — {self.config['can_speak_bot_ids_text']}"

        if cmd_l in ("!version",):
            return "AI Chatter Bridge: AI Chatter Gajim Plugin 4.0.17 Branch 4 addressed Qwen + DeepSeek direct"

        if cmd_l in ("!queue",):
            return (
                "AI Chatter Bridge: очередь задач\n"
                f"- включена: {self.config['queue_enabled']}\n"
                f"- файл: {self._queue_path()}"
            )

        if cmd_l in ("!results",):
            total, delivered, pending = self._result_counts()
            return (
                "AI Chatter Bridge: результаты\n"
                "- режим: ручная доставка, без таймера\n"
                f"- файл: {self._results_path()}\n"
                f"- всего результатов: {total}\n"
                f"- уже доставлено: {delivered}\n"
                f"- ожидают доставки: {pending}\n"
                f"- размер пачки .deliver: {self.config['result_delivery_batch_size']}"
            )

        if cmd_l in ("!deliver",):
            count = self._deliver_results_once()
            total, delivered, pending = self._result_counts()
            return (
                "AI Chatter Bridge: доставка результатов выполнена.\n"
                f"- доставлено сейчас: {count}\n"
                f"- осталось недоставленных: {pending}"
            )

        if cmd_l.startswith("!routing"):
            parts = cmd_l.split()
            if len(parts) < 2 or parts[1] not in ("on", "off"):
                return f"AI Chatter Bridge: routing сейчас {self.config['routing_enabled']}. Используйте .routing on/off"
            self.config["routing_enabled"] = parts[1] == "on"
            self.save_config()
            return f"AI Chatter Bridge: routing = {self.config['routing_enabled']}"

        return None


    def _branch4_home(self) -> Path:
        return Path(self._detect_ai_chatter_home())

    def _branch4_python(self) -> str:
        try:
            return self._detect_python_executable()
        except Exception:
            value = str(self._cfg("python_executable", "")).strip()
            return value or "python"

    def _branch4_executor_script(self, name: str) -> Path:
        return self._branch4_home() / "ai_chatter_executor" / name

    def _branch4_run_cli(self, script_name: str, args: list[str], timeout: int = 90) -> tuple[int, str]:
        script = self._branch4_executor_script(script_name)
        if not script.exists():
            return 127, f"script not found: {script}"
        cmd = [self._branch4_python(), str(script), "--home", str(self._branch4_home()), *args]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(script.parent),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return int(proc.returncode), proc.stdout or ""
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or "") + f"\nTIMEOUT after {timeout}s"
        except Exception as exc:
            log.exception("Branch 4 CLI call failed")
            return 1, f"{type(exc).__name__}: {exc}"

    def _format_branch4_bus_status(self) -> str:
        home = self._branch4_home()
        executor = home / "ai_chatter_executor"
        native_host = home / "ai_chatter_native_host"
        extension_note = "проверяется командой .шина-пинг"
        req = home / "configs" / "ai_chatter_bus_requests.jsonl"
        ev = home / "logs" / "ai_chatter_bus_events.jsonl"
        return (
            "AI Chatter Gajim Plugin 4.0.17: Branch 4 Local Bus status\n"
            f"- home: {home} [exists={home.exists()}]\n"
            f"- executor: {executor} [exists={executor.exists()}]\n"
            f"- native host: {native_host} [exists={native_host.exists()}]\n"
            f"- requests: {req} [exists={req.exists()}]\n"
            f"- events: {ev} [exists={ev.exists()}]\n"
            f"- python: {self._branch4_python()}\n"
            f"- extension/native: {extension_note}\n"
            "Команды: .шина-пинг, .шина-квен <текст>, .шина-дипсик <текст>; адресные Qwen/DeepSeek идут через Branch 4"
        )

    def _tail_for_muc(self, text: str, limit: int = 3200) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return "...\n" + text[-limit:]

    def _extract_reply_from_cli_output(self, output: str) -> str:
        # Best-effort extraction for provider.job.done JSON printed by Branch 4 CLI.
        try:
            matches = list(re.finditer(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', output, flags=re.S))
            if matches:
                raw = matches[-1].group(1)
                return json.loads('"' + raw + '"')
        except Exception:
            pass
        return ""

    def _run_branch4_bus_ping(self) -> str:
        code, out = self._branch4_run_cli("ai_chatter_bus_ping.py", [], timeout=15)
        ok = code == 0 and "OK: bus.pong received" in out
        return (
            "AI Chatter Gajim Plugin 4.0.17: Branch 4 bus ping\n"
            f"- ok: {ok}\n"
            f"- exit code: {code}\n"
            "- output:\n"
            f"{self._tail_for_muc(out)}"
        )

    def _run_branch4_qwen_test(self, probe_text: str) -> str:
        return self._run_branch4_provider_test("qwen", probe_text or "ответь одним словом: qwen-gajim")

    def _run_branch4_provider_test(self, provider: str, probe_text: str) -> str:
        provider = str(provider or "qwen").strip().lower()
        default_text = "ответь одним словом: qwen-gajim" if provider == "qwen" else "answer one word: deepseek-gajim"
        probe_text = str(probe_text or "").strip() or default_text
        code, out = self._branch4_run_cli(
            "ai_chatter_bus_job_provider.py",
            ["--provider", provider, "--text", probe_text],
            timeout=120,
        )
        reply = self._extract_reply_from_cli_output(out)
        ok = code == 0 and "OK: provider job completed" in out

        version = ""
        elapsed = ""
        try:
            m = re.search(r'"version"\s*:\s*"([^"]+)"', out)
            if m:
                version = m.group(1)
            m = re.search(r'"elapsed_ms"\s*:\s*(\d+)', out)
            if m:
                elapsed = m.group(1)
        except Exception:
            pass

        title_provider = "Qwen" if provider == "qwen" else "DeepSeek"
        lines = [
            f"AI Chatter Gajim Plugin 4.0.17: Branch 4 {title_provider} provider test",
            f"- ok: {ok}",
            f"- exit code: {code}",
            f"- prompt: {probe_text}",
        ]
        if reply:
            lines.append(f"- reply: {reply}")
        if version:
            lines.append(f"- version: {version}")
        if elapsed:
            lines.append(f"- elapsed_ms: {elapsed}")
        if not ok:
            lines.extend(["- output tail:", self._tail_for_muc(out, limit=1200)])
        else:
            lines.append("- full diagnostics: G:\\AI_chatter\\logs\\ai_chatter_bus_events.jsonl")
        return "\n".join(lines)

    def _branch4_provider_enabled_for_addressed(self, provider: str) -> bool:
        # Branch 4 rollout: addressed Qwen/DeepSeek are routed through Local Bus.
        # Other agents, multi-target and circle flows stay on the legacy pipeline
        # until their provider workers are implemented and tested.
        return str(provider or "").strip().lower() in ("qwen", "deepseek")

    def _branch4_qwen_enabled_for_addressed(self) -> bool:
        return self._branch4_provider_enabled_for_addressed("qwen")

    def _send_branch4_qwen_addressed(self, **kwargs) -> bool:
        kwargs["provider"] = "qwen"
        return self._send_branch4_provider_addressed(**kwargs)

    def _send_branch4_provider_addressed(
        self,
        *,
        controller_account: str,
        room_jid: str,
        agent_account: str,
        payload: str,
        provider: str,
    ) -> bool:
        payload = str(payload or "").strip() or "(пусто)"
        provider = str(provider or "qwen").strip().lower()
        title_provider = "Qwen" if provider == "qwen" else "DeepSeek"

        def worker() -> None:
            code, out = self._branch4_run_cli(
                "ai_chatter_bus_job_provider.py",
                ["--provider", provider, "--text", payload],
                timeout=150,
            )
            reply = self._extract_reply_from_cli_output(out)
            ok = code == 0 and "OK: provider job completed" in out and bool(reply)

            def deliver() -> bool:
                if ok:
                    self._remember_agent_output(room_jid, reply)
                    self._send_reply(agent_account, room_jid, reply)
                    return False
                err = ""
                try:
                    m = re.search(r'"code"\s*:\s*"([^"]+)"', out)
                    if m:
                        err = m.group(1)
                except Exception:
                    pass
                message = (
                    f"AI Chatter Gajim Plugin 4.0.17: Branch 4 {title_provider} addressed failed\n"
                    f"- ok: False\n"
                    f"- exit code: {code}\n"
                    f"- error: {err or '-'}\n"
                    "- full diagnostics: G:\\AI_chatter\\logs\\ai_chatter_bus_events.jsonl"
                )
                self._send_reply(controller_account, room_jid, message)
                return False

            GLib.idle_add(deliver)

        try:
            threading.Thread(target=worker, name=f"ai-chatter-branch4-{provider}", daemon=True).start()
        except Exception:
            log.exception("Could not start Branch 4 provider thread")
            message = (
                f"AI Chatter Gajim Plugin 4.0.17: Branch 4 {title_provider} addressed failed\n"
                "- ok: False\n"
                "- error: thread_start_failed"
            )
            self._send_reply(controller_account, room_jid, message)
        return True

    def _format_bots(self) -> str:
        self._load_profiles()
        self._bots = self._build_bot_registry(self._profiles)
        if not self._bots:
            return "AI Chatter Bridge: профили ботов не найдены."

        statuses = self._update_bot_statuses()
        visible = set(self._string_list_config("active_bot_ids_text"))
        can_speak = set(self._string_list_config("can_speak_bot_ids_text"))

        lines = ["AI Chatter Bridge: боты из профилей:"]
        for bot in self._bots:
            flags = []
            flags.append("адресуется" if bot["id"] in visible else "не адресуется")
            flags.append("говорит" if bot["id"] in can_speak else "молчит")
            status = statuses.get(bot["id"], "offline")
            icon = "●" if status == "online" else "●"
            lines.append(
                f'- {icon} {bot["title"]} ({bot["id"]}) — {", ".join(flags)}; '
                f'status={status}; profile={bot["profileKey"]}'
            )
        return "\n".join(lines)

    def _format_status(self) -> str:
        self._load_profiles()
        self._bots = self._build_bot_registry(self._profiles)
        statuses = self._update_bot_statuses()
        lines = ["AI Chatter Bridge: статус ботов"]
        for bot in self._bots:
            status = statuses.get(bot["id"], "offline")
            icon = "●" if status == "online" else "●"
            lines.append(f'- {icon} {bot["title"]} ({bot["id"]}) — {status}')
        return "\n".join(lines)

    def _format_config(self) -> str:
        return (
            "AI Chatter Bridge: настройки\n"
            f"- конференция: {self.config['room_jid']}\n"
            f"- routing обычных сообщений: {self.config['routing_enabled']}\n"
            f"- мягкое распознавание обычного текста: {self.config['route_any_non_command_in_room']}\n"
            f"- адресовать: {self.config['active_bot_ids_text']}\n"
            f"- могут говорить: {self.config['can_speak_bot_ids_text']}\n"
            f"- скрытые уведомления: {self.config['hidden_bots_can_notify']}\n"
            f"- приоритетные маркеры: {self.config['priority_markers_text']}\n"
            f"- боты общаются между собой: {self.config['free_bot_conversation']}\n"
            f"- только по активным задачам: {self.config['only_within_active_task']}\n"
            f"- лимит сообщений ботов подряд: {self.config['max_bot_only_turns']}\n"
            f"- CDP status URL: {self.config['chrome_cdp_url']}\n"
            f"- offline behavior: {self.config['offline_behavior']}\n"
            f"- auto-run executor: {self.config['auto_run_executor']}\n"
            f"- executor command: {self.config['executor_command']}"
        )

    def _target_bot_ids(self) -> list[str]:
        visible = set(self._string_list_config("active_bot_ids_text"))
        can_speak = set(self._string_list_config("can_speak_bot_ids_text"))
        statuses = self._update_bot_statuses()
        known = self._bot_ids()
        return [
            bot_id
            for bot_id in known
            if bot_id in visible
            and bot_id in can_speak
            and statuses.get(bot_id) == "online"
        ]

    def _checked_target_bot_ids(self) -> list[str]:
        visible = set(self._string_list_config("active_bot_ids_text"))
        can_speak = set(self._string_list_config("can_speak_bot_ids_text"))
        known = self._bot_ids()
        return [bot_id for bot_id in known if bot_id in visible and bot_id in can_speak]


    def _ensure_runtime_config_defaults(self) -> None:
        defaults = {
            "python_executable": "",
            "ai_chatter_home": "",
            "executor_workdir": "",
            "executor_command": "",
            "auto_run_executor": True,
            "executor_log_file_name": "logs/ai_chatter_event_executor.log",
            "chrome_executable": "",
            "chrome_user_data_dir": "",
        }
        changed = False
        for key, value in defaults.items():
            try:
                _ = self.config[key]
            except Exception:
                try:
                    self.config[key] = value
                    changed = True
                except Exception:
                    pass
        if changed:
            try:
                self.save_config()
            except Exception:
                pass

    def _cfg(self, key: str, default: object = "") -> object:
        try:
            return self.config[key]
        except Exception:
            return default

    def _path_exists_text(self, path_text: str) -> str:
        try:
            return "True" if path_text and Path(path_text).exists() else "False"
        except Exception:
            return "False"

    def _path_writable_text(self, path_text: str) -> str:
        try:
            path = Path(path_text)
            target_dir = path if path.suffix == "" else path.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            probe = target_dir / ".ai_chatter_write_test.tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return "True"
        except Exception:
            return "False"

    def _detect_chrome_executable(self) -> str:
        configured = str(self._cfg("chrome_executable", "")).strip()
        if configured and Path(configured).exists():
            return configured
        candidates = [
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path("G:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("G:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ]
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        found = shutil.which("chrome") or shutil.which("chrome.exe")
        return found or ""

    def _detect_chrome_user_data_dir(self) -> str:
        configured = str(self._cfg("chrome_user_data_dir", "")).strip()
        if configured:
            return configured
        return str(Path(self._detect_ai_chatter_home()) / "chrome-cdp-profile")

    def _runtime_paths(self) -> dict[str, str]:
        home = self._detect_ai_chatter_home()
        executor_workdir = self._detect_executor_workdir()
        queue = str(self._queue_path())
        paths = {
            "home": home,
            "python": self._detect_python_executable(),
            "chrome": self._detect_chrome_executable(),
            "chrome_user_data_dir": self._detect_chrome_user_data_dir(),
            "chrome_cdp_url": str(self._cfg("chrome_cdp_url", "http://127.0.0.1:9222")),
            "executor_workdir": executor_workdir,
            "run_once": str(Path(executor_workdir) / "ai_chatter_run_once.py"),
            "queue": queue,
            "results": str(self._results_path()),
            "executor_config": str(self._home_configs_path("ai_chatter_executor_config.json")),
            "agent_registry": str(self._agent_registry_path()),
            "configs_dir": str(self._configs_dir_path()),
            "profiles": str(self._home_configs_path("ai_chatter_profiles.json")),
            "agent_jobs": str(self._agent_jobs_path()),
            "agent_outbox": str(self._agent_outbox_path()),
            "chrome_jobs": str(self._home_logs_path("ai_chatter_chrome_jobs.jsonl")),
            "chrome_results": str(self._home_logs_path("ai_chatter_chrome_results.jsonl")),
            "plugin_dir": str(Path(self.local_file_path("")).parent),
            "gajim_python": sys.executable,
        }
        return paths

    def _format_env_status(self) -> str:
        self._ensure_runtime_config_defaults()
        self._ensure_executor_runtime_config()
        paths = self._runtime_paths()
        command = self._format_executor_command(max_items=3)
        return "\n".join([
            "AI Chatter Bridge R3.2: environment",
            f"- home: {paths['home']} [exists={self._path_exists_text(paths['home'])}; writable={self._path_writable_text(paths['home'])}]",
            f"- python: {paths['python']} [exists={self._path_exists_text(paths['python'])}]",
            f"- gajim python: {paths['gajim_python']}",
            f"- chrome: {paths['chrome'] or '?'} [exists={self._path_exists_text(paths['chrome'])}]",
            f"- chrome profile: {paths['chrome_user_data_dir']}",
            f"- cdp: {paths['chrome_cdp_url']}",
            f"- executor dir: {paths['executor_workdir']} [exists={self._path_exists_text(paths['executor_workdir'])}]",
            f"- run_once: {paths['run_once']} [exists={self._path_exists_text(paths['run_once'])}]",
            f"- queue: {paths['queue']}",
            f"- results: {paths['results']}",
            f"- executor config: {paths['executor_config']} [exists={self._path_exists_text(paths['executor_config'])}]",
            f"- registry: {paths['agent_registry']} [exists={self._path_exists_text(paths['agent_registry'])}]",
            f"- profiles: {paths['profiles']} [exists={self._path_exists_text(paths['profiles'])}]",
            f"- jobs: {paths['agent_jobs']}",
            f"- outbox: {paths['agent_outbox']}",
            f"- chrome jobs: {paths['chrome_jobs']}",
            f"- chrome results: {paths['chrome_results']}",
            f"- plugin dir: {paths['plugin_dir']}",
            f"- executor command: {command}",
        ])

    def _format_env_check(self) -> str:
        self._ensure_runtime_config_defaults()
        self._ensure_executor_runtime_config()
        paths = self._runtime_paths()
        checks: list[tuple[str, bool, str]] = []
        checks.append(("home exists", Path(paths["home"]).exists(), paths["home"]))
        checks.append(("home writable", self._path_writable_text(paths["home"]) == "True", paths["home"]))
        checks.append(("configs writable", self._path_writable_text(paths["configs_dir"]) == "True", paths["configs_dir"]))
        checks.append(("python exists", Path(paths["python"]).exists(), paths["python"]))
        checks.append(("executor dir exists", Path(paths["executor_workdir"]).exists(), paths["executor_workdir"]))
        checks.append(("run_once exists", Path(paths["run_once"]).exists(), paths["run_once"]))
        checks.append(("executor config exists", Path(paths["executor_config"]).exists(), paths["executor_config"]))
        checks.append(("registry exists", Path(paths["agent_registry"]).exists(), paths["agent_registry"]))
        checks.append(("profiles exists", Path(paths["profiles"]).exists(), paths["profiles"]))
        checks.append(("agent jobs writable", self._path_writable_text(paths["agent_jobs"]) == "True", paths["agent_jobs"]))
        checks.append(("agent outbox writable", self._path_writable_text(paths["agent_outbox"]) == "True", paths["agent_outbox"]))
        checks.append(("chrome executable exists", bool(paths["chrome"]) and Path(paths["chrome"]).exists(), paths["chrome"] or "?"))
        cdp_ok = False
        cdp_err = ""
        try:
            url = str(paths["chrome_cdp_url"]).rstrip("/") + "/json/version"
            with urllib.request.urlopen(url, timeout=2) as response:
                cdp_ok = response.status == 200
        except Exception as exc:
            cdp_err = str(exc)
        checks.append(("chrome cdp reachable", cdp_ok, paths["chrome_cdp_url"] if cdp_ok else f"{paths['chrome_cdp_url']} ({cdp_err})"))
        lines = ["AI Chatter Bridge R3.2: environment check"]
        ok_all = True
        for label, ok, detail in checks:
            ok_all = ok_all and ok
            mark = "OK" if ok else "FAIL"
            lines.append(f"- {mark}: {label}: {detail}")
        lines.append(f"- overall: {'OK' if ok_all else 'CHECK_FAILED'}")
        lines.append("Подсказка: если переехали между домом/работой, нажмите Auto-detect в настройках или укажите один AI Chatter home: C:\\AI_chatter / G:\\AI_chatter.")
        return "\n".join(lines)

    def _detect_python_executable(self) -> str:
        configured = str(self._cfg("python_executable", "")).strip()
        if configured and Path(configured).exists():
            return configured

        # Prefer Python launcher if available, because PATH can point to unrelated Python
        # such as Inkscape's bundled interpreter.
        try:
            completed = subprocess.run(
                ["py", "-0p"],
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
            )
            lines = (completed.stdout or "").splitlines()
            for preferred in ("3.14", "3.12", "3.11", "3.10", "3.9"):
                for line in lines:
                    if preferred in line:
                        candidate = line.split()[-1].strip()
                        if candidate.lower().endswith("python.exe") and Path(candidate).exists():
                            return candidate
        except Exception:
            pass

        # Common root and per-user installation paths.
        for root_candidate in (
            Path("C:/Python314/python.exe"),
            Path("C:/Python312/python.exe"),
            Path("C:/Python311/python.exe"),
            Path("G:/Python314/python.exe"),
            Path("G:/Python312/python.exe"),
            Path("G:/Python311/python.exe"),
        ):
            if root_candidate.exists():
                return str(root_candidate)

        user_profile = os.environ.get("USERPROFILE", "")
        for version in ("Python314", "Python312", "Python311", "Python310", "Python39"):
            candidate = Path(user_profile) / "AppData" / "Local" / "Programs" / "Python" / version / "python.exe"
            if candidate.exists():
                return str(candidate)

        # Last resort: PATH.
        path_python = shutil.which("python")
        if path_python:
            return path_python

        return "python"

    def _detect_ai_chatter_home(self) -> str:
        configured = str(self._cfg("ai_chatter_home", "")).strip()
        if configured and Path(configured).exists():
            return configured

        env_home = os.environ.get("AI_CHATTER_HOME", "").strip()
        if env_home and Path(env_home).exists():
            return env_home

        for candidate in (
            Path("C:/AI_chatter"),
            Path("G:/AI_chatter"),
            Path("D:/AI_chatter"),
        ):
            if (candidate / "ai_chatter_executor").exists():
                return str(candidate)

        # If executor_workdir is manually set, infer home from it.
        workdir = str(self._cfg("executor_workdir", "")).strip()
        if workdir:
            path = Path(workdir)
            if path.name == "ai_chatter_executor":
                return str(path.parent)

        return "C:\\AI_chatter"

    def _detect_executor_workdir(self) -> str:
        configured = str(self._cfg("executor_workdir", "")).strip()
        if configured and Path(configured).exists():
            return configured

        home = Path(self._detect_ai_chatter_home())
        candidate = home / "ai_chatter_executor"
        return str(candidate)

    def _build_default_executor_command(self) -> str:
        python_exe = self._detect_python_executable()
        executor_workdir = Path(self._detect_executor_workdir())
        run_once = executor_workdir / "ai_chatter_run_once.py"
        return f'"{python_exe}" "{run_once}" --queue "{{queue}}" --config "{{config}}" --max-tasks 3 --max-jobs 3 --timeout-ms 60000'

    def _autodetect_executor_settings(self, save: bool = True) -> tuple[str, str, str]:
        python_exe = self._detect_python_executable()
        home = self._detect_ai_chatter_home()
        workdir = self._detect_executor_workdir()
        command = f'"{python_exe}" "{Path(workdir) / "ai_chatter_run_once.py"}" --queue "{{queue}}" --config "{{config}}" --max-tasks 3 --max-jobs 3 --timeout-ms 60000'

        if save:
            self.config["python_executable"] = python_exe
            self.config["ai_chatter_home"] = home
            self.config["executor_workdir"] = workdir
            self.config["executor_command"] = command
            if not str(self._cfg("chrome_executable", "")).strip():
                self.config["chrome_executable"] = self._detect_chrome_executable()
            if not str(self._cfg("chrome_user_data_dir", "")).strip():
                self.config["chrome_user_data_dir"] = self._detect_chrome_user_data_dir()
            self.save_config()

        return python_exe, workdir, command

    def _ensure_executor_runtime_config(self) -> None:
        try:
            path = self._home_configs_path("ai_chatter_executor_config.json")
            payload = {}
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:
                    payload = {}

            changed = False

            # Executor Stage 3.2 reads legacy camelCase providerMode.
            # Keep both legacy and new keys in sync.
            if payload.get("providerMode") != "chrome_bridge":
                payload["providerMode"] = "chrome_bridge"
                changed = True
            if payload.get("provider_mode") != "chrome_bridge":
                payload["provider_mode"] = "chrome_bridge"
                changed = True
            if payload.get("bridge_notices") is not False:
                payload["bridge_notices"] = False
                changed = True

            bots = payload.get("bots")
            if not isinstance(bots, dict):
                bots = {}
                payload["bots"] = bots
                changed = True

            defaults = {
                "chatgpt": ("ChatGPT", "chatgpt::https://chatgpt.com"),
                "qwen": ("Qwen", "qwen::https://chat.qwen.ai"),
                "deepseek": ("DeepSeek", "deepseek::https://chat.deepseek.com"),
            }
            for bot_id, (title, profile_key) in defaults.items():
                bot = bots.get(bot_id)
                if not isinstance(bot, dict):
                    bot = {}
                    bots[bot_id] = bot
                    changed = True
                values = {
                    "title": title,
                    "profileKey": profile_key,
                    "provider": "chrome_bridge",
                    "enabled": True,
                }
                for key, value in values.items():
                    if bot.get(key) != value:
                        bot[key] = value
                        changed = True

            chrome_bridge = payload.get("chromeBridge")
            if not isinstance(chrome_bridge, dict):
                chrome_bridge = {}
                payload["chromeBridge"] = chrome_bridge
                changed = True
            values = {
                "jobsFileName": "logs/ai_chatter_chrome_jobs.jsonl",
                "resultsFileName": "logs/ai_chatter_chrome_results.jsonl",
                "intakeStateFileName": "logs/ai_chatter_chrome_intake_state.json",
                "writeBridgeNotificationsToGajimResults": False,
            }
            for key, value in values.items():
                if chrome_bridge.get(key) != value:
                    chrome_bridge[key] = value
                    changed = True

            limits = payload.get("limits")
            if not isinstance(limits, dict):
                limits = {}
                payload["limits"] = limits
                changed = True
            for key, value in {"maxTasksPerRun": 1, "maxBridgeResultsPerRun": 10}.items():
                if limits.get(key) != value:
                    limits[key] = value
                    changed = True

            if changed or not path.exists():
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.exception("Could not ensure ai_chatter_executor_config.json")

    def _executor_log_path(self) -> Path:
        name = str(self.config["executor_log_file_name"]).strip() or "logs/ai_chatter_event_executor.log"
        return self._home_logs_path(name)

    def _executor_command_looks_valid(self, command: str) -> bool:
        command = (command or "").strip()
        if not command:
            return False

        # Bare "python ..." is unsafe on Windows: it may point to Inkscape or
        # another bundled interpreter. Force auto-detect to store full path.
        lowered = command.lower().lstrip()
        if lowered.startswith("python ") or lowered.startswith("python.exe "):
            return False

        # Common case:
        # "C:\Python...\python.exe" "C:\AI_chatter\...\ai_chatter_run_once.py" ...
        # or:
        # C:\Python...\python.exe C:\AI_chatter\...\ai_chatter_run_once.py ...
        match = re.search(r'([A-Za-z]:\\[^" ]*ai_chatter_run_once\.py)', command)
        if not match:
            # If there is no explicit ai_chatter_run_once.py path, keep command only
            # when it does not obviously point to the old G:\AI_chatter location.
            return "G:\\AI_chatter" not in command and "G:/AI_chatter" not in command

        script_path = Path(match.group(1))
        return script_path.exists()

    def _format_executor_command(self, max_items: int | None = None) -> str:
        self._ensure_runtime_config_defaults()
        self._ensure_executor_runtime_config()

        command = str(self._cfg("executor_command", "")).strip()
        if not command or not self._executor_command_looks_valid(command):
            _python_exe, _workdir, command = self._autodetect_executor_settings(save=True)
            self._ensure_executor_runtime_config()

        queue = str(self._queue_path())
        local_dir = str(Path(self.local_file_path("")).parent)
        plugin_dir = local_dir
        rendered = (
            command
            .replace("{queue}", queue)
            .replace("{config}", str(self._home_configs_path("ai_chatter_executor_config.json")))
            .replace("{configs_dir}", str(self._configs_dir_path()))
            .replace("{plugin_dir}", plugin_dir)
            .replace("{local_dir}", local_dir)
        )
        if max_items is not None:
            limit = self._auto_run_limit(max_items)
            if re.search(r"--max-tasks\s+\d+", rendered):
                rendered = re.sub(r"--max-tasks\s+\d+", f"--max-tasks {limit}", rendered)
            else:
                rendered += f" --max-tasks {limit}"
            if re.search(r"--max-jobs\s+\d+", rendered):
                rendered = re.sub(r"--max-jobs\s+\d+", f"--max-jobs {limit}", rendered)
            else:
                rendered += f" --max-jobs {limit}"
        return rendered

    def _trigger_executor_async(self, pending_count: int | None = None) -> None:
        if not bool(self.config["auto_run_executor"]):
            return

        # Do not start parallel run_once processes. If new jobs arrive while the
        # executor is running, remember that another pass is needed.
        try:
            if self._executor_process is not None and self._executor_process.poll() is None:
                self._executor_needs_rerun = True
                return
        except Exception:
            self._executor_process = None

        if pending_count is None:
            pending_count = self._pending_agent_provider_count()
        limit = self._auto_run_limit(pending_count)
        self._executor_last_limit = limit

        command = self._format_executor_command(max_items=limit)
        if not command:
            return

        workdir_text = str(self._cfg("executor_workdir", "")).strip() or self._detect_executor_workdir()
        workdir = Path(workdir_text) if workdir_text else None

        try:
            log_path = self._executor_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("a", encoding="utf-8", errors="replace")
            log_file.write("\n=== AI Chatter R3.1 pending-aware executor launch ===\n")
            log_file.write(f"pending_count: {pending_count}\n")
            log_file.write(f"limit: {limit}\n")
            log_file.write(f"command: {command}\n")
            log_file.write(f"workdir: {workdir or ''}\n")
            log_file.flush()

            self._executor_process = subprocess.Popen(
                command,
                cwd=str(workdir) if workdir else None,
                shell=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            self._executor_needs_rerun = False
            GLib.timeout_add(1000, self._poll_executor_process)
            log.warning("AI Chatter R3.1 pending-aware executor launched: %s", command)
        except Exception:
            log.exception("Could not launch AI Chatter event-driven executor")
            self._executor_process = None

    def _poll_executor_process(self) -> bool:
        try:
            process = self._executor_process
            if process is None:
                return False
            if process.poll() is None:
                return True

            return_code = process.returncode
            self._executor_process = None
            log.warning("AI Chatter R3.1 executor finished: returncode=%s", return_code)

            delivered_now = 0
            if bool(self.config["auto_deliver_results"]):
                delivered_now = self._deliver_results_once(max_results=self._auto_run_limit(self._pending_agent_provider_count()))
                log.warning("AI Chatter R3.1 auto-delivered results: %s", delivered_now)

            pending = self._pending_agent_provider_count()
            if (self._executor_needs_rerun or (pending > 0 and bool(self.config["auto_rerun_pending"]))) and bool(self.config["auto_run_executor"]):
                self._executor_needs_rerun = False
                GLib.timeout_add(1500, self._trigger_executor_again_from_timeout)
            return False
        except Exception:
            log.exception("AI Chatter R3.1 executor polling failed")
            self._executor_process = None
            return False

    def _trigger_executor_again_from_timeout(self) -> bool:
        try:
            pending = self._pending_agent_provider_count()
            if pending > 0 or self._executor_needs_rerun:
                self._trigger_executor_async(pending_count=pending)
        except Exception:
            log.exception("AI Chatter R3.1 follow-up executor launch failed")
        return False

    def _is_ai_chatter_delivered_message(self, text: str) -> bool:
        normalized = " ".join((text or "").strip().split())
        if not normalized:
            return False

        # All own service/diagnostic messages must be ignored before routing.
        # Branch 4 diagnostic replies use a new prefix and must never be routed
        # into the old task queue after the plugin posts them into the MUC.
        branch4_prefixes = (
            "AI Chatter Gajim Plugin 4.",
            "AI Chatter Branch 4",
            "Branch 4 Qwen provider test",
            "Branch 4 DeepSeek provider test",
            "Branch 4 Local Bus status",
            "Branch 4 bus ping",
        )
        if normalized.startswith(branch4_prefixes):
            return True
        if normalized.startswith("AI Chatter Bridge:"):
            return True
        # Match all staged diagnostics: R1, R2, R2.1, R2.5, R3, etc.
        if normalized.startswith("AI Chatter Bridge R"):
            return True
        if normalized.startswith("Тест отправки:"):
            return True

        # Ignore messages delivered by this plugin, so they are not queued again.
        # Examples:
        # ChatGPT [RESULT]: ...
        # Qwen [ERROR]: ...
        # DeepSeek [QUESTION]: ...
        # AI Chatter Bridge: доставка результатов выполнена.
        if normalized.startswith("AI Chatter Bridge:"):
            return True

        bot_names = ("ChatGPT", "Qwen", "DeepSeek")
        tags = ("RESULT", "ERROR", "NOTICE", "QUESTION", "OFFLINE", "STUB")
        for bot_name in bot_names:
            for tag in tags:
                if normalized.startswith(f"{bot_name} [{tag}]:"):
                    return True

        # Defensive fallback for one-line rewritten result messages.
        if re.match(r"^(ChatGPT|Qwen|DeepSeek)\s+\[(RESULT|ERROR|NOTICE|QUESTION|OFFLINE|STUB)\]\s*:", normalized):
            return True

        # R1/R2 diagnostics must never become ordinary tasks.
        if normalized.startswith("AI Chatter Bridge R1:"):
            return True
        if normalized.startswith("AI Chatter Bridge R2:"):
            return True
        if normalized.startswith("AI Chatter Bridge R3:"):
            return True
        if normalized.startswith("Тест отправки:"):
            return True
        if ".agent-test " in normalized or ".agent-accounts" in normalized:
            return True

        return False

    def _queue_path(self) -> Path:
        name = str(self.config["queue_file_name"]).strip() or "ai_chatter_tasks.jsonl"
        candidate = Path(name)
        if candidate.is_absolute():
            return candidate
        try:
            home = Path(self._detect_ai_chatter_home())
            if str(home).strip():
                return home / name
        except Exception:
            pass
        return Path(self.local_file_path(name))

    def _append_task(self, *, account: str, jid: str, text: str, targets: list[str]) -> dict[str, object]:
        task = {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "status": "queued",
            "source": {
                "type": "gajim",
                "account": account,
                "roomJid": jid,
            },
            "targets": [
                {
                    "botId": bot_id,
                    "botTitle": self._bot_title(bot_id),
                }
                for bot_id in targets
            ],
            "message": {
                "text": text,
            },
            "routing": {
                "activeBotIds": self._string_list_config("active_bot_ids_text"),
                "canSpeakBotIds": self._string_list_config("can_speak_bot_ids_text"),
            },
        }

        queue_path = self._queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with queue_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(task, ensure_ascii=False) + "\n")
        return task

    def _simulate_routing(self, account: str, jid: str, text: str) -> None:
        checked_targets = self._checked_target_bot_ids()
        targets = self._target_bot_ids()
        if not targets:
            # If bots are checked but offline, default behavior is silent:
            # the panel status indicators show ● offline and no bot-specific
            # OFFLINE messages are added to the chat.
            if checked_targets and str(self.config["offline_behavior"]).lower() == "silent":
                return

            if bool(self.config["routing_echo_enabled"]):
                message = "AI Chatter Bridge: не выбран ни один bot-адресат. Поставьте галочку в AI-панели справа или используйте .only/.all."
                if checked_targets:
                    message = "AI Chatter Bridge: выбранные боты сейчас offline. Откройте вкладку бота в CDP Chrome или нажмите ↻ в панели."
                self._send_reply(account, jid, message)
            return

        task: dict[str, object] | None = None
        queue_error = ""
        if bool(self.config["queue_enabled"]):
            try:
                if self._is_ai_chatter_delivered_message(text):
                    return True

                self._ensure_executor_runtime_config()
                if self._is_ai_chatter_delivered_message(text):
                    return

                task = self._append_task(account=account, jid=jid, text=text, targets=targets)
                self._ensure_executor_runtime_config()
                self._trigger_executor_async(pending_count=len(targets) if "targets" in locals() else None)
            except Exception as error:
                queue_error = str(error)
                log.exception("Could not append AI Chatter task")

        if not bool(self.config["routing_echo_enabled"]):
            return

        names = ", ".join(self._bot_title(bot_id) for bot_id in targets)
        preview = text.replace("\n", " ").strip()
        if len(preview) > 180:
            preview = preview[:177] + "..."

        if task is not None:
            task_id = str(task["id"])
            queue_path = str(self._queue_path())
            reply = (
                "AI Chatter Bridge: задача поставлена в очередь.\n"
                f"ID: {task_id}\n"
                f"Кому: {names}\n"
                f"Файл очереди: {queue_path}\n"
                f"Текст: {preview}\n"
                "Исполнитель Python/Chrome запущен автоматически по событию новой задачи." if bool(self.config["auto_run_executor"]) else "Следующий этап — исполнитель Python/Chrome будет читать эту очередь и отправлять задачи в AI-чаты."
            )
        elif queue_error:
            reply = (
                "AI Chatter Bridge: задача маршрутизирована, но не записана в очередь.\n"
                f"Кому: {names}\n"
                f"Ошибка очереди: {queue_error}\n"
                f"Текст: {preview}"
            )
        else:
            reply = (
                "AI Chatter Bridge: задача принята в тестовую маршрутизацию.\n"
                f"Кому уйдёт дальше: {names}\n"
                f"Текст: {preview}\n"
                "Очередь задач выключена в настройках."
            )

        self._send_reply(account, jid, reply)

    def _results_path(self) -> Path:
        name = str(self.config["results_file_name"]).strip() or "logs/ai_chatter_results.jsonl"
        return self._home_logs_path(name)

    def _delivered_state_path(self) -> Path:
        return self._home_logs_path("ai_chatter_delivered_results.json")

    def _read_delivered_result_ids(self) -> set[str]:
        path = self._delivered_state_path()
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(data, dict):
            return set()
        items = data.get("deliveredResultIds", [])
        if not isinstance(items, list):
            return set()
        return {str(item) for item in items}

    def _write_delivered_result_ids(self, delivered: set[str]) -> None:
        path = self._delivered_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "deliveredResultIds": sorted(delivered),
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_results_jsonl(self) -> list[dict[str, object]]:
        path = self._results_path()
        if not path.exists():
            return []
        rows: list[dict[str, object]] = []
        try:
            with path.open("r", encoding="utf-8") as file:
                for line_no, line in enumerate(file, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        log.warning("Invalid AI Chatter result JSON at line %s", line_no)
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        except Exception:
            log.exception("Could not read AI Chatter results")
        return rows

    def _result_counts(self) -> tuple[int, int, int]:
        delivered_ids = self._read_delivered_result_ids()
        total = 0
        pending = 0
        for result in self._read_results_jsonl():
            result_id = str(result.get("id", ""))
            status = str(result.get("status", ""))
            if not result_id or status not in ("done", "error"):
                continue
            total += 1
            if result_id not in delivered_ids:
                pending += 1
        return total, len(delivered_ids), pending

    def _deliver_results_once(self, max_results: int | None = None) -> int:
        delivered_count = 0
        try:
            if max_results is None:
                try:
                    max_results = int(self.config["result_delivery_batch_size"])
                except Exception:
                    max_results = 3

            if max_results < 1:
                max_results = 1
            if max_results > 10:
                max_results = 10

            delivered = self._read_delivered_result_ids()
            changed = False

            for result in self._read_results_jsonl():
                if delivered_count >= max_results:
                    break

                result_id = str(result.get("id", ""))
                if not result_id or result_id in delivered:
                    continue

                status = str(result.get("status", ""))
                if status not in ("done", "error"):
                    continue

                source = result.get("source", {})
                if not isinstance(source, dict):
                    source = {}

                agent_delivery = source.get("agentDelivery")
                if not isinstance(agent_delivery, dict):
                    agent_delivery = self._lookup_agent_provider_delivery(result)
                if isinstance(agent_delivery, dict):
                    room_jid = str(agent_delivery.get("roomJid") or source.get("roomJid") or self.config["room_jid"])
                    agent_account = str(agent_delivery.get("agentAccount") or "")
                    agent_jid = str(agent_delivery.get("agentJid") or "")
                    if not agent_account and agent_jid:
                        agent_account = self._find_account_for_jid_or_name(agent_jid)
                    agent_id = str(agent_delivery.get("agentId") or result.get("botId") or "agent")
                    provider = str(agent_delivery.get("provider") or result.get("botId") or agent_id)
                    job_id = str(agent_delivery.get("jobId") or result_id)
                    input_text = str(agent_delivery.get("inputText") or "")

                    if status == "done":
                        message = str(result.get("answer", "")).strip() or "(пустой ответ)"
                    else:
                        error = str(result.get("error", "unknown error"))
                        message = f"[{provider} ERROR] {error}"

                    ok = self._deliver_agent_reply(
                        job_id=job_id,
                        stage="r3.1",
                        kind="agent_provider_result_delivery",
                        room_jid=self._normalize_jid(room_jid),
                        agent_id=agent_id,
                        provider=provider,
                        agent_jid=agent_jid,
                        agent_account=agent_account,
                        input_text=input_text,
                        reply_text=message,
                    )
                    if ok:
                        delivered.add(result_id)
                        delivered_count += 1
                        changed = True
                    continue

                account = str(source.get("account", ""))
                room_jid = str(source.get("roomJid") or self.config["room_jid"])
                bot_title = str(result.get("botTitle") or result.get("botId") or "Bot")

                if status == "done":
                    answer = str(result.get("answer", "")).strip()
                    if not answer:
                        answer = "(пустой ответ)"
                    message = f"{bot_title} [RESULT]:\n{answer}"
                else:
                    error = str(result.get("error", "unknown error"))
                    message = f"{bot_title} [ERROR]: {error}"

                ok = self._send_reply(account, self._normalize_jid(room_jid), message)
                if ok:
                    delivered.add(result_id)
                    delivered_count += 1
                    changed = True

            if changed:
                self._write_delivered_result_ids(delivered)

        except Exception:
            log.exception("AI Chatter manual result delivery failed")

        return delivered_count

    def _resolve_contact(self, client: object, jid: str) -> object | None:
        contacts = client.get_module("Contacts")
        try:
            contact = contacts.get_contact(jid, groupchat=True)
            if contact is not None:
                return contact
        except Exception:
            pass
        try:
            return contacts.get_contact(jid, groupchat=False)
        except Exception:
            return None

    def _send_reply_detailed(self, account: str, jid: str, message: str) -> tuple[bool, str]:
        if not account:
            return False, "missing_account"
        if not jid:
            return False, "missing_jid"
        if not message:
            return False, "missing_message"
        try:
            if not app.account_is_available(account):
                log.warning("Account reported not available, trying send anyway: %s", account)
        except Exception as exc:
            log.warning("Could not check account availability for %s: %s", account, exc)

        try:
            client = app.get_client(account)
        except Exception as exc:
            log.exception("Could not get client for account %s", account)
            return False, f"get_client_failed: {type(exc).__name__}: {exc}"

        contact = self._resolve_contact(client, jid)
        if contact is None:
            log.warning("Could not resolve contact for %s via account %s", jid, account)
            return False, f"contact_not_resolved: account={account}; room={jid}"

        # R2.6.7: do not fail early on GroupchatContact.is_joined.
        # In Gajim 2.4.x secondary accounts can be available and able to send to
        # the MUC even while the contact cache reports is_joined=False. The
        # previous diagnostic path produced false negatives even for Vova.
        # Let client.send_message() be the source of truth and report its real
        # exception if sending is impossible.
        if isinstance(contact, GroupchatContact) and not contact.is_joined:
            log.warning("Groupchat contact reports not joined, trying send anyway: account=%s room=%s", account, jid)

        if not isinstance(contact, (BareContact, GroupchatContact, GroupchatParticipant)):
            log.warning("Unsupported contact type for reply: %s", type(contact))
            return False, f"unsupported_contact_type: {type(contact).__name__}"

        try:
            outgoing = OutgoingMessage(account=account, contact=contact, text=message)
            client.send_message(outgoing)
        except Exception as exc:
            log.exception("Could not send reply through account %s to %s", account, jid)
            return False, f"send_message_failed: {type(exc).__name__}: {exc}"
        return True, ""

    def _send_reply(self, account: str, jid: str, message: str) -> bool:
        sent, _error = self._send_reply_detailed(account, jid, message)
        return sent

    # ------------------------------------------------------------------
    # Settings window remains as fallback
    # ------------------------------------------------------------------
    def _show_config_dialog(self, parent: Gtk.Window | None = None) -> None:
        if self._settings_window is not None:
            self._settings_window.present()
            return

        win = Gtk.Window(title="AI Chatter Bridge — настройки")
        win.set_default_size(640, 560)
        if parent is not None:
            win.set_transient_for(parent)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_child(root)

        top_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top_bar.set_margin_top(10)
        top_bar.set_margin_bottom(8)
        top_bar.set_margin_start(12)
        top_bar.set_margin_end(12)
        top_bar.set_halign(Gtk.Align.END)
        top_save_registry_button = Gtk.Button(label="Сохранить registry")
        top_save_button = Gtk.Button(label="Сохранить всё")
        top_close_button = Gtk.Button(label="Закрыть")
        top_save_registry_button.add_css_class("suggested-action")
        top_bar.append(top_save_registry_button)
        top_bar.append(top_save_button)
        top_bar.append(top_close_button)
        root.append(top_bar)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        root.append(scroll)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        scroll.set_child(box)

        title = Gtk.Label(label="AI Chatter Bridge")
        title.set_xalign(0)
        title.add_css_class("title-1")
        box.append(title)

        hint = Gtk.Label(label="Кнопки сохранения продублированы сверху, а окно настроек теперь прокручивается по высоте.")
        hint.set_wrap(True)
        hint.set_xalign(0)
        box.append(hint)

        room_entry = Gtk.Entry()
        room_entry.set_text(str(self.config["room_jid"]))
        room_entry.set_placeholder_text("kuhetaje@chat.yax.im")
        box.append(Gtk.Label(label="JID конференции"))
        box.append(room_entry)

        panel_enabled = Gtk.CheckButton(label="Показывать AI-панель справа в окне чата")
        panel_enabled.set_active(bool(self.config["chat_panel_enabled"]))
        box.append(panel_enabled)

        routing_echo = Gtk.CheckButton(label="Показывать отчёт о тестовой маршрутизации в чат")
        routing_echo.set_active(bool(self.config["routing_echo_enabled"]))
        box.append(routing_echo)

        route_any = Gtk.CheckButton(label="Считать обычный текст в комнате задачей, даже если Gajim не пометил его как исходящий")
        route_any.set_active(bool(self.config["route_any_non_command_in_room"]))
        box.append(route_any)

        queue_enabled = Gtk.CheckButton(label="Записывать задачи в JSONL-очередь")
        queue_enabled.set_active(bool(self.config["queue_enabled"]))
        box.append(queue_enabled)

        queue_name_entry = Gtk.Entry()
        queue_name_entry.set_text(str(self.config["queue_file_name"]))
        queue_name_entry.set_placeholder_text("ai_chatter_tasks.jsonl")
        box.append(Gtk.Label(label="Файл очереди"))
        box.append(queue_name_entry)

        cdp_entry = Gtk.Entry()
        cdp_entry.set_text(str(self.config["chrome_cdp_url"]))
        cdp_entry.set_placeholder_text("http://127.0.0.1:9222")
        box.append(Gtk.Label(label="Chrome CDP URL для online/offline статуса"))
        box.append(cdp_entry)

        chrome_exe_entry = Gtk.Entry()
        chrome_exe_entry.set_text(str(self._cfg("chrome_executable", "")))
        chrome_exe_entry.set_placeholder_text("пусто = автоопределение Chrome")
        box.append(Gtk.Label(label="Chrome executable"))
        box.append(chrome_exe_entry)

        chrome_profile_entry = Gtk.Entry()
        chrome_profile_entry.set_text(str(self._cfg("chrome_user_data_dir", "")))
        chrome_profile_entry.set_placeholder_text("пусто = {AI Chatter home}\chrome-cdp-profile")
        box.append(Gtk.Label(label="Chrome CDP profile / user-data-dir"))
        box.append(chrome_profile_entry)

        env_status_label = Gtk.Label(label="Environment: нажмите Auto-detect или .env-check для диагностики")
        env_status_label.set_wrap(True)
        env_status_label.set_xalign(0)
        box.append(env_status_label)

        auto_run = Gtk.CheckButton(label="Автоматически запускать executor после новой задачи")
        auto_run.set_active(bool(self.config["auto_run_executor"]))
        box.append(auto_run)

        auto_deliver = Gtk.CheckButton(label="Автоматически доставлять готовые provider-ответы")
        auto_deliver.set_active(bool(self.config["auto_deliver_results"]))
        box.append(auto_deliver)

        max_auto_jobs_entry = Gtk.Entry()
        max_auto_jobs_entry.set_text(str(self.config["max_auto_jobs_per_run"]))
        max_auto_jobs_entry.set_placeholder_text("например 3 или 5")
        box.append(Gtk.Label(label="Максимум provider jobs за auto-run"))
        box.append(max_auto_jobs_entry)

        python_entry = Gtk.Entry()
        python_entry.set_text(str(self._cfg("python_executable", "")))
        python_entry.set_placeholder_text("пусто = автоопределение через py -0p")
        box.append(Gtk.Label(label="Python executable"))
        box.append(python_entry)

        home_entry = Gtk.Entry()
        home_entry.set_text(str(self._cfg("ai_chatter_home", "")))
        home_entry.set_placeholder_text("например C:\\AI_chatter или G:\\AI_chatter")
        box.append(Gtk.Label(label="AI Chatter home"))
        box.append(home_entry)

        executor_workdir_entry = Gtk.Entry()
        executor_workdir_entry.set_text(str(self.config["executor_workdir"]))
        executor_workdir_entry.set_placeholder_text("например C:\\AI_chatter\\ai_chatter_executor")
        box.append(Gtk.Label(label="Рабочая папка executor"))

        workdir_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        workdir_row.append(executor_workdir_entry)

        choose_workdir_button = Gtk.Button(label="Выбрать папку…")
        choose_workdir_button.set_tooltip_text("Можно выбрать C:\\AI_chatter или сразу папку ai_chatter_executor")
        workdir_row.append(choose_workdir_button)

        autodetect_button = Gtk.Button(label="Auto-detect")
        autodetect_button.set_tooltip_text("Найти Python, Chrome и C:/G:/AI_chatter автоматически")
        workdir_row.append(autodetect_button)
        env_check_button = Gtk.Button(label="Проверить environment")
        workdir_row.append(env_check_button)
        box.append(workdir_row)

        executor_command_entry = Gtk.Entry()
        executor_command_entry.set_text(str(self.config["executor_command"]))
        executor_command_entry.set_placeholder_text('пусто = собрать автоматически')
        box.append(Gtk.Label(label="Команда executor"))
        box.append(executor_command_entry)

        def update_executor_command_from_fields() -> None:
            python_exe = python_entry.get_text().strip() or self._detect_python_executable()
            workdir_text = executor_workdir_entry.get_text().strip() or self._detect_executor_workdir()
            run_once = Path(workdir_text) / "ai_chatter_run_once.py"
            executor_command_entry.set_text(
                f'"{python_exe}" "{run_once}" --queue "{{queue}}" --config "{{config}}" --max-tasks 3 --max-jobs 3 --timeout-ms 60000'
            )

        def normalize_selected_folder(path_text: str) -> None:
            selected = Path(path_text)
            if selected.name != "ai_chatter_executor" and (selected / "ai_chatter_executor").exists():
                home = selected
                workdir = selected / "ai_chatter_executor"
            elif selected.name == "ai_chatter_executor":
                home = selected.parent
                workdir = selected
            else:
                home = selected
                workdir = selected

            home_entry.set_text(str(home))
            executor_workdir_entry.set_text(str(workdir))
            if not python_entry.get_text().strip():
                python_entry.set_text(self._detect_python_executable())
            update_executor_command_from_fields()

        def on_choose_folder_response(dialog: object, result: object) -> None:
            try:
                folder_obj = dialog.select_folder_finish(result)
                if folder_obj is not None:
                    path = folder_obj.get_path()
                    if path:
                        normalize_selected_folder(path)
            except Exception:
                log.exception("Could not select AI Chatter folder")

        def on_choose_workdir(_button: Gtk.Button) -> None:
            try:
                dialog = Gtk.FileDialog()
                dialog.set_title("Выберите C:\\AI_chatter или папку ai_chatter_executor")
                dialog.select_folder(win, None, on_choose_folder_response)
                return
            except Exception:
                pass

            # Fallback for older GTK bindings.
            try:
                native = Gtk.FileChooserNative(
                    title="Выберите C:\\AI_chatter или папку ai_chatter_executor",
                    transient_for=win,
                    action=Gtk.FileChooserAction.SELECT_FOLDER,
                    accept_label="Выбрать",
                    cancel_label="Отмена",
                )

                def on_native_response(_native: Gtk.FileChooserNative, response: int) -> None:
                    try:
                        if response == Gtk.ResponseType.ACCEPT:
                            file_obj = _native.get_file()
                            if file_obj is not None:
                                path = file_obj.get_path()
                                if path:
                                    normalize_selected_folder(path)
                    finally:
                        _native.destroy()

                native.connect("response", on_native_response)
                native.show()
            except Exception:
                log.exception("Could not open folder chooser")

        def on_autodetect(_button: Gtk.Button) -> None:
            python_exe, workdir, command = self._autodetect_executor_settings(save=False)
            python_entry.set_text(python_exe)
            executor_workdir_entry.set_text(workdir)
            home_entry.set_text(str(Path(workdir).parent) if Path(workdir).name == "ai_chatter_executor" else self._detect_ai_chatter_home())
            executor_command_entry.set_text(command)
            if not chrome_exe_entry.get_text().strip():
                chrome_exe_entry.set_text(self._detect_chrome_executable())
            if not chrome_profile_entry.get_text().strip():
                chrome_profile_entry.set_text(self._detect_chrome_user_data_dir())
            env_status_label.set_text("Auto-detect: paths filled. Нажмите «Сохранить всё» для записи.")

        def on_env_check(_button: Gtk.Button) -> None:
            self.config["ai_chatter_home"] = home_entry.get_text().strip()
            self.config["python_executable"] = python_entry.get_text().strip()
            self.config["chrome_executable"] = chrome_exe_entry.get_text().strip()
            self.config["chrome_user_data_dir"] = chrome_profile_entry.get_text().strip()
            self.config["executor_workdir"] = executor_workdir_entry.get_text().strip()
            self.config["executor_command"] = executor_command_entry.get_text().strip()
            env_status_label.set_text(self._format_env_check())

        choose_workdir_button.connect("clicked", on_choose_workdir)
        autodetect_button.connect("clicked", on_autodetect)
        env_check_button.connect("clicked", on_env_check)


        registry_text = Gtk.TextView()
        registry_text.set_monospace(True)
        registry_text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        registry_buffer = registry_text.get_buffer()
        registry_buffer.set_text(self._agent_registry_editor_text())

        registry_scroll = Gtk.ScrolledWindow()
        registry_scroll.set_min_content_height(140)
        registry_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        registry_scroll.set_child(registry_text)

        registry_label = Gtk.Label(label="AI agents registry: id | enabled | JID | provider | aliases (сохраняется только кнопкой «Сохранить registry»)")
        registry_label.set_xalign(0)
        box.append(registry_label)
        box.append(registry_scroll)

        registry_status = Gtk.Label(label="Registry: отдельное хранилище; «Сохранить всё» больше не перезаписывает его")
        registry_status.set_wrap(True)
        registry_status.set_xalign(0)
        box.append(registry_status)

        registry_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        registry_actions.set_halign(Gtk.Align.END)
        save_registry_button = Gtk.Button(label="Сохранить registry")
        reload_registry_button = Gtk.Button(label="Перезагрузить registry")
        template_registry_button = Gtk.Button(label="Шаблон registry")
        save_registry_button.add_css_class("suggested-action")
        registry_actions.append(save_registry_button)
        registry_actions.append(reload_registry_button)
        registry_actions.append(template_registry_button)
        box.append(registry_actions)

        def _get_registry_editor_value() -> str:
            start_iter = registry_buffer.get_start_iter()
            end_iter = registry_buffer.get_end_iter()
            return registry_buffer.get_text(start_iter, end_iter, True)

        def _save_registry_from_settings_ui() -> tuple[bool, str]:
            # Make the home path field authoritative before computing registry path.
            self.config["ai_chatter_home"] = home_entry.get_text().strip()
            self.config["agent_registry_text"] = _get_registry_editor_value()
            try:
                self.save_config()
            except Exception:
                log.exception("Could not save config before explicit registry save")
            ok, message = self._save_agent_registry_from_editor_text(str(self.config["agent_registry_text"]))
            if ok:
                # Reload from the just-written file so the editor reflects the runtime source of truth.
                try:
                    registry_buffer.set_text(self._agent_registry_editor_text())
                except Exception:
                    log.exception("Could not reload registry editor text after save")
                registry_status.set_text(f"Registry сохранён: {self._agent_registry_path()}")
            else:
                registry_status.set_text(f"Registry НЕ сохранён: {message}")
            log.warning("AI Chatter Bridge explicit registry save result: %s", message)
            return ok, message

        def on_save_registry(_button: Gtk.Button) -> None:
            _save_registry_from_settings_ui()

        def on_reload_registry(_button: Gtk.Button) -> None:
            try:
                registry_buffer.set_text(self._agent_registry_editor_text())
                registry_status.set_text(f"Registry перезагружен из файла: {self._agent_registry_path()}")
            except Exception as exc:
                log.exception("Could not reload registry editor text")
                registry_status.set_text(f"Registry не перезагружен: {exc}")

        def on_template_registry(_button: Gtk.Button) -> None:
            registry_buffer.set_text(self._agent_registry_template_text())
            registry_status.set_text("Шаблон вставлен. Нажмите «Сохранить registry», чтобы записать файл.")

        save_registry_button.connect("clicked", on_save_registry)
        reload_registry_button.connect("clicked", on_reload_registry)
        template_registry_button.connect("clicked", on_template_registry)

        info = Gtk.Label(
            label=(
                "Основное управление — в AI-панели справа в окне чата. "
                "Текстовые команды сохранены для мобильных клиентов: .ping, .bots, .only, .all, .mute."
            )
        )
        info.set_wrap(True)
        info.set_xalign(0)
        box.append(info)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        box.append(actions)

        save_button = Gtk.Button(label="Сохранить")
        close_button = Gtk.Button(label="Закрыть")
        save_button.add_css_class("suggested-action")
        actions.append(save_button)
        actions.append(close_button)

        def save_settings(_button: Gtk.Button) -> None:
            self.config["room_jid"] = room_entry.get_text().strip()
            self.config["chat_panel_enabled"] = panel_enabled.get_active()
            self.config["routing_echo_enabled"] = routing_echo.get_active()
            self.config["route_any_non_command_in_room"] = route_any.get_active()
            self.config["queue_enabled"] = queue_enabled.get_active()
            self.config["queue_file_name"] = queue_name_entry.get_text().strip() or "ai_chatter_tasks.jsonl"
            self.config["chrome_cdp_url"] = cdp_entry.get_text().strip() or "http://127.0.0.1:9222"
            self.config["chrome_executable"] = chrome_exe_entry.get_text().strip()
            self.config["chrome_user_data_dir"] = chrome_profile_entry.get_text().strip()
            self.config["python_executable"] = python_entry.get_text().strip()
            self.config["ai_chatter_home"] = home_entry.get_text().strip()
            self.config["auto_run_executor"] = auto_run.get_active()
            self.config["auto_deliver_results"] = auto_deliver.get_active()
            try:
                self.config["max_auto_jobs_per_run"] = max(1, min(20, int(max_auto_jobs_entry.get_text().strip() or "5")))
            except Exception:
                self.config["max_auto_jobs_per_run"] = 5
            self.config["executor_workdir"] = executor_workdir_entry.get_text().strip()
            self.config["executor_command"] = executor_command_entry.get_text().strip()
            # R3.4.6: Save-all must not rewrite agent registry from possibly stale
            # editor text.  Registry is persisted only by the explicit
            # "Сохранить registry" buttons or .agent-registry-set.
            registry_status.set_text("Save-all: общие настройки сохранены; registry не перезаписывался. Для registry используйте отдельную кнопку «Сохранить registry».")
            if not self.config["executor_command"]:
                self._autodetect_executor_settings(save=True)
            else:
                self._ensure_executor_runtime_config()
                self.save_config()
            self._remove_chat_panel()
            if bool(self.config["chat_panel_enabled"]):
                GLib.idle_add(self._inject_chat_panel)

        def on_close(*_args: object) -> None:
            self._settings_window = None

        save_button.connect("clicked", save_settings)
        top_save_button.connect("clicked", save_settings)
        top_save_registry_button.connect("clicked", on_save_registry)
        close_button.connect("clicked", lambda _button: win.close())
        top_close_button.connect("clicked", lambda _button: win.close())
        win.connect("close-request", on_close)

        self._settings_window = win
        win.present()
