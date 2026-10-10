param(
    [string]$Root = "$PSScriptRoot\..\third_party",
    [switch]$UseGit,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
if (-not $Python) {
    $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) { $Python = "python" }
}
# -UseGit remains accepted for older instructions. Archives work without Git
# and use the same tested commit IDs as a clone would.
& $Python (Join-Path $PSScriptRoot "bootstrap_sources.py") --root ([IO.Path]::GetFullPath($Root))
if ($LASTEXITCODE -ne 0) { throw "采集来源安装不完整。请查看 .cache\source-install-latest.json，检查网络后重试；已安装的来源会保留。" }
