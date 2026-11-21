# Installs the Discord Presence receiver for the current user and runs it from
# Task Scheduler: at sign-in, re-checked every 5 minutes (it exits at once if a
# copy is already running), windowless through `conhost --headless`.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Allow 192.0.2.20] [-Token secret]
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#
# Or straight from GitHub, no download needed (see the README's quick setup):
#   & ([scriptblock]::Create((irm <raw url of this file>)))
#
# Run from an elevated PowerShell, it also adds the inbound firewall rule: for the
# -Allow addresses, or your local network and Tailscale when -Allow is omitted.
# Without elevation it skips that and tells you.
param(
    [string[]]$Allow = @(),
    [int]$Port = 8920,
    [string]$Token = '',
    [string]$TaskName = 'Discord Presence Receiver',
    [string]$Source = 'https://raw.githubusercontent.com/chino/steamdeck-discord-status/relay-hub/receivers/windows/discord-receiver.ps1',
    [switch]$Uninstall
)
$RuleName = 'Discord Presence receiver'
$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
$ErrorActionPreference = 'Stop'
$dir = Join-Path $env:LOCALAPPDATA 'discord-presence-receiver'
$script = Join-Path $dir 'discord-receiver.ps1'

function Stop-Relay {
    $owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).OwningProcess
    if ($owner) { Stop-Process -Id $owner -Force }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Stop-Relay
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
    if ($elevated) { Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue }
    "Removed the task and $dir."
    return
}

New-Item -ItemType Directory -Force $dir | Out-Null
$local = if ($PSScriptRoot) { Join-Path $PSScriptRoot 'discord-receiver.ps1' }
if ($local -and (Test-Path $local)) {
    Copy-Item $local $script -Force
} else {
    Invoke-WebRequest -UseBasicParsing $Source -OutFile $script   # run from a one-liner
}

if ($elevated) {
    $remote = if ($Allow) { $Allow } else { @('LocalSubnet', '100.64.0.0/10', 'fd7a:115c:a1e0::/48') }
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort $Port -RemoteAddress $remote | Out-Null
    "Firewall: allowed TCP $Port from $($remote -join ', ')."
} else {
    Write-Warning "Not elevated, so no firewall rule was added. Allow inbound TCP $Port yourself, or rerun this from an admin PowerShell."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$taskArgs = "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Port $Port$(if ($Allow) { " -Allow $($Allow -join ',')" })$(if ($Token) { " -Token $Token" })"
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $taskArgs
$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn -User $user
    New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description 'Relays Discord rich presence from another machine.' -Force | Out-Null

Stop-Relay                      # replace an older copy, if any
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).OwningProcess
if ($owner) { "Relay running (pid $owner), listening on $Port. Log: $env:LOCALAPPDATA\discord-relay.log" }
else { Write-Warning "The task started but nothing is listening on $Port. Check $env:LOCALAPPDATA\discord-relay.log" }
