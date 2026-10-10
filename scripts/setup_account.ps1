param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("douyin", "weibo", "xhs", "bilibili", "x", "instagram")]
    [string]$Platform,

    [ValidateSet("edge", "chrome")]
    [string]$Browser = "edge",

    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Python) { $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path -LiteralPath $Python)) { throw "请先双击 安装桌面版.bat，再配置平台登录。" }
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom

$PreviousChannel = $env:BROWSER_CHANNEL
try {
    $env:BROWSER_CHANNEL = if ($Browser -eq "edge") { "msedge" } else { "chrome" }
    & $Python (Join-Path $PSScriptRoot "account_login.py") --platform $Platform
    if ($LASTEXITCODE -ne 0) { throw "平台登录未完成，请按官方窗口提示重试。" }
} finally {
    $env:BROWSER_CHANNEL = $PreviousChannel
}
