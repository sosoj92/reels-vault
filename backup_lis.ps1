# backup_lis.ps1 - Backup coherent et restaurable du Life Intelligence System.
#
# Cree <Destination>\LIS-BACKUP-YYYYMMDD-HHMMSS\ :
#     vault.db                  copie coherente (API de sauvegarde SQLite, jamais une copie brute)
#     manifest.json             date UTC, hote, SHA LIS/upstreams, versions, SHA-256 + tailles
#     lis.lock.json             versions exactes a reconstruire
#     installation-report.json  etat de la machine (sans secret)
#     config/                   reglages NON secrets (.env sans valeurs sensibles, taches, noms de variables)
#     macrodroid/               export MacroDroid s'il existe
#     syncthing/                dossiers / appareils Syncthing (sans cles)
#     personal-files/           donnees non reconstructibles (raw, images, index, inbox, Jarvis...)
#     secrets/                  UNIQUEMENT avec -IncludeSecrets, chiffre (AES-256 + HMAC, PBKDF2)
#
# Non sauvegarde (reconstructible) : .venv, modeles Ollama, modele e5, upstreams, caches, logs.
# Le dossier est ecrit en .partial puis renomme seulement apres verification du manifest.
# Chaque execution cree un nouveau dossier : un backup existant n'est jamais modifie.
#
# Usage :
#   .\backup_lis.ps1                                  # vers %USERPROFILE%\LIS-BACKUPS
#   .\backup_lis.ps1 -Destination E:\LIS-BACKUPS
#   .\backup_lis.ps1 -IncludeSecrets                  # passphrase demandee (saisie masquee)
#   .\backup_lis.ps1 -DryRun                          # liste sans rien ecrire
# Passphrase non interactive : variable d'environnement LIS_BACKUP_PASSPHRASE (jamais journalisee).
# -NoSystem (tests) : ignore taches Windows, Syncthing et variables utilisateur Windows.

param(
    [string]$Destination = "",
    [switch]$IncludeSecrets,
    [switch]$ExcludeImages,
    [switch]$DryRun,
    [switch]$NoSystem,
    [string]$Root = "",
    [string]$Db = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = $ScriptDir }
$Root = (Resolve-Path -LiteralPath $Root).Path
Import-Module (Join-Path $ScriptDir "deployment-kit\lib\LisDeploy.psm1") -Force -DisableNameChecking
if (-not $Destination) { $Destination = Join-Path $env:USERPROFILE "LIS-BACKUPS" }
if (-not $Db) { $Db = Join-Path $Root "vault.db" }
$LockFile = Join-Path $Root "deployment-kit\lis.lock.json"
$JarvisDir = Join-Path $Root "external\jarvis-assistant-vocal"

$report = New-LisReport -Name "backup_lis"
function Rec { param($c, $s, $d = "") Add-LisResult -Report $report -Component $c -Status $s -Details $d | Out-Null }
$mode = "BACKUP"
if ($DryRun) { $mode = "DRY-RUN" }
Write-LisHeader "LIS BACKUP ($mode)"

# --- Ce qui est sauvegarde (chemins relatifs a la racine) -------------------
$PersonalItems = @("raw", "index.md", "taxonomy.md", "progression.txt", "journal.json",
    "mobile-inbox", "inbox", "life_intelligence_v2.enabled", "mentor_proactive.enabled", "test-liens.txt")
