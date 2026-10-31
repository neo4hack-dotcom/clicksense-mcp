# =====================================================================
#  ClickSense - run the MCP HTTP server at boot on a Windows server.
#
#  Uses the built-in Task Scheduler (no download, no third-party service wrapper):
#  a task that starts at boot under a service account, restarts on failure and
#  appends its output to logs\clicksense-http.log.
#
#    powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 install [-OpenFirewall]
#    powershell -ExecutionPolicy Bypass -File scripts\windows\service.ps1 status
#    ... start | stop | restart | uninstall
#
#  install / uninstall / start / stop need an elevated (Administrator) prompt.
#  Address, port and tokens are read from .env by the server itself
#  (CLICKSENSE_HTTP_HOST, CLICKSENSE_HTTP_PORT, CLICKSENSE_HTTP_TOKENS).
# =====================================================================
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "restart", "status")]
    [string]$Action = "status",
    [string]$TaskName = "ClickSense MCP",
    # SYSTEM works for most setups. Use a domain service account when ClickHouse access
    # depends on the account (e.g. Kerberos); it is asked for with -Credential.
    [string]$User = "SYSTEM",
    [System.Management.Automation.PSCredential]$Credential,
    # Adds an inbound rule for the port (Domain and Private profiles only).
    [switch]$OpenFirewall
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Serve = Join-Path $Root "serve-windows.bat"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "clicksense-http.log"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Require-Admin {
    if (-not (Test-Admin)) {
        Write-Host "[ERROR] Run this from an elevated PowerShell (Run as Administrator)." -ForegroundColor Red
        exit 1
    }
}

function Get-EnvValue([string]$Name, [string]$Default) {
    # The server reads .env itself; this is only to show the address and open the firewall.
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match "^\s*$Name\s*=\s*(.+?)\s*$") { return $Matches[1].Trim('"', "'") }
        }
    }
    return $Default
}

function Stop-ServerProcesses {
    # Stopping the task ends cmd.exe; the python child would survive it, so end it explicitly.
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*-m clicksense*--transport http*" -and
                       ($_.ExecutablePath -eq $venvPython -or $_.CommandLine -like "*$Root*") } |
        ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host "Task '$TaskName' is not installed."
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $port = [int](Get-EnvValue "CLICKSENSE_HTTP_PORT" "3110")
    $hostName = Get-EnvValue "CLICKSENSE_HTTP_HOST" "127.0.0.1"
    $listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    Write-Host "Task       : $TaskName ($($task.State))"
    Write-Host "Last run   : $($info.LastRunTime)  result $($info.LastTaskResult)"
    Write-Host "Listening  : $(if ($listening) { "yes, port $port" } else { "no (port $port)" })"
    Write-Host "Endpoint   : http://$($hostName):$port/mcp"
    Write-Host "Log        : $LogFile"
    if (Test-Path $LogFile) {
        Write-Host "--- last log lines ---"
        Get-Content $LogFile -Tail 10
    }
}

switch ($Action) {
    "install" {
        Require-Admin
        if (-not (Test-Path (Join-Path $Root ".venv\Scripts\python.exe"))) {
            Write-Host "[ERROR] Run install-windows.bat first." -ForegroundColor Red
            exit 1
        }
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        $hostName = Get-EnvValue "CLICKSENSE_HTTP_HOST" "127.0.0.1"
        $port = [int](Get-EnvValue "CLICKSENSE_HTTP_PORT" "3110")
        $tokens = Get-EnvValue "CLICKSENSE_HTTP_TOKENS" ""
        if ($hostName -notin @("127.0.0.1", "localhost", "::1") -and -not $tokens) {
            Write-Host "[ERROR] CLICKSENSE_HTTP_HOST=$hostName needs CLICKSENSE_HTTP_TOKENS in .env." -ForegroundColor Red
            exit 1
        }

        $cmd = "/c `"`"$Serve`" >> `"$LogFile`" 2>&1`""
        $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd -WorkingDirectory $Root
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        if ($User -eq "SYSTEM") {
            $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                -Principal $principal -Description "ClickSense MCP server (HTTP) - $Root" -Force | Out-Null
        } else {
            if (-not $Credential) { $Credential = Get-Credential -UserName $User -Message "Service account for $TaskName" }
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password `
                -RunLevel Highest -Description "ClickSense MCP server (HTTP) - $Root" -Force | Out-Null
        }
        Write-Host "[OK] Task '$TaskName' registered (at boot, restart on failure)." -ForegroundColor Green

        if ($OpenFirewall) {
            $rule = "ClickSense MCP (TCP $port)"
            if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
                New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $port `
                    -Action Allow -Profile Domain, Private | Out-Null
            }
            Write-Host "[OK] Firewall rule '$rule' (Domain, Private)." -ForegroundColor Green
        }

        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 4
        Show-Status
    }
    "uninstall" {
        Require-Admin
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            Stop-ServerProcesses
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Get-NetFirewallRule -DisplayName "ClickSense MCP (TCP *)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        Write-Host "[OK] Task '$TaskName' removed (logs and configuration kept)." -ForegroundColor Green
    }
    "start" {
        Require-Admin
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 4
        Show-Status
    }
    "stop" {
        Require-Admin
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-ServerProcesses
        Write-Host "[OK] Stopped."
    }
    "restart" {
        Require-Admin
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Stop-ServerProcesses
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 4
        Show-Status
    }
    "status" { Show-Status }
}
