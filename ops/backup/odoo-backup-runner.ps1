[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-BackupConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Backup config file does not exist."
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*$' -or $line -match '^\s*#') { continue }
        if ($line -notmatch '^\s*([A-Z0-9_]+)=(.*)$') {
            throw "Backup config contains an invalid line."
        }
        $key = $Matches[1]
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        if ($values.ContainsKey($key)) { throw "Backup config contains a duplicate key." }
        $values[$key] = $value
    }
    return $values
}

function Require-ConfigValue {
    param([hashtable]$Config, [string]$Key)
    if (-not $Config.ContainsKey($Key) -or [string]::IsNullOrWhiteSpace($Config[$Key])) {
        throw "Backup config is missing required key $Key."
    }
    return [string]$Config[$Key]
}

function Invoke-CheckedProcess {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$Attempts = 1
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $output = & $FilePath @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($exitCode -eq 0) {
            return @($output | ForEach-Object { [string]$_ })
        }
        if ($attempt -lt $Attempts) {
            Start-Sleep -Seconds ([Math]::Min(2 * $attempt, 6))
        }
    }
    throw "$FilePath failed after $Attempts attempt(s)."
}

function Protect-ArchiveWithCertificate {
    param(
        [string]$SourcePath,
        [string]$DestinationPath,
        [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate
    )
    $base64Content = [Convert]::ToBase64String([IO.File]::ReadAllBytes($SourcePath))
    Protect-CmsMessage -Content $base64Content -To $Certificate -OutFile $DestinationPath
}

function Confirm-EncryptedRoundtrip {
    param(
        [string]$EncryptedPath,
        [string]$ExpectedSha256
    )
    $base64Content = (Unprotect-CmsMessage -Path $EncryptedPath).Trim()
    $content = [Convert]::FromBase64String($base64Content)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha256.ComputeHash($content)
    }
    finally {
        $sha256.Dispose()
    }
    $actual = ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
    if ($actual -ne $ExpectedSha256) {
        throw "Encrypted backup roundtrip checksum mismatch."
    }
}

function Confirm-Aes256CmsCipher {
    param([string]$EncryptedPath)
    $dump = & certutil.exe -dump $EncryptedPath 2>&1
    if ($LASTEXITCODE -ne 0 -or (($dump | Out-String) -notmatch '2\.16\.840\.1\.101\.3\.4\.1\.42')) {
        throw "Encrypted backup is not CMS AES-256-CBC."
    }
}

function Get-BackupFailureClass {
    param([string]$Message)

    $normalized = [string]$Message
    if ($normalized -match 'free-space|destination|artifact already exists') { return "storage" }
    if ($normalized -match 'certificate') { return "certificate" }
    if ($normalized -match 'ssh\.exe|scp\.exe|Remote backup') { return "remote" }
    if ($normalized -match 'transfer checksum|structural validation|checksum mismatch') { return "validation" }
    if ($normalized -match 'CMS|Encrypted backup') { return "encryption" }
    if ($normalized -match 'cleanup') { return "cleanup" }
    return "unknown"
}

function Write-BackupAttemptRecord {
    param(
        [string]$Path,
        [string]$BackupId,
        [datetime]$StartedAt,
        [string]$Status,
        [string]$FailureClass = $null
    )

    if ($Status -notin @("success", "failed")) { throw "Backup attempt status is invalid." }
    if ($FailureClass -and $FailureClass -notin @("storage", "certificate", "remote", "validation", "encryption", "cleanup", "unknown")) {
        throw "Backup attempt failure class is invalid."
    }
    $record = [ordered]@{
        schema_version = 1
        backup_id = $BackupId
        started_at_utc = $StartedAt.ToString("o")
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        source_app_service = "srv-captain--geotherm-odoo"
        source_db_service = "srv-captain--geotherm-odoo-db"
        status = $Status
        failure_class = if ([string]::IsNullOrWhiteSpace($FailureClass)) { $null } else { $FailureClass }
        odoo_metric_write_performed = $false
    }
    $temporaryPath = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::WriteAllText($temporaryPath, ($record | ConvertTo-Json -Depth 3), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) { Remove-Item -LiteralPath $temporaryPath -Force }
    }
}

$config = Read-BackupConfig -Path $ConfigPath
$allowedKeys = @(
    "ODOO_BACKUP_SSH_HOST",
    "ODOO_BACKUP_SSH_USER",
    "ODOO_BACKUP_KNOWN_HOSTS",
    "ODOO_BACKUP_LOCAL_DIR",
    "ODOO_BACKUP_CERT_THUMBPRINT",
    "ODOO_BACKUP_MIN_FREE_BYTES"
)
foreach ($key in $config.Keys) {
    if ($key -notin $allowedKeys) { throw "Backup config contains unsupported key $key." }
}

