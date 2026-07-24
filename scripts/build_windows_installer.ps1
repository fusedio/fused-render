[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [string]$Iscc = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $RepoRoot "build\windows"
$StageDir = Join-Path $BuildDir "stage"
$PythonCache = Join-Path $BuildDir "python-cache"
$PythonRoot = Join-Path $StageDir "python"
$DistDir = Join-Path $RepoRoot "dist"
# FusedRenderPy: this experiment (experiment/python-supervisor,
# docs/PYTHON_SUPERVISOR_SPEC.md) ships its own exe name distinct from the
# shipping "FusedRender" product (Rust supervisor) so a test install never
# collides with a real one's files.
$ExeName = "FusedRenderPy.exe"

function Invoke-Native([string]$Command, [string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command @Arguments
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "$Command failed with exit code $exitCode"
    }
}

function Resolve-Tool([string]$Name, [string[]]$Candidates) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    throw "$Name was not found"
}

function Resolve-VcVars() {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere) {
        $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($vsPath) {
            $candidate = Join-Path $vsPath "VC\Auxiliary\Build\vcvars64.bat"
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }
    # Fall back to a direct BuildTools glob (this machine's VS 2019 BuildTools
    # layout, no vswhere available in PATH).
    $glob = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Microsoft Visual Studio\*\*\VC\Auxiliary\Build\vcvars64.bat" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($glob) {
        return $glob.FullName
    }
    return $null
}

