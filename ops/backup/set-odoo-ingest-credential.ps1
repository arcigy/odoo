[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Target,
    [Parameter(Mandatory = $true)]
    [string]$UserName,
    [Parameter(Mandatory = $true)]
    [Security.SecureString]$ApiKey,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Target -notmatch '^Arcigy/GeothermOdoo/[A-Za-z0-9._-]{1,64}$') {
    throw "Credential target is outside the approved Geotherm Odoo namespace."
}
if ($UserName -ne "saas_integration_bot") {
    throw "Credential user must be the dedicated SaaS integration bot."
}
if ($ApiKey.Length -ne 40) {
    throw "Odoo API key must have the expected length."
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

    [DllImport("Advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredWrite(ref Credential credential, UInt32 flags);

    [DllImport("Advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, UInt32 type, UInt32 flags, out IntPtr credential);

    [DllImport("Advapi32.dll", SetLastError = false)]
    public static extern void CredFree(IntPtr credential);
}
'@
}

$existingPointer = [IntPtr]::Zero
$exists = [ArcigyOdooCredentialNative]::CredRead($Target, 1, 0, [ref]$existingPointer)
if ($exists) {
    [ArcigyOdooCredentialNative]::CredFree($existingPointer)
    if (-not $Force) {
        throw "Credential already exists; use -Force only during an approved rotation."
    }
}
elseif ([Runtime.InteropServices.Marshal]::GetLastWin32Error() -ne 1168) {
    throw "Unable to inspect the Windows credential target."
}

$secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToCoTaskMemUnicode($ApiKey)
try {
    $credential = [ArcigyOdooCredentialNative+Credential]::new()
    $credential.Type = 1
    $credential.TargetName = $Target
    $credential.UserName = $UserName
    $credential.CredentialBlob = $secretPointer
    $credential.CredentialBlobSize = [uint32]($ApiKey.Length * 2)
    $credential.Persist = 2
    if (-not [ArcigyOdooCredentialNative]::CredWrite([ref]$credential, 0)) {
        throw "Windows Credential Manager rejected the Odoo API key."
    }
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeCoTaskMemUnicode($secretPointer)
}

Write-Output "status=stored"
Write-Output "target=$Target"
Write-Output "user=$UserName"