$sshHost = Require-ConfigValue -Config $config -Key "ODOO_BACKUP_SSH_HOST"
$sshUser = Require-ConfigValue -Config $config -Key "ODOO_BACKUP_SSH_USER"
$knownHosts = Require-ConfigValue -Config $config -Key "ODOO_BACKUP_KNOWN_HOSTS"
$localDirectory = Require-ConfigValue -Config $config -Key "ODOO_BACKUP_LOCAL_DIR"
$thumbprint = (Require-ConfigValue -Config $config -Key "ODOO_BACKUP_CERT_THUMBPRINT").Replace(" ", "").ToUpperInvariant()
$minimumFreeBytes = [int64](Require-ConfigValue -Config $config -Key "ODOO_BACKUP_MIN_FREE_BYTES")

if ($sshHost -notmatch '^[A-Za-z0-9.-]+$') { throw "SSH host is invalid." }
if ($sshUser -notmatch '^[A-Za-z0-9_-]+$') { throw "SSH user is invalid." }
if (-not (Test-Path -LiteralPath $knownHosts -PathType Leaf)) { throw "Known-hosts file is missing." }
if ($minimumFreeBytes -lt 1073741824) { throw "Minimum free-space guard must be at least 1 GiB." }

$runnerDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$remoteSourceScript = Join-Path $runnerDirectory "create-odoo-backup.sh"
if (-not (Test-Path -LiteralPath $remoteSourceScript -PathType Leaf)) {
    throw "Remote backup source script is missing."
}

[IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$driveRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($localDirectory))
$driveName = $driveRoot.TrimEnd('\').TrimEnd(':')
$freeBytes = (Get-PSDrive -Name $driveName).Free
if ($freeBytes -lt $minimumFreeBytes) { throw "Off-host backup destination is below its free-space guard." }

$certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$thumbprint" -ErrorAction Stop
if (-not $certificate.HasPrivateKey) { throw "Backup certificate has no private key." }
if ($certificate.NotAfter.ToUniversalTime() -le [DateTime]::UtcNow.AddDays(30)) {
    throw "Backup certificate expires within 30 days."
}

$sshOptions = @(
    "-q",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "UserKnownHostsFile=$knownHosts"
)
$remoteTarget = "${sshUser}@${sshHost}"
$remoteInstallPath = "/root/arcigy-backups/bin/geotherm-odoo-backup.sh"
$nonceBytes = New-Object byte[] 3
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $random.GetBytes($nonceBytes)
}
finally {
    $random.Dispose()
}
$nonce = ([BitConverter]::ToString($nonceBytes)).Replace("-", "").ToLowerInvariant()
$requestedBackupId = "geotherm-odoo-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))-$nonce"
$remoteArchivePath = "/root/arcigy-backups/transfers/$requestedBackupId.tar.gz"
$rawLocalPath = $null
$encryptedPath = $null
$evidencePath = $null
$attemptPath = Join-Path $localDirectory "$requestedBackupId.attempt.json"
$completed = $false
$startedAt = [DateTime]::UtcNow

