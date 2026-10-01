# CHECK_LIS.ps1 - Health Check du Life Intelligence System (LECTURE SEULE).
#
# Verifie Git, Windows, Python, Vault, Agency/Security, LLM, Telegram, Notion/ChatGPT,
# Jarvis, Android, taches Windows et capacite de backup.
#
# Garanties :
#   - aucune ecriture dans vault.db (ouverture SQLite mode=ro), aucune donnee de test ;
#   - aucun secret affiche : uniquement CONFIGURED / NOT CONFIGURED ;
#   - aucun appel cloud (llm_setup.py --status n'est PAS lance : il teste Gemini en
#     reel ; utilise -LiveLlmStatus pour le lancer volontairement) ;
#   - seul fichier ecrit : le rapport dans deployment-kit/reports (ignore par Git),
#     desactivable avec -NoReport.
#
# Format : COMPONENT | STATUS | DETAILS
# Statuts : OK, WARNING, FAIL, NOT CONFIGURED, MANUAL TEST REQUIRED
# Codes : 0 = LIS READY, 1 = READY WITH WARNINGS, 2 = NOT READY
#
# Usage :
#   .\CHECK_LIS.ps1
#   .\CHECK_LIS.ps1 -SkipSystem          # sans taches/Ollama/Syncthing (CI, test isole)
#   .\CHECK_LIS.ps1 -Db D:\test\vault.db # autre base (tests)

param(
    [string]$Root = "",
    [string]$Db = "",
    [string]$LockFile = "",
    [string]$Python = "",
    [string]$ReportDir = "",
    [string]$JsonOut = "",
    [string]$BackupDir = "",
    [switch]$SkipSystem,
    [switch]$SkipSemantic,
    [switch]$LiveLlmStatus,
    [switch]$NoReport
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = $ScriptDir }
$Root = (Resolve-Path -LiteralPath $Root).Path
Import-Module (Join-Path $ScriptDir "deployment-kit\lib\LisDeploy.psm1") -Force -DisableNameChecking
if (-not $LockFile) { $LockFile = Join-Path $Root "deployment-kit\lis.lock.json" }
if (-not $ReportDir) { $ReportDir = Join-Path $Root "deployment-kit\reports" }
if (-not $Db) { $Db = Join-Path $Root "vault.db" }
if (-not $BackupDir) { $BackupDir = Join-Path $env:USERPROFILE "LIS-BACKUPS" }

$report = New-LisReport -Name "check_lis"
function Rec { param($c, $s, $d = "") Add-LisResult -Report $report -Component $c -Status $s -Details $d | Out-Null }
function Section { param($t) Write-Host ""; Write-Host "--- $t" -ForegroundColor White }

Write-LisHeader "LIS HEALTH CHECK (lecture seule) - $Root"

# ---------------------------------------------------------------------------
# Configuration : presence des cles, jamais leur valeur
# ---------------------------------------------------------------------------
$EnvFile = Join-Path $Root ".env"
$EnvPresent = @{}
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line.Split("=", 2)
        $EnvPresent[$k.Trim()] = [bool]($v.Trim())
    }
}
function Test-Configured {
    param([string]$Name)
    if ([Environment]::GetEnvironmentVariable($Name, "Process")) { return $true }
    if ([Environment]::GetEnvironmentVariable($Name, "User")) { return $true }
    if ($EnvPresent.ContainsKey($Name) -and $EnvPresent[$Name]) { return $true }
    return $false
}
function Get-NonSecretSetting {
    # Lecture d'un reglage NON secret (.env puis environnement).
    param([string]$Name)
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
            if ($line -match ("^\s*" + [regex]::Escape($Name) + "\s*=(.*)$")) { return $Matches[1].Trim() }
        }
    }
    return [Environment]::GetEnvironmentVariable($Name)
}

$lock = $null
try { $lock = Read-LisLock -Path $LockFile } catch { $lock = $null; $lockError = $_.Exception.Message }

