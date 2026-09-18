param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("douyin", "weibo", "x", "instagram")]
    [string]$Platform,

    [ValidateSet("edge", "chrome", "firefox")]
    [string]$Browser = "edge",

    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom

function Set-ProjectEnvironmentValue {
    param([string]$Name, [string]$Value)

    $path = Join-Path $ProjectRoot ".env"
    $lines = if (Test-Path -LiteralPath $path) {
        [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)
    } else {
        @()
    }
    $replacement = "$Name=$Value"
    $matched = $false
    $updated = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Name))=") {
            $matched = $true
            $replacement
        } else {
            $line
        }
    }
    if (-not $matched) {
        $updated += $replacement
    }
    [System.IO.File]::WriteAllLines($path, $updated, $Utf8NoBom)
}

function Get-BrowserExecutable {
    $commandName = switch ($Browser) {
        "edge" { "msedge.exe" }
        "chrome" { "chrome.exe" }
        "firefox" { "firefox.exe" }
    }
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = switch ($Browser) {
        "edge" {
            @(
                (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
                (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe")
            )
        }
        "chrome" {
            @(
                (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
                (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe")
            )
        }
        "firefox" {
            @(
                (Join-Path $env:ProgramFiles "Mozilla Firefox\firefox.exe"),
                (Join-Path ${env:ProgramFiles(x86)} "Mozilla Firefox\firefox.exe")
            )
        }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "$Browser executable was not found."
}

function Open-IsolatedLoginPage {
    param([string]$Url, [string]$ProfileRoot)

    New-Item -ItemType Directory -Force -Path $ProfileRoot | Out-Null
    $executable = Get-BrowserExecutable
    if ($Browser -eq "firefox") {
        $arguments = @("-no-remote", "-profile", "`"$ProfileRoot`"", $Url)
        $cookieProfile = $ProfileRoot
    } else {
        $arguments = @("--user-data-dir=`"$ProfileRoot`"", "--profile-directory=Default", "--no-first-run", $Url)
        $cookieProfile = Join-Path $ProfileRoot "Default"
    }
    Start-Process -FilePath $executable -ArgumentList $arguments | Out-Null
    return $cookieProfile
}

function Stop-IsolatedBrowser {
    param([string]$ProfileRoot)

    $processName = switch ($Browser) {
        "edge" { "msedge.exe" }
        "chrome" { "chrome.exe" }
        "firefox" { "firefox.exe" }
    }
    $profilePattern = [regex]::Escape([System.IO.Path]::GetFullPath($ProfileRoot))
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq $processName -and $_.CommandLine -match $profilePattern
    })
    foreach ($item in $processes) {
        $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
        if ($process -and $process.MainWindowHandle -ne 0) {
            try {
                $null = $process.CloseMainWindow()
            } catch {
                # The browser may have closed itself after the user pressed
                # Enter. A vanished process is already in the desired state.
            }
        }
    }
    Start-Sleep -Seconds 2
    $remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -ieq $processName -and $_.CommandLine -match $profilePattern
    })
    foreach ($item in $remaining) {
        Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

function Merge-PlatformCookieFiles {
    param([string]$Target)

    $lines = @("# Netscape HTTP Cookie File")
    $files = @(Get-ChildItem (Join-Path $ProjectRoot ".cache") -Filter "gallery-dl-*-cookies.txt" -File -ErrorAction SilentlyContinue)
    foreach ($file in $files) {
        foreach ($line in [System.IO.File]::ReadAllLines($file.FullName, [System.Text.Encoding]::UTF8)) {
            if ($line -and ((-not $line.StartsWith("#")) -or $line.StartsWith("#HttpOnly_"))) {
                $lines += $line
            }
        }
    }
    $unique = @($lines | Select-Object -Unique)
    [System.IO.File]::WriteAllLines($Target, $unique, $Utf8NoBom)
}

function Test-GalleryMediaOutput {
    param([object[]]$Lines)

    function Test-MediaValue {
        param([object]$Value)
        if ($null -eq $Value) { return $false }
        if ($Value -is [System.Array]) {
            if ($Value.Count -ge 3 -and [int]$Value[0] -eq 3 -and [string]$Value[1] -match '^https?://') {
                return $true
            }
            foreach ($child in $Value) {
                if (Test-MediaValue $child) { return $true }
            }
        }
        return $false
    }

    # gallery-dl --dump-json emits pretty-printed JSON across many lines;
    # stderr arrives as ErrorRecord objects through PowerShell's 2>&1 merge.
    $jsonText = (@($Lines | Where-Object { $_ -is [string] }) -join "`n").Trim()
    if (-not $jsonText) { return $false }
    try {
        $value = $jsonText | ConvertFrom-Json -ErrorAction Stop
        if (Test-MediaValue $value) { return $true }
    } catch {
        # Windows PowerShell may split gallery-dl's pretty JSON into records
        # and prepend native stderr. The media record itself still has a
        # stable `[3, "https://..."]` prefix, so accept that verified shape.
    }
    return $jsonText -match '(?m)^\s*\[\s*3\s*,\s*["'']https?://'
}

function Test-BridgeMediaOutput {
    param([object[]]$Lines)

    foreach ($line in $Lines) {
        try {
            $value = $line | ConvertFrom-Json -ErrorAction Stop
            if ([string]$value.image_url -match '^https?://') {
                return $true
            }
        } catch {
            continue
        }
    }
    return $false
}

Set-Location $ProjectRoot

if ($Platform -eq "douyin") {
    & (Join-Path $PSScriptRoot "douyin_login.ps1") -Python $Python
    if ($LASTEXITCODE -ne 0) {
        throw "Douyin login did not complete."
    }
    Write-Output "Douyin login completed. Open a new Codex task before testing the MCP."
    exit 0
}

if ($Platform -eq "weibo") {
    $bridge = Join-Path $PSScriptRoot "media_crawler_bridge.py"
    Write-Output "A visible Weibo login window will open. Complete the official login or QR confirmation."
    $weiboOutput = & $Python $bridge --platform weibo --query "coffee shop" --limit 1 --login-type qrcode --headless false
    if ($LASTEXITCODE -ne 0 -or -not (Test-BridgeMediaOutput $weiboOutput)) {
        $detail = ($weiboOutput | Select-Object -Last 12) -join [Environment]::NewLine
        throw "Weibo login or real candidate verification failed. No image candidate was returned.`n$detail"
    }
    Write-Output "Weibo returned a real candidate and its saved browser session can now be used by the MCP."
    Write-Output "Open a new Codex task before testing the MCP."
    exit 0
}

$galleryDl = Get-Command gallery-dl -ErrorAction SilentlyContinue
if (-not $galleryDl) {
    throw "gallery-dl is not installed in this PowerShell environment."
}

$loginUrl = if ($Platform -eq "x") { "https://x.com/login" } else { "https://www.instagram.com/accounts/login/" }
$testUrl = if ($Platform -eq "x") {
    # X search pages can return an empty gallery-dl result even with valid
    # cookies. Use a public media timeline to verify the session and media
    # extractor without depending on a particular search query.
    "https://x.com/NASA/media"
} else {
    "https://www.instagram.com/explore/tags/coffeeshop/"
}
$cookieDomain = if ($Platform -eq "x") { "x.com" } else { "instagram.com" }
$profileRoot = Join-Path $ProjectRoot ".cache\gallery-dl-$Platform-$Browser-profile"
$cookieProfile = Open-IsolatedLoginPage $loginUrl $profileRoot
$browserSession = "{0}/{1}:{2}" -f $Browser, $cookieDomain, $cookieProfile
$platformCookieFile = Join-Path $ProjectRoot ".cache\gallery-dl-$Platform-cookies.txt"
$cookieFile = Join-Path $ProjectRoot ".cache\gallery-dl-cookies.txt"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cookieFile) | Out-Null

Write-Output "Log in to $Platform in the dedicated $Browser window. Complete any platform confirmation yourself."
Read-Host "After the account home page is visible, press Enter"
Stop-IsolatedBrowser $profileRoot

$previousErrorAction = $ErrorActionPreference
try {
    # Windows PowerShell wraps native stderr lines as ErrorRecord objects.
    # gallery-dl logs successful cookie extraction to stderr, so do not let
    # ErrorActionPreference=Stop abort before its process exit code is read.
    $ErrorActionPreference = "Continue"
    $output = & $galleryDl.Source --cookies-from-browser $browserSession --cookies-export $platformCookieFile -o output.jsonl=true --range 1 --dump-json --no-download $testUrl 2>&1
    $galleryExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}
if ($galleryExitCode -ne 0 -or -not (Test-GalleryMediaOutput $output)) {
    $detail = ($output | Select-Object -Last 12) -join [Environment]::NewLine
    throw "$Platform session verification failed. gallery-dl reported:`n$detail"
}

Merge-PlatformCookieFiles $cookieFile
if ($Platform -eq "x") {
    # Recent Edge builds may not allow gallery-dl to export the encrypted
    # Twitter cookies to Netscape format. Keep the closed isolated profile
    # and let gallery-dl decrypt it directly on every request instead.
    Set-ProjectEnvironmentValue "GALLERY_DL_COOKIES_FROM_BROWSER" $browserSession
    Set-ProjectEnvironmentValue "GALLERY_DL_COOKIES_FILE" ""
    Write-Output "$Platform returned real media metadata through gallery-dl. The isolated browser profile is configured for direct cookie reading."
} else {
    Set-ProjectEnvironmentValue "GALLERY_DL_COOKIES_FROM_BROWSER" ""
    Set-ProjectEnvironmentValue "GALLERY_DL_COOKIES_FILE" ".cache/gallery-dl-cookies.txt"
    Write-Output "$Platform returned real media metadata through gallery-dl. Its isolated session was merged into the MCP cookie file under .cache."
}
Write-Output "Open a new Codex task before testing the MCP."
