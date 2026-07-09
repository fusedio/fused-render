# Registers fused-render in Explorer's "Open with" list and right-click menu.
# HKCU\Software\Classes only - no admin, per-user. Run unregister_open_with.ps1
# to undo. Windows still requires picking the app once via "Open with" before
# it can become a default for an extension - this script cannot skip that step.
param(
    [int]$Port,
    [string]$Launcher
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# The gui-scripts entry point exe, not pythonw.exe: the Open With dialog names
# entries after the command's executable, and pythonw.exe displays as "Python".
if (-not $Launcher) {
    $Launcher = Join-Path $RepoRoot ".venv\Scripts\fused-render-open.exe"
}
if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Error "launcher not found at: $Launcher`nPass -Launcher <path>, or install first (uv pip install -e .)."
    exit 1
}

$WinopenArgs = ""
if ($Port) {
    $WinopenArgs = " --port $Port"
}
$Command = '"' + $Launcher + '"' + $WinopenArgs + ' "%1"'

# --- ProgID ------------------------------------------------------------------
$ProgId = "FusedRender.file"
$ProgIdKey = "HKCU:\Software\Classes\$ProgId"
New-Item -Path $ProgIdKey -Force | Out-Null
Set-ItemProperty -Path $ProgIdKey -Name "(Default)" -Value "fused-render"
New-ItemProperty -Path $ProgIdKey -Name "FriendlyTypeName" -Value "fused-render" -PropertyType String -Force | Out-Null

$OpenCmdKey = "$ProgIdKey\shell\open\command"
New-Item -Path $OpenCmdKey -Force | Out-Null
Set-ItemProperty -Path $OpenCmdKey -Name "(Default)" -Value $Command

# --- Application identity: the Open With dialog resolves an entry's display
# name via Applications\<exe> FriendlyAppName (else the exe's version info,
# which for an entry-point launcher is blank). -------------------------------
$AppKey = "HKCU:\Software\Classes\Applications\fused-render-open.exe"
New-Item -Path $AppKey -Force | Out-Null
Set-ItemProperty -Path $AppKey -Name "FriendlyAppName" -Value "fused-render"
$AppCmdKey = "$AppKey\shell\open\command"
New-Item -Path $AppCmdKey -Force | Out-Null
Set-ItemProperty -Path $AppCmdKey -Name "(Default)" -Value $Command

# --- Extensions, derived from the built-in registry (fused_render/templates/
# registry.json): every key there except the zarr directory marker and its
# member-file names, which aren't real extensions Explorer can match on. -----
$RegistryJsonPath = Join-Path $RepoRoot "fused_render\templates\registry.json"
$RegistryData = Get-Content -LiteralPath $RegistryJsonPath -Raw | ConvertFrom-Json
$NotExtensions = @(".zarr/", ".zgroup", ".zattrs", ".zmetadata")
$Extensions = $RegistryData.PSObject.Properties.Name |
    Where-Object { $NotExtensions -notcontains $_ } |
    Sort-Object

foreach ($ext in $Extensions) {
    $OpenWithKey = "HKCU:\Software\Classes\$ext\OpenWithProgids"
    New-Item -Path $OpenWithKey -Force | Out-Null
    New-ItemProperty -Path $OpenWithKey -Name $ProgId -Value "" -PropertyType String -Force | Out-Null
}

# --- All-files context-menu verb (Explorer "Show more options" on Win11) ----
# Via the .NET API, not New-Item: 5.1's New-Item has no -LiteralPath, and its
# -Path would glob-expand the "*" against every existing key under Classes.
$VerbKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Software\Classes\*\shell\FusedRender")
$VerbKey.SetValue("", "Open with fused-render")
$VerbCmdKey = $VerbKey.CreateSubKey("command")
$VerbCmdKey.SetValue("", $Command)
$VerbCmdKey.Close()
$VerbKey.Close()

# Explorer caches associations; without this broadcast the new entry doesn't
# appear in "Open with" until the next Explorer restart.
Add-Type -Namespace Win32 -Name Shell -MemberDefinition '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int wEventId, int uFlags, IntPtr dwItem1, IntPtr dwItem2);'
[Win32.Shell]::SHChangeNotify(0x08000000, 0x1000, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Output "Registered fused-render:"
Write-Output "  command: $Command"
Write-Output "  extensions: $($Extensions -join ', ')"
Write-Output "  context menu: right-click any file -> Show more options -> Open with fused-render"
Write-Output ""
Write-Output "Windows requires choosing fused-render once via a file's 'Open with' dialog"
Write-Output "before it can become the default handler for that extension."
