param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $ProjectRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\install_desktop.ps1")
    if ($LASTEXITCODE -ne 0) { throw "桌面版安装失败，请查看上面的错误后重试。" }
}
if (-not (Test-Path -LiteralPath $python)) { throw "桌面版运行环境创建失败。请先双击 安装桌面版.bat 查看错误。" }
# -S skips site initialization, which can fail on legacy GBK .pth files.
& $python -S (Join-Path $ProjectRoot "scripts\repair_python_env.py") --venv (Join-Path $ProjectRoot ".venv")
if ($LASTEXITCODE -ne 0) { throw "桌面版运行环境修复失败，请查看上面的错误。" }
# Override project-relative source commands from .env so a copied desktop
# version always runs its own bridge scripts instead of the original folder.
$env:MEDIA_CRAWLER_COMMAND = ('"{0}" "{1}" --platform {{platform}} --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\media_crawler_bridge.py"))
$env:DOUYIN_SOURCE_COMMAND = ('"{0}" "{1}" --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\douyin_cli_bridge.py"))
$env:XHS_DOWNLOADER_COMMAND = ('"{0}" "{1}" --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\xhs_downloader_bridge.py"))
$env:BILIBILI_SOURCE_COMMAND = ('"{0}" "{1}" --query "{{query}}" --item-id "{{item_id}}" --url "{{url}}" --limit {{limit}}' -f $python, (Join-Path $ProjectRoot "scripts\bilibili_cli_bridge.py"))
$env:MEDIA_CRAWLER_ROOT = Join-Path $ProjectRoot "third_party\MediaCrawler"
# Prefer an installed Microsoft Edge when Playwright's bundled Chromium has
# not been downloaded yet. The bridge and webpage crawler both honor this
# channel, so a missing cache does not break the whole startup chain.
if (-not $env:BROWSER_CHANNEL) {
    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $envFile) {
        $channelLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*BROWSER_CHANNEL\s*=' } | Select-Object -First 1
        if ($channelLine) { $env:BROWSER_CHANNEL = ($channelLine -split '=', 2)[1].Trim().Trim('"') }
    }
}
if (-not $env:BROWSER_CHANNEL) {
    $edgeCandidates = @()
    if (${env:ProgramFiles(x86)}) { $edgeCandidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe" }
    if ($env:ProgramFiles) { $edgeCandidates += Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe" }
    if ($edgeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1) { $env:BROWSER_CHANNEL = "msedge" }
}
$env:DOUYIN_BROWSER_CHANNEL = $env:BROWSER_CHANNEL
# Check the complete local chain before opening the UI. The checker repairs
# missing dependencies and can launch the existing Douyin QR login flow when
# its storage state is missing or expired.
& $python (Join-Path $ProjectRoot "scripts\preflight.py") --project-root $ProjectRoot --python $python
if ($LASTEXITCODE -ne 0) { throw "启动前自检未通过。请按上面的提示修复后重试。" }
$appArguments = @((Join-Path $ProjectRoot "scripts\app_server.py"), "--port", $Port)
if ($NoBrowser) { $appArguments += "--no-browser" }
& $python @appArguments
if ($LASTEXITCODE -ne 0) { throw "桌面应用启动失败，退出码：$LASTEXITCODE。请查看上面的错误。" }
