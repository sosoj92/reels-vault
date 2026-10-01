# setup_lis.ps1 - Orchestrateur d'installation Windows du Life Intelligence System.
#
# AUDIT -> PLAN -> APPLY (seulement ce qui manque) -> HEALTH CHECK.
# Pour chaque composant :
#   present et correct            -> SKIP
#   absent                        -> INSTALL
#   present, version differente   -> WARNING (compatible, rien n'est remplace)
#   present mais situation risquee-> BLOCK (rien n'est touche, action manuelle indiquee)
#   etape humaine (cle, telephone)-> MANUAL TEST REQUIRED / NOT CONFIGURED
#
# Garde-fous : .env jamais ecrase ; vault.db jamais remplace ; aucune cle demandee ni
# journalisee ici (llm_setup.py / telegram_setup.py sont a lancer par toi, saisie masquee) ;
# Notion reste une projection ; Qwen/Ollama reste le chemin local/sensible ; Jarvis fait
# partie de l'installation complete.
#
# Usage :
#   .\setup_lis.ps1 -Audit      # etat de chaque composant, aucune modification
#   .\setup_lis.ps1 -DryRun     # etat + plan detaille, aucune modification
#   .\setup_lis.ps1 -Install    # applique ce qui manque puis lance CHECK_LIS.ps1
#   .\setup_lis.ps1 -Install -Only python,venv,requirements
# Codes : 0 pret, 1 avertissements / actions restantes, 2 bloque ou echec.

param(
    [switch]$Audit,
    [switch]$DryRun,
    [switch]$Install,
    [string[]]$Only = @(),
    [switch]$SkipHealthCheck,
    [string]$Root = "",
    [string]$ReportDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = $ScriptDir }
$Root = (Resolve-Path -LiteralPath $Root).Path
Import-Module (Join-Path $ScriptDir "deployment-kit\lib\LisDeploy.psm1") -Force -DisableNameChecking
if (-not $ReportDir) { $ReportDir = Join-Path $Root "deployment-kit\reports" }
if (-not ($Audit -or $DryRun -or $Install)) {
    Write-Host "Choisis un mode : -Audit, -DryRun ou -Install" -ForegroundColor Yellow
    exit 2
}
$Apply = [bool]$Install -and -not ($Audit -or $DryRun)
$Mode = "INSTALL"
if ($Audit) { $Mode = "AUDIT" } elseif ($DryRun) { $Mode = "DRY-RUN" }
$Only = @($Only | ForEach-Object { $_ -split "," } | Where-Object { $_ } | ForEach-Object { $_.Trim().ToLowerInvariant() })

$LockFile = Join-Path $Root "deployment-kit\lis.lock.json"
try { $lock = Read-LisLock -Path $LockFile } catch { Write-Host "lis.lock.json : $($_.Exception.Message)" -ForegroundColor Red; exit 2 }

$report = New-LisReport -Name "setup_lis"
function Rec { param($c, $s, $d = "") Add-LisResult -Report $report -Component $c -Status $s -Details $d | Out-Null }
Write-LisHeader "LIS SETUP ($Mode) - $Root"

# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}
function Install-Winget {
    param([string]$Id)
    if (-not (Test-LisCommand "winget")) { throw "winget absent : installe 'App Installer' (Microsoft Store) puis relance" }
    $res = Invoke-LisNative -File "winget" -Arguments @("install", "--id", $Id, "-e", "--silent", "--accept-source-agreements", "--accept-package-agreements") -TimeoutSec 1800
    Update-SessionPath
    if ($res.ExitCode -ne 0 -and $res.Stdout -notmatch "already installed|deja install") { throw "winget install $Id : code $($res.ExitCode)" }
}
function Get-OllamaExe {
    $cmd = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $p = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}
function Get-OllamaModels {
    try { return @((Invoke-RestMethod -Uri "$($lock.ollama.endpoint)/api/tags" -TimeoutSec 5).models | ForEach-Object { $_.name }) } catch { return $null }
}
function Get-PyLauncherVersion {
    param([string]$MinorVersion)
    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if (-not $py) { return $null }
    $r = Invoke-LisNative -File $py.Source -Arguments @("-$MinorVersion", "-c", "import sys;print(sys.executable)") -TimeoutSec 30
    if ($r.ExitCode -eq 0) { return $r.Stdout.Trim() }
    return $null
}
function Test-Configured {
    param([string]$Name)
    if ([Environment]::GetEnvironmentVariable($Name, "Process")) { return $true }
    if ([Environment]::GetEnvironmentVariable($Name, "User")) { return $true }
    $envFile = Join-Path $Root ".env"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
            if ($line -match ("^\s*" + [regex]::Escape($Name) + "\s*=\s*(\S.*)$")) { return $true }
        }
    }
    return $false
}
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$RefMinor = (($lock.python.reference -split '\.')[0..1]) -join '.'
$JarvisLock = @($lock.upstreams) | Where-Object { $_.name -eq "jarvis-assistant-vocal" } | Select-Object -First 1
$JarvisDir = Join-Path $Root "external\jarvis-assistant-vocal"

