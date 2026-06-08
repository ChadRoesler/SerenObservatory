<#
══════════════════════════════════════════════════════════════════════════
  seren-agent-setup.ps1  -  one-shot SerenAgent installer (Windows)

  Rip it and win. This script:
    1. Finds a usable Python (3.10-3.12) via the py launcher
    2. Makes a clean venv at %USERPROFILE%\seren-venvs\agent (no pip wrestling)
    3. Installs seren-agent from the latest GitHub release (or a local .whl)
    4. Drops a double-clickable start-seren-agent.cmd launcher
    5. (optional) registers a logon Scheduled Task so it starts automatically

  Defaults: binds 0.0.0.0 on port 7777 (Seren cluster convention).
  No admin needed for the basic path - pure native Windows Python.

  USAGE  (open PowerShell in the script's folder)
    .\seren-agent-setup.ps1                    # easy mode
    .\seren-agent-setup.ps1 -GenToken          # generate a bearer token
    .\seren-agent-setup.ps1 -AutoStart         # + start on logon
    .\seren-agent-setup.ps1 -Wheel .\seren_agent-1.0.0-py3-none-any.whl
    .\seren-agent-setup.ps1 -Ref v1.0.0        # pin to a release tag

  If you get "running scripts is disabled", launch with:
    powershell -ExecutionPolicy Bypass -File .\seren-agent-setup.ps1
══════════════════════════════════════════════════════════════════════════
#>
[CmdletBinding()]
param(
  [int]    $Port      = 7777,
  [string] $BindHost  = "0.0.0.0",
  [string] $Token     = "",
  [switch] $GenToken,
  [string] $Wheel     = "",
  [string] $Ref       = "",
  [string] $Repo      = "ChadRoesler/SerenAgent",
  [switch] $AutoStart,
  [string] $VenvDir   = "$env:USERPROFILE\seren-venvs\agent"
)

$ErrorActionPreference = "Stop"

# ── pretty output ──────────────────────────────────────────────────────────
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Blue }
function Ok($m)   { Write-Host "  + $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m"   -ForegroundColor Yellow }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Write-Host "==========================================" -ForegroundColor Green
Write-Host "  seren-agent setup (Windows)"             -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green

# ── 1. find a usable Python ────────────────────────────────────────────────
Step "Finding a usable Python (3.10-3.12)"
$pyCandidates = @(
  @{ cmd = "py";     pre = @("-3.12") },
  @{ cmd = "py";     pre = @("-3.11") },
  @{ cmd = "py";     pre = @("-3.10") },
  @{ cmd = "python"; pre = @() }
)
$PyCmd = $null; $PyPre = @()
foreach ($c in $pyCandidates) {
  if (-not (Get-Command $c.cmd -ErrorAction SilentlyContinue)) { continue }
  try {
    $ver = (& $c.cmd @($c.pre) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null | Out-String).Trim()
  } catch { continue }
  if ($ver -in @("3.10","3.11","3.12")) { $PyCmd = $c.cmd; $PyPre = $c.pre; break }
}
if (-not $PyCmd) {
  Die @"
No Python 3.10-3.12 found. Install one, e.g.:
    winget install Python.Python.3.12
  or grab it from https://www.python.org/downloads/ (tick 'Add to PATH').
"@
}
$pyVer = & $PyCmd @($PyPre) -c "import sys;print('%d.%d.%d'%sys.version_info[:3])"
Ok "Using '$PyCmd $($PyPre -join ' ')' (Python $pyVer)"

# ── 2. resolve the wheel to install ────────────────────────────────────────
$WheelSrc = ""; $CleanupWheel = $false
if ($Wheel) {
  if (-not (Test-Path $Wheel)) { Die "wheel not found: $Wheel" }
  $WheelSrc = (Resolve-Path $Wheel).Path
  Ok "Installing from local wheel: $(Split-Path $WheelSrc -Leaf)"
} else {
  Step "Resolving the latest seren-agent release from GitHub ($Repo)"
  $api = if ($Ref) { "https://api.github.com/repos/$Repo/releases/tags/$Ref" }
         else      { "https://api.github.com/repos/$Repo/releases/latest" }
  try {
    $rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "seren-setup" }
  } catch { Die "GitHub API request failed ($api). Check the repo/tag and your network." }
  $asset = $rel.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
  if (-not $asset) { Die "No .whl asset in release '$($rel.tag_name)'. Pass -Wheel to install a local file." }
  Ok "Release $($rel.tag_name)  ($($asset.name))"
  $WheelSrc = Join-Path $env:TEMP $asset.name
  $CleanupWheel = $true
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $WheelSrc -Headers @{ "User-Agent" = "seren-setup" }
  Ok "Downloaded"
}

