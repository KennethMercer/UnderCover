<#
.SYNOPSIS
  Sets up UnderCover on this PC: picks the right PyTorch for the GPU, installs Python + packages,
  fetches ffmpeg and Deno, writes the GPU settings, runs a self-test, and makes a launcher.

.DESCRIPTION
  Everything goes inside -InstallDir (default: the folder above this script), so uninstalling is
  deleting that folder. Nothing system-wide is changed: no PATH edits, no registry, no admin.
  The app code (undercover\, with its default settings, and tools\check_env.py) must already be in -InstallDir; the
  Inno Setup installer copies it there before running this.

  Re-running is safe: finished steps are skipped (use -Force to redo the Python packages).

.PARAMETER Backend
  auto (default) | rocm | cuda128 | cuda126 | cpu. auto runs detect_gpu.ps1.

.PARAMETER DryRun
  Print every step and command without downloading or installing anything.

.PARAMETER Repair
  Check an existing install against its record (manifest.json, written at the end of setup) with the Doctor
  (python -m undercover.doctor --json) and fix only what's wrong: missing tools, PyTorch or packages that changed,
  damaged or missing model files. Keeps your files and settings (config.toml is not rewritten). Combine with -DryRun
  to see what it would do.

.PARAMETER LibraryDir
  Where your library lives (songs, covers, speech, voices): a folder that gets uploads\, output\ and voices\.
  Default (or the install folder): inside the install folder, as before. Same for -WorkDir (cache\: stems, training
  projects) and -ModelsDir (models\). Written to config.toml [paths].

