param(
    [switch]$InstallSources
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $ProjectRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $candidate = (& $py.Source -3 -S -c "import sys; sys.exit('Python 3.10+ required') if sys.version_info < (3, 10) else print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and $candidate) { return $candidate }
        } catch {}
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        try {
            $candidate = (& $python.Source -S -c "import sys; sys.exit('Python 3.10+ required') if sys.version_info < (3, 10) else print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and $candidate) { return $candidate }
        } catch {}
    }
    throw "未找到 Python 3.10 或更高版本。请先从 https://www.python.org/downloads/ 安装 Python，并勾选 Add Python to PATH。"
}

$python = Find-Python
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "正在创建桌面版运行环境..." -ForegroundColor Cyan
    & $python -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "桌面版运行环境创建失败，请查看上面的 Python 错误。" }
}

# Repair existing installs before running pip, which also imports site.
& $venvPython -S (Join-Path $ProjectRoot "scripts\repair_python_env.py") --venv (Join-Path $ProjectRoot ".venv")
if ($LASTEXITCODE -ne 0) { throw "桌面版运行环境修复失败，请查看上面的错误。" }

Write-Host "正在安装桌面版运行依赖..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip 更新失败，请检查网络连接后重新运行 安装桌面版.bat。" }
& $venvPython -m pip install -e "${ProjectRoot}[browser]"
if ($LASTEXITCODE -ne 0) { throw "桌面版依赖安装失败，请查看上面的 pip 错误后重试。" }
if ($InstallSources) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\install_sources.ps1") -UseGit
    if ($LASTEXITCODE -ne 0) { throw "来源项目安装失败，请查看上面的错误。" }
}
# A copied source folder alone does not install dy-cli's dependencies into
# this virtual environment. Keep the local source and its patches intact.
$dyCliRoot = Join-Path $ProjectRoot "third_party\dy-cli"
if (Test-Path -LiteralPath (Join-Path $dyCliRoot "pyproject.toml")) {
    Write-Host "正在安装抖音采集依赖..." -ForegroundColor Cyan
    & $venvPython -m pip install -e $dyCliRoot
    if ($LASTEXITCODE -ne 0) { throw "抖音采集依赖安装失败，请查看上面的 pip 错误后重试。" }
}
$mediaCrawlerRoot = Join-Path $ProjectRoot "third_party\MediaCrawler"
if (Test-Path -LiteralPath (Join-Path $mediaCrawlerRoot "main.py")) {
    Write-Host "正在安装微博备用采集依赖..." -ForegroundColor Cyan
    & $venvPython -m pip install -r (Join-Path $ProjectRoot "scripts\requirements_media_crawler_bridge.txt")
    if ($LASTEXITCODE -ne 0) { throw "微博采集依赖安装失败，请查看上面的 pip 错误后重试。" }
}
# Keep editable paths readable when users run Python without UTF-8 mode too.
& $venvPython -S (Join-Path $ProjectRoot "scripts\repair_python_env.py") --venv (Join-Path $ProjectRoot ".venv")
if ($LASTEXITCODE -ne 0) { throw "桌面版安装后的路径修复失败，请查看上面的错误。" }

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot ".env.example") -Destination $envFile
    Write-Host "已创建 .env 配置文件。" -ForegroundColor DarkGray
}

Write-Host "桌面版安装完成。双击 启动应用.bat 即可启动。" -ForegroundColor Green
