# Installe le modele d'embedding local de Life Intelligence V2 (recherche semantique).
# multilingual-e5-small (Xenova, ONNX quantifie int8, ~118 Mo), verifie par SHA-256.
# Sans ce modele, la recherche du Vault reste purement lexicale (fonctionnement V1).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $root "models\multilingual-e5-small"
$base = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main"

$files = @(
    @{ Path = "onnx/model_quantized.onnx"; Sha256 = "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193" },
    @{ Path = "tokenizer.json";            Sha256 = "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39" },
    @{ Path = "config.json";               Sha256 = $null },
    @{ Path = "tokenizer_config.json";     Sha256 = $null },
    @{ Path = "special_tokens_map.json";   Sha256 = $null }
)

foreach ($file in $files) {
    $target = Join-Path $dest ($file.Path -replace "/", "\")
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    if (-not (Test-Path $target)) {
        Write-Host "Telechargement de $($file.Path)..."
        & curl.exe -sSL --fail -o $target "$base/$($file.Path)"
        if ($LASTEXITCODE -ne 0) { throw "Echec du telechargement : $($file.Path)" }
    }
    if ($file.Sha256) {
        $hash = (Get-FileHash -Algorithm SHA256 $target).Hash.ToLower()
        if ($hash -ne $file.Sha256) {
            Remove-Item $target
            throw "Empreinte SHA-256 inattendue pour $($file.Path) : fichier supprime."
        }
    }
}
Write-Host "Modele pret : $dest"
Write-Host "Indexation : .\.venv\Scripts\python.exe vault_embeddings.py"
