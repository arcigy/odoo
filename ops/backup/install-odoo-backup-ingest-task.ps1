[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoDirectory,
    [Parameter(Mandatory = $true)]
    [string]$EvidenceConfigPath,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$CredentialTarget = "Arcigy/GeothermOdoo/SaaSIntegrationBot",
    [string]$TaskName = "Geotherm Odoo Backup Evidence Ingest",
    [datetime]$DailyAt = [datetime]::Today.AddHours(4).AddMinutes(40),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoPath = [IO.Path]::GetFullPath($RepoDirectory)
$evidenceConfigFile = [IO.Path]::GetFullPath($EvidenceConfigPath)
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $repoPath -PathType Container)) { throw "Odoo repository directory is missing." }
if (-not (Test-Path -LiteralPath $evidenceConfigFile -PathType Leaf)) { throw "Odoo evidence configuration is missing." }
if ($CredentialTarget -notmatch '^Arcigy/GeothermOdoo/[A-Za-z0-9._-]{1,64}$') {
    throw "Credential target is outside the approved Geotherm Odoo namespace."
}
if ($configFile.StartsWith($repoPath.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Credential target configuration must stay outside the repository."
}
if ([IO.Path]::GetExtension($configFile) -ne ".env") {
    throw "Odoo ingest configuration must use the .env suffix."
}

$runnerPath = Join-Path $PSScriptRoot "odoo-backup-ingest-runner.ps1"
foreach ($requiredPath in @(
    $runnerPath,
    (Join-Path $repoPath "ops\backup\odoo-backup-evidence-runner.ps1"),
    (Join-Path $repoPath "integrations\saas_operational_sync.mjs"),
    (Join-Path $repoPath "integrations\saas_operational_sync.example.json")
)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required Odoo ingest file is missing."
    }
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and -not $Force) { throw "Scheduled task already exists; use -Force only after reviewing it." }

[IO.Directory]::CreateDirectory((Split-Path -Parent $configFile)) | Out-Null
$configLines = @(
    "ODOO_INGEST_EVIDENCE_CONFIG_PATH=$evidenceConfigFile",
    "ODOO_INGEST_CREDENTIAL_TARGET=$CredentialTarget"
)
[IO.File]::WriteAllLines($configFile, $configLines, [Text.UTF8Encoding]::new($false))
& icacls.exe $configFile /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect Odoo ingest configuration ACL." }

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
    Description = "Daily credential-backed live ingest of verified Geotherm Odoo backup evidence. Independent from Arcigy tasks."
}
if ($Force) { $registration.Force = $true }
Register-ScheduledTask @registration | Out-Null

Write-Output "task=$TaskName"
Write-Output "config=$configFile"
Write-Output "credential_target=$CredentialTarget"
Write-Output "mode=live"