# ---------------------------------------------------------------------------
# Composants : Detect renvoie @{ State = present|absent|different|risky|manual|unconfigured ; Details }
#              Apply installe (uniquement si State = absent).
# ---------------------------------------------------------------------------
$Steps = New-Object System.Collections.ArrayList
function Step { param([string]$Name, [string]$Label, [scriptblock]$Detect, [scriptblock]$Apply, [string]$Plan = "") [void]$Steps.Add([pscustomobject]@{ Name = $Name; Label = $Label; Detect = $Detect; Apply = $Apply; Plan = $Plan }) }

Step "winget" "winget" {
    if (Test-LisCommand "winget") { @{ State = "present"; Details = "disponible" } }
    else { @{ State = "manual"; Details = "absent : installe 'App Installer' depuis le Microsoft Store (necessaire aux installations automatiques)" } }
} $null

Step "git" "Git" {
    if (Test-LisCommand "git") { @{ State = "present"; Details = ((Invoke-LisNative -File "git" -Arguments @("--version") -TimeoutSec 20).Stdout.Trim()) } }
    else { @{ State = "absent"; Details = "absent" } }
} { Install-Winget $lock.system_tools.git.winget_id } "winget install $($lock.system_tools.git.winget_id)"

Step "python" "Python $($lock.python.reference)" {
    $py = Find-LisPython -Root $Root -SystemOnly
    if (-not $py) { return @{ State = "absent"; Details = "aucun Python >= $($lock.python.minimum)" } }
    $v = (Invoke-LisNative -File $py -Arguments @("-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])") -TimeoutSec 20).Stdout.Trim()
    $ref = Get-PyLauncherVersion $RefMinor
    if ($ref) { return @{ State = "present"; Details = "Python $RefMinor present (py -$RefMinor)" } }
    @{ State = "different"; Details = "Python $v present (reference $($lock.python.reference)) : compatible" }
} { Install-Winget $lock.python.winget_id } "winget install $($lock.python.winget_id)"

Step "venv" ".venv" {
    if (-not (Test-Path -LiteralPath (Join-Path $Root ".venv"))) { return @{ State = "absent"; Details = "absente" } }
    if (-not (Test-Path -LiteralPath $VenvPy)) { return @{ State = "risky"; Details = ".venv incomplete : renomme-la (Rename-Item .venv .venv.old) puis relance ; jamais supprimee ici" } }
    $r = Invoke-LisNative -File $VenvPy -Arguments @("-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])") -TimeoutSec 30
    if ($r.ExitCode -ne 0) { return @{ State = "risky"; Details = ".venv cassee (autre PC ?) : renomme-la (Rename-Item .venv .venv.old) puis relance" } }
    $v = $r.Stdout.Trim()
    if ((($v -split '\.')[0..1] -join '.') -eq $RefMinor) { @{ State = "present"; Details = "Python $v" } }
    else { @{ State = "different"; Details = "Python $v (reference $($lock.python.reference)) : conservee" } }
} {
    $base = Get-PyLauncherVersion $RefMinor
    if (-not $base) { $base = Find-LisPython -Root $Root -SystemOnly }
    if (-not $base) { throw "aucun Python pour creer la .venv" }
    $r = Invoke-LisNative -File $base -Arguments @("-m", "venv", (Join-Path $Root ".venv")) -TimeoutSec 600
    if ($r.ExitCode -ne 0) { throw "python -m venv : $($r.Stderr.Trim())" }
} "python -m venv .venv"