# ---------------------------------------------------------------------------
Section "GIT"
# ---------------------------------------------------------------------------
if (-not (Test-LisCommand "git")) {
    Rec "git" "FAIL" "git introuvable"
} else {
    $git = Get-LisGitState -Path $Root
    if (-not $git.IsRepo) {
        Rec "git repo" "WARNING" "pas un clone Git ($Root)"
    } else {
        Rec "git repo" "OK" "branche $($git.Branch) @ $($git.Sha.Substring(0, 10))"
        if ($git.Dirty) { Rec "git working tree" "WARNING" "$($git.Modified.Count) fichier(s) suivi(s) modifie(s)" }
        else { Rec "git working tree" "OK" "propre" }
        if ($lock) {
            $validated = $lock.lis.validated_sha
            if ($git.Sha -eq $validated) {
                Rec "git vs lock" "OK" "HEAD = SHA valide du lock"
            } else {
                $anc = Invoke-LisNative -File "git" -Arguments @("-C", $Root, "merge-base", "--is-ancestor", $validated, "HEAD") -TimeoutSec 30
                if ($anc.ExitCode -eq 0) { Rec "git vs lock" "OK" "HEAD contient le SHA valide $($validated.Substring(0, 10))" }
                else { Rec "git vs lock" "WARNING" "HEAD ne contient pas le SHA valide $($validated.Substring(0, 10))" }
            }
        }
        $py0 = $Python
        if (-not $py0) { $py0 = Find-LisPython -Root $Root }
        if ($py0) {
            $scan = Invoke-LisHelper -Python $py0 -Arguments @("secret-scan", "--root", $Root) -TimeoutSec 120
            if ($scan.Data -and $scan.Data.ok) { Rec "git secrets" "OK" "aucun secret ni vault.db suivi ($($scan.Data.scanned) fichiers)" }
            elseif ($scan.Data -and $scan.Data.findings) {
                $names = (@($scan.Data.findings) | ForEach-Object { "$($_.file) [$($_.kind)]" }) -join ", "
                Rec "git secrets" "FAIL" "fichiers suivis suspects : $names"
            } else { Rec "git secrets" "WARNING" "scan impossible" }
        }
    }
}
if ($lock) { Rec "lis.lock.json" "OK" "schema v$($lock.schema_version), $(@($lock.upstreams).Count) upstreams pinnes" }
else { Rec "lis.lock.json" "FAIL" "$lockError" }

# ---------------------------------------------------------------------------
Section "WINDOWS"
# ---------------------------------------------------------------------------
Rec "powershell" "OK" "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq "AMD64") { Rec "architecture" "OK" $arch } else { Rec "architecture" "WARNING" "$arch (reference : AMD64)" }
try {
    $drive = (Get-Item -LiteralPath $Root).PSDrive
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt 2) { Rec "disk space" "FAIL" "$freeGb Go libres sur $($drive.Name):" }
    elseif ($freeGb -lt 8) { Rec "disk space" "WARNING" "$freeGb Go libres sur $($drive.Name):" }
    else { Rec "disk space" "OK" "$freeGb Go libres sur $($drive.Name):" }
} catch { Rec "disk space" "WARNING" "espace disque non lisible" }
$sched = Get-Service -Name "Schedule" -ErrorAction SilentlyContinue
if ($sched -and $sched.Status -eq "Running" -and (Test-LisCommand "Get-ScheduledTask")) { Rec "task scheduler" "OK" "service Schedule actif" }
else { Rec "task scheduler" "WARNING" "Planificateur de taches indisponible" }

# ---------------------------------------------------------------------------
Section "PYTHON"
# ---------------------------------------------------------------------------
$sysPy = Find-LisPython -Root $Root -SystemOnly
if ($sysPy) {
    $v = (Invoke-LisNative -File $sysPy -Arguments @("--version") -TimeoutSec 20).Stdout.Trim()
    Rec "python (systeme)" "OK" "$v"
} else { Rec "python (systeme)" "WARNING" "aucun Python >= 3.12 hors .venv" }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if ($Python) { $VenvPy = $Python }
$pyOk = $false
if (Test-Path -LiteralPath $VenvPy) {
    $ver = Invoke-LisNative -File $VenvPy -Arguments @("-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])") -TimeoutSec 20
    if ($ver.ExitCode -eq 0) {
        $pyOk = $true
        $actual = $ver.Stdout.Trim()
        $ref = ""
        if ($lock) { $ref = $lock.python.reference }
        $refMinor = ($ref -split '\.')[0..1] -join '.'
        $actMinor = ($actual -split '\.')[0..1] -join '.'
        if (-not $ref -or $refMinor -eq $actMinor) { Rec ".venv python" "OK" "Python $actual" }
        else { Rec ".venv python" "WARNING" "Python $actual (reference lock : $ref)" }
    } else { Rec ".venv python" "FAIL" ".venv cassee (python.exe ne demarre pas)" }
} else { Rec ".venv python" "FAIL" ".venv absente : .\setup_lis.ps1 -Install" }

