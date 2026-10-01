# restore_lis.ps1 - Reconstruction du LIS depuis GitHub + un backup backup_lis.ps1.
#
# 1. VERIFY  (aucune ecriture) : manifest.json, format/version, SHA-256 de chaque fichier,
#            integrity_check de vault.db, lock file, SHA du code attendu.
# 2. DATA    : vault.db (jamais par-dessus une base existante sans -ReplaceVault, qui la
#            DEPLACE dans backups\ au lieu de la supprimer), fichiers personnels (jamais
#            d'ecrasement), configuration non sensible, secrets seulement avec -RestoreSecrets.
# 3. REBUILD : setup_lis.ps1 -Install (Python, .venv, paquets, FFmpeg, Ollama/Qwen, e5,
#            upstreams aux SHA exacts, Jarvis + Voice Bridge, mobile-inbox, taches Windows).
# 4. CHECK   : CHECK_LIS.ps1 ; "LIS RESTORED" seulement si les composants critiques passent.
#
# Usage :
#   .\restore_lis.ps1 -BackupPath "E:\LIS-BACKUPS\LIS-BACKUP-20261001-120000" -VerifyOnly
#   .\restore_lis.ps1 -BackupPath "E:\...\LIS-BACKUP-..."                  # PC neuf
#   .\restore_lis.ps1 -BackupPath "..." -RestoreSecrets                    # + secrets chiffres
#   .\restore_lis.ps1 -BackupPath "..." -ReplaceVault                      # base existante deplacee
# Ne JAMAIS tester par-dessus le LIS reel : utiliser -Root vers un dossier temporaire.
# -NoSystem (tests) : ne touche ni aux variables utilisateur Windows ni a Syncthing.

param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$Root = "",
    [switch]$VerifyOnly,
    [switch]$RestoreSecrets,
    [switch]$ReplaceVault,
    [switch]$SkipInstall,
    [switch]$SkipTasks,
    [switch]$SkipHealthCheck,
    [switch]$NoSystem,
    [string]$Python = "",
    [string]$ReportDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = $ScriptDir }
New-Item -ItemType Directory -Force -Path $Root | Out-Null
$Root = (Resolve-Path -LiteralPath $Root).Path
Import-Module (Join-Path $ScriptDir "deployment-kit\lib\LisDeploy.psm1") -Force -DisableNameChecking
if (-not $ReportDir) { $ReportDir = Join-Path $Root "deployment-kit\reports" }
$JarvisDir = Join-Path $Root "external\jarvis-assistant-vocal"

$report = New-LisReport -Name "restore_lis"
function Rec { param($c, $s, $d = "") Add-LisResult -Report $report -Component $c -Status $s -Details $d | Out-Null }
function Finish {
    param([int]$Code, [string]$Verdict)
    try { $saved = Save-LisReport -Report $report -Directory $ReportDir -Extra @{ verdict = $Verdict; backup = (Split-Path -Leaf $BackupPath) } } catch { $saved = "" }
    Write-Host ""
    Write-Host ("=" * 78)
    $color = "Green"
    if ($Code -eq 1) { $color = "Yellow" } elseif ($Code -ge 2) { $color = "Red" }
    Write-Host $Verdict -ForegroundColor $color
    Write-Host ("=" * 78)
    if ($saved) { Write-Host "Rapport : $saved" }
    exit $Code
}

Write-LisHeader "LIS RESTORE - backup : $BackupPath -> $Root"

# ===========================================================================
# 1. VERIFY (lecture seule)
# ===========================================================================
if (-not (Test-Path -LiteralPath $BackupPath)) { Rec "backup" "FAIL" "introuvable : $BackupPath"; Finish 2 "BACKUP REFUSE" }
$BackupPath = (Resolve-Path -LiteralPath $BackupPath).Path
$manifestPath = Join-Path $BackupPath "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath)) { Rec "manifest.json" "FAIL" "absent"; Finish 2 "BACKUP REFUSE" }
try { $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Rec "manifest.json" "FAIL" "illisible"; Finish 2 "BACKUP REFUSE" }
Rec "manifest.json" "OK" "format $($manifest.format) v$($manifest.format_version), cree $($manifest.created_utc) sur $($manifest.hostname)"

