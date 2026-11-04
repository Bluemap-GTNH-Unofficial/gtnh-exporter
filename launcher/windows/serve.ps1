<#
.SYNOPSIS
Runs `gtnh-client serve` on this Windows host, hidden and detached: the dev container's client commands
(BMGTNH_CLIENT_HOST=http://host.wslc.internal:8390) then run here, natively on the GPU.

.DESCRIPTION
  serve.ps1              start the server unless one answers on the port; returns at once
  serve.ps1 -Stop        stop it (the clients it runs are stopped and cleaned up first)
  serve.ps1 -Install     also start it at every logon (scheduled task \BlueMap-GTNH\gtnh-client-serve)
  serve.ps1 -Uninstall   remove that task

Logs: WS\out\gtnh-client-serve.log (requests, launcher pids, exit codes, disconnects) and
WS\out\gtnh-client-serve.console.log (the server process's own output: uv, start-up errors, tracebacks).
The launcher's virtual environment is gtnh-exporter\launcher\.venv-windows (UV_PROJECT_ENVIRONMENT): .venv is the
container's, and uv on Windows would delete it. Java: the launcher's default, the Temurin JDK in
%USERPROFILE%\.jdks\temurin-25.0.5+8, unless BMGTNH_JAVA is set.
#>
param([int]$Port = 8390, [switch]$Stop, [switch]$Install, [switch]$Uninstall)
$ErrorActionPreference = "Stop"

$launcher = Split-Path -Parent $PSScriptRoot
$ws = Split-Path -Parent (Split-Path -Parent $launcher)
$log = Join-Path $ws "out\gtnh-client-serve.log"
$console = Join-Path $ws "out\gtnh-client-serve.console.log"
$task = "gtnh-client-serve"
$taskPath = "\BlueMap-GTNH\"

function Get-Health {
    try { Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$Port/health" } catch { $null }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -TaskPath $taskPath -Confirm:$false
    Write-Output "removed the logon task $taskPath$task"
    exit 0
}

if ($Stop) {
    $health = Get-Health
    if (-not $health) { Write-Output "no gtnh-client server on port $Port"; exit 0 }
    # The server's launchers see their stdin close and stop like on SIGTERM (client trees killed, files restored).
    Stop-Process -Id $health.pid
    Write-Output "stopped the gtnh-client server (pid $($health.pid), $($health.running) command(s) were running)"
    exit 0
}

if ($Install) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    # interactive session (the clients need the desktop's GPU), no run-time limit
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $task -TaskPath $taskPath -Action $action -Trigger $trigger -Settings $settings `
        -Force | Out-Null
    Write-Output "registered the logon task $taskPath$task"
}

$health = Get-Health
if ($health) {
    Write-Output "a gtnh-client server already runs on port $Port (pid $($health.pid), workspace $($health.root))"
    exit 0
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
$uv = (Get-Command uv).Source
# `python -m gtnh_client`, not the gtnh-client.exe entry point: Windows locks a running executable, and `uv run` could
# then not reinstall the project. cmd keeps one console log for the detached server; the server writes $log itself.
$command = "`"$uv`" run --directory `"$launcher`" python -m gtnh_client serve --port $Port --log `"$log`" >> `"$console`" 2>&1"
Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", "`"$command`"" -WindowStyle Hidden -WorkingDirectory $launcher
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    $health = Get-Health
    if ($health) {
        Write-Output "gtnh-client server on http://127.0.0.1:$Port (pid $($health.pid)); log $log"
        exit 0
    }
}
Write-Error "the gtnh-client server did not answer on port $Port within 60 s; see $console"
exit 1