function Build-Launcher([string]$OutputExe) {
    $source = Join-Path $RepoRoot "windows\launcher\launcher.c"
    # The .rc embeds fused-render.ico so Explorer, the taskbar, and the "Open
    # with" picker all show the app icon (the picker ignores DefaultIcon and
    # reads the exe's own icon). rc.exe finds the .ico via the /i include path.
    $rc = Join-Path $RepoRoot "windows\launcher\launcher.rc"
    $iconDir = Join-Path $RepoRoot "fused_render\assets"
    $res = Join-Path $BuildDir "launcher.res"
    $vcvars = Resolve-VcVars
    if ($vcvars) {
        Write-Host "Building launcher with cl.exe ($vcvars)"
        $cmd = "call `"$vcvars`" >nul 2>&1 && rc.exe /nologo /i `"$iconDir`" /fo `"$res`" `"$rc`" && cl.exe /nologo /W3 /O2 /DUNICODE /D_UNICODE `"$source`" `"$res`" /Fe:`"$OutputExe`" /link /SUBSYSTEM:WINDOWS user32.lib"
        cmd.exe /c $cmd
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OutputExe)) {
            throw "cl.exe failed to build the launcher"
        }
        return
    }
    # No MSVC found — fall back to a pinned zig cc (single ~80MB download,
    # no admin/VS install required). Not exercised on this machine (cl.exe is
    # present), kept for a from-scratch dev box.
    $zigVersion = "0.13.0"
    $zigDir = Join-Path $BuildDir "tools\zig"
    $zigExe = Join-Path $zigDir "zig-windows-x86_64-$zigVersion\zig.exe"
    if (-not (Test-Path -LiteralPath $zigExe)) {
        New-Item -ItemType Directory -Force -Path $zigDir | Out-Null
        $archive = Join-Path $zigDir "zig.zip"
        Invoke-WebRequest -Uri "https://ziglang.org/download/$zigVersion/zig-windows-x86_64-$zigVersion.zip" -OutFile $archive
        Expand-Archive -Path $archive -DestinationPath $zigDir -Force
    }
    Invoke-Native $zigExe @("rc", "/i", $iconDir, "/fo", $res, $rc)
    Invoke-Native $zigExe @("cc", "-municode", "-mwindows", "-O2", "-DUNICODE", "-D_UNICODE", $source, $res, "-o", $OutputExe, "-luser32")
}

$Uv = Resolve-Tool "uv" @()
$initPy = Get-Content (Join-Path $RepoRoot "fused_render\__init__.py") -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') {
    throw "project version not found"
}
$Version = $Matches[1]
$Branch = "$(& git -C $RepoRoot branch --show-current)".Trim()
if ($LASTEXITCODE -ne 0) {
    throw "could not resolve the Git branch"
}
if (-not $Branch) {
    # detached HEAD (a tag checkout in release CI)
    $Branch = "$(& git -C $RepoRoot rev-parse --short HEAD)".Trim()
}
Write-Host "Building FusedRender $Version from $Branch"

New-Item -ItemType Directory -Force -Path $BuildDir, $DistDir | Out-Null
Get-ChildItem -Path $DistDir -Filter "fused_render-*.whl" -ErrorAction SilentlyContinue |
    Remove-Item -Force
$env:FUSED_RENDER_BRANCH = ""
Invoke-Native $Uv @("build", "--wheel", "--out-dir", $DistDir, $RepoRoot)
$wheel = Get-ChildItem -Path $DistDir -Filter "fused_render-$Version-*.whl" |
    Select-Object -First 1
if (-not $wheel) {
    throw "wheel build did not produce fused-render $Version"
}

Remove-Item -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
Build-Launcher (Join-Path $StageDir $ExeName)

Remove-Item -LiteralPath $PythonCache -Recurse -Force -ErrorAction SilentlyContinue
Invoke-Native $Uv @(
    "python", "install", "3.12", "--install-dir", $PythonCache, "--no-bin", "--no-registry"
)
$runtime = Get-ChildItem -Path $PythonCache -Directory -Filter "cpython-3.12*-windows-*" |
    Select-Object -First 1
if (-not $runtime) {
    throw "uv did not install a CPython 3.12 Windows runtime"
}
Move-Item -LiteralPath $runtime.FullName -Destination $PythonRoot

$bundlePython = Join-Path $PythonRoot "python.exe"
$bundlePythonw = Join-Path $PythonRoot "pythonw.exe"
if (-not (Test-Path -LiteralPath $bundlePython) -or -not (Test-Path -LiteralPath $bundlePythonw)) {
    throw "staged Python runtime is incomplete"
}
Remove-Item -LiteralPath (Join-Path $PythonRoot "Lib\EXTERNALLY-MANAGED") -Force -ErrorAction SilentlyContinue
# A ._pth file disables Python's normal "prepend the invoked script's own
# directory to sys.path" behavior, which _child.py's worker subprocess relies
# on (it does a top-level `from _binding import ...`, invoked as
# `[sys.executable, .../fused_render/_child.py]` by executor.py, not as a
# package module) — so fused_render's own install dir must be listed
# explicitly, or every /api/run call through the built-in executor fails with
# ModuleNotFoundError in the packaged app.
@("python312.zip", "DLLs", "Lib", ".", "Lib\site-packages", "Lib\site-packages\fused_render", "import site") |
    Set-Content -Path (Join-Path $PythonRoot "python312._pth") -Encoding Ascii

Invoke-Native $Uv @("pip", "install", "--python", $bundlePython, "$($wheel.FullName)[bundled,fused,windows-desktop]")

# pywin32's win32*.pyd files load pythoncomXX.dll / pywintypesXX.dll from
# pywin32_system32\ at import time. That folder is normally added to the
# loader's search path by pywin32's postinstall script (which also does COM
# registration we don't need here and that needs admin rights); the portable
# equivalent for an embeddable-style runtime is simpler and OS-level: Windows
# always searches the directory of the running exe first, so copy the DLLs
# next to python.exe/pythonw.exe directly.
$pywin32System32 = Join-Path $PythonRoot "Lib\site-packages\pywin32_system32"
if (Test-Path -LiteralPath $pywin32System32) {
    Copy-Item -Path (Join-Path $pywin32System32 "*.dll") -Destination $PythonRoot -Force
}

$duckdbExtensions = Join-Path $PythonRoot "duckdb_extensions"
New-Item -ItemType Directory -Force -Path $duckdbExtensions | Out-Null
Invoke-Native $bundlePython @(
    "-I", "-c",
    "import duckdb, sys; con = duckdb.connect(config=dict(extension_directory=sys.argv[1])); [con.install_extension(name) for name in sys.argv[2:]]",
    $duckdbExtensions, "httpfs", "excel", "spatial"
)
Get-ChildItem -Path $PythonRoot -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force
Get-ChildItem -Path (Join-Path $PythonRoot "Scripts") -Filter "*.exe" -ErrorAction SilentlyContinue |
    Remove-Item -Force
Copy-Item -LiteralPath $Uv -Destination (Join-Path $PythonRoot "uv.exe") -Force

# rclone bundled next to uv.exe (the supervisor's child_environment points
# FUSED_RENDER_RCLONE_BIN here) so mounts work with zero user setup, matching
# the macOS DMG and the Linux AppImage. Pinned release, published-SHA256
# verified (same discipline as the pinned zig download above).
$RcloneVersion = "1.74.4"
$RcloneSha256 = "ef097ef9de37a57feb7d9f9c7afb34148ad3c65be8025f1d8f7f521554a701ea"
$rcloneZip = Join-Path $BuildDir "rclone-v$RcloneVersion-windows-amd64.zip"
if (-not (Test-Path -LiteralPath $rcloneZip)) {
    Invoke-WebRequest -Uri "https://downloads.rclone.org/v$RcloneVersion/rclone-v$RcloneVersion-windows-amd64.zip" -OutFile $rcloneZip
}
$actualSha = (Get-FileHash -LiteralPath $rcloneZip -Algorithm SHA256).Hash.ToLower()
if ($actualSha -ne $RcloneSha256) {
    throw "rclone zip SHA256 mismatch: expected $RcloneSha256, got $actualSha"
}
$rcloneExtract = Join-Path $BuildDir "rclone-extract"
Remove-Item -LiteralPath $rcloneExtract -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path $rcloneZip -DestinationPath $rcloneExtract -Force
$rcloneExe = Get-ChildItem -Path $rcloneExtract -Recurse -Filter "rclone.exe" |
    Select-Object -First 1
if (-not $rcloneExe) {
    throw "rclone.exe not found in the downloaded zip"
}
Copy-Item -LiteralPath $rcloneExe.FullName -Destination (Join-Path $PythonRoot "rclone.exe") -Force

$icons = Join-Path $StageDir "assets\icons"
New-Item -ItemType Directory -Force -Path $icons | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "fused_render\assets\fused-render.ico") -Destination $icons -Force
Copy-Item -Path (Join-Path $RepoRoot "fused_render\assets\file_icons\*.ico") -Destination $icons -Force

# Bundle learn.zip into assets\ (mirrors build_dmg.sh step 4e). ZipFile uses
# forward-slash entry names, which rclone's archive backend reads cleanly;
# child_environment points FUSED_RENDER_LEARN_ZIP here for ensure_learn_mount.
$LearnSrc = Join-Path $RepoRoot "learn"
if (-not (Test-Path -LiteralPath $LearnSrc -PathType Container)) {
    throw "learn/ content is missing — it is part of the app"
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$LearnZip = Join-Path $StageDir "assets\learn.zip"
[System.IO.Compression.ZipFile]::CreateFromDirectory($LearnSrc, $LearnZip)
# Smoke-test the archive backend with the just-bundled rclone (mirrors
# build_dmg.sh 4e): the exact binary and zip the app ships must list, so an
# rclone bump that drops :archive: fails the build, not the user's first mount.
Invoke-Native (Join-Path $PythonRoot "rclone.exe") @("lsf", ":archive:$LearnZip")
Invoke-Native $Uv @(
    "run", "python", (Join-Path $RepoRoot "scripts\windows\generate_installer_registry.py"),
    (Join-Path $StageDir "registry.iss")
)
Set-Content -Path (Join-Path $StageDir "payload.complete") -Encoding Ascii -Value $Version

Invoke-Native $bundlePython @(
    "-I", "-c",
    "import duckdb, fused_render, fused_render.cli, fused_render.supervisor.core, win32job, win32pipe, win32security, win32event, win32process, win32com.shell.shell, pystray, cryptography; print('bundle imports ok')"
)
Invoke-Native (Join-Path $PythonRoot "uv.exe") @("--version")
Invoke-Native (Join-Path $PythonRoot "rclone.exe") @("version")
$probe = Join-Path $env:TEMP "fused_render_installer_probe_$PID.py"
$request = Join-Path $env:TEMP "fused_render_installer_request_$PID.json"
try {
    Set-Content -Path $probe -Encoding Ascii -Value "import duckdb`ndef main():`n    return {'value': duckdb.sql('select 42').fetchone()[0]}"
    $payload = @{ path = $probe; params = @{} } | ConvertTo-Json -Compress
    Set-Content -Path $request -Encoding Ascii -NoNewline -Value $payload
    $child = Join-Path $PythonRoot "Lib\site-packages\fused_render\_child.py"
    $output = & cmd.exe /d /c "`"$bundlePython`" `"$child`" < `"$request`""
    if ($LASTEXITCODE -ne 0 -or $output -notmatch '"value"\s*:\s*42') {
        throw "staged _child.py smoke test failed: $output"
    }
} finally {
    Remove-Item -LiteralPath $probe, $request -Force -ErrorAction SilentlyContinue
}
Get-ChildItem -Path $PythonRoot -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

if ($SkipInstaller) {
    Write-Host "Staged bundle: $StageDir"
    exit 0
}
if (-not $Iscc) {
    $Iscc = Resolve-Tool "iscc" @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
}
$setupName = "FusedRenderPy-$Version-setup"
Invoke-Native $Iscc @(
    "/DAppVersion=$Version",
    "/DBundleDir=$StageDir",
    "/DOutputDir=$DistDir",
    "/DOutputBaseName=$setupName",
    (Join-Path $RepoRoot "scripts\windows\installer.iss")
)
$installer = Join-Path $DistDir "$setupName.exe"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Inno Setup did not produce $installer"
}
Write-Host "Installer: $installer"