if ($pyOk) {
    $critical = @("sqlite3", "fastapi", "uvicorn", "numpy", "onnxruntime", "tokenizers", "yt_dlp", "faster_whisper")
    $optional = @("gallery_dl", "anthropic", "openpyxl", "requests")
    if ($lock) { $critical = @($lock.python.critical_imports); $optional = @($lock.python.optional_imports) }
    $code = "import importlib,json,sys`nr={}`nfor m in sys.argv[1:]:`n    try:`n        importlib.import_module(m); r[m]=True`n    except Exception:`n        r[m]=False`nprint(json.dumps(r))"
    $imp = Invoke-LisNative -File $VenvPy -Arguments (@("-c", $code) + $critical + $optional) -TimeoutSec 120 -WorkingDirectory $Root
    try { $got = $imp.Stdout | ConvertFrom-Json } catch { $got = $null }
    if (-not $got) { Rec "imports critiques" "FAIL" "verification impossible" }
    else {
        $missC = @($critical | Where-Object { -not $got.$_ })
        $missO = @($optional | Where-Object { -not $got.$_ })
        if ($missC.Count) { Rec "imports critiques" "FAIL" ("manquants : " + ($missC -join ", ")) }
        else { Rec "imports critiques" "OK" ($critical -join ", ") }
        if ($missO.Count) { Rec "imports optionnels" "WARNING" ("manquants : " + ($missO -join ", ")) }
        else { Rec "imports optionnels" "OK" ($optional -join ", ") }
    }
    $reqLock = Join-Path $Root "deployment-kit\requirements-lock.txt"
    if (Test-Path -LiteralPath $reqLock) {
        $freeze = Invoke-LisNative -File $VenvPy -Arguments @("-m", "pip", "freeze", "--disable-pip-version-check") -TimeoutSec 120
        $installed = @{}
        foreach ($l in ($freeze.Stdout -split "`r?`n")) { if ($l -match '^([^=\s]+)==(.+)$') { $installed[($Matches[1].ToLower() -replace '[-_.]+', '-')] = $Matches[2].Trim() } }
        $diff = @()
        foreach ($l in (Get-Content -LiteralPath $reqLock)) {
            if ($l -match '^([^=#\s]+)==(.+)$') {
                $n = ($Matches[1].ToLower() -replace '[-_.]+', '-')
                if (-not $installed.ContainsKey($n)) { $diff += "$n absent" }
                elseif ($installed[$n] -ne $Matches[2].Trim()) { $diff += "$n $($installed[$n])" }
            }
        }
        if ($diff.Count -eq 0) { Rec "requirements (lock)" "OK" "versions identiques a requirements-lock.txt" }
        else { Rec "requirements (lock)" "WARNING" ("$($diff.Count) ecart(s) : " + (($diff | Select-Object -First 6) -join ", ")) }
    }
}

# ---------------------------------------------------------------------------
Section "VAULT"
# ---------------------------------------------------------------------------
$checkPy = $VenvPy
if (-not $pyOk) { $checkPy = Find-LisPython -Root $Root -SystemOnly }
if (-not (Test-Path -LiteralPath $Db)) {
    Rec "vault.db" "FAIL" "absent : $Db"
} elseif (-not $checkPy) {
    Rec "vault.db" "FAIL" "aucun Python pour verifier SQLite"
} else {
    $before = Get-Item -LiteralPath $Db
    $stampBefore = "$($before.Length)|$($before.LastWriteTimeUtc.Ticks)"
    Rec "vault.db" "OK" ("present, {0:N1} Mo" -f ($before.Length / 1MB))
    $required = @("contents", "actions", "notes", "meta")
    $recommended = @()
    if ($lock) { $required = @($lock.vault.required_tables); $recommended = @($lock.vault.recommended_tables) }
    $int = Invoke-LisHelper -Python $checkPy -Arguments @("integrity", "--db", $Db, "--tables", ($required -join ",")) -TimeoutSec 300
    if (-not $int.Data) { Rec "sqlite integrity_check" "FAIL" "verification impossible" }
    elseif ($int.Data.integrity -ne "ok") { Rec "sqlite integrity_check" "FAIL" "$($int.Data.integrity)" }
    else {
        Rec "sqlite integrity_check" "OK" "ok ($($int.Data.table_count) tables)"
        $miss = @($int.Data.missing_tables)
        if ($miss.Count) { Rec "schema minimal" "FAIL" ("tables manquantes : " + ($miss -join ", ")) }
        else { Rec "schema minimal" "OK" ($required -join ", ") }
        if ($recommended.Count) {
            $rec = Invoke-LisHelper -Python $checkPy -Arguments @("integrity", "--db", $Db, "--tables", ($recommended -join ",")) -TimeoutSec 300
            $missR = @()
            if ($rec.Data) { $missR = @($rec.Data.missing_tables) }
            if ($missR.Count) { Rec "schema LIS" "WARNING" ("tables pas encore creees : " + ($missR -join ", ")) }
            else { Rec "schema LIS" "OK" ($recommended -join ", ") }
        }
        if ($pyOk) {
            $healthArgs = @("vault-health", "--root", $Root, "--db", $Db)
            $modelFile = Join-Path $Root "models\multilingual-e5-small\onnx\model_quantized.onnx"
            $semantic = (-not $SkipSemantic) -and (Test-Path -LiteralPath $modelFile)
            if ($semantic) { $healthArgs += "--semantic" }
            $h = Invoke-LisHelper -Python $VenvPy -Arguments $healthArgs -TimeoutSec 300
            if (-not $h.Data) { Rec "retrieval lexical" "FAIL" "test impossible" }
            else {
                if ($h.Data.PSObject.Properties["contents"]) { Rec "vault contenu" "OK" "$($h.Data.contents) contenus, $($h.Data.sources) sources" }
                if ($h.Data.lexical.ok) { Rec "retrieval lexical" "OK" "requete de test : $($h.Data.lexical.sources) source(s), $($h.Data.lexical.ms) ms" }
                else { Rec "retrieval lexical" "FAIL" "$($h.Data.lexical.error)" }
                if ($semantic) {
                    if ($h.Data.semantic.ok -and $h.Data.semantic.used) { Rec "retrieval semantique" "OK" "index e5 utilise, $($h.Data.semantic.ms) ms" }
                    elseif ($h.Data.semantic.ok) { Rec "retrieval semantique" "WARNING" "modele present mais index non utilise (python vault_embeddings.py)" }
                    else { Rec "retrieval semantique" "WARNING" "$($h.Data.semantic.error)" }
                }
            }
            if (-not $semantic) {
                if ($SkipSemantic) { Rec "retrieval semantique" "NOT CONFIGURED" "ignore (-SkipSemantic)" }
                else { Rec "retrieval semantique" "NOT CONFIGURED" "modele e5 absent : .\setup_semantic_model.ps1" }
            }
        }
    }
    $after = Get-Item -LiteralPath $Db
    $stampAfter = "$($after.Length)|$($after.LastWriteTimeUtc.Ticks)"
    if ($stampBefore -eq $stampAfter) { Rec "vault.db lecture seule" "OK" "aucune ecriture pendant le check (mode=ro)" }
    else { Rec "vault.db lecture seule" "WARNING" "vault.db modifie pendant le check par un autre processus (passerelle/tache) ; le check est en mode=ro" }
}

