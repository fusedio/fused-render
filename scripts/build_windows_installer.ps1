<#
.SYNOPSIS
  Build the fused-render Windows installer (the analog of scripts/build_dmg.sh).

.DESCRIPTION
  Stages a relocatable CPython 3.12 (via `uv python install`), installs the
  fused-render wheel[bundled,fused] into it, prunes non-relocatable pip launcher
  exes, smoke-tests through the bundled interpreter, and compiles a per-user
  Inno Setup installer to dist/FusedRender<suffix>-<version>-setup.exe.

  No freezing: the executor, deploy CLI, and Explorer opener all re-invoke
  sys.executable as a real interpreter, so the bundle ships a real Python tree
  (same posture as py2app on macOS). Unsigned — SmartScreen shows the
  "unrecognized app" prompt; signing is a later step.

.PARAMETER Quick
  Reuse an existing wheel and runtime if present (fast local iteration).

.PARAMETER SkipInstaller
  Stage and smoke-test the bundle but don't compile the installer (no iscc).

.PARAMETER Iscc
  Path to ISCC.exe. Defaults to PATH, then the standard install locations.
#>
[CmdletBinding()]
param(
    [switch]$Quick,
    [switch]$SkipInstaller,
    [string]$Iscc = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $RepoRoot "build"
$DistDir  = Join-Path $RepoRoot "dist"
$Staging  = Join-Path $BuildDir "win-installer"      # {bundle}\python\...
$PyRoot   = Join-Path $Staging "python"
$RuntimeCache = Join-Path $BuildDir "win-runtime"    # uv python install target

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Die($msg)  { Write-Error $msg; exit 1 }

# --- 0. identity: ref / suffix / version (mirror build_dmg.sh) ---------------
$env:PYTHONPATH = $RepoRoot
$ref    = (& python -m fused_render._branch ref).Trim()
$suffix = (& python -m fused_render._branch suffix).Trim()
$pyproject = Get-Content (Join-Path $RepoRoot "pyproject.toml") -Raw
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { Die "version not found in pyproject.toml" }
$version = $Matches[1]
$appName = "FusedRender$suffix"
$setupBase = "$appName-$version-setup"
Step "$appName $version -> $setupBase.exe"

New-Item -ItemType Directory -Force -Path $BuildDir, $DistDir | Out-Null

# --- 1. build the wheel once, with the branch ref baked in -------------------
$env:FUSED_RENDER_BRANCH = $ref
$wheel = Get-ChildItem -Path $DistDir -Filter "fused_render-$version-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Quick -and $wheel) {
    Step "reusing wheel $($wheel.Name)"
} else {
    Step "building wheel (frontend build runs in the hatch hook; needs Node)"
    Get-ChildItem -Path $DistDir -Filter "*.whl" -ErrorAction SilentlyContinue | Remove-Item -Force
    & uv build --wheel --out-dir $DistDir $RepoRoot
    if ($LASTEXITCODE -ne 0) { Die "wheel build failed" }
    $wheel = Get-ChildItem -Path $DistDir -Filter "*.whl" | Select-Object -First 1
}
if (-not $wheel) { Die "no wheel in $DistDir" }
$wheelPath = $wheel.FullName

