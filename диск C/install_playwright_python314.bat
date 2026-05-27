@echo off
setlocal

"C:\Users\789\AppData\Local\Programs\Python\Python314\python.exe" -m pip install playwright
"C:\Users\789\AppData\Local\Programs\Python\Python314\python.exe" -c "from playwright.sync_api import sync_playwright; print('playwright ok')"

pause
