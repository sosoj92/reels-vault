# setup_windows_tasks.ps1 - Demarrage Windows du Life Intelligence System.
#
# Reutilise le Planificateur de taches (aucun service Windows) :
#   - "Reels Vault Ingest" / "Reels Vault - Hourly" (existantes, inchangees) : ingestion horaire ;
#   - "Reels Vault - Telegram Gateway" : a l'ouverture de session, en permanence, redemarrage
#     automatique si elle s'arrete en erreur ;
#   - "Reels Vault - Mentor Daily" : chaque jour a 07:30 + 20 min apres l'ouverture de session,
#     uniquement PC au repos (10 min), rattrapage si le PC etait eteint (StartWhenAvailable).
#
# Usage : powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"
$User = "$env:USERDOMAIN\$env:USERNAME"

# --- Passerelle Telegram : permanente, relancee si elle tombe.
$gatewayAction = New-ScheduledTaskAction -Execute $PyW `
    -Argument "`"$Root\telegram_gateway.py`"" -WorkingDirectory $Root
$gatewayTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$gatewaySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Reels Vault - Telegram Gateway" -Action $gatewayAction `
    -Trigger $gatewayTrigger -Settings $gatewaySettings -User $User -Force `
    -Description "Life Intelligence : passerelle Telegram (polling local, whitelist)." | Out-Null

# --- Passage quotidien du Mentor : lourd (Qwen + Agents), seulement PC au repos.
$dailyAction = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$Root\mentor_daily.bat`"" -WorkingDirectory $Root
$daily = New-ScheduledTaskTrigger -Daily -At 07:30
$logon = New-ScheduledTaskTrigger -AtLogOn -User $User
$logon.Delay = "PT20M"
$dailySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfIdle `
    -IdleDuration (New-TimeSpan -Minutes 10) -IdleWaitTimeout (New-TimeSpan -Hours 12) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries
$dailySettings.IdleSettings.StopOnIdleEnd = $false
Register-ScheduledTask -TaskName "Reels Vault - Mentor Daily" -Action $dailyAction `
    -Trigger @($daily, $logon) -Settings $dailySettings -User $User -Force `
    -Description "Life Intelligence : passage quotidien du Mentor (PC au repos, rattrapage)." | Out-Null

Get-ScheduledTask | Where-Object { $_.TaskName -like "Reels Vault*" } |
    Select-Object TaskName, State | Format-Table -AutoSize
