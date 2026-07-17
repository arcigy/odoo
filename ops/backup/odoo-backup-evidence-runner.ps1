[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-EvidenceConfig {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Backup evidence config file does not exist."
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*$' -or $line -match '^\s*#') { continue }
        if ($line -notmatch '^\s*([A-Z0-9_]+)=(.*)$') {
            throw "Backup evidence config contains an invalid line."
        }
        $key = $Matches[1]
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        if ($values.ContainsKey($key)) { throw "Backup evidence config contains a duplicate key." }
        $values[$key] = $value
    }
    return $values
}

function Require-ConfigValue {
    param([hashtable]$Config, [string]$Key)
    if (-not $Config.ContainsKey($Key) -or [string]::IsNullOrWhiteSpace($Config[$Key])) {
        throw "Backup evidence config is missing required key $Key."
    }
    return [string]$Config[$Key]
}

function Invoke-CheckedProcess {
    param([string]$FilePath, [string[]]$Arguments)
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { throw "$FilePath failed with exit code $exitCode." }
    return @($output | ForEach-Object { [string]$_ })
}

$config = Read-EvidenceConfig -Path $ConfigPath
$allowedKeys = @(
    "ODOO_EVIDENCE_REPO_DIR",
    "ODOO_EVIDENCE_BACKUP_DIR",
    "ODOO_EVIDENCE_OUTPUT_PATH",
    "ODOO_EVIDENCE_NODE_PATH",
    "ODOO_EVIDENCE_ENVIRONMENT",
    "ODOO_EVIDENCE_APP_SERVICE",
    "ODOO_EVIDENCE_DB_SERVICE"
)
foreach ($key in $config.Keys) {
    if ($key -notin $allowedKeys) { throw "Backup evidence config contains unsupported key $key." }
}

$repoDirectory = [IO.Path]::GetFullPath((Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_REPO_DIR"))
$backupDirectory = [IO.Path]::GetFullPath((Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_BACKUP_DIR"))
$outputPath = [IO.Path]::GetFullPath((Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_OUTPUT_PATH"))
$nodePath = [IO.Path]::GetFullPath((Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_NODE_PATH"))
$environment = Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_ENVIRONMENT"
$appService = Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_APP_SERVICE"
$dbService = Require-ConfigValue -Config $config -Key "ODOO_EVIDENCE_DB_SERVICE"

if (-not (Test-Path -LiteralPath $repoDirectory -PathType Container)) { throw "Odoo repository directory is missing." }
if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) { throw "Odoo backup directory is missing." }
if (-not (Test-Path -LiteralPath $nodePath -PathType Leaf)) { throw "Configured Node.js executable is missing." }
if ($environment -notin @("develop", "main")) { throw "Backup evidence environment is invalid." }
if ($appService -notmatch '^srv-captain--[a-z0-9][a-z0-9-]{0,100}$') { throw "Backup evidence app service is invalid." }
if ($dbService -notmatch '^srv-captain--[a-z0-9][a-z0-9-]{0,100}$') { throw "Backup evidence database service is invalid." }

$backupPrefix = $backupDirectory.TrimEnd('\') + '\'
if (-not $outputPath.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must stay inside the approved backup directory."
}
if ([IO.Path]::GetExtension($outputPath) -ne ".json" -or -not $outputPath.EndsWith(".local.json", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must use the .local.json suffix."
}

$compilerPath = Join-Path $repoDirectory "integrations\saas_odoo_backup_rollup.mjs"
$syncPath = Join-Path $repoDirectory "integrations\saas_operational_sync.mjs"
$syncConfigPath = Join-Path $repoDirectory "integrations\saas_operational_sync.example.json"
foreach ($requiredPath in @($compilerPath, $syncPath, $syncConfigPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required Odoo evidence integration file is missing."
    }
}

$compileArguments = @(
    $compilerPath,
    "--input-dir=$backupDirectory",
    "--environment=$environment",
    "--app-service=$appService",
    "--db-service=$dbService",
    "--output=$outputPath"
)
Invoke-CheckedProcess -FilePath $nodePath -Arguments $compileArguments | Out-Null

$dryRunArguments = @(
    $syncPath,
    "--config=$syncConfigPath",
    "--evidence=$outputPath",
    "--dry-run"
)
$dryRunOutput = Invoke-CheckedProcess -FilePath $nodePath -Arguments $dryRunArguments
$dryRun = ($dryRunOutput -join "`n") | ConvertFrom-Json
if ($dryRun.ok -ne $true -or $dryRun.dryRun -ne $true -or $dryRun.model -ne "saas.backup.run") {
    throw "Odoo backup evidence dry-run returned an invalid result."
}
if ($dryRun.environment -ne $environment -or [int]$dryRun.records -le 0) {
    throw "Odoo backup evidence dry-run did not validate the expected records."
}

& icacls.exe $outputPath /inheritance:r /grant:r "$env:USERNAME`:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to protect generated backup evidence ACL." }

Write-Output "status=success"
Write-Output "environment=$environment"
Write-Output "records=$([int]$dryRun.records)"
Write-Output "mode=dry-run"