try {
    Invoke-CheckedProcess -FilePath "ssh.exe" -Arguments ($sshOptions + @($remoteTarget, "install -d -m 700 /root/arcigy-backups/bin /root/arcigy-backups/transfers")) -Attempts 3 | Out-Null
    Invoke-CheckedProcess -FilePath "scp.exe" -Arguments ($sshOptions + @($remoteSourceScript, "${remoteTarget}:$remoteInstallPath")) -Attempts 3 | Out-Null
    Invoke-CheckedProcess -FilePath "ssh.exe" -Arguments ($sshOptions + @($remoteTarget, "chmod 700 $remoteInstallPath")) -Attempts 3 | Out-Null
    $remoteOutput = Invoke-CheckedProcess -FilePath "ssh.exe" -Arguments ($sshOptions + @($remoteTarget, "$remoteInstallPath $requestedBackupId")) -Attempts 3

    foreach ($line in $remoteOutput) {
        $normalizedLine = ([string]$line).Trim()
        if ($normalizedLine -match '^archive_path=(/root/arcigy-backups/transfers/geotherm-odoo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\.tar\.gz)$') {
            $remoteArchivePath = $Matches[1]
        }
    }

    $result = @{}
    $allowedResultKeys = @(
        "backup_id", "archive_path", "archive_sha256", "archive_size_bytes",
        "database_size_bytes", "filestore_size_bytes", "source_app_service", "source_db_service"
    )
    foreach ($line in $remoteOutput) {
        $normalizedLine = ([string]$line).Trim()
        if ([string]::IsNullOrWhiteSpace($normalizedLine)) { continue }
        if ($normalizedLine -notmatch '^([a-z0-9_]+)=([^\r\n]+)$') {
            throw "Remote backup returned an invalid output line type=$($line.GetType().FullName) length=$($normalizedLine.Length)."
        }
        if ($Matches[1] -notin $allowedResultKeys) { throw "Remote backup returned an unsupported result key." }
        if ($result.ContainsKey($Matches[1])) { throw "Remote backup returned a duplicate result key." }
        $result[$Matches[1]] = $Matches[2]
    }
    foreach ($key in @("backup_id", "archive_path", "archive_sha256", "archive_size_bytes", "database_size_bytes", "filestore_size_bytes")) {
        if (-not $result.ContainsKey($key)) { throw "Remote backup result is missing $key." }
    }
    if ($result.backup_id -notmatch '^geotherm-odoo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$') { throw "Remote backup ID is invalid." }
    if ($result.backup_id -ne $requestedBackupId) { throw "Remote backup ID does not match the requested run." }
    if ($result.archive_path -ne "/root/arcigy-backups/transfers/$($result.backup_id).tar.gz") { throw "Remote archive path is outside the approved transfer directory." }
    if ($result.archive_sha256 -notmatch '^[0-9a-f]{64}$') { throw "Remote archive checksum is invalid." }

    $remoteArchivePath = $result.archive_path
    $rawLocalPath = Join-Path $localDirectory ".$($result.backup_id).raw.tar.gz"
    $encryptedPath = Join-Path $localDirectory "$($result.backup_id).tar.gz.p7m"
    $evidencePath = Join-Path $localDirectory "$($result.backup_id).evidence.json"
    if (Test-Path -LiteralPath $encryptedPath) { throw "Encrypted backup artifact already exists." }

    Invoke-CheckedProcess -FilePath "scp.exe" -Arguments ($sshOptions + @("${remoteTarget}:$remoteArchivePath", $rawLocalPath)) -Attempts 3 | Out-Null
    $localSha256 = (Get-FileHash -LiteralPath $rawLocalPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($localSha256 -ne $result.archive_sha256) { throw "Off-host transfer checksum mismatch." }
    & tar.exe -tzf $rawLocalPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Transferred backup archive failed structural validation." }

    Protect-ArchiveWithCertificate -SourcePath $rawLocalPath -DestinationPath $encryptedPath -Certificate $certificate
    Confirm-Aes256CmsCipher -EncryptedPath $encryptedPath
    Confirm-EncryptedRoundtrip -EncryptedPath $encryptedPath -ExpectedSha256 $localSha256
    $encryptedSha256 = (Get-FileHash -LiteralPath $encryptedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $encryptedSizeBytes = (Get-Item -LiteralPath $encryptedPath).Length

    Invoke-CheckedProcess -FilePath "ssh.exe" -Arguments ($sshOptions + @($remoteTarget, "$remoteInstallPath --cleanup $requestedBackupId")) -Attempts 3 | Out-Null
    $remoteArchivePath = $null

    $evidence = [ordered]@{
        schema_version = 1
        backup_id = $result.backup_id
        started_at_utc = $startedAt.ToString("o")
        completed_at_utc = [DateTime]::UtcNow.ToString("o")
        source_app_service = "srv-captain--geotherm-odoo"
        source_db_service = "srv-captain--geotherm-odoo-db"
        source_archive_sha256 = $localSha256
        source_archive_size_bytes = [int64]$result.archive_size_bytes
        database_size_bytes = [int64]$result.database_size_bytes
        filestore_size_bytes = [int64]$result.filestore_size_bytes
        encryption = "CMS EnvelopedData AES-256-CBC"
        certificate_thumbprint = $thumbprint
        encrypted_archive_sha256 = $encryptedSha256
        encrypted_archive_size_bytes = $encryptedSizeBytes
        off_host_path = $encryptedPath
        transfer_checksum_verified = $true
        structural_validation_passed = $true
        encryption_roundtrip_verified = $true
        remote_plaintext_removed = $true
        odoo_metric_write_performed = $false
        status = "success"
    }
    [IO.File]::WriteAllText($evidencePath, ($evidence | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    try {
        Write-BackupAttemptRecord -Path $attemptPath -BackupId $result.backup_id -StartedAt $startedAt -Status "success"
    }
    catch {
        Write-Warning "Backup attempt ledger requires operator review."
    }
    $completed = $true
    Write-Output "backup_id=$($result.backup_id)"
    Write-Output "status=success"
    Write-Output "artifact=$encryptedPath"
    Write-Output "evidence=$evidencePath"
}
catch {
    try {
        Write-BackupAttemptRecord -Path $attemptPath -BackupId $requestedBackupId -StartedAt $startedAt -Status "failed" -FailureClass (Get-BackupFailureClass -Message $_.Exception.Message)
    }
    catch {
        Write-Warning "Backup attempt ledger requires operator review."
    }
    throw
}
finally {
    if ($rawLocalPath -and (Test-Path -LiteralPath $rawLocalPath)) {
        Remove-Item -LiteralPath $rawLocalPath -Force
    }
    if (-not $completed -and $encryptedPath -and (Test-Path -LiteralPath $encryptedPath)) {
        Remove-Item -LiteralPath $encryptedPath -Force
    }
    if (-not $completed -and $evidencePath -and (Test-Path -LiteralPath $evidencePath)) {
        Remove-Item -LiteralPath $evidencePath -Force
    }
    if ($remoteArchivePath -and $remoteArchivePath -match '^/root/arcigy-backups/transfers/geotherm-odoo-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\.tar\.gz$') {
        try {
            Invoke-CheckedProcess -FilePath "ssh.exe" -Arguments ($sshOptions + @($remoteTarget, "$remoteInstallPath --cleanup $requestedBackupId")) -Attempts 3 | Out-Null
        }
        catch {
            Write-Warning "Remote temporary archive cleanup requires operator review."
        }
    }
}