# ---------------------------------------------------------------------------
Section "AGENCY / SECURITY"
# ---------------------------------------------------------------------------
$agencyDir = Join-Path $Root "external\agency-agents"
if (-not $pyOk) {
    Rec "agency registry" "FAIL" ".venv requise"
} else {
    if (-not (Test-Path -LiteralPath $agencyDir)) { Rec "agency agents" "FAIL" "external/agency-agents absent : .\setup_upstreams.ps1" }
    elseif (-not (Test-Path -LiteralPath (Join-Path $Root "vault_agent_registry.py"))) { Rec "agency registry" "FAIL" "vault_agent_registry.py absent" }
    else {
        $reg = Invoke-LisNative -File $VenvPy -Arguments @((Join-Path $Root "vault_agent_registry.py"), "--list") -TimeoutSec 120 -WorkingDirectory $Root
        $first = ($reg.Stdout -split "`r?`n" | Select-Object -First 1)
        if ($reg.ExitCode -eq 0 -and $first -match '^(\d+) agents') {
            if ([int]$Matches[1] -gt 0) { Rec "agency registry" "OK" "$($Matches[1]) profils Agency disponibles" }
            else { Rec "agency registry" "FAIL" "aucun profil Agency" }
        } else { Rec "agency registry" "FAIL" "registre illisible" }
    }
    $gateScript = Join-Path $Root "vault_security_gate.py"
    if (Test-Path -LiteralPath $gateScript) {
        $gate = Invoke-LisNative -File $VenvPy -Arguments @($gateScript, (Join-Path $Root "integrations\jarvis")) -TimeoutSec 120 -WorkingDirectory $Root
        $ok = $false
        try { $null = $gate.Stdout | ConvertFrom-Json; $ok = ($gate.ExitCode -eq 0) } catch { $ok = $false }
        if ($ok) { Rec "security gate" "OK" "analyse lecture seule fonctionnelle" } else { Rec "security gate" "FAIL" "vault_security_gate.py en erreur" }
    } else { Rec "security gate" "FAIL" "vault_security_gate.py absent" }
    $impScript = Join-Path $Root "lis_improvement.py"
    if ((Test-Path -LiteralPath $impScript) -and (Test-Path -LiteralPath $Db)) {
        $imp = Invoke-LisNative -File $VenvPy -Arguments @($impScript, "--scan", "--limit", "3", "--db", $Db) -TimeoutSec 180 -WorkingDirectory $Root
        $items = $null
        try { $items = $imp.Stdout | ConvertFrom-Json } catch { $items = $null }
        if ($imp.ExitCode -eq 0 -and $null -ne $items) { Rec "lis_improvement --scan" "OK" "lecture seule, $(@($items).Count) candidat(s) (limite 3)" }
        else { Rec "lis_improvement --scan" "WARNING" "scan en erreur" }
    }
}

