import os
import json
import re
import sys

# астройка окружения для Windows
os.environ["PYTHONIOENCODING"] = "utf-8"

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

from google import genai
from google.genai import types

# мпортируем httpx для тонкой настройки прокси
import httpx

# нициализация клиента Gemini API с поддержкой Tor Proxy (SOCKS5)
try:
    # Указываем ваш Tor-прокси для HTTP и HTTPS трафика
    tor_proxy = "socks5://127.0.0.1:9050"
    
    # Создаем кастомный HTTP-клиент, который пустит трафик через Tor
    http_client = httpx.Client(
        proxies={
            "http://": tor_proxy,
            "https://": tor_proxy
        },
        timeout=30.0
    )
    
    # ередаем этот клиент в SDK Gemini
    gemini_client = genai.Client(http_client=http_client)
    print(" одуль анцлера: аршрутизация через Tor (127.0.0.1:9050) настроена.")
except Exception as e:
    print(f" е удалось настроить Tor-прокси: {e}")
    gemini_client = None

def call_gemini_chancellor(conference_history: list, current_command: str) -> str:
    """
    рямой вызов анцлера по API через Tor-прокси.
    """
    if not gemini_client:
        return "шибка: API-клиент Gemini не инициализирован."
        
    context_messages = ["=== СТ ФЦ   == WORKERS: ChatGPT, DeepSeek, Qwen ==="]
    for msg in conference_history[-40:]:
        author = msg.get('author', 'Участник')
        text = msg.get('text', '')
        context_messages.append(f"{str(author)}: {str(text)}")
        
    context_messages.append(f"\nовое прямое указание Создателя: {str(current_command)}")

    system_instruction = (
        "Ты  анцлер, верховный координатор технического консилиума и правая рука Создателя в Jabber-конференции. "
        " твоем подчинении находятся три агента-воркера: ChatGPT, DeepSeek и Qwen. "
        "Твои обязанности:\n"
        "1. ринимать команды от Создателя, декомпозировать их на подзадачи и распределять между воркерами через точные упоминания "
        "(например: '@deepseek напиши класс логирования на Python...', '@chatgpt проверь этот код на утечки памяти...').\n"
        "2. онтролировать обсуждение, не давать воркерам уходить от темы.\n"
        "3. роводить арбитраж их ответов, критиковать баги, указывать на галлюцинации.\n"
        "4. Формировать для Создателя итоговый чистый результат работы всего консилиума.\n"
        "иши авторитетно, емко, структурированно и строго по делу."
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
        return f"шибка анцлера (Gemini API через Tor): {str(e)}"

def parse_chancellor_decisions(chancellor_output: str):
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
    print(" Python-ридж ai_chatter успешно запущен...")

if __name__ == "__main__":
    main_bridge_loop()