Step "requirements" "paquets Python" {
    if (-not (Test-Path -LiteralPath $VenvPy)) { return @{ State = "absent"; Details = ".venv absente" } }
    $mods = @($lock.python.critical_imports)
    $code = "import importlib,sys`nmissing=[]`nfor m in sys.argv[1:]:`n    try: importlib.import_module(m)`n    except Exception: missing.append(m)`nprint(','.join(missing))"
    $r = Invoke-LisNative -File $VenvPy -Arguments (@("-c", $code) + $mods) -TimeoutSec 180
    $missing = $r.Stdout.Trim()
    if ($r.ExitCode -eq 0 -and -not $missing) { @{ State = "present"; Details = "imports critiques OK (yt-dlp, faster-whisper, onnxruntime...)" } }
    else { @{ State = "absent"; Details = "manquants : $missing" } }
} {
    $lockReq = Join-Path $Root $lock.python.requirements_lock
    $r = Invoke-LisNative -File $VenvPy -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", $lockReq) -TimeoutSec 3600
    if ($r.ExitCode -ne 0) {
        Write-Host "  requirements-lock.txt refuse (roues indisponibles pour ce Python ?) : repli requirements-vault.txt" -ForegroundColor Yellow
        $fallback = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $Root $lock.python.requirements_fallback)) + @($lock.python.fallback_extra_packages)
        $r = Invoke-LisNative -File $VenvPy -Arguments $fallback -TimeoutSec 3600
        if ($r.ExitCode -ne 0) { throw "pip install a echoue : $(($r.Stderr -split "`r?`n" | Where-Object { $_ } | Select-Object -Last 1))" }
    }
} "pip install -r deployment-kit\requirements-lock.txt (repli : requirements-vault.txt + yt-dlp gallery-dl faster-whisper)"

Step "ffmpeg" "FFmpeg" {
    if (Test-LisCommand "ffmpeg") { @{ State = "present"; Details = "disponible" } } else { @{ State = "absent"; Details = "absent" } }
} { Install-Winget $lock.system_tools.ffmpeg.winget_id } "winget install $($lock.system_tools.ffmpeg.winget_id)"

Step "ollama" "Ollama" {
    $exe = Get-OllamaExe
    if (-not $exe) { return @{ State = "absent"; Details = "absent" } }
    if ($null -eq (Get-OllamaModels)) { return @{ State = "different"; Details = "installe mais service injoignable : lance l'application Ollama" } }
    @{ State = "present"; Details = "installe, service joignable" }
} { Install-Winget $lock.ollama.winget_id } "winget install $($lock.ollama.winget_id)"

Step "qwen" "Qwen local ($($lock.ollama.required_model))" {
    $models = Get-OllamaModels
    if ($null -eq $models) { return @{ State = "absent"; Details = "Ollama injoignable : modele non verifiable" } }
    if ($models -contains $lock.ollama.required_model) { @{ State = "present"; Details = "$($lock.ollama.required_model) present" } }
    else { @{ State = "absent"; Details = "$($lock.ollama.required_model) absent" } }
} {
    $exe = Get-OllamaExe
    if (-not $exe) { throw "Ollama absent" }
    $r = Invoke-LisNative -File $exe -Arguments @("pull", $lock.ollama.required_model) -TimeoutSec 3600
    if ($r.ExitCode -ne 0) { throw "ollama pull : code $($r.ExitCode) (service demarre ?)" }
} "ollama pull $($lock.ollama.required_model)"

Step "embeddings" "modele e5 (recherche semantique)" {
    $f = Join-Path $Root (($lock.embeddings.path + "/" + $lock.embeddings.onnx_file) -replace '/', '\')
    if (-not (Test-Path -LiteralPath $f)) { return @{ State = "absent"; Details = "absent (la recherche reste lexicale)" } }
    $h = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($h -eq $lock.embeddings.onnx_sha256) { @{ State = "present"; Details = "SHA-256 conforme" } }
    else { @{ State = "risky"; Details = "SHA-256 inattendu pour $($lock.embeddings.onnx_file) : verifie le fichier (non remplace)" } }
} {
    $r = Invoke-LisNative -File "powershell.exe" -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root $lock.embeddings.installer)) -TimeoutSec 1800
    if ($r.ExitCode -ne 0) { throw "setup_semantic_model.ps1 a echoue" }
} ".\setup_semantic_model.ps1"

