param(
    [string]$Root = "$PSScriptRoot\..\third_party",
    [switch]$UseGit
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = [System.IO.Path]::GetFullPath($Root)
New-Item -ItemType Directory -Force -Path $Root | Out-Null

$repos = @(
    @{ Name = "MediaCrawler"; Url = "https://github.com/NanmiCoder/MediaCrawler.git"; Branch = "main" },
    @{ Name = "XHS-Downloader"; Url = "https://github.com/JoeanAmier/XHS-Downloader.git"; Branch = "master" }
)

function Install-Archive($repo, $target) {
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("social-image-mcp-" + [guid]::NewGuid().ToString("N"))
    $zipPath = "$tempRoot.zip"
    $repoPath = $repo.Url -replace '^https://github.com/', '' -replace '\.git$', ''
    $archiveUrl = "https://codeload.github.com/$repoPath/zip/refs/heads/$($repo.Branch)"
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    try {
        Write-Output "Downloading $($repo.Name) archive from $archiveUrl"
        Invoke-WebRequest -Uri $archiveUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
        $extracted = Get-ChildItem -LiteralPath $tempRoot -Directory | Select-Object -First 1
        if (-not $extracted) { throw "Archive did not contain a source directory" }
        Move-Item -LiteralPath $extracted.FullName -Destination $target
    } finally {
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
    }
}

foreach ($repo in $repos) {
    $target = Join-Path $Root $repo.Name
    if (Test-Path $target) {
        $existing = Get-ChildItem -LiteralPath $target -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existing) {
            Write-Output "$($repo.Name) already exists at $target"
            continue
        }
        Remove-Item -LiteralPath $target -Force
    }
    if ($UseGit) {
        & git clone --depth 1 --branch $repo.Branch $repo.Url $target
        if ($LASTEXITCODE -eq 0) { continue }
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        Write-Warning "git clone failed for $($repo.Name); falling back to GitHub archive download"
    }
    Install-Archive $repo $target
}

Write-Output "Install gallery-dl separately with: python -m pip install gallery-dl"
Write-Output "Do not install both vendor requirements files wholesale into the MCP environment."
Write-Output "MediaCrawler currently declares matplotlib>=3.11 (not available for Python 3.10),"
Write-Output "and the newest XHS-Downloader dependency can upgrade MCP to an incompatible 2.x release."
Write-Output "Install the tested bridge dependencies into the desktop environment instead:"
Write-Output "  .\.venv\Scripts\python.exe -m pip install -r scripts\requirements_media_crawler_bridge.txt"
Write-Output "  python -m pip install --no-deps --force-reinstall fastmcp==3.4.7 fastmcp-slim==3.4.7"
Write-Output "  playwright install chromium"
Write-Output "Set MEDIA_CRAWLER_COMMAND, XHS_DOWNLOADER_COMMAND, and BILIBILI_SOURCE_COMMAND to the bundled CLI bridges in .env."
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$MediaBridge = Join-Path $ProjectRoot "scripts\media_crawler_bridge.py"
$XhsBridge = Join-Path $ProjectRoot "scripts\xhs_downloader_bridge.py"
$BilibiliBridge = Join-Path $ProjectRoot "scripts\bilibili_cli_bridge.py"
Write-Output "Recommended .env values for this checkout:"
Write-Output "MEDIA_CRAWLER_COMMAND=python `"$MediaBridge`" --platform {platform} --query `"{query}`" --item-id `"{item_id}`" --url `"{url}`" --limit {limit}"
Write-Output "XHS_DOWNLOADER_COMMAND=python `"$XhsBridge`" --query `"{query}`" --item-id `"{item_id}`" --url `"{url}`" --limit {limit}"
Write-Output "BILIBILI_SOURCE_COMMAND=python `"$BilibiliBridge`" --query `"{query}`" --item-id `"{item_id}`" --url `"{url}`" --limit {limit}"