# ── 3. venv + install ──────────────────────────────────────────────────────
Step "Creating venv at $VenvDir"
$Vpy = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $Vpy) {
  Warn "venv already exists - reusing it (will upgrade the package)"
} else {
  & $PyCmd @($PyPre) -m venv $VenvDir
  if (-not (Test-Path $Vpy)) { Die "venv creation failed" }
  Ok "venv created"
}

Step "Installing seren-agent"
& $Vpy -m pip install -q --upgrade pip
& $Vpy -m pip install -q --upgrade $WheelSrc
if ($LASTEXITCODE -ne 0) { Die "pip install failed - see output above" }
Ok "Installed"
if ($CleanupWheel) { Remove-Item $WheelSrc -ErrorAction SilentlyContinue }

# ── 4. sanity check ────────────────────────────────────────────────────────
Step "Sanity-checking the install"
$check = & $Vpy -c "import seren_agent; print('OK: v' + seren_agent.__version__)"
if ($check -like "OK:*") {
  Ok "Package imports cleanly ($check)"
} else {
  Die "Install looks broken: $check"
}

# ── 5. token (optional) ────────────────────────────────────────────────────
if ($GenToken) {
  $Token = (& $Vpy -c "import secrets;print(secrets.token_urlsafe(32))").Trim()
  Ok "Generated bearer token"
}

# ── 6. launcher ────────────────────────────────────────────────────────────
$Launcher = "$env:USERPROFILE\start-seren-agent.cmd"
$tokenLine = if ($Token) { "set SEREN_AGENT_TOKEN=$Token" } else { "rem No bearer token configured" }
@"
@echo off
REM Start seren-agent. Double-click this, or run it from a terminal.
set AGENT_HOST=$BindHost
set AGENT_PORT=$Port
$tokenLine
"$Vpy" -m seren_agent.app
"@ | Set-Content -Path $Launcher -Encoding ASCII
Ok "Launcher: $Launcher  (double-clickable)"

# ── 7. optional logon autostart ───────────────────────────────────────────
if ($AutoStart) {
  Step "Registering a logon Scheduled Task"
  try {
    $action  = New-ScheduledTaskAction -Execute $Vpy `
                 -Argument "-m seren_agent.app" -WorkingDirectory $env:USERPROFILE
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $env_vars = @("AGENT_HOST=$BindHost", "AGENT_PORT=$Port")
    if ($Token) { $env_vars += "SEREN_AGENT_TOKEN=$Token" }
    Register-ScheduledTask -TaskName "seren-agent" -Action $action -Trigger $trigger `
        -Settings $set -Description "seren-agent per-node management plane" -Force | Out-Null
    Ok "Autostart registered (Task Scheduler -> 'seren-agent')"
  } catch {
    Warn "Couldn't register the task automatically ($($_.Exception.Message))."
    Warn "You can still start it any time with: $Launcher"
  }
}

# ── done ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  seren-agent is set up +"                  -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Start it:   $Launcher"
Write-Host "  Ping:       http://${BindHost}:${Port}/api/v1/system/ping"
Write-Host "  Docs:       http://${BindHost}:${Port}/docs"
if ($Token) { Write-Host "  Token:      $Token" -ForegroundColor Yellow }
$pyVer = & $PyCmd @($PyPre) -c "import sys;print('%d.%d.%d'%sys.version_info[:3])"
Ok "Using '$PyCmd $($PyPre -join ' ')' (Python $pyVer)"

# ── 2. resolve the wheel to install ────────────────────────────────────────
$WheelSrc = ""; $CleanupWheel = $false
if ($Wheel) {
  if (-not (Test-Path $Wheel)) { Die "wheel not found: $Wheel" }
  $WheelSrc = (Resolve-Path $Wheel).Path
  Ok "Installing from local wheel: $(Split-Path $WheelSrc -Leaf)"
} else {
  Step "Resolving the latest SerenMemory release from GitHub ($Repo)"
  $api = if ($Ref) { "https://api.github.com/repos/$Repo/releases/tags/$Ref" }
         else      { "https://api.github.com/repos/$Repo/releases/latest" }
  try {
    $rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "seren-setup" }
  } catch { Die "GitHub API request failed ($api). Check the repo/tag and your network." }
  $asset = $rel.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
  if (-not $asset) { Die "No .whl asset in release '$($rel.tag_name)'. Pass -Wheel to install a local file." }
  Ok "Release $($rel.tag_name)  ($($asset.name))"
  $WheelSrc = Join-Path $env:TEMP $asset.name
  $CleanupWheel = $true
  Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $WheelSrc -Headers @{ "User-Agent" = "seren-setup" }
  Ok "Downloaded"
}

