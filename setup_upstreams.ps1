param(
    [switch]$ForceRefresh
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$External = Join-Path $Root "external"
$AgencyDir = Join-Path $External "agency-agents"
$ReelsUpstreamDir = Join-Path $External "reels-vault-upstream"
$JarvisUpstreamDir = Join-Path $External "jarvis-assistant-vocal"

New-Item -ItemType Directory -Force -Path $External | Out-Null

function Sync-Repo {
    param(
        [Parameter(Mandatory=$true)][string]$Url,
        [Parameter(Mandatory=$true)][string]$Path
    )

    if (Test-Path (Join-Path $Path ".git")) {
        Write-Host "Updating $Path"
        git -C $Path fetch --depth 1 origin main
        git -C $Path reset --hard origin/main
        return
    }

    if (Test-Path $Path) {
        if (-not $ForceRefresh) {
            throw "$Path existe deja mais n'est pas un clone Git. Relance avec -ForceRefresh pour le remplacer."
        }
        Remove-Item -Recurse -Force $Path
    }

    Write-Host "Cloning $Url"
    git clone --depth 1 $Url $Path
}

Sync-Repo -Url "https://github.com/msitarzewski/agency-agents.git" -Path $AgencyDir
Sync-Repo -Url "https://github.com/sosoj92/reels-vault.git" -Path $ReelsUpstreamDir
Sync-Repo -Url "https://github.com/sosoj92/jarvis-assistant-vocal.git" -Path $JarvisUpstreamDir

$VaultPython = Join-Path $Root ".venv\Scripts\python.exe"
$BridgeInstaller = Join-Path $Root "install_jarvis_voice_bridge.py"
if ((Test-Path $VaultPython) -and (Test-Path $BridgeInstaller)) {
    Write-Host "Installing Life Intelligence voice bridge into Jarvis..."
    & $VaultPython $BridgeInstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Installation du pont vocal Jarvis/Life Intelligence echouee."
    }
} else {
    Write-Host "WARNING: voice bridge installer skipped (Vault venv or installer missing)."
}

Write-Host ""
Write-Host "Upstreams ready:"
Write-Host "  Agency Agents : $AgencyDir"
Write-Host "  Reels Vault   : $ReelsUpstreamDir"
Write-Host "  Jarvis        : $JarvisUpstreamDir"