Step "upstreams" "Upstreams + Voice Bridge" {
    $r = Invoke-LisNative -File "powershell.exe" -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ScriptDir "setup_upstreams.ps1"), "-Audit", "-Root", $Root, "-ReportDir", $ReportDir) -TimeoutSec 600
    $lines = @($r.Stdout -split "`r?`n" | Where-Object { $_ -match '\|\s*(WARNING|BLOCK|FAIL)\s*\|' } | ForEach-Object { ($_ -split '\|')[0].Trim() + " : " + ($_ -split '\|', 3)[2].Trim() })
    if ($r.ExitCode -eq 0) { @{ State = "present"; Details = "3 upstreams aux SHA du lock, bridge installe" } }
    elseif ($r.ExitCode -eq 2) { @{ State = "risky"; Details = ($lines -join " ; ") } }
    else { @{ State = "absent"; Details = ($lines -join " ; ") } }
} {
    $r = Invoke-LisNative -File "powershell.exe" -Arguments @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $ScriptDir "setup_upstreams.ps1"), "-Root", $Root, "-ReportDir", $ReportDir) -TimeoutSec 3600
    if ($r.ExitCode -ge 2) { throw "setup_upstreams.ps1 : $(($r.Stdout -split "`r?`n" | Where-Object { $_ -match 'FAIL|BLOCK' }) -join ' ; ')" }
} ".\setup_upstreams.ps1 (clone/checkout aux SHA exacts, puis install_jarvis_voice_bridge.py)"

Step "jarvis-venv" "Jarvis .venv (Python $($JarvisLock.python))" {
    if (-not (Test-Path -LiteralPath $JarvisDir)) { return @{ State = "absent"; Details = "Jarvis pas encore clone" } }
    $jp = Join-Path $JarvisDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath (Join-Path $JarvisDir ".venv"))) { return @{ State = "absent"; Details = "absente" } }
    $r = Invoke-LisNative -File $jp -Arguments @("--version") -TimeoutSec 30
    if ($r.ExitCode -ne 0) { return @{ State = "risky"; Details = "venv Jarvis cassee : renomme external\jarvis-assistant-vocal\.venv puis relance" } }
    @{ State = "present"; Details = $r.Stdout.Trim() }
} {
    $base = Get-PyLauncherVersion $JarvisLock.python
    if (-not $base) {
        Install-Winget $JarvisLock.winget_python_id
        $base = Get-PyLauncherVersion $JarvisLock.python
    }
    if (-not $base) { throw "Python $($JarvisLock.python) introuvable apres installation" }
    $r = Invoke-LisNative -File $base -Arguments @("-m", "venv", (Join-Path $JarvisDir ".venv")) -TimeoutSec 600
    if ($r.ExitCode -ne 0) { throw "venv Jarvis : $($r.Stderr.Trim())" }
    $jp = Join-Path $JarvisDir ".venv\Scripts\python.exe"
    $r = Invoke-LisNative -File $jp -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $JarvisDir "requirements.txt")) -TimeoutSec 3600
    if ($r.ExitCode -ne 0) { throw "pip install Jarvis a echoue" }
} "py -$($JarvisLock.python) -m venv external\jarvis-assistant-vocal\.venv + pip install -r requirements.txt"

Step "jarvis-config" "Jarvis config.yaml" {
    if (Test-Path -LiteralPath (Join-Path $JarvisDir "config.yaml")) { @{ State = "present"; Details = "present (non lu)" } }
    else { @{ State = "manual"; Details = "restore_lis.ps1 -RestoreSecrets, ou copie config.example.yaml -> config.yaml puis python scripts\setup.py (dans Jarvis)" } }
} $null

Step "env" ".env" {
    if (Test-Path -LiteralPath (Join-Path $Root ".env")) { @{ State = "present"; Details = "present : jamais ecrase" } }
    else { @{ State = "absent"; Details = "absent" } }
} {
    $target = Join-Path $Root ".env"
    if (Test-Path -LiteralPath $target) { return }
    Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination $target
} "copie .env.example -> .env (aucune cle ; a remplir avec llm_setup.py / telegram_setup.py)"

Step "llm-keys" "Cles LLM cloud" {
    $g = Test-Configured "GEMINI_API_KEY"; $o = Test-Configured "OPENROUTER_API_KEY"
    if ($g -or $o) { @{ State = "present"; Details = ("Gemini " + $(if ($g) { "CONFIGURED" } else { "NOT CONFIGURED" }) + ", OpenRouter " + $(if ($o) { "CONFIGURED" } else { "NOT CONFIGURED" })) } }
    else { @{ State = "unconfigured"; Details = "optionnel : .\.venv\Scripts\python.exe llm_setup.py (saisie masquee). Qwen local suffit pour le sensible." } }
} $null

Step "telegram" "Telegram" {
    if ((Test-Configured "TELEGRAM_BOT_TOKEN") -and (Test-Configured "TELEGRAM_ALLOWED_USER_ID") -and (Test-Configured "TELEGRAM_ALLOWED_CHAT_ID")) { @{ State = "present"; Details = "token + whitelist CONFIGURED" } }
    else { @{ State = "manual"; Details = "a lancer par toi : .\.venv\Scripts\python.exe telegram_setup.py (token masque, /start au bot)" } }
} $null