# ── 3. venv + install ──────────────────────────────────────────────────────
Step "Creating venv at $VenvDir"
$Vpy = Join-Path $VenvDir "Scripts\python.exe"
if (Test-Path $Vpy) {
  Warn "venv already exists - reusing it (will upgrade the package)"
} else {
  & $PyCmd @($PyPre) -m venv $VenvDir
  if (-not (Test-Path $Vpy)) { Die "venv creation failed" }
  Ok "venv created"
}

# ── 4. sanity check (import + the viewer asset that's bitten us before) ─────
Step "Sanity-checking the install"
$check = & $Vpy -c @"
import pathlib
try:
    import seren_agent
except Exception as e:
    print('IMPORT_FAILED:', e); raise SystemExit
v = pathlib.Path(seren_agent.__file__).parent / 'viewer' / 'halls.html'
print('OK' if v.exists() else 'VIEWER_MISSING')
"@
switch -Wildcard ($check) {
  "OK"             { Ok "Package imports and the Halls viewer asset is present" }
  "VIEWER_MISSING" { Warn "Installed but halls.html missing - /viewer will 404 (packaging regression)" }
  default          { Die "Install looks broken: $check" }
}

# ── 5. config ──────────────────────────────────────────────────────────────
Step "Writing config at $CfgPath"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
if ($GenToken) { $Token = (& $Vpy -c "import secrets;print(secrets.token_urlsafe(32))").Trim() }
if (Test-Path $CfgPath) {
  $bak = "$CfgPath.bak.$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
  Copy-Item $CfgPath $bak
  Warn "Existing config backed up to $(Split-Path $bak -Leaf)"
}
# ~ expands to your home dir on Windows too (Python's expanduser), so the
@"
# SerenMemory config - generated by seren-agent-setup.ps1
# Full reference: see seren-agent.yaml.sample in the repo.
server:
  host: $BindHost          # 127.0.0.1 = this machine only; 0.0.0.0 = the LAN
  port: $Port
  # Empty = no auth (fine for local). A token requires
  #   Authorization: Bearer <token>  on every route except / and /health.
  bearer_token: "$Token"

storage:
  # ~ expands to your user folder. Created on first run. THIS is your agent -
  # back it up, and it survives package upgrades untouched.
"@ | Set-Content -Path $CfgPath -Encoding UTF8
Ok "Config written"
# If a token is set, the config holds a secret - lock its ACL to you + admins.
if ($Token) {
  try {
    icacls $CfgPath /inheritance:r /grant:r "$($env:USERNAME):F" "Administrators:F" | Out-Null
    Ok "Config ACL locked (it holds your token)"
  } catch { Warn "Couldn't tighten the config ACL automatically - do it by hand if this box is shared." }
}

# ── 6. launcher (the rip-it-and-win artifact) ──────────────────────────────
$Launcher = "$AppDir\start-seren-agent.cmd"
@"
@echo off
REM Start SerenAgent. Double-click this, or run it from a terminal.
"$Vpy" -m seren_agent --config "$CfgPath"
"@ | Set-Content -Path $Launcher -Encoding ASCII
Ok "Launcher: $Launcher  (double-clickable)"

# ── 7. optional logon autostart (no admin service wrapper needed) ──────────
if ($AutoStart) {
  Step "Registering a logon Scheduled Task (starts SerenAgent when you log in)"
  try {
    $action  = New-ScheduledTaskAction -Execute $Vpy `
                 -Argument "-m seren_agent --config `"$CfgPath`"" -WorkingDirectory $AppDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "SerenAgent" -Action $action -Trigger $trigger `
        -Settings $set -Description "SerenAgent local agent service" -Force | Out-Null
    Ok "Autostart registered (Task Scheduler -> 'SerenAgent')"
  } catch {
    Warn "Couldn't register the task automatically ($($_.Exception.Message))."
    Warn "You can still start it any time with the launcher above."
  }
}

# ── done ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  SerenMemory is set up +"                  -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Start it:        $Launcher"
Write-Host "  Viewer:          http://${BindHost}:${Port}/viewer"
Write-Host "  MCP endpoint:    http://${BindHost}:${Port}/mcp/   (note the trailing slash)"
Write-Host "  VSCode plugin:   set serenMemory.endpoint to http://${BindHost}:${Port}"
if ($Token) {
  Write-Host "  Bearer token:    $Token" -ForegroundColor Yellow
  Write-Host "                   (also set it in the plugin via 'Seren Memory: Set Bearer Token')"
}
Write-Host ""
Warn "First write/search downloads the embedding model (~80MB) - that one needs internet."
Write-Host "Rip it and win. (hot-dog) (wrench)" -ForegroundColor Green