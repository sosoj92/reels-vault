param(
    [switch]$ForceRefresh
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$External = Join-Path $Root "external"
$AgencyDir = Join-Path $External "agency-agents"
$ReelsUpstreamDir = Join-Path $External "reels-vault-upstream"

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

Write-Host ""
Write-Host "Upstreams ready:"
Write-Host "  Agency Agents : $AgencyDir"
Write-Host "  Reels Vault   : $ReelsUpstreamDir"