if (-not $Python) { $Python = Find-LisPython -Root $Root }
if (-not $Python -and -not $VerifyOnly -and -not $SkipInstall) {
    Write-Host "Aucun Python : installation de Python via setup_lis.ps1 -Install -Only winget,python" -ForegroundColor Yellow
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "setup_lis.ps1") -Install -Only "winget,python" -Root $Root
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $Python = Find-LisPython -Root $Root
}
if (-not $Python) { Rec "python" "FAIL" "aucun Python >= 3.12 : winget install Python.Python.3.14 puis relance"; Finish 2 "LIS NOT RESTORED" }

# SHA-256 de chaque fichier (helper Python : noms de fichiers > 260 caracteres supportes).
$mv = Invoke-LisHelper -Python $Python -Arguments @("manifest-verify", "--dir", $BackupPath) -TimeoutSec 3600
if (-not $mv.Data -or -not $mv.Data.ok) {
    $why = "verification impossible"
    if ($mv.Data -and $mv.Data.PSObject.Properties["errors"]) { $why = (@($mv.Data.errors) | Select-Object -First 5) -join " ; " }
    Rec "SHA-256" "FAIL" "backup altere ou incomplet : $why"
    Finish 2 "BACKUP REFUSE (aucune donnee restauree)"
}
Rec "SHA-256" "OK" "$(@($manifest.files).Count) fichier(s) verifies"

try { $backupLock = Read-LisLock -Path (Join-Path $BackupPath "lis.lock.json") } catch { Rec "lis.lock.json (backup)" "FAIL" $_.Exception.Message; Finish 2 "BACKUP REFUSE" }
$tables = @($backupLock.vault.required_tables) -join ","
$int = Invoke-LisHelper -Python $Python -Arguments @("integrity", "--db", (Join-Path $BackupPath "vault.db"), "--tables", $tables) -TimeoutSec 600
if (-not $int.Data -or -not $int.Data.ok) {
    $why = "verification impossible"
    if ($int.Data) { $why = "$($int.Data.integrity) ; tables manquantes : $(@($int.Data.missing_tables) -join ', ')" }
    Rec "vault.db (backup)" "FAIL" $why
    Finish 2 "BACKUP REFUSE (aucune donnee restauree)"
}
Rec "vault.db (backup)" "OK" "integrity_check ok, $($int.Data.table_count) tables"

$repoLockPath = Join-Path $Root "deployment-kit\lis.lock.json"
if (Test-Path -LiteralPath $repoLockPath) {
    $repoLock = Read-LisLock -Path $repoLockPath
    $diff = @()
    foreach ($up in @($backupLock.upstreams)) {
        $cur = @($repoLock.upstreams) | Where-Object { $_.name -eq $up.name } | Select-Object -First 1
        if (-not $cur -or $cur.sha -ne $up.sha) { $diff += $up.name }
    }
    if ($diff.Count) { Rec "lock file" "WARNING" ("upstreams differents du backup (le lock du code est utilise) : " + ($diff -join ", ")) }
    else { Rec "lock file" "OK" "upstreams identiques au backup" }
} elseif (-not $VerifyOnly) {
    Rec "lock file" "WARNING" "repo sans deployment-kit\lis.lock.json : clone GitHub attendu dans $Root"
}

$git = Get-LisGitState -Path $Root
$wantSha = "$($manifest.lis.sha)"
if (-not $git.IsRepo) { Rec "code LIS" "WARNING" "$Root n'est pas un clone Git (attendu : git clone puis restore)" }
elseif (-not $wantSha) { Rec "code LIS" "WARNING" "SHA du code absent du manifest" }
elseif ($git.Sha -eq $wantSha) { Rec "code LIS" "OK" "HEAD = SHA du backup ($($wantSha.Substring(0, 10)))" }
else {
    $anc = Invoke-LisNative -File "git" -Arguments @("-C", $Root, "merge-base", "--is-ancestor", $wantSha, "HEAD") -TimeoutSec 30
    if ($anc.ExitCode -eq 0) { Rec "code LIS" "OK" "HEAD contient le SHA du backup ($($wantSha.Substring(0, 10)))" }
    else { Rec "code LIS" "WARNING" "HEAD $($git.Sha.Substring(0, 10)) != backup $($wantSha.Substring(0, 10)) : pour le code exact, git checkout $wantSha" }
}

