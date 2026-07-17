[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EncryptedPath,
    [Parameter(Mandatory = $true)]
    [string]$EvidencePath,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [switch]$AllowPlaintextOutput
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $AllowPlaintextOutput) {
    throw "Plaintext restore output requires explicit -AllowPlaintextOutput approval."
}
if (-not (Test-Path -LiteralPath $EncryptedPath -PathType Leaf)) { throw "Encrypted backup is missing." }
if (-not (Test-Path -LiteralPath $EvidencePath -PathType Leaf)) { throw "Backup evidence is missing." }
if (Test-Path -LiteralPath $OutputPath) { throw "Restore output already exists." }

$evidence = Get-Content -Raw -LiteralPath $EvidencePath | ConvertFrom-Json
foreach ($field in @(
    "backup_id", "source_archive_sha256", "encrypted_archive_sha256",
    "certificate_thumbprint", "encryption", "status"
)) {
    if (-not $evidence.PSObject.Properties.Name.Contains($field)) { throw "Backup evidence is missing $field." }
}
if ($evidence.status -ne "success") { throw "Backup evidence is not successful." }
if ($evidence.encryption -ne "CMS EnvelopedData AES-256-CBC") { throw "Backup evidence encryption contract is unsupported." }
if ($evidence.source_archive_sha256 -notmatch '^[0-9a-f]{64}$') { throw "Source archive checksum is invalid." }
if ($evidence.encrypted_archive_sha256 -notmatch '^[0-9a-f]{64}$') { throw "Encrypted archive checksum is invalid." }

$encryptedSha256 = (Get-FileHash -LiteralPath $EncryptedPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($encryptedSha256 -ne $evidence.encrypted_archive_sha256) { throw "Encrypted archive checksum mismatch." }

$thumbprint = ([string]$evidence.certificate_thumbprint).Replace(" ", "").ToUpperInvariant()
$certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$thumbprint" -ErrorAction Stop
if (-not $certificate.HasPrivateKey) { throw "Backup certificate private key is unavailable." }

$outputDirectory = Split-Path -Parent ([IO.Path]::GetFullPath($OutputPath))
[IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$partialPath = "$OutputPath.partial"
try {
    $base64Content = (Unprotect-CmsMessage -Path $EncryptedPath).Trim()
    $content = [Convert]::FromBase64String($base64Content)
    [IO.File]::WriteAllBytes($partialPath, $content)
    $sourceSha256 = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($sourceSha256 -ne $evidence.source_archive_sha256) { throw "Decrypted archive checksum mismatch." }
    & tar.exe -tzf $partialPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Decrypted archive failed structural validation." }
    [IO.File]::Move($partialPath, $OutputPath)
    & icacls.exe $OutputPath /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to protect plaintext restore output ACL." }
    Write-Output "backup_id=$($evidence.backup_id)"
    Write-Output "status=success"
    Write-Output "plaintext_output=$OutputPath"
}
finally {
    if (Test-Path -LiteralPath $partialPath) {
        Remove-Item -LiteralPath $partialPath -Force
    }
}
