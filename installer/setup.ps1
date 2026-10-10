<#
.SYNOPSIS
  Sets up UnderCover on this PC: picks the right PyTorch for the GPU, installs Python + packages,
  fetches ffmpeg and Deno, writes the GPU settings, runs a self-test, and makes a launcher.

.DESCRIPTION
  Everything goes inside -InstallDir (default: the folder above this script), so uninstalling is
  deleting that folder. Nothing system-wide is changed: no PATH edits, no registry, no admin.
  The app code (undercover\, config.toml, check_env.py) must already be in -InstallDir; the
  Inno Setup installer copies it there before running this.

  Re-running is safe: finished steps are skipped (use -Force to redo the Python packages).

.PARAMETER Backend
  auto (default) | rocm | cuda128 | cuda126 | cpu. auto runs detect_gpu.ps1.

.PARAMETER DryRun
  Print every step and command without downloading or installing anything.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup.ps1 -DryRun
#>
param(
    [string]$InstallDir = (Split-Path $PSScriptRoot -Parent),
    [ValidateSet('auto', 'rocm', 'cuda128', 'cuda126', 'cpu')][string]$Backend = 'auto',
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Pause      # keep the window open at the end (the installer runs setup in its own window)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with the progress bar

# Pinned tool versions. Bump deliberately, then re-test.
$UvVersion   = '0.12.18'   # 2026-09-22; pick releases at least 2 weeks old
$DenoVersion = '2.9.7'
$PythonVer   = '3.12'
$FfmpegUrl   = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

$Req     = Join-Path $PSScriptRoot 'requirements'
$Tools   = Join-Path $InstallDir 'tools'
$Venv    = Join-Path $InstallDir '.venv'
$Py      = Join-Path $Venv 'Scripts\python.exe'
$Uv      = Join-Path $Tools 'uv\uv.exe'
$LogFile = Join-Path $InstallDir 'logs\setup.log'

function Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan; Log "STEP $msg" }
function Info([string]$msg) { Write-Host "    $msg"; Log $msg }
function Log([string]$msg) {
    if ($DryRun) { return }
    New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value ("{0:u}  {1}" -f (Get-Date), $msg)
}
function Fail([string]$msg) {
    Write-Host ""; Write-Host "Setup failed: $msg" -ForegroundColor Red
    Write-Host "Details are in $LogFile"; Log "FAIL $msg"
    if ($Pause) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

# Run a native command; in -DryRun just print it.
function Run([string]$exe, [string[]]$argv) {
    $shown = ($argv | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    if ($DryRun) { Write-Host "    [dry-run] $exe $shown" -ForegroundColor DarkGray; return }
    Log "RUN $exe $shown"
    # Windows PowerShell 5.1 turns any stderr line into a terminating error under 'Stop', and uv
    # writes its progress to stderr, so relax it for the call and rely on the exit code.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $exe @argv 2>&1 | ForEach-Object { $line = "$_"; Log "    $line"; $line } | Out-Host }
    finally { $ErrorActionPreference = $old }
    if ($LASTEXITCODE -ne 0) { Fail "'$(Split-Path $exe -Leaf) $($argv[0])' exited with code $LASTEXITCODE" }
}

function Fetch([string]$url, [string]$dest) {
    if ($DryRun) { Write-Host "    [dry-run] download $url -> $dest" -ForegroundColor DarkGray; return }
    Log "GET $url"
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    for ($i = 1; $i -le 3; $i++) {
        try { Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing; return }
        catch { if ($i -eq 3) { Fail "couldn't download $url ($($_.Exception.Message))" }; Start-Sleep -Seconds (2 * $i) }
    }
}

function Unzip([string]$zip, [string]$dest) {
    if ($DryRun) { Write-Host "    [dry-run] unzip $zip -> $dest" -ForegroundColor DarkGray; return }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Expand-Archive -Path $zip -DestinationPath $dest -Force
}

# ---------------------------------------------------------------------------------------------
Write-Host "UnderCover setup" -ForegroundColor White
Write-Host "Install folder: $InstallDir"
if ($DryRun) { Write-Host "DRY RUN: nothing will be downloaded or installed." -ForegroundColor Yellow }
if (-not (Test-Path (Join-Path $InstallDir 'undercover\server.py'))) {
    if ($DryRun) { Write-Host "    (note: app code not found in $InstallDir; a real run needs it)" -ForegroundColor Yellow }
    else { Fail "the app code isn't in $InstallDir (expected undercover\server.py)" }
}
# Never touch a .venv this script didn't make (e.g. the hand-built dev venv in D:\UnderCover).
$Marker = Join-Path $InstallDir '.venv\undercover-setup.txt'
if ((Test-Path $Venv) -and -not (Test-Path $Marker)) {
    if ($DryRun) { Write-Host "    (note: $Venv wasn't made by setup; a real run would stop here)" -ForegroundColor Yellow }
    else { Fail "$Venv already exists and wasn't made by this setup, so it won't be changed. Pick another -InstallDir." }
}

# 1. GPU ----------------------------------------------------------------------------------------
Step "Checking your graphics card"
$gpu = $null
if ($Backend -eq 'auto') {
    $gpu = & (Join-Path $PSScriptRoot 'detect_gpu.ps1') | Out-String | ConvertFrom-Json
    $Backend = $gpu.backend
    Info "Found: $($gpu.gpu)$(if ($gpu.vram_gb) { " ($($gpu.vram_gb) GB)" })"
    Info "Using: $Backend  ($($gpu.reason))"
} else {
    Info "Using: $Backend (chosen on the command line)"
}
if ($Backend -eq 'cpu') {
    Write-Host "    No supported GPU, so UnderCover will run on the processor. Covers will take minutes" -ForegroundColor Yellow
    Write-Host "    instead of seconds, and training voices isn't practical." -ForegroundColor Yellow
}

# 2. uv (Python + package manager, one exe) -----------------------------------------------------
Step "Getting the Python installer (uv $UvVersion)"
if ((Test-Path $Uv) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP "uv-$UvVersion.zip"
    Fetch "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
    Unzip $zip (Join-Path $Tools 'uv')
}
# Keep uv's Python downloads and cache inside the install folder.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR          = Join-Path $InstallDir 'cache\uv'
$env:UV_NO_CONFIG          = '1'      # ignore any uv.toml the user may have elsewhere

# 3. Python + venv -----------------------------------------------------------------------------
Step "Installing Python $PythonVer"
Run $Uv @('python', 'install', $PythonVer)
if ((Test-Path $Py) -and -not $Force -and -not $DryRun) { Info ".venv already exists" }
else {
    Run $Uv @('venv', $Venv, '--python', $PythonVer, '--clear')
    if (-not $DryRun) { Set-Content -Path $Marker -Value "Made by UnderCover setup.ps1 on $(Get-Date -Format u)" -Encoding ascii }
}

# 4. PyTorch for this GPU, then everything else pinned around it -------------------------------
$torchReq = Join-Path $Req "torch-$Backend.txt"
Step "Installing PyTorch for $Backend (about 2-3 GB, this is the slow part)"
$torchArgs = @('pip', 'install', '--python', $Py, '-r', $torchReq)
if ($Backend -ne 'rocm') {
    # Only the PyTorch index for this step: mixing it with PyPI lets uv pick wrong builds.
    $tag = $Backend -replace '^cuda', 'cu'
    $torchArgs += @('--index-url', "https://download.pytorch.org/whl/$tag")
}
Run $Uv $torchArgs

Step "Installing the other packages"
# Constrain to the torch that is now installed, so no package can swap it for a different build.
$pins = Join-Path $InstallDir 'cache\torch-pins.txt'
if (-not $DryRun) {
    $ver = & $Py -c "import importlib.metadata as m; print('\n'.join(f'{p}=={m.version(p)}' for p in ('torch','torchaudio','torchvision')))"
    if ($LASTEXITCODE -ne 0) { Fail "PyTorch didn't install correctly" }
    New-Item -ItemType Directory -Force (Split-Path $pins) | Out-Null
    Set-Content -Path $pins -Value $ver -Encoding ascii
    Info "pinned: $($ver -join ', ')"
}
Run $Uv @('pip', 'install', '--python', $Py, '-r', (Join-Path $Req 'common.txt'), '-c', $pins)

# 5. ffmpeg + Deno (portable, inside tools\) ---------------------------------------------------
Step "Getting ffmpeg"
$ffBin = Join-Path $Tools 'ffmpeg\bin'
if ((Test-Path (Join-Path $ffBin 'ffmpeg.exe')) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP 'ffmpeg-essentials.zip'
    $tmp = Join-Path $env:TEMP 'ffmpeg-unzip'
    Fetch $FfmpegUrl $zip
    Unzip $zip $tmp
    if (-not $DryRun) {
        # The zip holds one versioned folder (ffmpeg-8.x-essentials_build\bin\...).
        $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $ffBin | Out-Null
        Copy-Item (Join-Path $src.FullName 'bin\*.exe') $ffBin -Force
        Remove-Item -Recurse -Force $tmp
    }
}

Step "Getting Deno $DenoVersion (lets yt-dlp read YouTube)"
$denoDir = Join-Path $Tools 'deno'
if ((Test-Path (Join-Path $denoDir 'deno.exe')) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP "deno-$DenoVersion.zip"
    Fetch "https://github.com/denoland/deno/releases/download/v$DenoVersion/deno-x86_64-pc-windows-msvc.zip" $zip
    Unzip $zip $denoDir
}

# 6. GPU settings in this install's config.toml ------------------------------------------------
Step "Writing GPU settings"
$cfg = Join-Path $InstallDir 'config.toml'
$deviceName = ''
if ($gpu -and $Backend -ne 'cpu') {
    # A distinctive part of the name ("RX 7900 XTX", "RTX 4070") so an integrated GPU is never chosen.
    $m = [regex]::Match($gpu.gpu, '(RX|RTX|GTX|PRO|Arc)\s.*$')
    $deviceName = if ($m.Success) { $m.Value.Trim() } else { $gpu.gpu }
}
if ($DryRun) { Info "[dry-run] config.toml [gpu] device_name = `"$deviceName`"" }
elseif (Test-Path $cfg) {
    $text = Get-Content $cfg -Raw
    $text = [regex]::Replace($text, '(?m)^device_name\s*=\s*".*?"', "device_name = `"$deviceName`"")
    Set-Content -Path $cfg -Value $text -NoNewline -Encoding utf8
    Info "device_name = `"$deviceName`""
}

# 7. Self-test --------------------------------------------------------------------------------
Step "Testing"
$env:PATH = "$ffBin;$denoDir;$env:PATH"
Run $Py @((Join-Path $PSScriptRoot 'selftest.py'), '--backend', $Backend, '--report', (Join-Path $InstallDir 'logs\selftest.json'))

# 8. Launcher --------------------------------------------------------------------------------
Step "Making the launcher"
$launcher = Join-Path $InstallDir 'UnderCover.cmd'
$body = @"
@echo off
title UnderCover
rem Starts UnderCover and opens it in your browser. Keep this window open while you use it.
cd /d "%~dp0"
set "PATH=%~dp0tools\ffmpeg\bin;%~dp0tools\deno;%PATH%"
".venv\Scripts\python.exe" -m undercover.server %*
if errorlevel 1 pause
"@
if ($DryRun) { Info "[dry-run] write $launcher" } else { Set-Content -Path $launcher -Value $body -Encoding ascii; Info $launcher }

Write-Host ""
Write-Host "Done. Start UnderCover from the Start menu or with UnderCover.cmd." -ForegroundColor Green
Write-Host "The first start downloads the voice and separation models (about 3 GB)."
if ($Pause) { Read-Host "Press Enter to close" | Out-Null }
