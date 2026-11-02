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

    That one line does everything: Session Keeper, the kiosk settings, and
    automatic Windows sign-in. You will be asked for the Windows password
    once, in the window that asks permission. Press Enter at that prompt to
    leave automatic sign-in alone.

    To avoid the login prompt on a new machine, set these first:
        $env:CHIROTV_USER = "yourusername"
        $env:CHIROTV_PASS = "yourpassword"
        irm https://raw.githubusercontent.com/billybnyc/ChiroTVScreens/main/bootstrap.ps1 | iex

    This file is pure ASCII on purpose. Windows PowerShell 5.1 reads .ps1 as
    Windows-1252, and a UTF-8 dash breaks parsing.
#>

[CmdletBinding()]
param(
    [switch] $SkipKiosk,          # do not touch sleep / screensaver / update settings
    [switch] $SkipAutoLogon,      # do NOT set up automatic Windows sign-in
    [int]    $ActiveHoursStart = 7,
    [int]    $ActiveHoursEnd   = 19
)

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

$BootstrapBuild = "b2 (2026-11-02) - autologon on by default"

Write-Host ""
Write-Host "  Session Keeper deploy  $BootstrapBuild" -ForegroundColor White
Write-Host "  ------------------------------------------------------"

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
# Kiosk settings - needs administrator, so it runs in its own elevated window
# ---------------------------------------------------------------------------
if (-not $SkipKiosk) {
    Step "Preparing the machine to run unattended"

    $alreadyAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    $hardening = @'
param([int] $ActiveHoursStart = 7, [int] $ActiveHoursEnd = 19, [switch] $EnableAutoLogon)
$ErrorActionPreference = "Continue"
Write-Host ""
Write-Host "  Kiosk settings" -ForegroundColor Cyan
Write-Host "  --------------"

powercfg /change monitor-timeout-ac 0   | Out-Null
powercfg /change monitor-timeout-dc 0   | Out-Null
powercfg /change standby-timeout-ac 0   | Out-Null
powercfg /change standby-timeout-dc 0   | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-dc 0 | Out-Null
powercfg /change disk-timeout-ac 0      | Out-Null
Write-Host "  [ok] Display and sleep set to Never" -ForegroundColor Green

powercfg /setacvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null
powercfg /setdcvalueindex SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 2>$null
powercfg /setactive SCHEME_CURRENT 2>$null
Write-Host "  [ok] Sign-in on wake disabled" -ForegroundColor Green

foreach ($root in @("HKCU:\Control Panel\Desktop", "HKU:\.DEFAULT\Control Panel\Desktop")) {
    try {
        if (Test-Path $root) {
            Set-ItemProperty -Path $root -Name ScreenSaveActive -Value "0" -Force
            Set-ItemProperty -Path $root -Name ScreenSaveTimeOut -Value "0" -Force
            Set-ItemProperty -Path $root -Name ScreenSaverIsSecure -Value "0" -Force
        }
    } catch { }
}
Write-Host "  [ok] Screensaver disabled" -ForegroundColor Green

try {
    $sys = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    if (-not (Test-Path $sys)) { New-Item -Path $sys -Force | Out-Null }
    Set-ItemProperty -Path $sys -Name "InactivityTimeoutSecs" -Value 0 -Type DWord -Force
    Write-Host "  [ok] Idle lock disabled" -ForegroundColor Green
} catch { Write-Host "  [!]  Could not disable idle lock" -ForegroundColor Yellow }

try {
    $au = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    if (-not (Test-Path $au)) { New-Item -Path $au -Force | Out-Null }
    Set-ItemProperty -Path $au -Name "ActiveHoursStart" -Value $ActiveHoursStart -Type DWord -Force
    Set-ItemProperty -Path $au -Name "ActiveHoursEnd"   -Value $ActiveHoursEnd   -Type DWord -Force
    Set-ItemProperty -Path $au -Name "IsActiveHoursEnabled" -Value 1 -Type DWord -Force
    Write-Host ("  [ok] No update reboots between {0:00}:00 and {1:00}:00" -f $ActiveHoursStart, $ActiveHoursEnd) -ForegroundColor Green
} catch { Write-Host "  [!]  Could not set active hours" -ForegroundColor Yellow }

try {
    $pl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device"
    if (-not (Test-Path $pl)) { New-Item -Path $pl -Force | Out-Null }
    Set-ItemProperty -Path $pl -Name DevicePasswordLessBuildVersion -Value 0 -Type DWord -Force
    Write-Host "  [ok] netplwiz auto sign-in tick box unhidden" -ForegroundColor Green
} catch { Write-Host "  [!]  Could not unhide the netplwiz tick box" -ForegroundColor Yellow }

if ($EnableAutoLogon) {
    Write-Host ""
    Write-Host "  Automatic Windows sign-in for $env:USERNAME" -ForegroundColor Cyan
    Write-Host "  Anyone who powers this PC on will get a desktop with no password." -ForegroundColor Yellow
    Write-Host ""
    $secure = Read-Host "  Windows password for $env:USERNAME (blank to skip)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ($plain) {
        $wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        Set-ItemProperty -Path $wl -Name "AutoAdminLogon" -Value "1" -Force
        Set-ItemProperty -Path $wl -Name "DefaultUserName" -Value $env:USERNAME -Force
        Set-ItemProperty -Path $wl -Name "DefaultDomainName" -Value $env:COMPUTERNAME -Force
        Remove-ItemProperty -Path $wl -Name "AutoLogonCount" -ErrorAction SilentlyContinue

        # Sysinternals Autologon keeps the password in the LSA secret store
        # instead of the registry. Use it when it is available.
        $stored = $false
        $exe = Get-Command "autologon64.exe","autologon.exe" -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($exe) {
            try {
                & $exe.Source $env:USERNAME $env:COMPUTERNAME $plain /accepteula | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    Remove-ItemProperty -Path $wl -Name "DefaultPassword" -ErrorAction SilentlyContinue
                    $stored = $true
                    Write-Host "  [ok] Automatic sign-in enabled (password stored encrypted)" -ForegroundColor Green
                }
            } catch { }
        }

        if (-not $stored) {
            Set-ItemProperty -Path $wl -Name "DefaultPassword" -Value $plain -Force
            Write-Host "  [ok] Automatic sign-in enabled" -ForegroundColor Green
            Write-Host "  [!]  The Windows password is in the registry in PLAIN TEXT." -ForegroundColor Yellow
            Write-Host "  [!]  Any administrator on this PC can read it." -ForegroundColor Yellow
            Write-Host "  [!]  To store it encrypted instead: run netplwiz, untick the box," -ForegroundColor Yellow
            Write-Host "  [!]  and enter the password there. That replaces this." -ForegroundColor Yellow
        }
        $plain = $null
    } else {
        Write-Host "  Automatic sign-in left unchanged." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "  Kiosk settings done." -ForegroundColor Green
Start-Sleep -Seconds 4
'@

    $hardenFile = Join-Path $env:TEMP ("sk-kiosk-" + [Guid]::NewGuid().ToString("N").Substring(0,8) + ".ps1")
    Set-Content -Path $hardenFile -Value $hardening -Encoding ASCII

    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$hardenFile`"",
                 "-ActiveHoursStart", $ActiveHoursStart, "-ActiveHoursEnd", $ActiveHoursEnd)
    if (-not $SkipAutoLogon) { $argList += "-EnableAutoLogon" }

    try {
        if ($alreadyAdmin) {
            Say "Applying kiosk settings..."
            & powershell @argList
        } else {
            Say "Windows will ask permission to change these settings - click Yes."
            Start-Process -FilePath "powershell" -ArgumentList $argList -Verb RunAs -Wait
        }
        Good "Kiosk settings applied"
    } catch {
        Warn "Kiosk settings were not applied: $($_.Exception.Message)"
        Warn "The screen still works; it may sleep or show a screensaver."
        Warn "To do it later, run this again and click Yes on the prompt."
    }
    Remove-Item $hardenFile -Force -ErrorAction SilentlyContinue
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
