# LisDeploy.psm1 - Fonctions communes aux scripts de deploiement du LIS.
#
# Compatible Windows PowerShell 5.1 et PowerShell 7. Fichier volontairement ASCII.
# Aucune fonction de ce module n'affiche ni ne journalise une valeur secrete.

Set-StrictMode -Version 2.0

$script:StatusRank = @{
    "OK" = 0; "SKIP" = 0; "NOT CONFIGURED" = 0; "MANUAL TEST REQUIRED" = 0;
    "WARNING" = 1; "INSTALL" = 1; "FAIL" = 2; "BLOCK" = 2
}

# ---------------------------------------------------------------------------
# Rapports : COMPONENT | STATUS | DETAILS
# ---------------------------------------------------------------------------

function New-LisReport {
    param([string]$Name)
    return [pscustomobject]@{
        Name    = $Name
        Started = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        Results = New-Object System.Collections.ArrayList
    }
}

function Add-LisResult {
    param(
        [Parameter(Mandatory = $true)]$Report,
        [Parameter(Mandatory = $true)][string]$Component,
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Details = "",
        [switch]$Quiet
    )
    $row = [pscustomobject]@{ Component = $Component; Status = $Status; Details = $Details }
    [void]$Report.Results.Add($row)
    if (-not $Quiet) { Write-LisRow $row }
    return $row
}

function Write-LisRow {
    param($Row)
    $color = "Gray"
    switch ($Row.Status) {
        "OK" { $color = "Green" }
        "SKIP" { $color = "Green" }
        "WARNING" { $color = "Yellow" }
        "NOT CONFIGURED" { $color = "DarkYellow" }
        "MANUAL TEST REQUIRED" { $color = "Cyan" }
        "FAIL" { $color = "Red" }
        "BLOCK" { $color = "Red" }
        default { $color = "Gray" }
    }
    $line = "{0,-34} | {1,-20} | {2}" -f $Row.Component, $Row.Status, $Row.Details
    Write-Host $line -ForegroundColor $color
}

function Write-LisHeader {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 78)
    Write-Host $Title
    Write-Host ("=" * 78)
    Write-Host ("{0,-34} | {1,-20} | {2}" -f "COMPONENT", "STATUS", "DETAILS")
    Write-Host ("-" * 78)
}

function Get-LisWorstRank {
    param($Report)
    $worst = 0
    foreach ($row in $Report.Results) {
        $rank = 0
        if ($script:StatusRank.ContainsKey($row.Status)) { $rank = $script:StatusRank[$row.Status] }
        if ($rank -gt $worst) { $worst = $rank }
    }
    return $worst
}

function Save-LisReport {
    param(
        [Parameter(Mandatory = $true)]$Report,
        [Parameter(Mandatory = $true)][string]$Directory,
        [hashtable]$Extra = @{}
    )
    New-Item -ItemType Directory -Force -Path $Directory | Out-Null
    $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
    $base = Join-Path $Directory ("{0}-{1}" -f $Report.Name, $stamp)
    $payload = [ordered]@{
        report   = $Report.Name
        started  = $Report.Started
        finished = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        results  = @($Report.Results)
    }
    foreach ($key in $Extra.Keys) { $payload[$key] = $Extra[$key] }
    $payload | ConvertTo-Json -Depth 6 | Set-Content -Path ($base + ".json") -Encoding UTF8
    $lines = @("COMPONENT | STATUS | DETAILS")
    foreach ($row in $Report.Results) { $lines += ("{0} | {1} | {2}" -f $row.Component, $row.Status, $row.Details) }
    $lines | Set-Content -Path ($base + ".txt") -Encoding UTF8
    return ($base + ".json")
}

# ---------------------------------------------------------------------------
# Lock file
# ---------------------------------------------------------------------------

function Read-LisLock {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Lock file introuvable : $Path" }
    $lock = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema -ne "lis-lock") { throw "Lock file invalide (schema '$($lock.schema)')." }
    if ([int]$lock.schema_version -ne 1) { throw "Version de lock file non supportee : $($lock.schema_version)" }
    foreach ($up in @($lock.upstreams)) {
        if (-not ($up.sha -match '^[0-9a-f]{40}$')) { throw "SHA invalide pour l'upstream $($up.name)" }
        if (-not $up.url -or -not $up.path) { throw "Upstream $($up.name) incomplet (url/path)." }
    }
    if (-not ($lock.lis.validated_sha -match '^[0-9a-f]{40}$')) { throw "SHA du LIS invalide dans le lock." }
    return $lock
}

# ---------------------------------------------------------------------------
# Outils externes
# ---------------------------------------------------------------------------

