param([int]$Port = 8765)
$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $ProjectRoot
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "Python 3.10 or newer was not found." }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# Override project-relative source commands from .env so a copied desktop
# version always runs its own bridge scripts instead of the original folder.
$env:MEDIA_CRAWLER_COMMAND = ('"{0}" "{1}" --platform {{platform}} --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\media_crawler_bridge.py"))
$env:DOUYIN_SOURCE_COMMAND = ('"{0}" "{1}" --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\douyin_cli_bridge.py"))
$env:XHS_DOWNLOADER_COMMAND = ('"{0}" "{1}" --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\xhs_downloader_bridge.py"))
$env:MEDIA_CRAWLER_ROOT = Join-Path $ProjectRoot "third_party\MediaCrawler"
& $python (Join-Path $ProjectRoot "scripts\app_server.py") --port $Port
