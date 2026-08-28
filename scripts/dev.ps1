<#
.SYNOPSIS
  Windows dev launcher for a git worktree, provably isolated from every other
  fused-render instance (dev, production, desktop app).

.DESCRIPTION
  The Unix scripts/dev.sh assumes .venv/bin/python, pgrep and 'ps -o lstart=',
  none of which exist under Git Bash on Windows, so this is its Windows
  counterpart. It runs THIS worktree's fused_render code (PYTHONPATH-shadow)
  against the primary checkout's already-built .venv, serving the in-repo
  templates live.

  Isolation is enforced, not hoped for:
    FUSED_RENDER_HOME            -> <worktree>\.dev-home   (state, venvs, mounts)
    FUSED_RENDER_CORE_TEMPLATES  -> <worktree>\fused_render\templates (no staging)
  Before the server is allowed to start, a child interpreter (same environment
  the server will inherit) must confirm home_dir() resolves inside .dev-home and
  the template override is active. If it does not, the launcher REFUSES to start
  rather than risk the shared ~/.fused-render (whose staged .core-templates a
  non-isolated server on the worktree's differing template digest would wipe).

  Port: defaults to 8799; if that is busy the next free port is used
  automatically, so many independent instances coexist. Never 1777 (the
  desktop/production port), which is rejected outright.

  Self-healing: a missing .venv is built with uv, a missing React shell is
  copied from the primary checkout (or built with npm), and .dev-home is
  created — so a fresh worktree starts without manual setup.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Port 8801

.EXAMPLE
  # Stop the dev server on a port (starts nothing; leaves other servers alone):
  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Stop -Port 8801
#>
param(
  [int]$Port = 8799,
  [string]$StartDir = "",
  [switch]$Stop
)
$ErrorActionPreference = "Stop"

function Test-PortFree([int]$p) {
  -not (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

if ($Port -eq 1777) {
  throw "Refusing port 1777 (desktop/production). Pick another, e.g. -Port 8799."
}

# Stop mode: kill only the server listening on this port, then exit. Never
# touches another port's server (e.g. :1777).
if ($Stop) {
  $pid_ = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty OwningProcess
  if ($pid_) {
    Write-Host "==> stopping the dev server on :$Port (pid $pid_)"
    taskkill /PID $pid_ /T /F | Out-Null
  } else {
    Write-Host "==> nothing listening on :$Port"
  }
  return
}

# Worktree root = this script's parent's parent (scripts\..).
$WT = Split-Path -Parent $PSScriptRoot
# Primary checkout that owns the real .git; its .venv carries the server deps.
$commonDir = (& git -C $WT rev-parse --git-common-dir).Trim()
if (-not [System.IO.Path]::IsPathRooted($commonDir)) {
  $commonDir = Join-Path $WT $commonDir
}
$MAIN = Split-Path -Parent (Resolve-Path $commonDir)

# Interpreter: prefer a worktree-local .venv, else the primary checkout's, else
# build one (the project uses uv/Astral).
$PY = Join-Path $WT ".venv\Scripts\python.exe"
if (-not (Test-Path $PY)) {
  $mainPy = Join-Path $MAIN ".venv\Scripts\python.exe"
  if (Test-Path $mainPy) { $PY = $mainPy }
}
if (-not (Test-Path $PY)) {
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "No .venv found and uv is not on PATH. Install uv, or create one: uv venv --python 3.12 .venv; uv pip install -e .[dev,fused,bundled]"
  }
  $wtVenv = Join-Path $WT ".venv"
  Write-Host "==> no .venv found; creating $wtVenv (uv venv --python 3.12 + editable install)"
  & uv venv --python 3.12 $wtVenv
  & uv pip install --python (Join-Path $wtVenv "Scripts\python.exe") -e "$WT[dev,fused,bundled]"
  $PY = Join-Path $wtVenv "Scripts\python.exe"
  if (-not (Test-Path $PY)) { throw "Failed to create a .venv at $wtVenv." }
}

# The React shell (gitignored). Reuse the primary checkout's build (this branch
# changes only templates, never the shell) so a full npm build is unnecessary.
$dist = Join-Path $WT "fused_render\static\shell-dist\index.html"
if (-not (Test-Path $dist)) {
  $mainDist = Join-Path $MAIN "fused_render\static\shell-dist"
  $wtStatic = Join-Path $WT "fused_render\static\shell-dist"
  if (Test-Path (Join-Path $mainDist "index.html")) {
    Write-Host "==> copying shell-dist from the primary checkout"
    New-Item -ItemType Directory -Force -Path (Split-Path $wtStatic) | Out-Null
    Copy-Item -Recurse -Force $mainDist $wtStatic
  } else {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
      throw "No shell-dist anywhere and npm is not on PATH. Install Node, or build it: cd frontend; npm install; npm run build"
    }
    Write-Host "==> no shell-dist found; building it (npm install + npm run build)"
    Push-Location (Join-Path $WT "frontend")
    try {
      & npm install
      if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit $LASTEXITCODE)." }
      & npm run build
      if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit $LASTEXITCODE)." }
    } finally { Pop-Location }
  }
}

