# Removes the fused-render "Open with" associations (HKCU only). Reverses
# register_open_with.ps1.
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $Python) {
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "python not found at: $Python`nPass -Python <path>, or create the venv first."
    exit 1
}

& $Python -m fused_render.winopen --unregister
exit $LASTEXITCODE