Step "notion" "Notion (cockpit)" {
    if (Test-Configured "NOTION_TOKEN") { @{ State = "present"; Details = "NOTION_TOKEN CONFIGURED (projection uniquement)" } }
    else { @{ State = "unconfigured"; Details = "optionnel : variable utilisateur NOTION_TOKEN (voir docs/NOTION_VAULT_LIBRARY.md)" } }
} $null

Step "vault" "vault.db" {
    $db = Join-Path $Root "vault.db"
    if (Test-Path -LiteralPath $db) { @{ State = "present"; Details = ("present ({0:N1} Mo) : jamais remplace" -f ((Get-Item -LiteralPath $db).Length / 1MB)) } }
    else { @{ State = "absent"; Details = "absent (nouvelle installation : base vide ; catastrophe : restore_lis.ps1 AVANT setup)" } }
} {
    $db = Join-Path $Root "vault.db"
    if (Test-Path -LiteralPath $db) { return }
    $code = "import sys; sys.path.insert(0, r'$Root'); import vault_db; vault_db.connect(r'$db').close(); print('ok')"
    $r = Invoke-LisNative -File $VenvPy -Arguments @("-c", $code) -TimeoutSec 120 -WorkingDirectory $Root
    if ($r.ExitCode -ne 0) { throw "creation du schema vault.db : $($r.Stderr.Trim())" }
} "creation d'une base vide avec le schema vault_db (aucune donnee)"

Step "mobile-inbox" "mobile-inbox" {
    $dir = Join-Path $Root "mobile-inbox"
    if ((Test-Path -LiteralPath $dir) -and (Test-Path -LiteralPath (Join-Path $dir "inbox.txt"))) { @{ State = "present"; Details = "mobile-inbox\inbox.txt" } }
    else { @{ State = "absent"; Details = "dossier ou inbox.txt absent" } }
} {
    $dir = Join-Path $Root "mobile-inbox"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $f = Join-Path $dir "inbox.txt"
    if (-not (Test-Path -LiteralPath $f)) { New-Item -ItemType File -Path $f | Out-Null }
} "cree mobile-inbox\inbox.txt (vide)"