# --- 2. relocatable CPython 3.12 into the staging bundle ---------------------
if (-not ($Quick -and (Test-Path (Join-Path $PyRoot "python.exe")))) {
    Step "installing relocatable CPython 3.12"
    if (Test-Path $PyRoot) { Remove-Item -Recurse -Force $PyRoot }
    if (Test-Path $RuntimeCache) { Remove-Item -Recurse -Force $RuntimeCache }
    & uv python install 3.12 --install-dir $RuntimeCache
    if ($LASTEXITCODE -ne 0) { Die "uv python install failed" }
    # uv lays the runtime under <dir>\cpython-3.12.*-windows-*\ — move its
    # contents up to <bundle>\python so paths match installer.iss.
    $cpython = Get-ChildItem -Path $RuntimeCache -Directory -Filter "cpython-3.12*" | Select-Object -First 1
    if (-not $cpython) { Die "no cpython-3.12* dir under $RuntimeCache" }
    New-Item -ItemType Directory -Force -Path (Split-Path $PyRoot) | Out-Null
    Move-Item -Path $cpython.FullName -Destination $PyRoot
}
$bundlePy  = Join-Path $PyRoot "python.exe"
$bundlePyw = Join-Path $PyRoot "pythonw.exe"
if (-not (Test-Path $bundlePy))  { Die "bundle python.exe missing at $bundlePy" }
if (-not (Test-Path $bundlePyw)) { Die "bundle pythonw.exe missing at $bundlePyw" }

# --- 3. install the wheel + extras into the bundle ---------------------------
Step "installing $($wheel.Name)[bundled,fused] into the bundle"
& uv pip install --python $bundlePy "$wheelPath[bundled,fused]"
if ($LASTEXITCODE -ne 0) { Die "uv pip install failed" }
# Force a fresh reinstall of fused-render itself so a reused bundle picks up
# this build's _baked_branch.py (same reasoning as build_dmg.sh).
& uv pip install --python $bundlePy --reinstall-package fused-render "$wheelPath"
if ($LASTEXITCODE -ne 0) { Die "wheel force-reinstall failed" }

# --- 4. prune ----------------------------------------------------------------
Step "pruning __pycache__ and non-relocatable pip launcher exes"
Get-ChildItem -Path $PyRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# pip-generated Scripts\*.exe bake the build machine's python path (pip #13162)
# and are dead after relocation; winopen._build_command falls back to
# `pythonw -m fused_render.winopen` when they're absent.
$scripts = Join-Path $PyRoot "Scripts"
if (Test-Path $scripts) {
    Get-ChildItem -Path $scripts -Filter "*.exe" -ErrorAction SilentlyContinue | Remove-Item -Force
}

# --- 5. smoke tests through the BUNDLE interpreter (mirror build_dmg.sh) ------
Step "smoke: import + duckdb via _child.py + server boot"
& $bundlePy -c "import fused_render, fused_render.winopen, fused_render.executor, fused_render._child; print('imports ok')"
if ($LASTEXITCODE -ne 0) { Die "bundle import smoke failed" }
& $bundlePy -c "import duckdb; assert duckdb.sql('select 42').fetchall()[0][0] == 42; print('duckdb ok')"
if ($LASTEXITCODE -ne 0) { Die "duckdb smoke failed" }
$sizeGB = [math]::Round((Get-ChildItem -Path $PyRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1GB, 2)
Step "bundle staged: $sizeGB GB at $PyRoot"

# --- 6. compile the installer ------------------------------------------------
if ($SkipInstaller) { Step "skipping installer compile (-SkipInstaller)"; exit 0 }

if (-not $Iscc) {
    $cands = @(
        (Get-Command iscc -ErrorAction SilentlyContinue).Source,
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $cands | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc) { Die "ISCC.exe not found; install Inno Setup or pass -Iscc <path>" }

Step "compiling installer with $Iscc"
$iss = Join-Path $PSScriptRoot "windows\installer.iss"
& $Iscc `
    "/DAppVersion=$version" `
    "/DAppNameSuffix=$suffix" `
    "/DBundleDir=$Staging" `
    "/DOutputDir=$DistDir" `
    "/DOutputBaseName=$setupBase" `
    $iss
if ($LASTEXITCODE -ne 0) { Die "iscc compile failed" }

$out = Join-Path $DistDir "$setupBase.exe"
if (-not (Test-Path $out)) { Die "expected installer not produced: $out" }
$outMB = [math]::Round((Get-Item $out).Length / 1MB, 1)
Step "done: $out ($outMB MB)"
