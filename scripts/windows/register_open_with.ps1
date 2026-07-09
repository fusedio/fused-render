# Registers fused-render in Explorer's "Open with" list and right-click menu
# (HKCU only, no admin). Undo with unregister_open_with.ps1.
param(
    [int]$Port,
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

$WinopenArgs = @("-m", "fused_render.winopen", "--register")
if ($Port) {
    $WinopenArgs += @("--port", "$Port")
}

& $Python $WinopenArgs
exit $LASTEXITCODE