$devHome = Join-Path $WT ".dev-home"
$override = Join-Path $WT "fused_render\templates"
if (-not $StartDir) { $StartDir = Join-Path $devHome "workspace" }
New-Item -ItemType Directory -Force -Path $devHome, $StartDir | Out-Null

# Isolation env. FUSED_RENDER_BRANCH="" pins the port logic to the explicit
# --port rather than a branch-derived default.
$env:PYTHONPATH = $WT
$env:FUSED_RENDER_HOME = $devHome
$env:FUSED_RENDER_CORE_TEMPLATES = $override
$env:FUSED_RENDER_BRANCH = ""

# HARD pre-flight: a child interpreter, inheriting exactly what the server will,
# must confirm the isolation actually took. This is what makes a wipe of the
# shared home impossible: if the env did not propagate (whatever the reason), the
# check fails and no server is started. It imports only home_dir + reads env; it
# never calls ensure_core_templates, so the check itself can never stage. The
# code goes through a temp file rather than -c: PowerShell mangles quotes in a
# multi-line string passed as one native-command argument.
$probeFile = Join-Path $devHome "_isolation_probe.py"
@'
import os, sys
from fused_render.shell.storage import home_dir
want_home = os.path.realpath(os.environ.get("FUSED_RENDER_HOME") or "__unset__")
override = (os.environ.get("FUSED_RENDER_CORE_TEMPLATES") or "").strip()
ok = os.path.realpath(home_dir()) == want_home and bool(override) and os.path.isdir(override)
sys.exit(0 if ok else 3)
'@ | Set-Content -Path $probeFile -Encoding utf8
& $PY $probeFile
$probeRC = $LASTEXITCODE
Remove-Item $probeFile -ErrorAction SilentlyContinue
if ($probeRC -ne 0) {
  throw "Isolation did not reach the server interpreter (home_dir not inside .dev-home, or template override inactive). REFUSING to start so the shared ~/.fused-render is never touched."
}

# Never fail on a busy port: walk up to the next free one so many independent
# dev servers coexist. Use `-Stop -Port <n>` to shut a specific one down.
if (-not (Test-PortFree $Port)) {
  $chosen = $null
  for ($i = 1; $i -le 50; $i++) {
    $probe = $Port + $i
    if ($probe -eq 1777) { continue }
    if (Test-PortFree $probe) { $chosen = $probe; break }
  }
  if (-not $chosen) { throw "No free port found in $Port..$($Port + 50)." }
  Write-Host "==> :$Port is busy; using the next free port :$chosen"
  $Port = $chosen
}

Write-Host "==> worktree : $WT"
Write-Host "==> python   : $PY"
Write-Host "==> home     : $devHome  (isolated)"
Write-Host "==> templates: $override  (served live, no staging)"
Write-Host "==> serving  : http://127.0.0.1:$Port/  (Ctrl-C, or: scripts\dev.ps1 -Stop -Port $Port)"
& $PY -m fused_render.cli serve --port $Port --no-browser --start-dir $StartDir
