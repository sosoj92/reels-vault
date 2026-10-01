# setup_upstreams.ps1 - Upstreams du LIS aux SHA EXACTS du lock file.
#
# Lit deployment-kit/lis.lock.json et, pour chaque upstream :
#   - absent                      -> clone puis checkout du SHA exact ;
#   - present au bon SHA          -> rien (idempotent) ;
#   - present a un autre SHA      -> checkout du SHA exact, SAUF si des modifications
#                                    locales non gerees seraient en jeu (BLOCK sans -Force) ;
#   - dossier present non Git ou remote inattendu -> BLOCK (jamais de suppression).
# Puis reapplique le Jarvis Voice Bridge (install_jarvis_voice_bridge.py, idempotent).
#
# Ne suit JAMAIS origin/main : une mise a jour d'upstream = modification volontaire du
# lock file apres Security Gate et revue. Aucun reset --hard, aucun git clean : les
# fichiers non suivis (config.yaml, jetons, sauvegardes locales) ne sont jamais touches.
#
# Usage :
#   .\setup_upstreams.ps1 -Audit     # etat seul, aucune modification
#   .\setup_upstreams.ps1 -DryRun    # etat + actions prevues, aucune modification
#   .\setup_upstreams.ps1            # applique
#   .\setup_upstreams.ps1 -Force     # autorise le checkout malgre des modifications
#                                    # locales non gerees (patch sauvegarde avant)
# Codes de sortie : 0 OK, 1 avertissements, 2 bloque / echec.

param(
    [switch]$Audit,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$SkipBridge,
    [switch]$ForceRefresh,
    [string]$Root = "",
    [string]$LockFile = "",
    [string]$ReportDir = ""
)

$ErrorActionPreference = "Stop"
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Root = (Resolve-Path -LiteralPath $Root).Path
$KitLib = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "deployment-kit\lib\LisDeploy.psm1"
Import-Module $KitLib -Force -DisableNameChecking
if (-not $LockFile) { $LockFile = Join-Path $Root "deployment-kit\lis.lock.json" }
if (-not $ReportDir) { $ReportDir = Join-Path $Root "deployment-kit\reports" }

$Apply = -not ($Audit -or $DryRun)
$Mode = "APPLY"
if ($Audit) { $Mode = "AUDIT" } elseif ($DryRun) { $Mode = "DRY-RUN" }

$report = New-LisReport -Name "setup_upstreams"
Write-LisHeader "UPSTREAMS ($Mode) - lock : $LockFile"

if ($ForceRefresh) {
    Add-LisResult $report "option -ForceRefresh" "WARNING" "obsolete : ce script ne supprime plus jamais un dossier ; ignoree" | Out-Null
}
if (-not (Test-LisCommand "git")) {
    Add-LisResult $report "git" "FAIL" "git introuvable (winget install Git.Git)" | Out-Null
    exit 2
}

try {
    $lock = Read-LisLock -Path $LockFile
} catch {
    Add-LisResult $report "lis.lock.json" "FAIL" $_.Exception.Message | Out-Null
    exit 2
}

function Invoke-Git {
    param([string[]]$GitArgs, [int]$TimeoutSec = 600)
    $res = Invoke-LisNative -File "git" -Arguments $GitArgs -TimeoutSec $TimeoutSec
    if ($res.ExitCode -ne 0) {
        $msg = ($res.Stderr + " " + $res.Stdout).Trim()
        if ($msg.Length -gt 300) { $msg = $msg.Substring(0, 300) }
        throw "git $($GitArgs -join ' ') : $msg"
    }
    return $res.Stdout
}

function Get-ShaObject {
    param([string]$Path, [string]$Sha)
    $res = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "cat-file", "-e", "$Sha^{commit}") -TimeoutSec 30
    return ($res.ExitCode -eq 0)
}

