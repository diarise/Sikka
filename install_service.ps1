<#
    Sikka Sync Agent - Windows Service installer (via NSSM)

    Run this ONCE, as Administrator, after you've already run
    "python sync_agent.py" manually one time to complete the interactive
    activation (tenant ID + SQL Server credentials). That creates
    license.key and db_config.json, which the service will reuse — the
    service itself runs with no console, so it can't answer prompts.

    Usage (from an elevated PowerShell prompt, in this folder):
        .\install_service.ps1

    Requires: nssm.exe (https://nssm.cc/download) — place nssm.exe in this
    same folder, or edit $NssmPath below to point at it.
#>

$ErrorActionPreference = "Stop"

$ServiceName = "SikkaSyncAgent"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe   = (Get-Command python).Source
$AgentScript = Join-Path $ScriptDir "sync_agent.py"
$NssmPath    = Join-Path $ScriptDir "nssm.exe"

if (-not (Test-Path $NssmPath)) {
    Write-Error "nssm.exe not found in $ScriptDir. Download it from https://nssm.cc/download and place it here."
    exit 1
}

if (-not (Test-Path (Join-Path $ScriptDir "license.key"))) {
    Write-Warning "license.key not found. Run 'python sync_agent.py' manually first to complete activation, then re-run this installer."
    exit 1
}

Write-Host "Installing '$ServiceName' as a Windows service..."

& $NssmPath install $ServiceName $PythonExe $AgentScript
& $NssmPath set $ServiceName AppDirectory $ScriptDir
& $NssmPath set $ServiceName DisplayName "Sikka Sync Agent"
& $NssmPath set $ServiceName Description "Synchronise Sage 100 vers Sikka Cloud (Supabase) en continu."
& $NssmPath set $ServiceName Start SERVICE_AUTO_START

# Restart automatically if the process dies, with a short delay to avoid a crash loop.
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppRestartDelay 5000

# Redirect stdout/stderr into the same logs folder the agent already writes to.
$LogDir = Join-Path $ScriptDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
& $NssmPath set $ServiceName AppStdout (Join-Path $LogDir "service_stdout.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $LogDir "service_stderr.log")
& $NssmPath set $ServiceName AppRotateFiles 1
& $NssmPath set $ServiceName AppRotateBytes 5000000

Start-Service $ServiceName

Write-Host "✅ Service '$ServiceName' installed and started."
Write-Host "   Check status:   Get-Service $ServiceName"
Write-Host "   Stop it:        Stop-Service $ServiceName"
Write-Host "   Uninstall it:   & '$NssmPath' remove $ServiceName confirm"
Write-Host "   Logs:           $LogDir"