# ---------------------------------------------------------------------------
Section "LLM"
# ---------------------------------------------------------------------------
if (-not $SkipSystem) {
    $ollamaExe = $null
    $cmd = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($cmd) { $ollamaExe = $cmd.Source }
    elseif (Test-Path (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")) { $ollamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    if ($ollamaExe) { Rec "ollama" "OK" "installe" } else { Rec "ollama" "FAIL" "absent : winget install Ollama.Ollama" }
    $endpoint = "http://127.0.0.1:11434"
    if ($lock) { $endpoint = $lock.ollama.endpoint }
    $models = $null
    try { $models = (Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 5).models } catch { $models = $null }
    if ($null -eq $models) { Rec "ollama service" "WARNING" "injoignable sur $endpoint (Ollama arrete ?)" }
    else {
        Rec "ollama service" "OK" "joignable ($(@($models).Count) modele(s))"
        $want = "qwen3:1.7b"
        if ($lock) { $want = $lock.ollama.required_model }
        $configured = Get-NonSecretSetting "OLLAMA_MODEL"
        if ($configured) { $want = $configured }
        if (@($models | Where-Object { $_.name -eq $want }).Count) { Rec "qwen local" "OK" "$want present" }
        else { Rec "qwen local" "FAIL" "$want absent : ollama pull $want" }
    }
}
foreach ($p in @(@("OpenRouter", "OPENROUTER_API_KEY"), @("Gemini", "GEMINI_API_KEY"), @("Anthropic", "ANTHROPIC_API_KEY"))) {
    if (Test-Configured $p[1]) { Rec "provider $($p[0])" "OK" "CONFIGURED (cle non affichee, non testee)" }
    else { Rec "provider $($p[0])" "NOT CONFIGURED" "optionnel ; python llm_setup.py" }
}
$chains = (@("LLM_CHAIN_LIGHT", "LLM_CHAIN_STANDARD", "LLM_CHAIN_COMPLEX") | ForEach-Object { Get-NonSecretSetting $_ }) -join ","
if ($chains -match "mistral" -or (Test-Configured "MISTRAL_API_KEY")) { Rec "mistral" "WARNING" "Mistral ne doit pas etre un runtime par defaut" }
else { Rec "mistral" "OK" "hors runtime par defaut" }
if ($pyOk -and (Test-Path -LiteralPath (Join-Path $Root "llm_router.py"))) {
    $routeCode = @"
import json, os, sys, tempfile
sys.path.insert(0, os.getcwd())
try:
    import telegram_api; telegram_api.load_env()
except Exception:
    pass
import llm_router as lr
r = lr.LLMRouter(usage_db=os.path.join(tempfile.mkdtemp(), 'usage.db'))
bad = []
for task in ('light', 'standard', 'complex'):
    for s in ('sensitive', 'personal', 'secret'):
        bad += ['%s/%s:%s' % (task, s, n) for n in r.chain(task, s) if r.providers[n].cloud]
print(json.dumps({'cloud_in_sensitive': bad}))
"@
    $rt = Invoke-LisNative -File $VenvPy -Arguments @("-c", $routeCode) -TimeoutSec 60 -WorkingDirectory $Root
    $rj = $null
    try { $rj = $rt.Stdout | ConvertFrom-Json } catch { $rj = $null }
    if (-not $rj) { Rec "routage sensible" "WARNING" "verification impossible" }
    elseif (@($rj.cloud_in_sensitive).Count -eq 0) { Rec "routage sensible" "OK" "sensible / personnel / secret : local uniquement" }
    else { Rec "routage sensible" "FAIL" ("cloud autorise : " + (@($rj.cloud_in_sensitive) -join ", ")) }
}
if ($LiveLlmStatus -and $pyOk) {
    $st = Invoke-LisNative -File $VenvPy -Arguments @((Join-Path $Root "llm_setup.py"), "--status") -TimeoutSec 120 -WorkingDirectory $Root
    foreach ($l in ($st.Stdout -split "`r?`n")) { if ($l -match '^(\w+)\s+:\s+(.+)$') { Rec "llm_setup $($Matches[1])" "OK" $Matches[2] } }
} else {
    Rec "llm_setup.py --status" "MANUAL TEST REQUIRED" "non lance (appel Gemini reel si cle) ; -LiveLlmStatus pour l'executer"
}

# ---------------------------------------------------------------------------
Section "TELEGRAM"
# ---------------------------------------------------------------------------
$tgFiles = @("telegram_gateway.py", "telegram_api.py", "telegram_setup.py") | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Root $_)) }
if ($tgFiles) { Rec "telegram fichiers" "FAIL" ("absents : " + ($tgFiles -join ", ")) } else { Rec "telegram fichiers" "OK" "gateway, api, setup" }
$tok = Test-Configured "TELEGRAM_BOT_TOKEN"
$wl = (Test-Configured "TELEGRAM_ALLOWED_USER_ID") -and (Test-Configured "TELEGRAM_ALLOWED_CHAT_ID")
if ($tok -and $wl) { Rec "telegram config" "OK" "token CONFIGURED ; whitelist user + chat CONFIGURED" }
elseif ($tok) { Rec "telegram config" "FAIL" "token present mais whitelist user/chat incomplete" }
else { Rec "telegram config" "NOT CONFIGURED" "python telegram_setup.py" }
$gw = Join-Path $Root "telegram_gateway.py"
if (Test-Path -LiteralPath $gw) {
    $shell = Select-String -LiteralPath $gw -Pattern 'subprocess|os\.system|shell\s*=\s*True|Popen' -Quiet
    if ($shell) { Rec "telegram shell" "WARNING" "appel de processus detecte dans telegram_gateway.py : a revoir" }
    else { Rec "telegram shell" "OK" "aucune commande shell exposee" }
}

