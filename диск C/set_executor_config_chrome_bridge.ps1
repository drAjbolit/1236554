$pluginDir = "$env:APPDATA\Gajim\Plugins\aichatter_bridge"

if (-not (Test-Path $pluginDir)) {
    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
}

@'
{
  "provider_mode": "chrome_bridge",
  "bridge_notices": false
}
'@ | Set-Content "$pluginDir\ai_chatter_executor_config.json" -Encoding UTF8

Write-Host "Written:"
Write-Host "$pluginDir\ai_chatter_executor_config.json"
Get-Content "$pluginDir\ai_chatter_executor_config.json"
