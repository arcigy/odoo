[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SshHost,
    [string]$SshUser = "root",
    [Parameter(Mandatory = $true)]
    [string]$KnownHostsPath,
    [Parameter(Mandatory = $true)]
    [string]$LocalBackupDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$TaskName = "Geotherm Odoo Encrypted Off-host Backup",
    [datetime]$DailyAt = [datetime]::Today.AddHours(4).AddMinutes(15),
    [int64]$MinimumFreeBytes = 5368709120,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($SshHost -notmatch '^[A-Za-z0-9.-]+$') { throw "SSH host is invalid." }
if ($SshUser -notmatch '^[A-Za-z0-9_-]+$') { throw "SSH user is invalid." }
if (-not (Test-Path -LiteralPath $KnownHostsPath -PathType Leaf)) { throw "Known-hosts file is missing." }
if ($MinimumFreeBytes -lt 1073741824) { throw "Minimum free-space guard must be at least 1 GiB." }

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and -not $Force) { throw "Scheduled task already exists; use -Force only after reviewing it." }

$subject = "CN=Geotherm Odoo Encrypted Off-host Backup"
$certificate = Get-ChildItem Cert:\CurrentUser\My | Where-Object {
    $_.Subject -eq $subject -and $_.HasPrivateKey -and $_.NotAfter.ToUniversalTime() -gt [DateTime]::UtcNow.AddDays(30)
} | Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $certificate) {
    $certificateParameters = @{
        Type = "DocumentEncryptionCert"
        Subject = $subject
        CertStoreLocation = "Cert:\CurrentUser\My"
        KeyExportPolicy = "NonExportable"
        NotAfter = [DateTime]::UtcNow.AddYears(5)
    }
    $certificate = New-SelfSignedCertificate @certificateParameters
}
if (-not $certificate.HasPrivateKey) { throw "Backup certificate private key is unavailable." }

[IO.Directory]::CreateDirectory((Split-Path -Parent $ConfigPath)) | Out-Null
[IO.Directory]::CreateDirectory($LocalBackupDirectory) | Out-Null
$configLines = @(
    "ODOO_BACKUP_SSH_HOST=$SshHost",
    "ODOO_BACKUP_SSH_USER=$SshUser",
    "ODOO_BACKUP_KNOWN_HOSTS=$KnownHostsPath",
    "ODOO_BACKUP_LOCAL_DIR=$LocalBackupDirectory",
    "ODOO_BACKUP_CERT_THUMBPRINT=$($certificate.Thumbprint)",
    "ODOO_BACKUP_MIN_FREE_BYTES=$MinimumFreeBytes"
)
[IO.File]::WriteAllLines($ConfigPath, $configLines, [Text.UTF8Encoding]::new($false))
& icacls.exe $ConfigPath /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect backup config ACL." }
& icacls.exe $LocalBackupDirectory /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect backup destination ACL." }

$runnerPath = Join-Path $PSScriptRoot "odoo-backup-runner.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) { throw "Backup runner is missing." }
$actionParameters = @{
    Execute = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    Argument = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runnerPath`" -ConfigPath `"$ConfigPath`""
}
$action = New-ScheduledTaskAction @actionParameters
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settingsParameters = @{
    StartWhenAvailable = $true
    ExecutionTimeLimit = (New-TimeSpan -Hours 2)
    MultipleInstances = "IgnoreNew"
}
$settings = New-ScheduledTaskSettingsSet @settingsParameters
$registrationParameters = @{
    TaskName = $TaskName
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    Description = "Daily encrypted off-host backup of Geotherm Odoo PostgreSQL, filestore and service definitions. Independent from Arcigy kitchen_app backup tasks."
}
if ($Force) { $registrationParameters.Force = $true }
Register-ScheduledTask @registrationParameters | Out-Null

Write-Output "task=$TaskName"
Write-Output "certificate_thumbprint=$($certificate.Thumbprint)"
Write-Output "config=$ConfigPath"
Write-Output "destination=$LocalBackupDirectory"