function Test-LisCommand {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-LisNative {
    # Lance un executable et capture stdout+stderr sans transformer stderr en erreur PS.
    param([string]$File, [string[]]$Arguments, [int]$TimeoutSec = 120, [string]$WorkingDirectory = "")
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join " "
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return [pscustomobject]@{ ExitCode = -1; Stdout = ""; Stderr = $_.Exception.Message; TimedOut = $false }
    }
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    $timedOut = -not $proc.WaitForExit($TimeoutSec * 1000)
    if ($timedOut) { try { $proc.Kill() } catch { } }
    $code = -1
    if (-not $timedOut) { $code = $proc.ExitCode }
    return [pscustomobject]@{
        ExitCode = $code; Stdout = $outTask.Result; Stderr = $errTask.Result; TimedOut = $timedOut
    }
}

function Find-LisPython {
    # Python du LIS (.venv) ; sinon un Python systeme (py launcher puis PATH).
    param([string]$Root, [switch]$SystemOnly)
    if (-not $SystemOnly) {
        $venv = Join-Path $Root ".venv\Scripts\python.exe"
        if (Test-Path -LiteralPath $venv) { return $venv }
    }
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($cmd.Source -like "*WindowsApps*") { continue }  # alias Microsoft Store
        $probe = Invoke-LisNative -File $cmd.Source -Arguments @("-c", "import sys,sqlite3;print(sys.version_info[0]*100+sys.version_info[1])") -TimeoutSec 20
        if ($probe.ExitCode -eq 0 -and [int]($probe.Stdout.Trim()) -ge 312) { return $cmd.Source }
    }
    return $null
}

function Invoke-LisHelper {
    # Appelle deployment-kit/lib/lis_deploy.py et renvoie l'objet JSON (ou $null).
    param([string]$Python, [string[]]$Arguments, [int]$TimeoutSec = 180)
    $helper = Join-Path $PSScriptRoot "lis_deploy.py"
    $res = Invoke-LisNative -File $Python -Arguments (@($helper) + $Arguments) -TimeoutSec $TimeoutSec
    $data = $null
    try { $data = $res.Stdout | ConvertFrom-Json } catch { $data = $null }
    return [pscustomobject]@{ ExitCode = $res.ExitCode; Data = $data; Stderr = $res.Stderr; TimedOut = $res.TimedOut }
}

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

function Get-LisGitState {
    param([Parameter(Mandatory = $true)][string]$Path)
    $state = [ordered]@{ IsRepo = $false; Sha = ""; Branch = ""; Dirty = $false; Modified = @(); Untracked = @(); Origin = "" }
    if (-not (Test-Path -LiteralPath (Join-Path $Path ".git"))) { return [pscustomobject]$state }
    $sha = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "rev-parse", "HEAD") -TimeoutSec 30
    if ($sha.ExitCode -ne 0) { return [pscustomobject]$state }
    $state.IsRepo = $true
    $state.Sha = $sha.Stdout.Trim()
    $branch = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "rev-parse", "--abbrev-ref", "HEAD") -TimeoutSec 30
    $state.Branch = $branch.Stdout.Trim()
    $origin = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "remote", "get-url", "origin") -TimeoutSec 30
    if ($origin.ExitCode -eq 0) { $state.Origin = $origin.Stdout.Trim() }
    $status = Invoke-LisNative -File "git" -Arguments @("-C", $Path, "status", "--porcelain", "--untracked-files=all") -TimeoutSec 60
    $modified = @(); $untracked = @()
    foreach ($line in ($status.Stdout -split "`r?`n")) {
        if (-not $line) { continue }
        $file = $line.Substring(3).Trim('"')
        if ($line.StartsWith("??")) { $untracked += $file } else { $modified += $file }
    }
    $state.Modified = $modified
    $state.Untracked = $untracked
    $state.Dirty = ($modified.Count -gt 0)
    return [pscustomobject]$state
}

function ConvertTo-LisRepoUrl {
    param([string]$Url)
    if (-not $Url) { return "" }
    return ($Url.Trim().ToLowerInvariant() -replace '\.git$', '' -replace '/$', '')
}

# ---------------------------------------------------------------------------
# Manifest de backup : calcule et verifie par lis_deploy.py (manifest-files /
# manifest-verify), qui supporte les chemins de plus de 260 caracteres.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Chiffrement des secrets : AES-256-CBC + HMAC-SHA256 (encrypt-then-MAC),
# cle derivee de la passphrase par PBKDF2-SHA256. .NET uniquement, aucune
# dependance externe ; dechiffrable sur n'importe quel Windows (PS 5.1+).
# ---------------------------------------------------------------------------

function Get-LisPassphrase {
    # LIS_BACKUP_PASSPHRASE (automatisation / tests) sinon saisie masquee.
    param([switch]$Confirm)
    if ($env:LIS_BACKUP_PASSPHRASE) { return $env:LIS_BACKUP_PASSPHRASE }
    $first = Read-Host -AsSecureString "Passphrase du backup (saisie masquee, 12 caracteres minimum)"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($first))
    if ($Confirm) {
        $second = Read-Host -AsSecureString "Confirme la passphrase"
        $plain2 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($second))
        if ($plain -ne $plain2) { throw "Les deux passphrases different." }
    }
    return $plain
}

