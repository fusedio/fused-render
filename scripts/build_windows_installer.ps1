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

$Uv = Resolve-Tool "uv" @()
$Cargo = Resolve-Tool "cargo" @((Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"))
$initPy = Get-Content (Join-Path $RepoRoot "fused_render\__init__.py") -Raw
if ($initPy -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') {
    throw "project version not found"
}
$Version = $Matches[1]
$Branch = (& git -C $RepoRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "could not resolve the Git branch"
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

Invoke-Native $Cargo @("build", "--manifest-path", (Join-Path $RepoRoot "windows\supervisor\Cargo.toml"), "--release")
$supervisor = Join-Path $RepoRoot "windows\supervisor\target\release\FusedRender.exe"
if (-not (Test-Path -LiteralPath $supervisor)) {
    throw "release supervisor was not produced"
}

Remove-Item -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PythonCache -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
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
@("python312.zip", "DLLs", "Lib", ".", "Lib\site-packages", "import site") |
    Set-Content -Path (Join-Path $PythonRoot "python312._pth") -Encoding Ascii

Invoke-Native $Uv @("pip", "install", "--python", $bundlePython, "$($wheel.FullName)[bundled,fused]")
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

Copy-Item -LiteralPath $supervisor -Destination (Join-Path $StageDir "FusedRender.exe") -Force
$icons = Join-Path $StageDir "assets\icons"
New-Item -ItemType Directory -Force -Path $icons | Out-Null
Copy-Item -LiteralPath (Join-Path $RepoRoot "fused_render\assets\fused-render.ico") -Destination $icons -Force
Copy-Item -Path (Join-Path $RepoRoot "fused_render\assets\file_icons\*.ico") -Destination $icons -Force
Invoke-Native $Uv @(
    "run", "python", (Join-Path $RepoRoot "scripts\windows\generate_installer_registry.py"),
    (Join-Path $StageDir "registry.iss")
)
Set-Content -Path (Join-Path $StageDir "payload.complete") -Encoding Ascii -Value $Version

Invoke-Native $bundlePython @("-I", "-c", "import duckdb, fused_render, fused_render.cli; print('bundle imports ok')")
Invoke-Native (Join-Path $PythonRoot "uv.exe") @("--version")
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
$setupName = "FusedRender-$Version-setup"
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
