# Reverses register_open_with.ps1: removes the ProgID, the all-files context
# verb, and only the FusedRender.file value from each extension's
# OpenWithProgids (leaves the rest of that key, and any other default
# handler, untouched).
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ProgId = "FusedRender.file"

$ProgIdKey = "HKCU:\Software\Classes\$ProgId"
if (Test-Path -Path $ProgIdKey) {
    Remove-Item -Path $ProgIdKey -Recurse -Force
}

$VerbKey = "HKCU:\Software\Classes\*\shell\FusedRender"
if (Test-Path -LiteralPath $VerbKey) {
    Remove-Item -LiteralPath $VerbKey -Recurse -Force
}

$AppKey = "HKCU:\Software\Classes\Applications\fused-render-open.exe"
if (Test-Path -Path $AppKey) {
    Remove-Item -Path $AppKey -Recurse -Force
}

$RegistryJsonPath = Join-Path $RepoRoot "fused_render\templates\registry.json"
$RegistryData = Get-Content -LiteralPath $RegistryJsonPath -Raw | ConvertFrom-Json
$NotExtensions = @(".zarr/", ".zgroup", ".zattrs", ".zmetadata")
$Extensions = $RegistryData.PSObject.Properties.Name |
    Where-Object { $NotExtensions -notcontains $_ } |
    Sort-Object

foreach ($ext in $Extensions) {
    $OpenWithKey = "HKCU:\Software\Classes\$ext\OpenWithProgids"
    if (Test-Path -Path $OpenWithKey) {
        $existing = Get-ItemProperty -Path $OpenWithKey -Name $ProgId -ErrorAction SilentlyContinue
        if ($existing) {
            Remove-ItemProperty -Path $OpenWithKey -Name $ProgId
        }
    }
}

Add-Type -Namespace Win32 -Name Shell -MemberDefinition '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int wEventId, int uFlags, IntPtr dwItem1, IntPtr dwItem2);'
[Win32.Shell]::SHChangeNotify(0x08000000, 0x1000, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Output "Unregistered fused-render (ProgID, context menu verb, OpenWithProgids entries)."