function Get-LisDerivedKeys {
    param([string]$Passphrase, [byte[]]$Salt, [int]$Iterations)
    $kdf = New-Object System.Security.Cryptography.Rfc2898DeriveBytes -ArgumentList @(
        $Passphrase, $Salt, $Iterations, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    $bytes = $kdf.GetBytes(64)
    $kdf.Dispose()
    return @{ Enc = [byte[]]$bytes[0..31]; Mac = [byte[]]$bytes[32..63] }
}

function Get-LisMac {
    param([byte[]]$Key, [string]$Header, [byte[]]$Iv, [byte[]]$Cipher)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, $Key)
    $headerBytes = [System.Text.Encoding]::UTF8.GetBytes($Header)
    $all = New-Object byte[] ($headerBytes.Length + $Iv.Length + $Cipher.Length)
    [Array]::Copy($headerBytes, 0, $all, 0, $headerBytes.Length)
    [Array]::Copy($Iv, 0, $all, $headerBytes.Length, $Iv.Length)
    [Array]::Copy($Cipher, 0, $all, $headerBytes.Length + $Iv.Length, $Cipher.Length)
    $mac = $hmac.ComputeHash($all)
    $hmac.Dispose()
    return $mac
}

function Protect-LisBytes {
    param([Parameter(Mandatory = $true)][byte[]]$Plain, [Parameter(Mandatory = $true)][string]$Passphrase, [int]$Iterations = 600000)
    if ($Passphrase.Length -lt 12) { throw "Passphrase trop courte (12 caracteres minimum)." }
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $salt = New-Object byte[] 16
    $rng.GetBytes($salt)
    $keys = Get-LisDerivedKeys -Passphrase $Passphrase -Salt $salt -Iterations $Iterations
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.KeySize = 256
    $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7
    $aes.Key = $keys.Enc
    $aes.GenerateIV()
    $iv = $aes.IV
    $enc = $aes.CreateEncryptor()
    $cipher = $enc.TransformFinalBlock($Plain, 0, $Plain.Length)
    $enc.Dispose(); $aes.Dispose()
    $header = "lis-secrets-v1|pbkdf2-sha256|$Iterations|aes-256-cbc|hmac-sha256|" + [Convert]::ToBase64String($salt)
    $mac = Get-LisMac -Key $keys.Mac -Header $header -Iv $iv -Cipher $cipher
    return ([ordered]@{
            format     = "lis-secrets"
            version    = 1
            kdf        = "PBKDF2-SHA256"
            iterations = $Iterations
            cipher     = "AES-256-CBC"
            mac        = "HMAC-SHA256 (encrypt-then-MAC)"
            salt       = [Convert]::ToBase64String($salt)
            iv         = [Convert]::ToBase64String($iv)
            header     = $header
            hmac       = [Convert]::ToBase64String($mac)
            ciphertext = [Convert]::ToBase64String($cipher)
        } | ConvertTo-Json -Depth 3)
}

function Unprotect-LisBytes {
    param([Parameter(Mandatory = $true)][string]$Json, [Parameter(Mandatory = $true)][string]$Passphrase)
    $box = $Json | ConvertFrom-Json
    if ($box.format -ne "lis-secrets" -or [int]$box.version -ne 1) { throw "Format de secrets inconnu." }
    $salt = [Convert]::FromBase64String($box.salt)
    $iv = [Convert]::FromBase64String($box.iv)
    $cipher = [Convert]::FromBase64String($box.ciphertext)
    $expected = "lis-secrets-v1|pbkdf2-sha256|$([int]$box.iterations)|aes-256-cbc|hmac-sha256|$($box.salt)"
    if ($box.header -ne $expected) { throw "En-tete des secrets incoherent (fichier altere)." }
    $keys = Get-LisDerivedKeys -Passphrase $Passphrase -Salt $salt -Iterations ([int]$box.iterations)
    $mac = Get-LisMac -Key $keys.Mac -Header $box.header -Iv $iv -Cipher $cipher
    $given = [Convert]::FromBase64String($box.hmac)
    $diff = $mac.Length -bxor $given.Length
    for ($i = 0; $i -lt [Math]::Min($mac.Length, $given.Length); $i++) { $diff = $diff -bor ($mac[$i] -bxor $given[$i]) }
    if ($diff -ne 0) { throw "Passphrase incorrecte ou secrets alteres (HMAC invalide)." }
    $aes = [System.Security.Cryptography.Aes]::Create()
    $aes.KeySize = 256
    $aes.Mode = [System.Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [System.Security.Cryptography.PaddingMode]::PKCS7
    $aes.Key = $keys.Enc
    $aes.IV = $iv
    $dec = $aes.CreateDecryptor()
    $plain = $dec.TransformFinalBlock($cipher, 0, $cipher.Length)
    $dec.Dispose(); $aes.Dispose()
    return , $plain
}

Export-ModuleMember -Function *-Lis*
