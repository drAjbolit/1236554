import os
import json
import re
from google import genai
from google.genai import types

# Инициализация клиента Gemini API для Канцлера
try:
    gemini_client = genai.Client()
except Exception:
    gemini_client = None

def call_gemini_chancellor(conference_history: list, current_command: str) -> str:
    """
    Прямой вызов Канцлера по API с передачей контекста чата Jabber.
    """
    if not gemini_client:
        return "Ошибка: Переменная среды GEMINI_API_KEY не установлена."
        
    context_messages = ["=== ИСТОРИЯ КОНФЕРЕНЦИИ ДЛЯ АНАЛИЗА == WORKERS: ChatGPT, DeepSeek, Qwen ==="]
    for msg in conference_history[-40:]:
        author = msg.get('author', 'Участник')
        text = msg.get('text', '')
        context_messages.append(f"{author}: {text}")
        
    context_messages.append(f"\nНовое прямое указание Создателя: {current_command}")

    # Системная инструкция, задающая жесткую роль Канцлера-Оркестратора
    system_instruction = (
        "Ты — Канцлер, верховный координатор технического консилиума и правая рука Создателя in Jabber-конференции. "
        "В твоем подчинении находятся три агента-воркера: ChatGPT, DeepSeek и Qwen. "
        "Твои обязанности:\n"
        "1. Принимать команды от Создателя, декомпозировать их на подзадачи и распределять между воркерами через точные упоминания "
        "(например: '@deepseek напиши класс логирования на Python...', '@chatgpt проверь этот код на утечки памяти...').\n"
        "2. Контролировать обсуждение, не давать воркерам уходить от темы.\n"
        "3. Проводить арбитраж их ответов, критиковать баги, указывать на галлюцинации.\n"
        "4. Формировать для Создателя итоговый чистый результат работы всего консилиума.\n"
        "Пиши авторитетно, емко, структурированно и строго по делу."
    )

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.2,
    )

    try:
        response = gemini_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=context_messages,
            config=config
        )
        return response.text
    except Exception as e:
        return f"Ошибка Канцлера (Gemini API): {str(e)}"

def parse_chancellor_decisions(chancellor_output: str):
    """
    Анализирует ответ Канцлера для автоматической выдачи подзадач воркерам.
    """
    tasks = {}
    patterns = {
        'deepseek': r'@deepseek\s+([^@]+)',
        'chatgpt': r'@chatgpt\s+([^@]+)',
        'qwen': r'@qwen\s+([^@]+)'
    }
    for agent, pattern in patterns.items():
        match = re.search(pattern, chancellor_output, re.IGNORECASE)
        if match:
            tasks[agent] = match.group(1).strip()
    return tasks

def main_bridge_loop():
    print("🚀 Python-Бридж ai_chatter успешно запущен...")
    print("🤖 Канцлер (Gemini API) активен и слушает эфир...")

if __name__ == "__main__":
    main_bridge_loop()