# ---------------------------------------------------------------------------
Section "NOTION / CHATGPT"
# ---------------------------------------------------------------------------
$notionFiles = @("vault_notion_cockpit.py", "vault_notion_library.py", "mentor_notion_sync.py") | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Root $_)) }
if ($notionFiles) { Rec "notion scripts" "WARNING" ("absents : " + ($notionFiles -join ", ")) } else { Rec "notion scripts" "OK" "cockpit, library, mentor sync" }
if (Test-Configured "NOTION_TOKEN") { Rec "notion config" "OK" "NOTION_TOKEN CONFIGURED ; Notion = projection, jamais memoire canonique" }
else { Rec "notion config" "NOT CONFIGURED" "NOTION_TOKEN (variable utilisateur Windows) ; optionnel" }
Rec "chatgpt connecteurs" "MANUAL TEST REQUIRED" "interface d'analyse ; verifier dans ChatGPT (pas une dependance runtime)"

# ---------------------------------------------------------------------------
Section "JARVIS"
# ---------------------------------------------------------------------------
$jarvisLock = $null
if ($lock) { $jarvisLock = @($lock.upstreams) | Where-Object { $_.name -eq "jarvis-assistant-vocal" } | Select-Object -First 1 }
$jarvisDir = Join-Path $Root "external\jarvis-assistant-vocal"
if (-not (Test-Path -LiteralPath $jarvisDir)) {
    Rec "jarvis upstream" "FAIL" "absent : .\setup_upstreams.ps1"
} else {
    $js = Get-LisGitState -Path $jarvisDir
    if ($jarvisLock -and $js.Sha -eq $jarvisLock.sha) { Rec "jarvis upstream" "OK" "SHA $($js.Sha.Substring(0, 10)) conforme au lock" }
    elseif ($js.IsRepo) { Rec "jarvis upstream" "WARNING" "SHA $($js.Sha.Substring(0, 10)) different du lock" }
    else { Rec "jarvis upstream" "WARNING" "pas un clone Git" }
    $jv = Join-Path $jarvisDir ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $jv) { Rec "jarvis venv" "OK" "presente" } else { Rec "jarvis venv" "WARNING" "absente : .\setup_lis.ps1 -Install" }
    if (Test-Path -LiteralPath (Join-Path $jarvisDir "config.yaml")) { Rec "jarvis config" "OK" "config.yaml present (non lu)" }
    else { Rec "jarvis config" "NOT CONFIGURED" "config.yaml absent (restore -RestoreSecrets ou config.example.yaml)" }
}
$jfiles = @("integrations\jarvis\life_intelligence.py", "install_jarvis_voice_bridge.py", "vault_voice_bridge.py") | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Root $_)) }
if ($jfiles) { Rec "jarvis bridge fichiers" "FAIL" ("absents : " + ($jfiles -join ", ")) } else { Rec "jarvis bridge fichiers" "OK" "integrations/jarvis, installateur, vault_voice_bridge.py" }
if ($pyOk -and (Test-Path -LiteralPath $jarvisDir) -and (Test-Path -LiteralPath (Join-Path $Root "install_jarvis_voice_bridge.py"))) {
    $bc = Invoke-LisNative -File $VenvPy -Arguments @((Join-Path $Root "install_jarvis_voice_bridge.py"), "--check", "--jarvis", $jarvisDir) -TimeoutSec 60
    if ($bc.ExitCode -eq 0) { Rec "jarvis voice bridge" "OK" "installe (modules, routage, brief)" }
    else { Rec "jarvis voice bridge" "WARNING" "incomplet : .\setup_upstreams.ps1" }
}
Rec "jarvis vocal reel" "MANUAL TEST REQUIRED" "dire une phrase 'vault ...' a Jarvis et verifier la reponse du LIS"

