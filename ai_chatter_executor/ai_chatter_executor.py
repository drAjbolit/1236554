
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI Chatter Executor — Stage 3.5.5 provider session reset for Qwen parent_id

Stable folder name: ai_chatter_executor

Change from Stage 3.1:
- chrome_bridge task processing creates ai_chatter_chrome_jobs.jsonl jobs;
- it no longer writes intermediate [BRIDGE] notifications to ai_chatter_results.jsonl;
- only final chrome worker results are converted to ai_chatter_results.jsonl during --intake-bridge-results.

Reliable flow:
Gajim task
-> ai_chatter_tasks.jsonl
-> executor --tasks-only
-> ai_chatter_chrome_jobs.jsonl
-> chrome bridge worker
-> ai_chatter_chrome_results.jsonl
-> executor --intake-bridge-results
-> ai_chatter_results.jsonl
-> Gajim .deliver
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = {
    "providerMode": "stub",
    "bots": {
        "chatgpt": {
            "title": "ChatGPT",
            "profileKey": "chatgpt::https://chatgpt.com",
            "provider": "stub",
            "enabled": True,
        },
        "qwen": {
            "title": "Qwen",
            "profileKey": "qwen::https://chat.qwen.ai",
            "provider": "stub",
            "enabled": True,
        },
        "deepseek": {
            "title": "DeepSeek",
            "profileKey": "deepseek::https://chat.deepseek.com",
            "provider": "stub",
            "enabled": True,
        },
    },
    "chromeBridge": {
        "jobsFileName": "logs/ai_chatter_chrome_jobs.jsonl",
        "resultsFileName": "logs/ai_chatter_chrome_results.jsonl",
        "intakeStateFileName": "logs/ai_chatter_chrome_intake_state.json",
        "writeBridgeNotificationsToGajimResults": False,
    },
    "limits": {
        "maxTasksPerRun": 1,
        "maxBridgeResultsPerRun": 10,
    },
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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


def ensure_config(path: Path) -> dict[str, Any]:
    data = read_json(path, None)
    if data is None:
        write_json(path, DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))

    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value

    write_json(path, merged)
    return merged


def ids_state(path: Path, key: str) -> set[str]:
    data = read_json(path, {key: []})
    if not isinstance(data, dict):
        return set()
    ids = data.get(key, [])
    return {str(item) for item in ids} if isinstance(ids, list) else set()


def write_ids_state(path: Path, key: str, ids: set[str]) -> None:
    write_json(path, {key: sorted(ids), "updatedAt": now()})


def task_state_ids(path: Path) -> set[str]:
    return ids_state(path, "processedTaskIds")


def write_task_state(path: Path, ids: set[str]) -> None:
    write_ids_state(path, "processedTaskIds", ids)


def sibling_or_relative_to_queue(queue: Path, name: str) -> Path:
    raw = str(name or "").strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if len(candidate.parts) > 1:
        return queue.parent / candidate
    return queue.with_name(candidate.name)


def default_config_for_queue(queue: Path) -> Path:
    return queue.parent / "configs" / "ai_chatter_executor_config.json"

def intake_state_path(queue: Path, cfg: dict[str, Any]) -> Path:
    name = str(cfg.get("chromeBridge", {}).get("intakeStateFileName", "ai_chatter_chrome_intake_state.json"))
    return sibling_or_relative_to_queue(queue, name)


def intake_state_ids(queue: Path, cfg: dict[str, Any]) -> set[str]:
    return ids_state(intake_state_path(queue, cfg), "processedChromeResultIds")


def write_intake_state(queue: Path, cfg: dict[str, Any], ids: set[str]) -> None:
    write_ids_state(intake_state_path(queue, cfg), "processedChromeResultIds", ids)


def chrome_jobs_path(queue: Path, cfg: dict[str, Any]) -> Path:
    return sibling_or_relative_to_queue(queue, str(cfg["chromeBridge"].get("jobsFileName", "logs/ai_chatter_chrome_jobs.jsonl")))


def chrome_results_path(queue: Path, cfg: dict[str, Any]) -> Path:
    return sibling_or_relative_to_queue(queue, str(cfg["chromeBridge"].get("resultsFileName", "logs/ai_chatter_chrome_results.jsonl")))


def stub_answer(task: dict[str, Any], target: dict[str, Any]) -> str:
    title = str(target.get("botTitle") or target.get("botId") or "Bot")
    text = str(task.get("message", {}).get("text", "")).replace("\n", " ").strip()
    if len(text) > 400:
        text = text[:397] + "..."
    return f"[STUB][{title}] Executor Stage 3.2 получил задачу.\nТекст: {text}\nChrome/AHK пока не вызывался."