if (-not $ExcludeImages) { $PersonalItems += "images" }
$PersonalItems += @(Get-ChildItem -LiteralPath $Root -Filter "recovered_tiktok_links_*.txt" -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
# Jarvis : donnees personnelles NON secretes (ignorees par Git upstream).
$JarvisPersonal = @("memoire.json", "memory.json", "contenus.yaml", "budget.json", "reminders.json",
    "scenes.yaml", "notes", "gestes\calibration.json")
# Secrets : uniquement chiffres, uniquement avec -IncludeSecrets.
$SecretFiles = @(
    @{ Target = "root:.env" },
    @{ Target = "jarvis:config.yaml" },
    @{ Target = "jarvis:desktop_agent\config.yaml" },
    @{ Target = "jarvis:google_credentials.json" },
    @{ Target = "jarvis:google_token.json" },
    @{ Target = "jarvis:.env" },
    @{ Target = "syncthing:cert.pem" },
    @{ Target = "syncthing:key.pem" }
)
$SecretEnvPattern = '^(NOTION_|CLICKUP_|GOOGLE_|GEMINI_|OPENROUTER_|ANTHROPIC_|TELEGRAM_|TODOIST_|TRELLO_|AIRTABLE_|DAVY_|TASK_PROVIDER$)'
$SensitiveKey = '(TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIAL|AUTH|_ID$)'

function Resolve-SecretPath {
    param([string]$Target)
    $kind, $rel = $Target.Split(":", 2)
    switch ($kind) {
        "root" { return (Join-Path $Root $rel) }
        "jarvis" { return (Join-Path $JarvisDir $rel) }
        "syncthing" { return (Join-Path $env:LOCALAPPDATA "Syncthing\$rel") }
    }
    return $null
}

# --- Pre-requis -------------------------------------------------------------
if (-not (Test-Path -LiteralPath $Db)) { Rec "vault.db" "FAIL" "absent : $Db"; exit 2 }
if (-not $Python) { $Python = Find-LisPython -Root $Root }
if (-not $Python) { Rec "python" "FAIL" "aucun Python >= 3.12 (necessaire pour l'API backup SQLite)"; exit 2 }
try { $lock = Read-LisLock -Path $LockFile } catch { Rec "lis.lock.json" "FAIL" $_.Exception.Message; exit 2 }

$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$name = "LIS-BACKUP-$stamp"
$final = Join-Path $Destination $name
$work = "$final.partial"
if ((Test-Path -LiteralPath $final) -or (Test-Path -LiteralPath $work)) { Rec "destination" "FAIL" "$final existe deja (jamais ecrase)"; exit 2 }

if ($DryRun) {
    Rec "destination" "OK" "$final (non cree)"
    Rec "vault.db" "OK" ("API backup SQLite, {0:N1} Mo" -f ((Get-Item -LiteralPath $Db).Length / 1MB))
    foreach ($item in $PersonalItems) { if (Test-Path -LiteralPath (Join-Path $Root $item)) { Rec "personal-files" "OK" $item } }
    foreach ($item in $JarvisPersonal) { if (Test-Path -LiteralPath (Join-Path $JarvisDir $item)) { Rec "personal-files" "OK" "jarvis\$item" } }
    if ($IncludeSecrets) {
        foreach ($s in $SecretFiles) { if (Test-Path -LiteralPath (Resolve-SecretPath $s.Target)) { Rec "secrets (chiffres)" "OK" $s.Target } }
    } else { Rec "secrets" "SKIP" "non inclus (-IncludeSecrets pour les chiffrer)" }
    Write-Host ""
    Write-Host "DRY-RUN : aucun fichier ecrit."
    exit 0
}

New-Item -ItemType Directory -Force -Path $work | Out-Null
$ok = $false
try {
    # 1. vault.db : instantane coherent via l'API de sauvegarde SQLite.
    $bk = Invoke-LisHelper -Python $Python -Arguments @("sqlite-backup", "--src", $Db, "--dst", (Join-Path $work "vault.db")) -TimeoutSec 900
    if (-not $bk.Data -or -not $bk.Data.ok) {
        $why = "erreur inconnue"
        if ($bk.Data -and $bk.Data.PSObject.Properties["error"]) { $why = $bk.Data.error } elseif ($bk.Data) { $why = $bk.Data.integrity }
        throw "sauvegarde SQLite echouee : $why"
    }
    Rec "vault.db" "OK" ("{0} ; integrity_check {1} ; {2:N1} Mo" -f $bk.Data.method, $bk.Data.integrity, ($bk.Data.size / 1MB))

    # 2. lock file.
    Copy-Item -LiteralPath $LockFile -Destination (Join-Path $work "lis.lock.json")
    Rec "lis.lock.json" "OK" "copie"

    # 3. config non secrete.
    $cfg = Join-Path $work "config"
    New-Item -ItemType Directory -Force -Path $cfg | Out-Null
    $envFile = Join-Path $Root ".env"
    if (Test-Path -LiteralPath $envFile) {
        $lines = foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
                $key = $Matches[1]
                if ($key -match $SensitiveKey) { "$key=" } else { $line }
            } else { $line }
        }
        $lines | Set-Content -LiteralPath (Join-Path $cfg "env.redacted") -Encoding UTF8
        Rec "config .env" "OK" "cles conservees, valeurs sensibles retirees (env.redacted)"
    }
    $userEnvNames = @()
    if (-not $NoSystem) { $userEnvNames = @([Environment]::GetEnvironmentVariables("User").Keys | Where-Object { $_ -match $SecretEnvPattern } | Sort-Object) }
    ConvertTo-Json -InputObject @($userEnvNames) | Set-Content -LiteralPath (Join-Path $cfg "user-env-names.json") -Encoding UTF8
    Rec "config variables Windows" "OK" "$($userEnvNames.Count) nom(s) de variable (valeurs non sauvegardees)"
    $taskDir = Join-Path $cfg "tasks"
    New-Item -ItemType Directory -Force -Path $taskDir | Out-Null
    $tasks = @()
    if (-not $NoSystem) { $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "Reels Vault*" }) }
    foreach ($t in $tasks) {
        $file = Join-Path $taskDir (($t.TaskName -replace '[^A-Za-z0-9 _-]', '_') + ".xml")
        Export-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath | Set-Content -LiteralPath $file -Encoding Unicode
    }
    Rec "config taches Windows" "OK" "$($tasks.Count) tache(s) exportee(s) (reference, recreees par setup_windows_tasks.ps1)"

    # 4. MacroDroid.
    $md = Join-Path $work "macrodroid"
    New-Item -ItemType Directory -Force -Path $md | Out-Null
    $macros = @(Get-ChildItem -LiteralPath (Join-Path $Root "deployment-kit\android\macrodroid") -Filter "*.macro" -File -ErrorAction SilentlyContinue)
    foreach ($m in $macros) { Copy-Item -LiteralPath $m.FullName -Destination $md }
    if ($macros.Count) { Rec "macrodroid" "OK" "$($macros.Count) export(s)" }
    else {
        "EXPORT REQUIRED FROM ANDROID DEVICE - voir deployment-kit/android/README.md" | Set-Content -LiteralPath (Join-Path $md "EXPORT_REQUIRED.txt") -Encoding UTF8
        Rec "macrodroid" "MANUAL TEST REQUIRED" "EXPORT REQUIRED FROM ANDROID DEVICE"
    }

    # 5. Syncthing (sans cles).
    $st = Join-Path $work "syncthing"
    New-Item -ItemType Directory -Force -Path $st | Out-Null
    $stConfig = Join-Path $env:LOCALAPPDATA "Syncthing\config.xml"
    if ($NoSystem) {
        Rec "syncthing" "SKIP" "-NoSystem"
    } elseif (Test-Path -LiteralPath $stConfig) {
        $xml = [xml](Get-Content -LiteralPath $stConfig -Raw -Encoding UTF8)
        $info = [ordered]@{
            folders = @(@($xml.configuration.folder) | Where-Object { $_.path } | ForEach-Object { [ordered]@{ id = $_.id; label = $_.label; path = $_.path; type = $_.type } })
            devices = @(@($xml.configuration.device) | ForEach-Object { [ordered]@{ id = $_.id; name = $_.name } })
        }
        $info | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $st "syncthing-mapping.json") -Encoding UTF8
        Rec "syncthing" "OK" "$(@($info.folders).Count) dossier(s), $(@($info.devices).Count) appareil(s) (aucune cle)"
    } else { Rec "syncthing" "NOT CONFIGURED" "config.xml absent" }

    # 6. Fichiers personnels non reconstructibles.
    $pf = Join-Path $work "personal-files"
    New-Item -ItemType Directory -Force -Path $pf | Out-Null
    $count = 0
    function Copy-PersonalItem {
        # Dossiers copies par le helper Python (chemins > 260 caracteres supportes).
        param([string]$Src, [string]$Dst)
        if ((Get-Item -LiteralPath $Src -Force).PSIsContainer) {
            $r = Invoke-LisHelper -Python $Python -Arguments @("tree-copy", "--src", $Src, "--dst", $Dst, "--exclude", ".stfolder,.stversions,__pycache__") -TimeoutSec 3600
            if (-not $r.Data -or -not $r.Data.ok) { throw "copie impossible : $Src" }
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
            Copy-Item -LiteralPath $Src -Destination $Dst
        }
    }
    foreach ($item in $PersonalItems) {
        $src = Join-Path $Root $item
        if (-not (Test-Path -LiteralPath $src)) { continue }
        Copy-PersonalItem -Src $src -Dst (Join-Path $pf $item)
        $count++
    }
    $usage = Join-Path $Root "logs\llm_usage.db"
    if (Test-Path -LiteralPath $usage) {
        $u = Invoke-LisHelper -Python $Python -Arguments @("sqlite-backup", "--src", $usage, "--dst", (Join-Path $pf "logs\llm_usage.db")) -TimeoutSec 300
        if ($u.Data -and $u.Data.ok) { $count++ }
    }
    if (Test-Path -LiteralPath $JarvisDir) {
        foreach ($item in $JarvisPersonal) {
            $src = Join-Path $JarvisDir $item
            if (-not (Test-Path -LiteralPath $src)) { continue }
            Copy-PersonalItem -Src $src -Dst (Join-Path $pf "jarvis\$item")
            $count++
        }
    }
    Rec "personal-files" "OK" "$count element(s) (raw, index, inbox, Jarvis...)"

    # 7. Secrets : uniquement sur demande, chiffres en memoire (aucun clair sur disque).
    if ($IncludeSecrets) {
        $items = @()
        foreach ($s in $SecretFiles) {
            $p = Resolve-SecretPath $s.Target
            if ($NoSystem -and $s.Target -like "syncthing:*") { continue }
            if ($p -and (Test-Path -LiteralPath $p)) {
                $items += [ordered]@{ target = $s.Target; b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($p)) }
            }
        }
        $envValues = [ordered]@{}
        foreach ($n in $userEnvNames) { $envValues[$n] = [Environment]::GetEnvironmentVariable($n, "User") }
        $payload = [ordered]@{ files = $items; user_env = $envValues } | ConvertTo-Json -Depth 4 -Compress
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        $payload = $null; $envValues = $null
        $iterations = 600000
        if ($env:LIS_KDF_ITERATIONS -and [int]$env:LIS_KDF_ITERATIONS -ge 10000) { $iterations = [int]$env:LIS_KDF_ITERATIONS }
        $pass = Get-LisPassphrase -Confirm
        $boxed = Protect-LisBytes -Plain $bytes -Passphrase $pass -Iterations $iterations
        [Array]::Clear($bytes, 0, $bytes.Length)
        $pass = $null
        $sd = Join-Path $work "secrets"
        New-Item -ItemType Directory -Force -Path $sd | Out-Null
        [IO.File]::WriteAllText((Join-Path $sd "secrets.lisenc"), $boxed, (New-Object Text.UTF8Encoding($false)))
        Rec "secrets" "OK" "$($items.Count) fichier(s) + $($userEnvNames.Count) variable(s), AES-256-CBC + HMAC-SHA256, PBKDF2 x$iterations"
    } else {
        Rec "secrets" "SKIP" "non inclus (reconfiguration manuelle ou -IncludeSecrets)"
    }

    # 8. Rapport d'installation (aucun secret).
    $lisGit = Get-LisGitState -Path $Root
    $upInfo = @()
    foreach ($up in @($lock.upstreams)) {
        $g = Get-LisGitState -Path (Join-Path $Root ($up.path -replace '/', '\'))
        $upInfo += [ordered]@{ name = $up.name; sha_lock = $up.sha; sha_installed = $g.Sha; dirty = $g.Dirty }
    }
    $pyVer = (Invoke-LisNative -File $Python -Arguments @("-c", "import sys,sqlite3;print(sys.version.split()[0]+' '+sqlite3.sqlite_version)") -TimeoutSec 20).Stdout.Trim()
    $ollamaModels = @()
    if (-not $NoSystem) { try { $ollamaModels = @((Invoke-RestMethod -Uri "$($lock.ollama.endpoint)/api/tags" -TimeoutSec 5).models | ForEach-Object { $_.name }) } catch { } }
    $freeze = @()
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) { $freeze = @(((Invoke-LisNative -File $venvPy -Arguments @("-m", "pip", "freeze", "--disable-pip-version-check") -TimeoutSec 120).Stdout -split "`r?`n") | Where-Object { $_ }) }
    $install = [ordered]@{
        created_utc   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        hostname      = $env:COMPUTERNAME
        os            = [Environment]::OSVersion.VersionString
        powershell    = "$($PSVersionTable.PSVersion)"
        python        = $pyVer
        pip_freeze    = $freeze
        ollama_models = $ollamaModels
        lis           = [ordered]@{ sha = $lisGit.Sha; branch = $lisGit.Branch; dirty = $lisGit.Dirty }
        upstreams     = $upInfo
        tasks         = @($tasks | ForEach-Object { [ordered]@{ name = $_.TaskName; state = "$($_.State)" } })
    }
    $install | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $work "installation-report.json") -Encoding UTF8
    $lastCheck = Get-ChildItem -LiteralPath (Join-Path $Root "deployment-kit\reports") -Filter "check_lis-*.json" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($lastCheck) { Copy-Item -LiteralPath $lastCheck.FullName -Destination (Join-Path $work "health-check.json") }
    Rec "installation-report" "OK" "versions, SHA, modeles, taches (sans secret)"

    # 9. Manifest + verification immediate.
    $entries = Invoke-LisHelper -Python $Python -Arguments @("manifest-files", "--dir", $work) -TimeoutSec 3600
    if (-not $entries.Data -or -not $entries.Data.ok) { throw "calcul des SHA-256 impossible" }
    $files = @($entries.Data.files)
    $total = 0
    foreach ($f in $files) { $total += [int64]$f.size }
    $manifest = [ordered]@{
        format         = "lis-backup"
        format_version = 1
        created_utc    = $install.created_utc
        hostname       = $env:COMPUTERNAME
        lis            = $install.lis
        upstreams      = $upInfo
        versions       = [ordered]@{ python = $pyVer; powershell = $install.powershell; lock_schema = $lock.schema_version }
        sqlite_backup  = [ordered]@{ method = $bk.Data.method; sqlite_version = $bk.Data.sqlite_version; integrity = $bk.Data.integrity; sha256 = $bk.Data.sha256 }
        secrets        = [ordered]@{ included = [bool]$IncludeSecrets; encrypted = [bool]$IncludeSecrets; format = $(if ($IncludeSecrets) { "lis-secrets v1" } else { "" }) }
        options        = [ordered]@{ exclude_images = [bool]$ExcludeImages }
        not_included   = @(".venv", "modeles Ollama", "models/ (setup_semantic_model.ps1)", "external/ (setup_upstreams.ps1)", "caches", "logs (sauf llm_usage.db)")
        total_size     = $total
        files          = $files
    }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $work "manifest.json") -Encoding UTF8
    $verify = Invoke-LisHelper -Python $Python -Arguments @("manifest-verify", "--dir", $work) -TimeoutSec 3600
    if (-not $verify.Data -or -not $verify.Data.ok) {
        $why = "verification impossible"
        if ($verify.Data -and $verify.Data.PSObject.Properties["errors"]) { $why = (@($verify.Data.errors) | Select-Object -First 5) -join "; " }
        throw "manifest incoherent : $why"
    }
    Rename-Item -LiteralPath $work -NewName $name
    Rec "manifest" "OK" ("{0} fichier(s), {1:N1} Mo, SHA-256 verifies" -f $files.Count, ($total / 1MB))
    $ok = $true
} catch {
    Rec "backup" "FAIL" $_.Exception.Message
} finally {
    if (-not $ok -and (Test-Path -LiteralPath $work)) {
        # Dossier .partial cree par CETTE execution : retire pour ne jamais passer pour un backup valide.
        $null = Invoke-LisHelper -Python $Python -Arguments @("remove-partial", "--dir", $work) -TimeoutSec 600
    }
}

Write-Host ""
if ($ok) {
    Write-Host "BACKUP OK : $final" -ForegroundColor Green
    Write-Host "Copie ce dossier hors du PC (disque externe / cloud chiffre)."
    exit 0
}
Write-Host "BACKUP ECHOUE" -ForegroundColor Red
exit 2