.PARAMETER Addons
  Optional add-ons to install after setup, comma-separated ids (e.g. "f5"). Only pass ids whose licence the user has
  read and accepted (the installer's add-ons page asks); each runs python -m undercover.addons install <id> --accept-licence.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup.ps1 -DryRun
#>
param(
    [string]$InstallDir = (Split-Path $PSScriptRoot -Parent),
    [ValidateSet('auto', 'rocm', 'cuda128', 'cuda126', 'cpu')][string]$Backend = 'auto',
    [switch]$DryRun,
    [switch]$Force,
    [string]$LibraryDir = '',
    [string]$WorkDir = '',
    [string]$ModelsDir = '',
    [string]$Addons = '',
    [switch]$Pause,     # keep the window open at the end (the installer runs setup in its own window)
    [switch]$Repair     # check against the install record and fix only what's wrong (keeps files and settings)
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with the progress bar
# the installer passes "E:\." for a drive's root (a trailing \ would escape its closing quote): normalise
foreach ($v in 'LibraryDir', 'WorkDir', 'ModelsDir') {
    $d = Get-Variable $v -ValueOnly
    if ($d) { Set-Variable $v ([IO.Path]::GetFullPath($d)) }
}

# Pinned tool versions + the SHA-256 of every download. Bump deliberately (releases at least 2 weeks old), take the
# hash from the release page (GitHub shows each asset's sha256), then re-test with tools\smoke.py --full.
$UvVersion    = '0.12.18'   # 2026-09-22
$UvSha256     = 'cae6a3bc25239f83dffb467a4b180508d9da23986c04639ebfa44e43e6a84bff'
$DenoVersion  = '2.9.7'     # 2026-09-17
$DenoSha256   = 'a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238'
$PythonVer    = '3.12.10'   # exact, the dev venv's (uv checks its own Python downloads against its built-in hashes)
$FfmpegVer    = '8.0'       # the dev PC's ffmpeg; a fixed GyanD/codexffmpeg release instead of gyan.dev's "latest"
$FfmpegUrl    = "https://github.com/GyanD/codexffmpeg/releases/download/$FfmpegVer/ffmpeg-$FfmpegVer-essentials_build.zip"
$FfmpegSha256 = '647e467caf82b9fa200a562769b5ff4d736aaf725804ed2c64ea9752106fa569'

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

# Download into <dest>.part, resuming where an earlier try stopped (an HTTP Range request), then check the file's
# SHA-256: a file that doesn't match is deleted and setup stops.
function Fetch([string]$url, [string]$dest, [string]$sha256) {
    if ($DryRun) { Write-Host "    [dry-run] download $url -> $dest (sha256 $($sha256.Substring(0, 12))..., resumable)" -ForegroundColor DarkGray; return }
    Log "GET $url"
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    $part = "$dest.part"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    for ($i = 1; $i -le 4; $i++) {
        $have = if (Test-Path $part) { (Get-Item $part).Length } else { 0 }
        try {
            $req = [Net.HttpWebRequest]::Create($url)
            $req.UserAgent = 'UnderCover-setup'
            if ($have -gt 0) { $req.AddRange([long]$have); Log "    resuming at $have bytes" }
            $resp = $req.GetResponse()
            $mode = if ($have -gt 0 -and [int]$resp.StatusCode -eq 206) { [IO.FileMode]::Append } else { [IO.FileMode]::Create }
            $expect = if ($resp.ContentLength -ge 0) { $resp.ContentLength + $(if ($mode -eq [IO.FileMode]::Append) { $have } else { 0 }) } else { -1 }
            $in = $resp.GetResponseStream(); $out = [IO.File]::Open($part, $mode)
            try { $in.CopyTo($out) } finally { $out.Close(); $in.Close(); $resp.Close() }
            # a connection closed by the other end can end the copy without an error: a short file is an interruption
            if ($expect -ge 0 -and (Get-Item $part).Length -lt $expect) { throw "the connection closed after $((Get-Item $part).Length) of $expect bytes" }
            Move-Item -Force $part $dest
            break
        } catch {   # a WebException, or an IOException when the connection drops in the middle of the copy
            $resp0 = if ($_.Exception -is [Net.WebException]) { $_.Exception.Response } else { $null }
            $code = if ($resp0) { [int]$resp0.StatusCode } else { 0 }
            if ($code -eq 416 -and $have -gt 0) { Move-Item -Force $part $dest; break }   # the .part was already complete
            if ($i -eq 4) { Fail "couldn't download $url ($($_.Exception.Message)); run setup again to carry on from where it stopped" }
            Info "download interrupted ($($_.Exception.Message)); trying again from where it stopped"
            Start-Sleep -Seconds (2 * $i)
        }
    }
    $got = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLower()
    if ($got -ne $sha256.ToLower()) {
        Remove-Item -Force $dest
        Fail "the download of $url isn't the expected file (SHA-256 $got, expected $sha256). Try again later; if it keeps happening, the release may have changed."
    }
    Log "    sha256 ok"
}

function Unzip([string]$zip, [string]$dest) {
    if ($DryRun) { Write-Host "    [dry-run] unzip $zip -> $dest" -ForegroundColor DarkGray; return }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    Remove-Item -Force $zip -ErrorAction SilentlyContinue   # unpacked: the download (up to 106 MB) doesn't stay in %TEMP%
}

# ---------------------------------------------------------------------------------------------
Write-Host "UnderCover setup" -ForegroundColor White
Write-Host "Install folder: $InstallDir"
if ($DryRun) { Write-Host "DRY RUN: nothing will be downloaded or installed." -ForegroundColor Yellow }
if ($Repair -and ($LibraryDir -or $WorkDir -or $ModelsDir)) {
    Fail "-Repair keeps your files where they are. To move them, use SafeHouse > Storage in UnderCover."
}
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

# Repair: what's wrong, from the Doctor (fast checks, read-only) -------------------------------
$Fix = @{ torch = $true; packages = $true; models = $false; config = $true; launcher = $true; ffmpeg = $true; deno = $true }
if ($Repair) {
    Step "Checking this install against its record (repair)"
    $Fix = @{ torch = $false; packages = $false; models = $false; config = $false; launcher = $false; ffmpeg = $false; deno = $false }
    $manifestFile = Join-Path $InstallDir 'manifest.json'
    if (Test-Path $manifestFile) {
        $man = Get-Content $manifestFile -Raw | ConvertFrom-Json
        Info "install record: $($man.kind), $($man.made)"
        if ($man.torch_file -and $Backend -eq 'auto') { $Backend = ($man.torch_file -replace '^torch-', '' -replace '\.txt$', '') }
    } else { Info "no install record (manifest.json): checking against the pinned versions instead" }
    if ($Backend -eq 'auto') {   # an install from before the record: the backend it was set up with
        $cfgPath = Join-Path $InstallDir 'config.toml'
        $cfgText = if (Test-Path $cfgPath) { Get-Content $cfgPath -Raw } else { '' }
        $m = [regex]::Match($cfgText, '(?m)^backend\s*=\s*"(\w+)"')
        $was = if ($m.Success) { $m.Groups[1].Value } elseif (Test-Path $Py) {   # the defaults hold it when the user's file doesn't
            Push-Location $InstallDir; try { (& $Py -m undercover.core.config get gpu backend 2>$null | Out-String).Trim() } finally { Pop-Location }
        } else { '' }
        if ($was) { $Backend = if ($was -eq 'cuda') { 'cuda128' } else { $was } }
    }
    if (Test-Path $Py) {
        Push-Location $InstallDir
        $env:PATH = "$(Join-Path $Tools 'ffmpeg\bin');$(Join-Path $Tools 'deno');$env:PATH"
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $json = & $Py -m undercover.doctor --json 2>$null | Out-String
        $ErrorActionPreference = $old
        Pop-Location
        try { $rep = $json | ConvertFrom-Json } catch { $rep = $null }
        if ($rep) {
            foreach ($r in $rep.results) {
                if ($r.state -eq 'ok') { continue }
                Info "$($r.state.ToUpper()): $($r.title): $($r.say)"
                if ($r.id -eq 'torch') { $Fix.torch = $true }
                if ($r.id -eq 'packages' -or $r.id -eq 'pins') { $Fix.packages = $true }
                if ($r.id -like 'model:*') { $Fix.models = $true }
                if ($r.id -eq 'python') { $Fix.torch = $true; $Fix.packages = $true }
                if ($r.id -eq 'ffmpeg') { $Fix.ffmpeg = $true }
                if ($r.id -eq 'deno') { $Fix.deno = $true }
            }
            if (-not ($Fix.torch -or $Fix.packages -or $Fix.models)) { Info "Python, PyTorch, packages and model files: nothing to fix" }
        } else {
            Info "the Doctor couldn't run in this install: PyTorch and every package will be checked"
            $Fix.torch = $true; $Fix.packages = $true
        }
    } else {
        Info "the Python environment is missing: it will be made again"
        $Fix.torch = $true; $Fix.packages = $true; $Fix.ffmpeg = -not (Test-Path (Join-Path $Tools 'ffmpeg\bin\ffmpeg.exe'))
        $Fix.deno = -not (Test-Path (Join-Path $Tools 'deno\deno.exe'))
    }
    if (-not (Test-Path (Join-Path $InstallDir 'UnderCover.cmd'))) { $Fix.launcher = $true }
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
if ($Repair -and -not ($Fix.torch -or $Fix.packages)) { Info "not needed for this repair" }
elseif ((Test-Path $Uv) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP "uv-$UvVersion.zip"
    Fetch "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip $UvSha256
    Unzip $zip (Join-Path $Tools 'uv')
}
# Keep uv's Python downloads and cache inside the install folder.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR          = Join-Path $InstallDir 'cache\uv'
$env:UV_NO_CONFIG          = '1'      # ignore any uv.toml the user may have elsewhere

# 3. Python + venv -----------------------------------------------------------------------------
Step "Installing Python $PythonVer"
if ($Repair -and (Test-Path $Py)) { Info ".venv kept (repair never makes it again while it's there)" }
else {
Run $Uv @('python', 'install', $PythonVer)
if ((Test-Path $Py) -and -not $Force -and -not $DryRun) { Info ".venv already exists" }
else {
    Run $Uv @('venv', $Venv, '--python', $PythonVer, '--clear')
    if (-not $DryRun) { Set-Content -Path $Marker -Value "Made by UnderCover setup.ps1 on $(Get-Date -Format u)" -Encoding ascii }
}
}

# 4. PyTorch for this GPU, then everything else pinned around it -------------------------------
$torchReq = Join-Path $Req "torch-$Backend.txt"
if (-not $Fix.torch) { Info "PyTorch: as recorded, left alone" } else {
Step "Installing PyTorch for $Backend (about 2-3 GB, this is the slow part)"
$torchArgs = @('pip', 'install', '--python', $Py, '-r', $torchReq)
if ($Backend -ne 'rocm') {
    # Only the PyTorch index for this step: mixing it with PyPI lets uv pick wrong builds.
    $tag = $Backend -replace '^cuda', 'cu'
    $torchArgs += @('--index-url', "https://download.pytorch.org/whl/$tag")
}
Run $Uv $torchArgs
}

if (-not $Fix.packages) { Info "Python packages: as recorded, left alone" } else {
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
# common.txt is the dev venv's complete package set (minus PyTorch and the cadence set), every line with its SHA-256
# (installer/pin_hashes.py): installed exactly as listed (--no-deps; uv's hash mode needs every dependency listed up
# front, and they are), and uv refuses any file that doesn't match its hash. The torch pins stop anything replacing it.
Run $Uv @('pip', 'install', '--python', $Py, '--no-deps', '--require-hashes', '-r', (Join-Path $Req 'common.txt'), '-c', $pins)

Step "Installing the cadence engine (Chatterbox + Whisper)"
# --no-deps, like on the dev PC: their metadata asks for torch 2.6 / numpy < 2, which would replace the GPU's torch.
Run $Uv @('pip', 'install', '--python', $Py, '--no-deps', '--require-hashes', '-r', (Join-Path $Req 'cadence.txt'))
}

# 5. ffmpeg + Deno (portable, inside tools\) ---------------------------------------------------
Step "Getting ffmpeg $FfmpegVer"
$ffBin = Join-Path $Tools 'ffmpeg\bin'
if (-not $Fix.ffmpeg) { Info "found and working: left alone" }
elseif ((Test-Path (Join-Path $ffBin 'ffmpeg.exe')) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP "ffmpeg-$FfmpegVer-essentials.zip"
    $tmp = Join-Path $env:TEMP 'ffmpeg-unzip'
    Fetch $FfmpegUrl $zip $FfmpegSha256
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
if (-not $Fix.deno) { Info "found and working: left alone" }
elseif ((Test-Path (Join-Path $denoDir 'deno.exe')) -and -not $DryRun) { Info "already there" }
else {
    $zip = Join-Path $env:TEMP "deno-$DenoVersion.zip"
    Fetch "https://github.com/denoland/deno/releases/download/v$DenoVersion/deno-x86_64-pc-windows-msvc.zip" $zip $DenoSha256
    Unzip $zip $denoDir
}

# 6. GPU settings in this install's config.toml ------------------------------------------------
if (-not $Fix.config) { Info "Settings (config.toml): kept as they are" } else {
Step "Writing GPU settings"
$cfg = Join-Path $InstallDir 'config.toml'
$deviceName = ''
if ($gpu -and $Backend -ne 'cpu') {
    # A distinctive part of the name ("RX 7900 XTX", "RTX 4070") so an integrated GPU is never chosen.
    $m = [regex]::Match($gpu.gpu, '(RX|RTX|GTX|PRO|Arc)\s.*$')
    $deviceName = if ($m.Success) { $m.Value.Trim() } else { $gpu.gpu }
}
$cfgBackend = $Backend -replace '^cuda\d+$', 'cuda'      # config knows rocm / cuda / cpu
if ($DryRun) { Info "[dry-run] config.toml [gpu] device_name = `"$deviceName`", backend = `"$cfgBackend`"" }
else {
    # the user's config.toml (made if missing) gets only these; the rest comes from undercover\config.default.toml
    Push-Location $InstallDir
    try { & $Py -m undercover.core.config set gpu "device_name=$deviceName" "backend=$cfgBackend" } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { Fail "couldn't write the GPU settings to config.toml" }
    Info "device_name = `"$deviceName`", backend = `"$cfgBackend`""
}

# 6b. Where the files live (the installer's "Where should UnderCover keep your files?" page) ----------------------
# Each group is one folder holding today's sub-folders; the install folder (the default) changes nothing.
function SameDir([string]$a, [string]$b) {
    if (-not $a) { return $true }
    return ([IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [IO.Path]::GetFullPath($b).TrimEnd('\'))
}
$paths = [ordered]@{}
if (-not (SameDir $LibraryDir $InstallDir)) {   # your library: songs, covers + speech, voices (with their cadence)
    $paths['uploads_dir'] = Join-Path $LibraryDir 'uploads'
    $paths['output_dir']  = Join-Path $LibraryDir 'output'
    $paths['voices_dir']  = Join-Path $LibraryDir 'voices'
}
if (-not (SameDir $WorkDir $InstallDir)) { $paths['cache_dir'] = Join-Path $WorkDir 'cache' }
if (-not (SameDir $ModelsDir $InstallDir)) {
    $paths['models_dir'] = Join-Path $ModelsDir 'models'
    if (-not $paths.Contains('voices_dir')) { $paths['voices_dir'] = 'models/voices' }   # voices stay with the library
}
if ($paths.Count) {
    Step "Writing where your files live"
    if ($DryRun) { $paths.GetEnumerator() | ForEach-Object { Info "[dry-run] config.toml [paths] $($_.Key) = $($_.Value)" } }
    else {
        $kv = @($paths.Keys | ForEach-Object { "$_=$($paths[$_])" })   # one argument each (spaces kept): strings in the user's file
        Push-Location $InstallDir
        try { & $Py -m undercover.core.config set paths @kv } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Fail "couldn't write where your files live to config.toml" }
        $kv | ForEach-Object { Info $_ }
    }
}
}

# 6c. Model files the record knew about that are missing or damaged (repair): fetched again from their pinned source,
# each checked against its SHA-256 before it replaces the old copy.
if ($Repair -and $Fix.models) {
    Step "Downloading damaged or missing model files again"
    if (-not $DryRun) { Push-Location $InstallDir }
    try { Run $Py @('-m', 'undercover.doctor', 'fix', 'models') } finally { if (-not $DryRun) { Pop-Location } }
}

# 6d. The YouTube downloader (phase 16, the one exception to pinned-only): PyPI's newest yt-dlp + the solver it pins,
# each wheel checked against PyPI's SHA-256, unpacked beside the venv (cache\ytdlp) and tested on known public videos;
# used only if it does at least as well as the pinned pair common.txt just installed. If PyPI can't be reached, a hash
# fails or the test does, the pinned pair stays in use (the command always succeeds). Repair runs it too.
$env:PATH = "$ffBin;$denoDir;$env:PATH"
Step "Bringing the YouTube downloader up to date"
if (-not $DryRun) { Push-Location $InstallDir }
try { Run $Py @('-m', 'undercover.ytdlp', 'install') } finally { if (-not $DryRun) { Pop-Location } }

# 7. Self-test --------------------------------------------------------------------------------
Step "Testing"
Run $Py @((Join-Path $PSScriptRoot 'selftest.py'), '--backend', $Backend, '--report', (Join-Path $InstallDir 'logs\selftest.json'))

# 7b. Optional add-ons the user accepted on the installer's add-ons page ----------------------
$addonIds = @($Addons -split '[,; ]+' | Where-Object { $_ })
if ($addonIds.Count) {
    Step "Adding the add-ons you accepted: $($addonIds -join ', ')"
    foreach ($id in $addonIds) { Run $Py @('-m', 'undercover.addons', 'install', $id, '--accept-licence') }
}

# 8. Launcher --------------------------------------------------------------------------------
# Same behaviour as UnderCover.bat (dev): from the Start menu / a shortcut (started by Explorer) it runs in the
# background with no window (quit it from SafeHouse); from a terminal it stays in that window. --console / --background
# force either.
if (-not $Fix.launcher) { Info "Launcher: already there" } else {
Step "Making the launcher"
$launcher = Join-Path $InstallDir 'UnderCover.cmd'
$body = @'
@echo off
title UnderCover
rem Starts UnderCover and opens it in your browser.
rem  - From the Start menu or a shortcut: runs in the background, no window. Quit it from the page
rem    (SafeHouse, "Quit UnderCover"); its output goes to logs\server.log.
rem  - Run from a terminal: stays in that window with its output; Ctrl+C stops it.
rem  - Force either way: UnderCover.cmd --console   or   UnderCover.cmd --background
cd /d "%~dp0"
set "PATH=%~dp0tools\ffmpeg\bin;%~dp0tools\deno;%PATH%"
set "UC_MODE="
set "UC_ARGS=%*"
if /i "%~1"=="--console" (set "UC_MODE=console" & set "UC_ARGS=")
if /i "%~1"=="--background" (set "UC_MODE=background" & set "UC_ARGS=")
rem Started by Explorer (Start menu, double-click, a shortcut)? Walk up: powershell <- for's cmd <- this cmd <- parent
if not defined UC_MODE for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$i=$PID; foreach($n in 1..3){$i=(Get-CimInstance Win32_Process -Filter ('ProcessId='+$i)).ParentProcessId}; (Get-Process -Id $i -EA 0).ProcessName"`) do set "UC_PARENT=%%P"
if not defined UC_MODE if /i "%UC_PARENT%"=="explorer" set "UC_MODE=background"
if not defined UC_MODE set "UC_MODE=console"
if "%UC_MODE%"=="background" (
  if not exist logs mkdir logs
  if exist ".venv\Scripts\pythonw.exe" (start "" ".venv\Scripts\pythonw.exe" -m undercover.core.relaunch 0) else (start "" /min ".venv\Scripts\python.exe" -m undercover.core.relaunch 0)
  exit /b 0
)
".venv\Scripts\python.exe" -m undercover.server %UC_ARGS%
if errorlevel 1 pause
'@
if ($DryRun) { Info "[dry-run] write $launcher" } else { Set-Content -Path $launcher -Value $body -Encoding ascii; Info $launcher }
}

# 9. The install record (manifest.json): what was installed, for the Doctor and the next -Repair ----------------
if (-not $Repair -or $Fix.torch -or $Fix.packages -or $Fix.models -or -not (Test-Path (Join-Path $InstallDir 'manifest.json'))) {
    Step "Writing the install record"
    if (-not $DryRun) { Push-Location $InstallDir }   # python -m finds the app's package from here
    try { Run $Py @('-m', 'undercover.doctor', 'manifest', '--installed', '--json') } finally { if (-not $DryRun) { Pop-Location } }
}

Write-Host ""
Write-Host "Done. Start UnderCover from the Start menu or with UnderCover.cmd." -ForegroundColor Green
Write-Host "The first start downloads the voice and separation models (about 1-3 GB, depending on which features you use)."
if ($Pause) { Read-Host "Press Enter to close" | Out-Null }