# ---------------------------------------------------------------------------
Section "ANDROID"
# ---------------------------------------------------------------------------
$mobile = Join-Path $Root "mobile-inbox"
if (Test-Path -LiteralPath $mobile) {
    if (Test-Path -LiteralPath (Join-Path $mobile "inbox.txt")) { Rec "mobile-inbox" "OK" "mobile-inbox\inbox.txt present" }
    else { Rec "mobile-inbox" "WARNING" "inbox.txt absent (cree au premier envoi Syncthing)" }
} else { Rec "mobile-inbox" "FAIL" "dossier mobile-inbox absent" }
if (Test-Path -LiteralPath (Join-Path $Root "inbox.bat")) { Rec "inbox.bat" "OK" "present" } else { Rec "inbox.bat" "FAIL" "absent" }
if (-not $SkipSystem) {
    $stConfig = Join-Path $env:LOCALAPPDATA "Syncthing\config.xml"
    $stRunning = [bool](Get-Process -Name "syncthing" -ErrorAction SilentlyContinue)
    if (Test-Path -LiteralPath $stConfig) {
        $xml = [xml](Get-Content -LiteralPath $stConfig -Raw -Encoding UTF8)
        $folder = @($xml.configuration.folder) | Where-Object { $_.path -and ((Resolve-Path -LiteralPath $_.path -ErrorAction SilentlyContinue).Path -eq (Resolve-Path -LiteralPath $mobile -ErrorAction SilentlyContinue).Path) } | Select-Object -First 1
        if ($folder) {
            $state = "arrete"
            if ($stRunning) { $state = "en cours" }
            Rec "syncthing" "OK" "dossier '$($folder.label)' ($($folder.type)) -> mobile-inbox ; Syncthing $state"
        } else { Rec "syncthing" "WARNING" "aucun dossier Syncthing ne pointe vers mobile-inbox" }
    } elseif ($stRunning) { Rec "syncthing" "WARNING" "en cours mais config.xml introuvable" }
    else { Rec "syncthing" "NOT CONFIGURED" "winget install Syncthing.Syncthing puis appairage (deployment-kit/android)" }
}
$macro = @(Get-ChildItem -LiteralPath (Join-Path $Root "deployment-kit\android\macrodroid") -Filter "*.macro" -ErrorAction SilentlyContinue)
if ($macro.Count) { Rec "macrodroid export" "OK" "$($macro.Count) export(s) .macro" }
else { Rec "macrodroid export" "MANUAL TEST REQUIRED" "EXPORT REQUIRED FROM ANDROID DEVICE (deployment-kit/android/macrodroid)" }
Rec "android bout en bout" "MANUAL TEST REQUIRED" "Partager un TikTok -> MacroDroid -> verifier mobile-inbox\inbox.txt puis vault.db"