function Get-LockedSha {
    param([string]$Path, [string]$Sha)
    if (-not (Get-ShaObject -Path $Path -Sha $Sha)) {
        $res = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "fetch", "--depth", "1", "origin", $Sha) -TimeoutSec 600
        if ($res.ExitCode -ne 0 -or -not (Get-ShaObject -Path $Path -Sha $Sha)) {
            # Serveur refusant le fetch par SHA : historique complet puis recherche.
            Invoke-Git @("-C", $Path, "fetch", "--tags", "origin") | Out-Null
            $shallow = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "rev-parse", "--is-shallow-repository") -TimeoutSec 30
            if ($shallow.Stdout.Trim() -eq "true") { Invoke-Git @("-C", $Path, "fetch", "--unshallow", "origin") | Out-Null }
        }
    }
    if (-not (Get-ShaObject -Path $Path -Sha $Sha)) { throw "SHA $Sha introuvable sur le remote." }
}

foreach ($up in @($lock.upstreams)) {
    $path = Join-Path $Root ($up.path -replace '/', '\')
    $short = $up.sha.Substring(0, 10)
    $component = "upstream $($up.name)"
    $managed = @($up.managed_paths)

    if (-not (Test-Path -LiteralPath $path)) {
        if (-not $Apply) {
            Add-LisResult $report $component "WARNING" "absent ; action prevue : clone + checkout $short" | Out-Null
            continue
        }
        try {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
            Invoke-Git @("-C", $path, "init", "-q") | Out-Null
            Invoke-Git @("-C", $path, "remote", "add", "origin", $up.url) | Out-Null
            Get-LockedSha -Path $path -Sha $up.sha
            Invoke-Git @("-C", $path, "checkout", "-q", "--detach", $up.sha) | Out-Null
            $now = (Get-LisGitState -Path $path).Sha
            if ($now -ne $up.sha) { throw "SHA installe $now different du lock" }
            Add-LisResult $report $component "OK" "clone au SHA $short" | Out-Null
        } catch {
            Add-LisResult $report $component "FAIL" $_.Exception.Message | Out-Null
        }
        continue
    }

    if (-not (Test-Path -LiteralPath (Join-Path $path ".git"))) {
        Add-LisResult $report $component "BLOCK" "$($up.path) existe mais n'est pas un clone Git ; deplace-le toi-meme (jamais supprime)" | Out-Null
        continue
    }

    $state = Get-LisGitState -Path $path
    if (-not $state.IsRepo) {
        Add-LisResult $report $component "BLOCK" "depot Git illisible (aucun commit ?)" | Out-Null
        continue
    }
    if ((ConvertTo-LisRepoUrl $state.Origin) -ne (ConvertTo-LisRepoUrl $up.url)) {
        Add-LisResult $report $component "BLOCK" "remote origin inattendu ($($state.Origin)) ; attendu $($up.url)" | Out-Null
        continue
    }
    $unmanaged = @($state.Modified | Where-Object { $managed -notcontains $_ })
    $managedMods = @($state.Modified | Where-Object { $managed -contains $_ })
    $extra = ""
    if ($managedMods.Count -gt 0) { $extra += " ; $($managedMods.Count) fichier(s) du bridge" }
    if ($state.Untracked.Count -gt 0) { $extra += " ; $($state.Untracked.Count) non suivi(s) preserve(s)" }

    if ($state.Sha -eq $up.sha) {
        if ($unmanaged.Count -gt 0) {
            Add-LisResult $report $component "WARNING" ("SHA $short conforme ; modifications locales hors bridge : " + ($unmanaged -join ", ")) | Out-Null
        } else {
            Add-LisResult $report $component "OK" "SHA $short conforme$extra" | Out-Null
        }
        continue
    }

    $from = $state.Sha.Substring(0, 10)
    if ($unmanaged.Count -gt 0 -and -not $Force) {
        Add-LisResult $report $component "BLOCK" ("HEAD $from != lock $short et modifications locales non gerees : " + ($unmanaged -join ", ") + " ; relance avec -Force (patch sauvegarde avant)") | Out-Null
        continue
    }
    if (-not $Apply) {
        Add-LisResult $report $component "WARNING" "HEAD $from != lock $short ; action prevue : checkout $short$extra" | Out-Null
        continue
    }
    try {
        if ($state.Modified.Count -gt 0) {
            # Sauvegarde des modifications locales AVANT de les retirer de l'arbre de travail.
            $patchDir = Join-Path $Root "backups\upstream-patches"
            New-Item -ItemType Directory -Force -Path $patchDir | Out-Null
            $patch = Join-Path $patchDir ("{0}-{1}.patch" -f $up.name, (Get-Date).ToString("yyyyMMdd-HHmmss"))
            $diff = Invoke-Git @("-C", $path, "diff", "--binary", "HEAD")
            [System.IO.File]::WriteAllText($patch, $diff, (New-Object System.Text.UTF8Encoding($false)))
            foreach ($file in $state.Modified) { Invoke-Git @("-C", $path, "checkout", "-q", "HEAD", "--", $file) | Out-Null }
            $extra += " ; patch : backups\upstream-patches\" + (Split-Path -Leaf $patch)
        }
        Get-LockedSha -Path $path -Sha $up.sha
        Invoke-Git @("-C", $path, "checkout", "-q", "--detach", $up.sha) | Out-Null
        $now = (Get-LisGitState -Path $path).Sha
        if ($now -ne $up.sha) { throw "SHA installe $now different du lock" }
        Add-LisResult $report $component "OK" "checkout $from -> $short$extra" | Out-Null
    } catch {
        Add-LisResult $report $component "FAIL" $_.Exception.Message | Out-Null
    }
}

# --- Jarvis Voice Bridge (reapplique apres tout checkout ; idempotent).
$jarvis = @($lock.upstreams) | Where-Object { $_.name -eq "jarvis-assistant-vocal" } | Select-Object -First 1
$installer = Join-Path $Root "install_jarvis_voice_bridge.py"
if ($jarvis -and -not $SkipBridge) {
    $jarvisPath = Join-Path $Root ($jarvis.path -replace '/', '\')
    $python = Find-LisPython -Root $Root
    if (-not (Test-Path -LiteralPath $jarvisPath)) {
        Add-LisResult $report "jarvis voice bridge" "WARNING" "Jarvis absent : bridge installe apres le clone" | Out-Null
    } elseif (-not $python -or -not (Test-Path -LiteralPath $installer)) {
        Add-LisResult $report "jarvis voice bridge" "WARNING" "Python ou installateur absent : bridge non verifie" | Out-Null
    } else {
        $check = Invoke-LisNative -File $python -Arguments @($installer, "--check", "--jarvis", $jarvisPath) -TimeoutSec 60
        if ($check.ExitCode -eq 0) {
            Add-LisResult $report "jarvis voice bridge" "OK" "deja installe (aucune ecriture)" | Out-Null
        } elseif (-not $Apply) {
            Add-LisResult $report "jarvis voice bridge" "WARNING" "incomplet ; action prevue : python install_jarvis_voice_bridge.py" | Out-Null
        } else {
            $run = Invoke-LisNative -File $python -Arguments @($installer, "--jarvis", $jarvisPath) -TimeoutSec 120
            if ($run.ExitCode -eq 0) {
                Add-LisResult $report "jarvis voice bridge" "OK" "installe / reapplique" | Out-Null
            } else {
                $msg = ($run.Stderr -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 1)
                Add-LisResult $report "jarvis voice bridge" "FAIL" "installation echouee : $msg" | Out-Null
            }
        }
    }
}

$worst = Get-LisWorstRank $report
try { $saved = Save-LisReport -Report $report -Directory $ReportDir -Extra @{ mode = $Mode } } catch { $saved = "" }
Write-Host ""
if ($saved) { Write-Host "Rapport : $saved" }
exit $worst