if ($VerifyOnly) { Finish 0 "BACKUP VERIFIED (aucune ecriture)" }

# ===========================================================================
# 2. DATA
# ===========================================================================
$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$targetDb = Join-Path $Root "vault.db"
$backupDbHash = (Get-FileHash -LiteralPath (Join-Path $BackupPath "vault.db") -Algorithm SHA256).Hash
$alreadyRestored = (Test-Path -LiteralPath $targetDb) -and -not (Test-Path -LiteralPath (Join-Path $Root "vault.db-journal")) -and
    ((Get-FileHash -LiteralPath $targetDb -Algorithm SHA256).Hash -eq $backupDbHash)
if ($alreadyRestored) {
    # Relance (idempotence) : la base est exactement celle du backup, rien a faire.
    Rec "vault.db" "OK" "deja restauree (identique au backup), conservee"
} else {
    if (Test-Path -LiteralPath $targetDb) {
        if (-not $ReplaceVault) {
            Rec "vault.db" "BLOCK" "une base existe deja dans $Root : jamais ecrasee. -ReplaceVault la deplace dans backups\ avant restauration"
            Finish 2 "LIS NOT RESTORED (aucune donnee modifiee)"
        }
        $aside = Join-Path $Root "backups\vault-before-restore-$stamp"
        New-Item -ItemType Directory -Force -Path $aside | Out-Null
        foreach ($f in @("vault.db", "vault.db-journal", "vault.db-wal", "vault.db-shm")) {
            $p = Join-Path $Root $f
            if (Test-Path -LiteralPath $p) { Move-Item -LiteralPath $p -Destination (Join-Path $aside $f) }
        }
        Rec "vault.db existant" "OK" "deplace (non supprime) : backups\vault-before-restore-$stamp"
    }
    $tmpDb = Join-Path $Root "vault.db.restoring"
    Copy-Item -LiteralPath (Join-Path $BackupPath "vault.db") -Destination $tmpDb -Force
    $chk = Invoke-LisHelper -Python $Python -Arguments @("integrity", "--db", $tmpDb) -TimeoutSec 600
    if (-not $chk.Data -or -not $chk.Data.ok) { Remove-Item -LiteralPath $tmpDb -Force; Rec "vault.db" "FAIL" "copie restauree non integre"; Finish 2 "LIS NOT RESTORED" }
    Move-Item -LiteralPath $tmpDb -Destination $targetDb
    Rec "vault.db" "OK" "restauree, integrity_check ok"
}

function Restore-Tree {
    # Copie src -> dst sans jamais ecraser un fichier existant. Renvoie @(copies, ignores).
    param([string]$Src, [string]$Dst)
    $r = Invoke-LisHelper -Python $Python -Arguments @("tree-copy", "--src", $Src, "--dst", $Dst, "--no-overwrite") -TimeoutSec 3600
    if (-not $r.Data -or -not $r.Data.ok) { throw "copie impossible : $Src" }
    return @([int]$r.Data.copied, [int]$r.Data.skipped, [int]$r.Data.identical)
}

$pf = Join-Path $BackupPath "personal-files"
if (Test-Path -LiteralPath $pf) {
    $c = 0; $s = 0; $same = 0
    foreach ($item in (Get-ChildItem -LiteralPath $pf -Force)) {
        if ($item.Name -eq "jarvis") { continue }
        if ($item.PSIsContainer) {
            $r = Restore-Tree -Src $item.FullName -Dst (Join-Path $Root $item.Name)
            $c += $r[0]; $s += $r[1]; $same += $r[2]
        } else {
            $t = Join-Path $Root $item.Name
            if (-not (Test-Path -LiteralPath $t)) { Copy-Item -LiteralPath $item.FullName -Destination $t; $c++ }
            elseif ((Get-FileHash -LiteralPath $t).Hash -eq (Get-FileHash -LiteralPath $item.FullName).Hash) { $same++ }
            else { $s++ }
        }
    }
    $msg = "$c fichier(s) restaures, $same deja identiques"
    if ($s) { Rec "personal-files" "WARNING" "$msg, $s different(s) deja presents (conserves, non ecrases)" }
    else { Rec "personal-files" "OK" $msg }
}

