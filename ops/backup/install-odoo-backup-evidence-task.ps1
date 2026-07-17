[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoDirectory,
    [Parameter(Mandatory = $true)]
    [string]$BackupDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$OutputPath,
    [string]$Environment = "main",
    [string]$AppService = "srv-captain--geotherm-odoo",
    [string]$DbService = "srv-captain--geotherm-odoo-db",
    [string]$TaskName = "Geotherm Odoo Backup Evidence Compile",
    [datetime]$DailyAt = [datetime]::Today.AddHours(4).AddMinutes(30),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoPath = [IO.Path]::GetFullPath($RepoDirectory)
$backupPath = [IO.Path]::GetFullPath($BackupDirectory)
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $backupPath "odoo-operational-backup-$Environment.local.json"
}
$outputFile = [IO.Path]::GetFullPath($OutputPath)

if (-not (Test-Path -LiteralPath $repoPath -PathType Container)) { throw "Odoo repository directory is missing." }
if (-not (Test-Path -LiteralPath $backupPath -PathType Container)) { throw "Odoo backup directory is missing." }
if ($Environment -notin @("develop", "main")) { throw "Backup evidence environment is invalid." }
if ($AppService -notmatch '^srv-captain--[a-z0-9][a-z0-9-]{0,100}$') { throw "Backup evidence app service is invalid." }
if ($DbService -notmatch '^srv-captain--[a-z0-9][a-z0-9-]{0,100}$') { throw "Backup evidence database service is invalid." }
$backupPrefix = $backupPath.TrimEnd('\') + '\'
if (-not $outputFile.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must stay inside the approved backup directory."
}
if (-not $outputFile.EndsWith(".local.json", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must use the .local.json suffix."
}

$node = Get-Command node.exe -ErrorAction Stop
$runnerPath = Join-Path $PSScriptRoot "odoo-backup-evidence-runner.ps1"
if (-not (Test-Path -LiteralPath $runnerPath -PathType Leaf)) { throw "Backup evidence runner is missing." }
foreach ($requiredPath in @(
    (Join-Path $repoPath "integrations\saas_odoo_backup_rollup.mjs"),
    (Join-Path $repoPath "integrations\saas_operational_sync.mjs"),
    (Join-Path $repoPath "integrations\saas_operational_sync.example.json")
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required Odoo evidence integration file is missing."
    }
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and -not $Force) { throw "Scheduled task already exists; use -Force only after reviewing it." }

[IO.Directory]::CreateDirectory((Split-Path -Parent $configFile)) | Out-Null
$configLines = @(
    "ODOO_EVIDENCE_REPO_DIR=$repoPath",
    "ODOO_EVIDENCE_BACKUP_DIR=$backupPath",
    "ODOO_EVIDENCE_OUTPUT_PATH=$outputFile",
    "ODOO_EVIDENCE_NODE_PATH=$($node.Source)",
    "ODOO_EVIDENCE_ENVIRONMENT=$Environment",
    "ODOO_EVIDENCE_APP_SERVICE=$AppService",
    "ODOO_EVIDENCE_DB_SERVICE=$DbService"
)
[IO.File]::WriteAllLines($configFile, $configLines, [Text.UTF8Encoding]::new($false))
& icacls.exe $configFile /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect backup evidence config ACL." }
& icacls.exe $backupPath /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect backup evidence destination ACL." }

$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runnerPath`" -ConfigPath `"$configFile`""
$trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew
$registration = @{
    TaskName = $TaskName
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    Description = "Daily secret-free validation and Odoo dry-run compilation of encrypted Geotherm Odoo backup evidence. Does not write Odoo or alter Arcigy tasks."
}
if ($Force) { $registration.Force = $true }
Register-ScheduledTask @registration | Out-Null

Write-Output "task=$TaskName"
Write-Output "config=$configFile"
Write-Output "output=$outputFile"
Write-Output "mode=dry-run"