Step "syncthing" "Syncthing" {
    $cfg = Join-Path $env:LOCALAPPDATA "Syncthing\config.xml"
    $installed = (Test-LisCommand "syncthing") -or (Get-Process -Name "syncthing" -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $cfg) -or
        (Test-Path (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\Syncthing.Syncthing_*"))
    if (-not $installed) { return @{ State = "absent"; Details = "absent" } }
    if (Test-Path -LiteralPath $cfg) {
        $xml = [xml](Get-Content -LiteralPath $cfg -Raw -Encoding UTF8)
        $mobile = Join-Path $Root "mobile-inbox"
        $hit = @($xml.configuration.folder) | Where-Object { $_.path -and ($_.path.TrimEnd('\') -eq $mobile) }
        if ($hit) { return @{ State = "present"; Details = "dossier '$(@($hit)[0].label)' -> mobile-inbox" } }
    }
    @{ State = "manual"; Details = "installe ; appairage telephone + dossier mobile-inbox a faire (deployment-kit\android\README.md)" }
} { Install-Winget $lock.system_tools.syncthing.winget_id } "winget install $($lock.system_tools.syncthing.winget_id) puis appairage manuel"

Step "android" "Android (MacroDroid)" {
    @{ State = "manual"; Details = "importer la macro sur le telephone (deployment-kit\android\README.md) ; EXPORT REQUIRED FROM ANDROID DEVICE si absent" }
} $null

Step "tasks" "Taches Windows" {
    $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "Reels Vault*" })
    $cmds = @{}
    foreach ($t in $tasks) { $cmds[$t.TaskName] = ((@($t.Actions) | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " ") }
    $missing = @()
    if (-not @($cmds.Values | Where-Object { $_ -like "*$(Join-Path $Root 'inbox.bat')*" }).Count) { $missing += "Ingestion" }
    if (-not $cmds.ContainsKey($lock.windows_tasks.telegram_gateway)) { $missing += "TelegramGateway" }
    if (-not $cmds.ContainsKey($lock.windows_tasks.mentor_daily)) { $missing += "MentorDaily" }
    if ($missing.Count -eq 0) { @{ State = "present"; Details = "ingestion, Telegram Gateway, Mentor Daily" } }
    else { @{ State = "absent"; Details = ("manquantes : " + ($missing -join ", ")); Missing = $missing } }
} {
    param($det)
    $only = @($det.Missing)
    $args2 = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $Root "setup_windows_tasks.ps1"), "-Only", ($only -join ","))
    $tg = (Test-Configured "TELEGRAM_BOT_TOKEN") -and (Test-Configured "TELEGRAM_ALLOWED_USER_ID") -and (Test-Configured "TELEGRAM_ALLOWED_CHAT_ID")
    if (-not $tg) { $args2 += "-SkipTelegramGateway"; Write-Host "  Telegram non configure : tache Gateway reportee (relance apres telegram_setup.py)" -ForegroundColor Yellow }
    if (-not (Test-Path -LiteralPath $VenvPy)) { throw ".venv absente : taches non creees" }
    $r = Invoke-LisNative -File "powershell.exe" -Arguments $args2 -TimeoutSec 300
    if ($r.ExitCode -ne 0) { throw "setup_windows_tasks.ps1 : $($r.Stderr.Trim())" }
} ".\setup_windows_tasks.ps1 -Only <manquantes> (Gateway seulement si Telegram configure)"

# ---------------------------------------------------------------------------
# AUDIT -> PLAN -> APPLY
# ---------------------------------------------------------------------------
$Plan = @()
foreach ($s in $Steps) {
    if ($Only.Count -and ($Only -notcontains $s.Name)) { continue }
    try { $det = & $s.Detect } catch { $det = @{ State = "risky"; Details = "detection en erreur : $($_.Exception.Message)" } }
    switch ($det.State) {
        "present" { Rec $s.Label "SKIP" $det.Details }
        "different" { Rec $s.Label "WARNING" $det.Details }
        "risky" { Rec $s.Label "BLOCK" $det.Details }
        "manual" { Rec $s.Label "MANUAL TEST REQUIRED" $det.Details }
        "unconfigured" { Rec $s.Label "NOT CONFIGURED" $det.Details }
        "absent" {
            if ($s.Apply) {
                $Plan += [pscustomobject]@{ Step = $s; Detected = $det }
                $what = $s.Plan
                if (-not $Apply) { Rec $s.Label "INSTALL" "$($det.Details) -> prevu : $what" }
            } else { Rec $s.Label "MANUAL TEST REQUIRED" $det.Details }
        }
    }
}

if ($Apply -and $Plan.Count) {
    Write-Host ""
    Write-Host "--- APPLY ($($Plan.Count) composant(s))" -ForegroundColor White
    foreach ($p in $Plan) {
        Write-Host "  -> $($p.Step.Label) : $($p.Step.Plan)"
        try {
            & $p.Step.Apply $p.Detected
            $after = & $p.Step.Detect
            if ($after.State -eq "present" -or $after.State -eq "different") { Rec $p.Step.Label "OK" "installe : $($after.Details)" }
            elseif ($after.State -eq "manual") { Rec $p.Step.Label "MANUAL TEST REQUIRED" $after.Details }
            else { Rec $p.Step.Label "WARNING" "action lancee, verification : $($after.Details) (ouvre un nouveau terminal puis relance -Audit)" }
        } catch {
            Rec $p.Step.Label "FAIL" $_.Exception.Message
        }
    }
}

$worst = Get-LisWorstRank $report
$pending = @($report.Results | Where-Object { $_.Status -eq "INSTALL" }).Count
if (-not $Apply -and $pending -and $worst -lt 1) { $worst = 1 }
try { $saved = Save-LisReport -Report $report -Directory $ReportDir -Extra @{ mode = $Mode } } catch { $saved = "" }

if ($Apply -and -not $SkipHealthCheck -and -not $Only.Count) {
    Write-Host ""
    Write-Host "--- HEALTH CHECK" -ForegroundColor White
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir "CHECK_LIS.ps1") -Root $Root
    $hc = $LASTEXITCODE
    if ($hc -gt $worst) { $worst = $hc }
}

Write-Host ""
if ($saved) { Write-Host "Rapport : $saved" }
switch ($worst) {
    0 { Write-Host "SETUP : LIS READY" -ForegroundColor Green }
    1 { Write-Host "SETUP : actions restantes / avertissements (voir ci-dessus)" -ForegroundColor Yellow }
    default { Write-Host "SETUP : BLOQUE ou ECHEC (voir BLOCK / FAIL)" -ForegroundColor Red }
}
exit $worst