# Secrets : dechiffres en memoire, ecrits seulement si la cible est absente.
$deferredJarvis = @()
if ($RestoreSecrets) {
    $box = Join-Path $BackupPath "secrets\secrets.lisenc"
    if (-not (Test-Path -LiteralPath $box)) { Rec "secrets" "WARNING" "ce backup ne contient pas de secrets (-IncludeSecrets non utilise)" }
    else {
        try {
            $pass = Get-LisPassphrase
            $plain = Unprotect-LisBytes -Json ([IO.File]::ReadAllText($box)) -Passphrase $pass
            $pass = $null
            $data = [Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json
            [Array]::Clear($plain, 0, $plain.Length)
            $written = 0; $kept = 0
            foreach ($item in @($data.files)) {
                $kind, $rel = $item.target.Split(":", 2)
                $bytes = [Convert]::FromBase64String($item.b64)
                switch ($kind) {
                    "root" {
                        $t = Join-Path $Root $rel
                        if (Test-Path -LiteralPath $t) { $kept++ } else { [IO.File]::WriteAllBytes($t, $bytes); $written++ }
                    }
                    "jarvis" { $deferredJarvis += [pscustomobject]@{ Rel = $rel; Bytes = $bytes } }
                    "syncthing" {
                        if ($NoSystem) { continue }
                        $stDir = Join-Path $env:LOCALAPPDATA "Syncthing"
                        $t = Join-Path $stDir $rel
                        if ((Test-Path -LiteralPath (Join-Path $stDir "config.xml")) -or (Test-Path -LiteralPath $t)) { $kept++ }
                        else { New-Item -ItemType Directory -Force -Path $stDir | Out-Null; [IO.File]::WriteAllBytes($t, $bytes); $written++ }
                    }
                }
            }
            $envSet = 0; $envKept = 0
            foreach ($p in @($data.user_env.PSObject.Properties | Where-Object { -not $NoSystem })) {
                if ([Environment]::GetEnvironmentVariable($p.Name, "User")) { $envKept++ }
                else { [Environment]::SetEnvironmentVariable($p.Name, [string]$p.Value, "User"); $envSet++ }
            }
            $data = $null
            $status = "OK"
            if ($kept -or $envKept) { $status = "WARNING" }
            Rec "secrets" $status "$written fichier(s) + $envSet variable(s) restaures ; $kept fichier(s) / $envKept variable(s) deja presents conserves (.env jamais ecrase)"
        } catch {
            Rec "secrets" "FAIL" $_.Exception.Message
        }
    }
}
$envTarget = Join-Path $Root ".env"
$redacted = Join-Path $BackupPath "config\env.redacted"
if (-not (Test-Path -LiteralPath $envTarget) -and (Test-Path -LiteralPath $redacted)) {
    Copy-Item -LiteralPath $redacted -Destination $envTarget
    Rec ".env" "OK" "reglages non sensibles restaures ; cles a ressaisir (llm_setup.py, telegram_setup.py)"
} elseif (Test-Path -LiteralPath $envTarget) {
    Rec ".env" "OK" "present (jamais ecrase)"
}

# ===========================================================================
# 3. REBUILD
# ===========================================================================
if ($SkipInstall) {
    Rec "reconstruction" "SKIP" "-SkipInstall : setup_lis.ps1 non lance"
} else {
    Write-Host ""
    Write-Host "--- REBUILD : setup_lis.ps1 -Install" -ForegroundColor White
    $setupArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ScriptDir "setup_lis.ps1"), "-Install", "-SkipHealthCheck", "-Root", $Root)
    if ($SkipTasks) {
        $setupArgs += @("-Only", "winget,git,python,venv,requirements,ffmpeg,ollama,qwen,embeddings,upstreams,jarvis-venv,jarvis-config,env,llm-keys,telegram,notion,vault,mobile-inbox,syncthing,android")
    }
    & powershell.exe @setupArgs
    $setupCode = $LASTEXITCODE
    if ($setupCode -ge 2) { Rec "reconstruction" "WARNING" "setup_lis.ps1 signale des BLOCK/FAIL (voir ci-dessus)" }
    else { Rec "reconstruction" "OK" "setup_lis.ps1 termine (code $setupCode)" }
}

