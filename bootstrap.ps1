<#
    Session Keeper - one-line deploy.

    Works on a brand-new machine and on one that already has Session Keeper.
    It decides which it is and does the right thing.

    NEW MACHINE
        downloads the package, installs Python and Chromium if needed, asks
        for the ChiroTV login, sets up kiosk mode and start-at-login.

    EXISTING MACHINE
        stops the running copy, downloads the current script, refuses it if
        it does not compile, patches the settings, restarts. The existing
        login is left alone - it is never downloaded or uploaded.

    Usage on each machine (PowerShell):
        irm https://raw.githubusercontent.com/billybnyc/ChiroTVScreens/main/bootstrap.ps1 | iex

    To avoid the login prompt on a new machine, set these first:
        $env:CHIROTV_USER = "yourusername"
        $env:CHIROTV_PASS = "yourpassword"
        irm https://raw.githubusercontent.com/billybnyc/ChiroTVScreens/main/bootstrap.ps1 | iex

    This file is pure ASCII on purpose. Windows PowerShell 5.1 reads .ps1 as
    Windows-1252, and a UTF-8 dash breaks parsing.
#>

$ErrorActionPreference = "Stop"

$Repo        = "https://raw.githubusercontent.com/billybnyc/ChiroTVScreens/main"
$ScriptUrl   = "$Repo/session_keeper.py"
$ManifestUrl = "$Repo/manifest.json"
$PackageUrl  = "$Repo/session-keeper.zip"
$InstallDir  = "$env:LOCALAPPDATA\SessionKeeper"

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Good ($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail ($m) { Write-Host ""; Write-Host "FAILED: $m" -ForegroundColor Red; Write-Host ""; exit 1 }

# PowerShell 5.1 negotiates TLS 1.0 by default; GitHub refuses it.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

Write-Host ""
Write-Host "  Session Keeper deploy" -ForegroundColor White
Write-Host "  ---------------------"

$venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
$envFile    = Join-Path $InstallDir ".env"
$isUpgrade  = (Test-Path $venvPython) -and (Test-Path $envFile)

if ($isUpgrade) {
    Say "Existing install found - upgrading it."
} else {
    Say "No install found here - setting this machine up from scratch."
}

# ---------------------------------------------------------------------------
# Stop anything already running, either way
# ---------------------------------------------------------------------------
Step "Stopping Session Keeper if it is running"

$stopped = 0
Get-Process python, pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($InstallDir, "OrdinalIgnoreCase") } |
    ForEach-Object { try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $stopped++ } catch { } }

$profileDir = Join-Path $InstallDir "profile"
try {
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$profileDir*" } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $stopped++ } catch { } }
} catch { }

if ($stopped -gt 0) { Good "Stopped $stopped process(es)" } else { Say "Nothing was running" }
Start-Sleep -Seconds 2