# ---------------------------------------------------------------------------
Section "WINDOWS TASKS"
# ---------------------------------------------------------------------------
if (-not $SkipSystem) {
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "Reels Vault*" })
    $venvPyw = (Join-Path $Root ".venv\Scripts\pythonw.exe")
    function Get-TaskCommand { param($t) return ((@($t.Actions) | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " | ") }
    $ingest = @($tasks | Where-Object { (Get-TaskCommand $_) -match [regex]::Escape((Join-Path $Root "inbox.bat")) })
    if ($ingest.Count -eq 0) { Rec "task ingestion" "FAIL" "aucune tache ne lance inbox.bat : .\setup_windows_tasks.ps1" }
    else {
        $names = ($ingest | ForEach-Object { "$($_.TaskName) [$($_.State)]" }) -join ", "
        $sw = @($ingest | Where-Object { $_.Settings.StartWhenAvailable }).Count
        if ($ingest.Count -gt 1) { Rec "task ingestion" "WARNING" "$($ingest.Count) taches lancent inbox.bat (doublon horaire) : $names" }
        elseif ($sw -eq 0) { Rec "task ingestion" "WARNING" "$names ; StartWhenAvailable desactive" }
        else { Rec "task ingestion" "OK" $names }
    }
    $tgName = "Reels Vault - Telegram Gateway"
    if ($lock) { $tgName = $lock.windows_tasks.telegram_gateway }
    $tg = $tasks | Where-Object { $_.TaskName -eq $tgName } | Select-Object -First 1
    if (-not $tg) { Rec "task telegram gateway" "WARNING" "absente : .\setup_windows_tasks.ps1 (apres telegram_setup.py)" }
    else {
        $issues = @()
        if ((Get-TaskCommand $tg) -notmatch [regex]::Escape($venvPyw)) { $issues += "n'utilise pas la .venv du repo" }
        if (-not @($tg.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskLogonTrigger" }).Count) { $issues += "pas de declencheur a l'ouverture de session" }
        if (-not $tg.Settings.StartWhenAvailable) { $issues += "StartWhenAvailable desactive" }
        if ([int]$tg.Settings.RestartCount -lt 1) { $issues += "pas de redemarrage automatique" }
        if ($issues.Count) { Rec "task telegram gateway" "WARNING" ("[$($tg.State)] " + ($issues -join " ; ")) }
        else { Rec "task telegram gateway" "OK" "[$($tg.State)] logon, restart x$($tg.Settings.RestartCount), StartWhenAvailable, .venv" }
    }
    $mdName = "Reels Vault - Mentor Daily"
    if ($lock) { $mdName = $lock.windows_tasks.mentor_daily }
    $md = $tasks | Where-Object { $_.TaskName -eq $mdName } | Select-Object -First 1
    if (-not $md) { Rec "task mentor daily" "WARNING" "absente : .\setup_windows_tasks.ps1" }
    else {
        $issues = @()
        if ((Get-TaskCommand $md) -notmatch [regex]::Escape((Join-Path $Root "mentor_daily.bat"))) { $issues += "ne lance pas mentor_daily.bat du repo" }
        if (-not @($md.Triggers | Where-Object { $_.CimClass.CimClassName -eq "MSFT_TaskDailyTrigger" }).Count) { $issues += "pas de declencheur quotidien" }
        if (-not $md.Settings.StartWhenAvailable) { $issues += "StartWhenAvailable desactive" }
        if ($issues.Count) { Rec "task mentor daily" "WARNING" ("[$($md.State)] " + ($issues -join " ; ")) }
        else { Rec "task mentor daily" "OK" "[$($md.State)] quotidien + logon, StartWhenAvailable" }
    }
}
foreach ($bat in @("inbox.bat", "mentor_daily.bat")) {
    $p = Join-Path $Root $bat
    if ((Test-Path -LiteralPath $p) -and -not (Select-String -LiteralPath $p -Pattern '\.venv\\Scripts\\python\.exe' -Quiet)) { Rec "$bat" "WARNING" "n'utilise pas la .venv du repo" }
}

# ---------------------------------------------------------------------------
Section "BACKUP"
# ---------------------------------------------------------------------------
$bk = @("backup_lis.ps1", "restore_lis.ps1", "deployment-kit\lib\lis_deploy.py") | Where-Object { -not (Test-Path -LiteralPath (Join-Path $Root $_)) }
if ($bk) { Rec "backup scripts" "FAIL" ("absents : " + ($bk -join ", ")) }
elseif (-not $checkPy) { Rec "backup scripts" "FAIL" "aucun Python pour l'API de sauvegarde SQLite" }
else { Rec "backup scripts" "OK" "backup_lis.ps1 / restore_lis.ps1, API backup SQLite disponible" }
$last = $null
if (Test-Path -LiteralPath $BackupDir) { $last = Get-ChildItem -LiteralPath $BackupDir -Directory -Filter "LIS-BACKUP-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1 }
if ($last) {
    $age = [int]((Get-Date) - $last.CreationTime).TotalDays
    if ($age -gt 14) { Rec "dernier backup" "WARNING" "$($last.Name) ($age jours)" } else { Rec "dernier backup" "OK" "$($last.Name) ($age jour(s))" }
} else { Rec "dernier backup" "WARNING" "aucun backup dans $BackupDir : .\backup_lis.ps1" }

# ---------------------------------------------------------------------------
$worst = Get-LisWorstRank $report
$verdict = "LIS READY"
if ($worst -eq 1) { $verdict = "READY WITH WARNINGS" }
if ($worst -ge 2) { $verdict = "NOT READY" }
$counts = @{}
foreach ($row in $report.Results) { $counts[$row.Status] = 1 + [int]$counts[$row.Status] }
Write-Host ""
Write-Host ("=" * 78)
Write-Host ("RESULTAT : {0}   ({1})" -f $verdict, (($counts.Keys | Sort-Object | ForEach-Object { "$_=$($counts[$_])" }) -join ", "))
Write-Host ("=" * 78)
$extra = @{ verdict = $verdict; exit_code = $worst; root = $Root }
if ($JsonOut) {
    $payload = [ordered]@{ verdict = $verdict; exit_code = $worst; results = @($report.Results) }
    $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $JsonOut -Encoding UTF8
}
if (-not $NoReport) {
    try { $saved = Save-LisReport -Report $report -Directory $ReportDir -Extra $extra; Write-Host "Rapport : $saved" } catch { }
}
exit $worst
