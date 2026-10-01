# setup_windows_tasks.ps1 - Demarrage Windows du Life Intelligence System.
#
# Reutilise le Planificateur de taches (aucun service Windows) :
#   - "Reels Vault - Hourly" : ingestion horaire (inbox.bat). Creee UNIQUEMENT si aucune
#     tache existante ne lance deja inbox.bat (les taches existantes restent inchangees) ;
#   - "Reels Vault - Telegram Gateway" : a l'ouverture de session, en permanence, redemarrage
#     automatique si elle s'arrete en erreur ;
#   - "Reels Vault - Mentor Daily" : chaque jour a 07:30 + 20 min apres l'ouverture de session,
#     uniquement PC au repos (10 min), rattrapage si le PC etait eteint (StartWhenAvailable).
#
# Toutes les taches utilisent la .venv du repo (chemins calcules depuis ce dossier).
#
# Usage : powershell -ExecutionPolicy Bypass -File .\setup_windows_tasks.ps1
#         .\setup_windows_tasks.ps1 -Only Ingestion,MentorDaily   # sous-ensemble
#         .\setup_windows_tasks.ps1 -SkipTelegramGateway          # Telegram pas encore configure

param(
    [ValidateSet("Ingestion", "TelegramGateway", "MentorDaily")]
    [string[]]$Only = @("Ingestion", "TelegramGateway", "MentorDaily"),
    [switch]$SkipTelegramGateway
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PyW = Join-Path $Root ".venv\Scripts\pythonw.exe"
$User = "$env:USERDOMAIN\$env:USERNAME"

# --- Ingestion horaire : seulement si aucune tache ne lance deja inbox.bat.
if ($Only -contains "Ingestion") {
    $inbox = Join-Path $Root "inbox.bat"
    $existing = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
            ((@($_.Actions) | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " ") -like "*$inbox*"
        })
    if ($existing.Count -gt 0) {
        Write-Host "Ingestion : deja planifiee ($(($existing | ForEach-Object TaskName) -join ', ')), inchangee."
    } else {
        $ingestAction = New-ScheduledTaskAction -Execute "cmd.exe" `
            -Argument "/c `"$inbox`"" -WorkingDirectory $Root
        $ingestTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours((Get-Date).Hour + 1) `
            -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
        $ingestSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -WakeToRun -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName "Reels Vault - Hourly" -Action $ingestAction `
            -Trigger $ingestTrigger -Settings $ingestSettings -User $User `
            -Description "Ingestion automatique du mobile-inbox toutes les heures" | Out-Null
    }
}

# --- Passerelle Telegram : permanente, relancee si elle tombe.
if (($Only -contains "TelegramGateway") -and -not $SkipTelegramGateway) {
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
}

# --- Passage quotidien du Mentor : lourd (Qwen + Agents), seulement PC au repos.
if ($Only -contains "MentorDaily") {
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
}

Get-ScheduledTask | Where-Object { $_.TaskName -like "Reels Vault*" } |
    Select-Object TaskName, State | Format-Table -AutoSize
