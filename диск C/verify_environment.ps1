$python = "C:\Users\789\AppData\Local\Programs\Python\Python314\python.exe"
$executorDir = "C:\AI_chatter\ai_chatter_executor"
$pluginDir = "$env:APPDATA\Gajim\Plugins\aichatter_bridge"

Write-Host "== Python =="
& $python --version

Write-Host "`n== Playwright =="
& $python -c "from playwright.sync_api import sync_playwright; print('playwright ok')"

Write-Host "`n== Executor version =="
Get-Content "$executorDir\version.txt"

Write-Host "`n== Executor config =="
Get-Content "$pluginDir\ai_chatter_executor_config.json"

Write-Host "`n== CDP version =="
try {
    Invoke-RestMethod http://127.0.0.1:9222/json/version
} catch {
    Write-Host "CDP not available on 9222"
}

Write-Host "`n== CDP pages =="
try {
    Invoke-WebRequest http://127.0.0.1:9222/json/list | Select-Object -ExpandProperty Content
} catch {
    Write-Host "Could not read /json/list"
}