# Jarvis : donnees personnelles et secrets, une fois le checkout present.
$jpf = Join-Path $pf "jarvis"
if ((Test-Path -LiteralPath $jpf) -or $deferredJarvis.Count) {
    if (-not (Test-Path -LiteralPath $JarvisDir)) {
        Rec "jarvis donnees" "WARNING" "Jarvis absent : relance restore_lis.ps1 apres setup_upstreams.ps1"
    } else {
        $c = 0; $s = 0
        if (Test-Path -LiteralPath $jpf) { $r = Restore-Tree -Src $jpf -Dst $JarvisDir; $c += $r[0]; $s += $r[1] }
        foreach ($d in $deferredJarvis) {
            $t = Join-Path $JarvisDir $d.Rel
            if (Test-Path -LiteralPath $t) { $s++ } else { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $t) | Out-Null; [IO.File]::WriteAllBytes($t, $d.Bytes); $c++ }
        }
        Rec "jarvis donnees" "OK" "$c fichier(s) restaures, $s deja presents conserves"
    }
}
$deferredJarvis = $null

# Android / Syncthing : comparaison avec la configuration sauvegardee.
$mapping = Join-Path $BackupPath "syncthing\syncthing-mapping.json"
if ((Test-Path -LiteralPath $mapping) -and -not $NoSystem) {
    $saved = Get-Content -LiteralPath $mapping -Raw -Encoding UTF8 | ConvertFrom-Json
    $inboxFolder = @($saved.folders) | Where-Object { $_.path -like "*mobile-inbox" } | Select-Object -First 1
    $stConfig = Join-Path $env:LOCALAPPDATA "Syncthing\config.xml"
    $hit = $null
    if (Test-Path -LiteralPath $stConfig) {
        $xml = [xml](Get-Content -LiteralPath $stConfig -Raw -Encoding UTF8)
        $hit = @($xml.configuration.folder) | Where-Object { $_.path -and $_.path.TrimEnd('\') -eq (Join-Path $Root "mobile-inbox") } | Select-Object -First 1
    }
    if ($hit) { Rec "syncthing" "OK" "dossier '$($hit.label)' -> $Root\mobile-inbox" }
    elseif ($inboxFolder) { Rec "syncthing" "MANUAL TEST REQUIRED" "ajoute dans Syncthing le dossier ID '$($inboxFolder.id)' (label '$($inboxFolder.label)', $($inboxFolder.type)) -> $Root\mobile-inbox et accepte le telephone" }
    else { Rec "syncthing" "MANUAL TEST REQUIRED" "configure Syncthing (deployment-kit\android\README.md)" }
}
Rec "android" "MANUAL TEST REQUIRED" "partager une video -> verifier mobile-inbox\inbox.txt puis ingestion"

# ===========================================================================
# 4. CHECK
# ===========================================================================
if ($SkipHealthCheck) { Finish 1 "DATA RESTORED (Health Check non lance : -SkipHealthCheck)" }
$json = Join-Path ([IO.Path]::GetTempPath()) ("lis-restore-check-{0}.json" -f [guid]::NewGuid())
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "CHECK_LIS.ps1") -Root $Root -JsonOut $json
$hc = $LASTEXITCODE
$critical = @("vault.db", "sqlite integrity_check", "schema minimal", ".venv python", "imports critiques", "retrieval lexical", "agency registry", "jarvis upstream", "git secrets", "lis.lock.json")
$bad = @()
if (Test-Path -LiteralPath $json) {
    $res = Get-Content -LiteralPath $json -Raw -Encoding UTF8 | ConvertFrom-Json
    Remove-Item -LiteralPath $json -Force
    foreach ($name in $critical) {
        $row = @($res.results) | Where-Object { $_.Component -eq $name } | Select-Object -First 1
        if (-not $row) { if ($name -ne "git secrets") { $bad += "$name (non verifie)" } }
        elseif ($row.Status -eq "FAIL") { $bad += $name }
    }
} else { $bad += "CHECK_LIS.ps1 sans resultat" }
if ($bad.Count) {
    Rec "composants critiques" "FAIL" ($bad -join ", ")
    Finish 2 "LIS NOT RESTORED"
}
Rec "composants critiques" "OK" ($critical -join ", ")
if ($hc -ge 1) { Finish 1 "LIS RESTORED (avec avertissements / etapes manuelles)" }
Finish 0 "LIS RESTORED"
