# Reverses register_open_with.ps1: removes the ProgID, all-files verb, the
# Applications key, and only the FusedRender.file OpenWithProgids values.
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
