[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-StrictConfig {
    param([string]$Path, [string[]]$AllowedKeys)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required ingest configuration file does not exist."
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*$' -or $line -match '^\s*#') { continue }
        if ($line -notmatch '^\s*([A-Z0-9_]+)=(.*)$') {
            throw "Ingest configuration contains an invalid line."
        }
        $key = $Matches[1]
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        if ($key -notin $AllowedKeys) { throw "Ingest configuration contains unsupported key $key." }
        if ($values.ContainsKey($key)) { throw "Ingest configuration contains duplicate key $key." }
        $values[$key] = $value
    }
    return $values
}

function Require-ConfigValue {
    param([hashtable]$Config, [string]$Key)
    if (-not $Config.ContainsKey($Key) -or [string]::IsNullOrWhiteSpace($Config[$Key])) {
        throw "Ingest configuration is missing required key $Key."
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

if (-not ("ArcigyOdooCredentialNative" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class ArcigyOdooCredentialNative
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Credential
    {
        public UInt32 Flags;
        public UInt32 Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public UInt32 CredentialBlobSize;
        public IntPtr CredentialBlob;
        public UInt32 Persist;
        public UInt32 AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("Advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, UInt32 type, UInt32 flags, out IntPtr credential);

    [DllImport("Advapi32.dll", SetLastError = false)]
    public static extern void CredFree(IntPtr credential);
}
'@
}

function Read-WindowsCredential {
    param([string]$Target)
    $credentialPointer = [IntPtr]::Zero
    if (-not [ArcigyOdooCredentialNative]::CredRead($Target, 1, 0, [ref]$credentialPointer)) {
        throw "Approved Odoo ingest credential is missing from Windows Credential Manager."
    }
    try {
        $credential = [Runtime.InteropServices.Marshal]::PtrToStructure(
            $credentialPointer,
            [type][ArcigyOdooCredentialNative+Credential]
        )
        if ($credential.UserName -ne "saas_integration_bot") {
            throw "Stored credential does not belong to the dedicated SaaS integration bot."
        }
        if ($credential.CredentialBlobSize -ne 80 -or $credential.CredentialBlob -eq [IntPtr]::Zero) {
            throw "Stored Odoo API key has an invalid shape."
        }
        $secret = [Runtime.InteropServices.Marshal]::PtrToStringUni(
            $credential.CredentialBlob,
            [int]($credential.CredentialBlobSize / 2)
        )
        if ($secret -notmatch '^[0-9a-f]{40}$') {
            throw "Stored Odoo API key has an invalid shape."
        }
        return $secret
    }
    finally {
        [ArcigyOdooCredentialNative]::CredFree($credentialPointer)
    }
}

$ingestConfig = Read-StrictConfig -Path $ConfigPath -AllowedKeys @(
    "ODOO_INGEST_EVIDENCE_CONFIG_PATH",
    "ODOO_INGEST_CREDENTIAL_TARGET"
)
$evidenceConfigPath = [IO.Path]::GetFullPath((Require-ConfigValue -Config $ingestConfig -Key "ODOO_INGEST_EVIDENCE_CONFIG_PATH"))
$credentialTarget = Require-ConfigValue -Config $ingestConfig -Key "ODOO_INGEST_CREDENTIAL_TARGET"
if ($credentialTarget -notmatch '^Arcigy/GeothermOdoo/[A-Za-z0-9._-]{1,64}$') {
    throw "Credential target is outside the approved Geotherm Odoo namespace."
}

$evidenceConfig = Read-StrictConfig -Path $evidenceConfigPath -AllowedKeys @(
    "ODOO_EVIDENCE_REPO_DIR",
    "ODOO_EVIDENCE_BACKUP_DIR",
    "ODOO_EVIDENCE_OUTPUT_PATH",
    "ODOO_EVIDENCE_NODE_PATH",
    "ODOO_EVIDENCE_ENVIRONMENT",
    "ODOO_EVIDENCE_APP_SERVICE",
    "ODOO_EVIDENCE_DB_SERVICE"
)
$repoDirectory = [IO.Path]::GetFullPath((Require-ConfigValue -Config $evidenceConfig -Key "ODOO_EVIDENCE_REPO_DIR"))
$backupDirectory = [IO.Path]::GetFullPath((Require-ConfigValue -Config $evidenceConfig -Key "ODOO_EVIDENCE_BACKUP_DIR"))
$outputPath = [IO.Path]::GetFullPath((Require-ConfigValue -Config $evidenceConfig -Key "ODOO_EVIDENCE_OUTPUT_PATH"))
$nodePath = [IO.Path]::GetFullPath((Require-ConfigValue -Config $evidenceConfig -Key "ODOO_EVIDENCE_NODE_PATH"))
$environment = Require-ConfigValue -Config $evidenceConfig -Key "ODOO_EVIDENCE_ENVIRONMENT"
if ($environment -ne "main") { throw "Odoo control-plane backup evidence must use the Main environment." }
if (-not (Test-Path -LiteralPath $repoDirectory -PathType Container)) { throw "Odoo repository directory is missing." }
if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) { throw "Odoo backup directory is missing." }
if (-not (Test-Path -LiteralPath $nodePath -PathType Leaf)) { throw "Configured Node.js executable is missing." }
$backupPrefix = $backupDirectory.TrimEnd('\') + '\'
if (-not $outputPath.StartsWith($backupPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must stay inside the approved backup directory."
}
if (-not $outputPath.EndsWith(".local.json", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup evidence output must use the .local.json suffix."
}

$evidenceRunner = Join-Path $repoDirectory "ops\backup\odoo-backup-evidence-runner.ps1"
$syncPath = Join-Path $repoDirectory "integrations\saas_operational_sync.mjs"
$syncConfigPath = Join-Path $repoDirectory "integrations\saas_operational_sync.example.json"
foreach ($requiredPath in @($evidenceRunner, $syncPath, $syncConfigPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required Odoo ingest file is missing."
    }
}

$compileStartedAt = [datetime]::UtcNow
$evidenceOutput = & $evidenceRunner -ConfigPath $evidenceConfigPath 2>&1
$recordLine = @($evidenceOutput | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^records=[1-9][0-9]*$' })
if ($recordLine.Count -ne 1) { throw "Dry-run evidence did not report one valid record count." }
$expectedRecords = [int]($recordLine[0].Split('=', 2)[1])
$evidenceFile = Get-Item -LiteralPath $outputPath
if ($evidenceFile.LastWriteTimeUtc -lt $compileStartedAt.AddSeconds(-5)) {
    throw "Backup evidence was not freshly regenerated before live ingest."
}

if (-not [string]::IsNullOrWhiteSpace($env:ARCIGY_ODOO_API_KEY)) {
    throw "ARCIGY_ODOO_API_KEY is already set; refusing to override process state."
}

$apiKey = $null
try {
    $apiKey = Read-WindowsCredential -Target $credentialTarget
    $env:ARCIGY_ODOO_API_KEY = $apiKey
    $liveOutput = Invoke-CheckedProcess -FilePath $nodePath -Arguments @(
        $syncPath,
        "--config=$syncConfigPath",
        "--evidence=$outputPath"
    )
    $liveResult = ($liveOutput -join "`n") | ConvertFrom-Json
    if ($liveResult.ok -ne $true -or $liveResult.dryRun -ne $false) {
        throw "Odoo backup evidence live ingest returned an invalid result."
    }
    if ($liveResult.model -ne "saas.backup.run" -or $liveResult.environment -ne "main") {
        throw "Odoo backup evidence live ingest targeted an unexpected model or environment."
    }
    if ([int]$liveResult.records -ne $expectedRecords) {
        throw "Odoo backup evidence live ingest record count differs from the dry-run."
    }
}
finally {
    Remove-Item Env:ARCIGY_ODOO_API_KEY -ErrorAction SilentlyContinue
    $apiKey = $null
}

Write-Output "status=success"
Write-Output "environment=main"
Write-Output "records=$expectedRecords"
Write-Output "mode=live"