def create_chrome_job(queue: Path, task: dict[str, Any], target: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    bot_id = str(target.get("botId", "unknown"))
    job_id = f"chromejob-{task.get('id')}-{bot_id}"
    bot_cfg = cfg.get("bots", {}).get(bot_id, {})

    job = {
        "id": job_id,
        "createdAt": now(),
        "status": "queued",
        "taskId": str(task.get("id", "")),
        "botId": bot_id,
        "botTitle": str(target.get("botTitle") or bot_cfg.get("title") or bot_id),
        "profileKey": str(bot_cfg.get("profileKey", "")),
        "message": {"text": str(task.get("message", {}).get("text", ""))},
        "source": task.get("source", {}),
        "contract": {"version": 1, "expectedResultId": f"chromeresult-{job_id}"},
    }
    append_jsonl(chrome_jobs_path(queue, cfg), job)
    return job


def run_provider(queue: Path, task: dict[str, Any], target: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    bot_id = str(target.get("botId", "unknown"))
    bot_cfg = cfg.get("bots", {}).get(bot_id, {})
    provider = str(bot_cfg.get("provider") or cfg.get("providerMode") or "stub")

    if provider == "stub":
        return {"status": "done", "answer": stub_answer(task, target), "provider": "stub"}

    if provider == "chrome_bridge":
        job = create_chrome_job(queue, task, target, cfg)
        return {
            "status": "bridge_queued",
            "provider": "chrome_bridge",
            "jobId": job["id"],
            "botId": bot_id,
            "botTitle": str(job["botTitle"]),
        }

    return {"status": "error", "provider": provider, "error": f"Provider {provider} is not implemented"}


def write_gajim_result(
    results: Path,
    task: dict[str, Any],
    target: dict[str, Any],
    provider_result: dict[str, Any],
    *,
    stage: str,
) -> None:
    task_id = str(task.get("id", ""))
    bot_id = str(target.get("botId", "unknown"))
    bot_title = str(target.get("botTitle") or bot_id)
    status = str(provider_result.get("status", "error"))

    row = {
        "id": f"result-{task_id}-{bot_id}",
        "taskId": task_id,
        "createdAt": now(),
        "status": "done" if status == "done" else "error",
        "botId": bot_id,
        "botTitle": bot_title,
        "source": task.get("source", {}),
        "executor": {
            "name": "ai_chatter_executor",
            "stage": stage,
            "provider": provider_result.get("provider", "unknown"),
        },
    }

    if row["status"] == "done":
        row["answer"] = str(provider_result.get("answer", ""))
    else:
        row["error"] = str(provider_result.get("error", "unknown error"))

    append_jsonl(results, row)


def process_tasks(queue: Path, results: Path, state: Path, cfg: dict[str, Any], max_tasks: int | None) -> int:
    processed = task_state_ids(state)
    tasks = read_jsonl(queue)
    max_tasks = int(max_tasks or cfg.get("limits", {}).get("maxTasksPerRun", 1) or 1)
    count = 0

    for task in tasks:
        if count >= max_tasks:
            break

        task_id = str(task.get("id", ""))
        if not task_id or task_id in processed or task.get("status") not in (None, "queued"):
            continue

        targets = task.get("targets", [])
        if not isinstance(targets, list):
            targets = []

        for target in targets:
            if not isinstance(target, dict):
                continue

            bot_id = str(target.get("botId", "unknown"))
            if cfg.get("bots", {}).get(bot_id, {}).get("enabled", True) is False:
                provider_result = {"status": "error", "error": f"Bot {bot_id} disabled", "provider": "disabled"}
            else:
                provider_result = run_provider(queue, task, target, cfg)

            if provider_result.get("status") == "bridge_queued":
                if bool(cfg.get("chromeBridge", {}).get("writeBridgeNotificationsToGajimResults", False)):
                    bridge_notice = {
                        "status": "done",
                        "provider": "chrome_bridge",
                        "answer": (
                            f"[BRIDGE][{provider_result.get('botTitle', bot_id)}] "
                            f"Задача передана в Chrome/AHK bridge.\n"
                            f"Job ID: {provider_result.get('jobId')}\n"
                            f"Файл заданий: {chrome_jobs_path(queue, cfg)}"
                        ),
                    }
                    write_gajim_result(results, task, target, bridge_notice, stage="stage_3_2_no_bridge_notifications")
                continue

            write_gajim_result(results, task, target, provider_result, stage="stage_3_2_no_bridge_notifications")

        processed.add(task_id)
        count += 1
        print(f"Processed task {task_id}")

    write_task_state(state, processed)
    return count


def intake_chrome_results(queue: Path, results: Path, cfg: dict[str, Any], max_results: int | None) -> int:
    chrome_results = read_jsonl(chrome_results_path(queue, cfg))
    processed = intake_state_ids(queue, cfg)
    max_results = int(max_results or cfg.get("limits", {}).get("maxBridgeResultsPerRun", 10) or 10)
    count = 0

    for chrome_result in chrome_results:
        if count >= max_results:
            break

        chrome_result_id = str(chrome_result.get("id", ""))
        if not chrome_result_id or chrome_result_id in processed:
            continue

        status = str(chrome_result.get("status", "error"))
        bot_id = str(chrome_result.get("botId", "unknown"))
        bot_title = str(chrome_result.get("botTitle") or bot_id)
        task_id = str(chrome_result.get("taskId") or chrome_result.get("jobId") or chrome_result_id)

        row = {
            "id": f"result-intake-{chrome_result_id}",
            "taskId": task_id,
            "createdAt": now(),
            "status": "done" if status == "done" else "error",
            "botId": bot_id,
            "botTitle": bot_title,
            "source": chrome_result.get("source", {}),
            "executor": {
                "name": "ai_chatter_executor",
                "stage": "stage_3_2_no_bridge_notifications",
                "provider": "chrome_bridge_intake",
            },
            "bridgeResultId": chrome_result_id,
            "bridgeJobId": str(chrome_result.get("jobId", "")),
        }

        if status == "done":
            row["answer"] = str(chrome_result.get("answer", ""))
        else:
            row["error"] = str(chrome_result.get("error", "Chrome bridge result error"))

        append_jsonl(results, row)
        processed.add(chrome_result_id)
        count += 1
        print(f"Intaked chrome result {chrome_result_id} -> {bot_title}")

    write_intake_state(queue, cfg, processed)
    return count


def status(queue: Path, results: Path, state: Path, config: Path, cfg: dict[str, Any]) -> None:
    processed = task_state_ids(state)
    tasks = read_jsonl(queue)
    queued = [
        task for task in tasks
        if str(task.get("id", "")) not in processed and task.get("status") in (None, "queued")
    ]

    print("AI Chatter Executor Stage 3.2 status")
    print(f"Queue:          {queue}")
    print(f"Results:        {results}")
    print(f"State:          {state}")
    print(f"Config:         {config}")
    print(f"Chrome jobs:    {chrome_jobs_path(queue, cfg)}")
    print(f"Chrome results: {chrome_results_path(queue, cfg)}")
    print(f"Intake state:   {intake_state_path(queue, cfg)}")
    print(f"Provider mode:  {cfg.get('providerMode')}")
    print(f"Bridge notices: {cfg.get('chromeBridge', {}).get('writeBridgeNotificationsToGajimResults', False)}")
    print(f"Tasks total:    {len(tasks)}")
    print(f"Tasks queued:   {len(queued)}")
    print(f"Tasks done:     {len(processed)}")
    print(f"Results total:  {len(read_jsonl(results))}")
    print(f"Chrome jobs:    {len(read_jsonl(chrome_jobs_path(queue, cfg)))}")
    print(f"Chrome results: {len(read_jsonl(chrome_results_path(queue, cfg)))}")
    print(f"Chrome intaked: {len(intake_state_ids(queue, cfg))}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Chatter Executor Stage 3.2")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--results", default="")
    parser.add_argument("--state", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--max-bridge-results", type=int, default=None)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--intake-bridge-results", action="store_true")
    parser.add_argument("--tasks-only", action="store_true")
    args = parser.parse_args()

    queue = Path(args.queue).expanduser().resolve()
    results = Path(args.results).expanduser().resolve() if args.results else sibling_or_relative_to_queue(queue, "logs/ai_chatter_results.jsonl")
    state = Path(args.state).expanduser().resolve() if args.state else queue.with_name("ai_chatter_executor_state.json")
    config = Path(args.config).expanduser().resolve() if args.config else default_config_for_queue(queue)
    cfg = ensure_config(config)

    if args.status:
        status(queue, results, state, config, cfg)
        return 0

    print("AI Chatter Executor Stage 3.5.4 Unified Configs os-import hotfix")
    print(f"Queue:          {queue}")
    print(f"Results:        {results}")
    print(f"Config:         {config}")
    print(f"Provider mode:  {cfg.get('providerMode')}")

    if args.intake_bridge_results:
        count = intake_chrome_results(queue, results, cfg, args.max_bridge_results)
        print(f"New chrome results intaked: {count}")
        return 0

    task_count = process_tasks(queue, results, state, cfg, args.max_tasks)
    intake_count = 0 if args.tasks_only else intake_chrome_results(queue, results, cfg, args.max_bridge_results)
    print(f"New tasks processed: {task_count}")
    print(f"New chrome results intaked: {intake_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