if ($isUpgrade) {

    # =======================================================================
    # UPGRADE: swap the script, patch the settings, keep the login
    # =======================================================================
    Step "Downloading the current keeper script"

    $tempScript = Join-Path $env:TEMP "session_keeper.download.py"
    try {
        Invoke-WebRequest -Uri $ScriptUrl -OutFile $tempScript -UseBasicParsing
    } catch {
        Fail @"
Could not download the script.

  $($_.Exception.Message)

Check that session_keeper.py is in the repo:
  $ScriptUrl
"@
    }

    $buildLine = Select-String -Path $tempScript -Pattern 'SCRIPT_BUILD = "(.+)"' -ErrorAction SilentlyContinue
    if ($buildLine) {
        Good ("Downloaded build: " + $buildLine.Matches[0].Groups[1].Value)
    } else {
        Warn "No build marker in that file - is it really session_keeper.py?"
    }

    & $venvPython -m py_compile $tempScript
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $tempScript -ErrorAction SilentlyContinue
        Fail "The downloaded script does not compile. Nothing on this machine was changed."
    }
    Good "Script compiles"

    $target = Join-Path $InstallDir "session_keeper.py"
    if (Test-Path $target) { Copy-Item $target "$target.bak" -Force }
    Copy-Item $tempScript $target -Force
    Remove-Item $tempScript -ErrorAction SilentlyContinue
    Good "Installed (previous kept as session_keeper.py.bak)"

} else {

    # =======================================================================
    # FRESH INSTALL: full package, then the login
    # =======================================================================
    Step "Downloading the package"

    $work = Join-Path $env:TEMP ("sk-setup-" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $zip = Join-Path $work "session-keeper.zip"

    try {
        Invoke-WebRequest -Uri $PackageUrl -OutFile $zip -UseBasicParsing
    } catch {
        Fail @"
Could not download the package.

  $($_.Exception.Message)

Check that session-keeper.zip is in the repo:
  $PackageUrl
"@
    }
    Good ("Downloaded " + [math]::Round((Get-Item $zip).Length / 1MB, 2) + " MB")

    Expand-Archive -Path $zip -DestinationPath $work -Force
    $installer = Get-ChildItem -Path $work -Filter "install-windows.ps1" -Recurse |
                 Select-Object -First 1
    if (-not $installer) { Fail "install-windows.ps1 was not found inside the package." }
    Good "Extracted"

    Step "Running the installer (Python and Chromium if needed - a few minutes)"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer.FullName -Kiosk -AutoStart
    if ($LASTEXITCODE -ne 0) { Fail "The installer did not finish. See the messages above." }

    if (-not (Test-Path $envFile)) { Fail "The installer did not create $envFile." }

    # -------------------------------------------------------------------
    # The package carries no credentials on purpose - it lives in a public
    # repo. Ask for them here, or take them from environment variables.
    # -------------------------------------------------------------------
    Step "ChiroTV login for this screen"

    $user = $env:CHIROTV_USER
    $pass = $env:CHIROTV_PASS

    if (-not $user) {
        Say "The package carries no password (the repo is public), so enter it once here."
        Write-Host ""
        $user = Read-Host "  ChiroTV username"
    } else {
        Say "Using CHIROTV_USER from the environment."
    }
    if (-not $pass) {
        $secure = Read-Host "  ChiroTV password" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $pass = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    } else {
        Say "Using CHIROTV_PASS from the environment."
    }

    if (-not $user -or -not $pass) {
        Warn "No login entered. The screen will install but cannot sign in."
        Warn "Add SITE_USERNAME and SITE_PASSWORD to $envFile and restart it."
    }

    $lines = @(Get-Content $envFile)
    $out = foreach ($line in $lines) {
        if ($line -match '^\s*SITE_USERNAME\s*=') { "SITE_USERNAME=$user" }
        elseif ($line -match '^\s*SITE_PASSWORD\s*=') { "SITE_PASSWORD=$pass" }
        else { $line }
    }
    Set-Content -Path $envFile -Value $out -Encoding UTF8
    Good "Login saved to this machine only"

    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}


# ---------------------------------------------------------------------------
# Settings both paths need correct
# ---------------------------------------------------------------------------
Step "Applying settings"

Copy-Item $envFile "$envFile.bak" -Force

function Set-Setting {
    param([string[]] $Content, [string] $Key, [string] $Value)
    $found = $false
    $out = foreach ($line in $Content) {
        if ($line -match "^\s*$Key\s*=") { $found = $true; "$Key=$Value" } else { $line }
    }
    if (-not $found) { $out = $out + "$Key=$Value" }
    return $out
}

$lines = @(Get-Content $envFile)
$lines = Set-Setting $lines "FULLSCREEN_VIDEO" "false"
$lines = Set-Setting $lines "FINAL_KEYS" ""
$lines = Set-Setting $lines "UPDATE_MANIFEST_URL" $ManifestUrl
Set-Content -Path $envFile -Value $lines -Encoding UTF8
Good "Settings written (previous kept as .env.bak)"

Write-Host ""
Select-String -Path $envFile -Pattern '^(FULLSCREEN_VIDEO|FINAL_KEYS|KIOSK|UPDATE_MANIFEST_URL)=' |
    ForEach-Object { Say $_.Line }

# ---------------------------------------------------------------------------
Step "Checking the update feed"
try {
    $m = Invoke-WebRequest -Uri $ManifestUrl -UseBasicParsing
    $parsed = $m.Content | ConvertFrom-Json
    Good ("Manifest reachable, version " + $parsed.version)
} catch {
    Warn "Could not reach the manifest: $($_.Exception.Message)"
    Warn "The screen will still run; it just will not receive central updates yet."
}

# ---------------------------------------------------------------------------
Step "Starting Session Keeper"
$runner = Join-Path $InstallDir "Start Session Keeper.bat"
if (Test-Path $runner) {
    Start-Process -FilePath $runner -WorkingDirectory $InstallDir
    Good "Started"
} else {
    Warn "Launcher not found - start it from the Desktop shortcut."
}

Write-Host ""
Write-Host "  Done. This screen now takes its settings from GitHub." -ForegroundColor Green
Write-Host ""
