<#
.SYNOPSIS
  Sets up UnderCover on this PC: picks the right PyTorch for the GPU, installs Python + packages,
  unpacks ffmpeg and Deno, writes the GPU settings, runs a self-test, and makes a launcher.

.DESCRIPTION
  Everything goes inside -InstallDir (default: the folder above this script), so uninstalling is
  deleting that folder. Nothing system-wide is changed: no PATH edits, no registry, no admin.
  The app code (undercover\, with its default settings, and scripts\check_env.py) must already be in -InstallDir; the
  Inno Setup installer copies it there before running this.

  Re-running is safe: finished steps are skipped (use -Force to redo the Python packages).

  Every file it uses is in installer\downloads.tsv (installer/downloads.py, at build time): uv, Python, the packages,
  ffmpeg, Deno, the AI models and the add-ons, each with its SHA-256. The installer's own download page fetches them
  (rc-fix6): the tools and packages into cache\downloads, the models straight into the models folder; it then runs
  this with -Offline, and nothing goes online. Run by hand (or -Repair), setup fetches what's missing from the same
  list first.

.PARAMETER Offline
  Use only files already downloaded (the installer's download page); a missing one fails setup with a plain word.

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
    [switch]$Repair,    # check against the install record and fix only what's wrong (keeps files and settings)
    [switch]$Offline    # the installer downloaded everything already: nothing goes online
)
# Started from PowerShell 7 (a terminal, or anything started from one), Windows PowerShell inherits PowerShell 7's
# module path and can't load its own modules: Get-FileHash, Expand-Archive, ConvertFrom-Json are "not recognized". Its
# own path, then (only in Windows PowerShell: a PowerShell 7 session that dot-sources this keeps its own).
if ($PSVersionTable.PSEdition -ne 'Core') { $env:PSModulePath = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') }

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with the progress bar
# the installer passes "E:\." for a drive's root (a trailing \ would escape its closing quote): normalise
foreach ($v in 'LibraryDir', 'WorkDir', 'ModelsDir') {
    $d = Get-Variable $v -ValueOnly
    if ($d) { Set-Variable $v ([IO.Path]::GetFullPath($d)) }
}

# Pinned tool versions. Their files, URLs and SHA-256 are in installer/downloads.py (TOOLS, which writes downloads.tsv):
# bump both together, deliberately (releases at least 2 weeks old), then re-test with tests\smoke.py --full.
$UvVersion    = '0.12.18'   # 2026-09-22
$DenoVersion  = '2.9.7'     # 2026-09-17
$PythonVer    = '3.12.10'   # exact, the dev venv's (uv checks its own Python downloads against its built-in hashes)
$FfmpegVer    = '8.0'       # the dev PC's ffmpeg; a fixed GyanD/codexffmpeg release instead of gyan.dev's "latest"

$Req     = Join-Path $PSScriptRoot 'requirements'
$Tools   = Join-Path $InstallDir 'tools'
$Cache   = Join-Path $InstallDir 'cache\downloads'   # the installer's download page puts the tools and packages here
$Venv    = Join-Path $InstallDir '.venv'
$Py      = Join-Path $Venv 'Scripts\python.exe'
$Uv      = Join-Path $Tools 'uv\uv.exe'
$LogFile = Join-Path $InstallDir 'logs\setup.log'
# The installer reads this one line after setup ("ok", or "failed: <what>"): it says "installed" only on "ok".
$ResultFile = Join-Path $InstallDir 'logs\setup-result.txt'
function Result([string]$line) {
    if ($DryRun) { return }
    New-Item -ItemType Directory -Force (Split-Path $ResultFile) | Out-Null
    [IO.File]::WriteAllText($ResultFile, $line, (New-Object Text.UTF8Encoding $false))
}

function Step([string]$msg) {
    if ((-not $DryRun) -and (Test-Path -LiteralPath $CancelFile)) { Cancelled }
    $script:StepNo++
    if ($script:StepNo -gt $script:StepsTotal) { $script:StepsTotal = $script:StepNo }
    $script:StepName = $msg
    Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan; Log "STEP $msg"
    Status
}
function Info([string]$msg) { Write-Host "    $msg"; Log $msg }
function Log([string]$msg) {
    if ($DryRun) { return }
    New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
    Add-Content -Path $LogFile -Value ("{0:u}  {1}" -f (Get-Date), $msg)
}
# anything unexpected: logged, and said to the wizard as a failure in plain words (never a bare exit code)
trap {
    $m = "$($_.Exception.Message)".Trim()
    Write-Host ""; Write-Host "Setup failed: $m" -ForegroundColor Red
    Log "ERROR $m"; Log "    at $($_.InvocationInfo.PositionMessage -replace '\s+', ' ')"
    Result "failed: something went wrong ($m)"
    exit 1
}

function Fail([string]$msg) {
    Write-Host ""; Write-Host "Setup failed: $msg" -ForegroundColor Red
    Write-Host "Details are in $LogFile"; Log "FAIL $msg"
    Result "failed: $msg"
    if ($Pause) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

# ---- What the wizard shows (installer-fix4): setup runs hidden, and the wizard drives its own progress bar from the
# status lines setup prints ("@@ <step>|<of>|<step name>|<per mille of the step, or empty>|<detail>"). The same lines
# go to logs\setup-status.txt, so the release check can see every step pass. A file logs\setup-cancel (the wizard's
# Cancel) stops setup between steps, or mid-download, keeping what's done.
$StatusFile = Join-Path $InstallDir 'logs\setup-status.txt'
$CancelFile = Join-Path $InstallDir 'logs\setup-cancel'
$script:StepNo = 0
$script:StepName = ''
$script:StepsTotal = 12   # (the YouTube downloader's step, when it isn't -Offline, adds one)
function Status([string]$detail = '', [double]$frac = -1) {
    $f = if ($frac -ge 0) { [int][math]::Floor([math]::Min(0.99, $frac) * 1000) } else { '' }   # per mille: whole numbers for the wizard
    $line = "@@ $($script:StepNo)|$($script:StepsTotal)|$($script:StepName)|$f|$detail"
    [Console]::Out.WriteLine($line); [Console]::Out.Flush()
    if (-not $DryRun) {
        New-Item -ItemType Directory -Force (Split-Path $StatusFile) | Out-Null
        Add-Content -Path $StatusFile -Value ("{0:u}  {1}" -f (Get-Date), $line)
    }
}
# what a Cancel leaves for the next run, read from the disk: "Python 3.12.10, PyTorch and the downloads so far"
function KeptNow {
    $k = @()
    if (Get-ChildItem -LiteralPath (Join-Path $InstallDir 'tools\python') -Directory -Filter "cpython-$PythonVer-*" -ErrorAction SilentlyContinue) { $k += "Python $PythonVer" }
    if (Test-Path -LiteralPath (Join-Path $InstallDir '.venv\Lib\site-packages\torch')) { $k += 'PyTorch' }
    if (Test-Path -LiteralPath (Join-Path $InstallDir 'tools\ffmpeg\bin')) { $k += 'ffmpeg' }
    if (Test-Path -LiteralPath (Join-Path $InstallDir 'tools\deno')) { $k += 'Deno' }
    if ((Get-DirBytes (Join-Path $InstallDir 'cache\downloads')) -gt 0) { $k += 'the downloads so far' }
    if ($k.Count -gt 1) { return (($k[0..($k.Count - 2)] -join ', ') + ' and ' + $k[-1]) }
    return ($k -join '')
}
function Cancelled {
    $what = KeptNow
    $kept = if ($what) { "kept: $what" } else { 'nothing was finished yet' }
    Write-Host ""; Write-Host "Setup stopped, as asked ($kept)." -ForegroundColor Yellow
    Log "CANCELLED ($kept)"
    Result "cancelled: $kept"
    Remove-Item -LiteralPath $CancelFile -Force -ErrorAction SilentlyContinue
    exit 3
}

# Run a native command and return its exit code; in -DryRun just print it (0). Its output goes to the log only: on
# screen there are only setup's own plain steps (no tool output, no JSON).
function RunCode([string]$exe, [string[]]$argv) {
    $shown = ($argv | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    if ($DryRun) { Write-Host "    [dry-run] $exe $shown" -ForegroundColor DarkGray; return 0 }
    Log "RUN $exe $shown"
    # Windows PowerShell 5.1 turns any stderr line into a terminating error under 'Stop', and uv
    # writes its progress to stderr, so relax it for the call and rely on the exit code.
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $exe @argv 2>&1 | ForEach-Object { Log "    $_" } }
    finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}

# Run a native command; stop setup if it fails.
function Run([string]$exe, [string[]]$argv) {
    $code = RunCode $exe $argv
    if ($code -ne 0) { Fail "'$(Split-Path $exe -Leaf) $($argv[0])' exited with code $code" }
}

# A child that reports its own progress ('@@P <per mille>|<text>' lines: python -m undercover.addons install --status):
# those drive the wizard's bar within this step, from $from to $from + $span of it; every other line goes to the log.
function RunStatus([string]$exe, [string[]]$argv, [double]$from, [double]$span) {
    if ($DryRun) { return (RunCode $exe $argv) }
    $shown = ($argv | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    Log "RUN $exe $shown"
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        & $exe @argv 2>&1 | ForEach-Object {
            $line = "$_"
            if ($line -match '^@@P (\d+)\|(.*)$') { Status $Matches[2] ($from + $span * [int]$Matches[1] / 1000) }
            elseif ($line.Trim()) { Log "    $line" }
        }
    } finally { $ErrorActionPreference = $old }
    return $LASTEXITCODE
}

# A long run (uv downloading and unpacking): its output to the log, and every 2 s a status line with the bytes that
# have arrived in $watch (uv's cache) against what that step adds there ($expectGb; 0: unknown, "so far" instead).
# The wizard's Cancel stops it mid-download. Stops setup if it fails.
function RunWatched([string]$exe, [string[]]$argv, [string]$watch, [double]$expectGb, [string]$label) {
    if ($DryRun) { RunCode $exe $argv | Out-Null; return }
    $shown = ($argv | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    Log "RUN $exe $shown"
    $outFile = Join-Path $InstallDir 'logs\setup-run.out'
    $errFile = Join-Path $InstallDir 'logs\setup-run.err'
    $line = ($argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $p = Start-Process -FilePath $exe -ArgumentList $line -NoNewWindow -PassThru -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $null = $p.Handle   # (without it, ExitCode stays empty: a Start-Process quirk)
    $base = Get-DirBytes $watch
    $t0 = Get-Date
    while (-not $p.WaitForExit(2000)) {
        if (Test-Path -LiteralPath $CancelFile) {
            & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
            $p.WaitForExit()
            Cancelled
        }
        $gb = [math]::Max([long]0, [long]((Get-DirBytes $watch) - $base)) / 1GB   # (typed: an untyped 0 makes it Int32, which fails past 2 GB)
        $min = [math]::Floor(((Get-Date) - $t0).TotalMinutes)
        $time = if ($min -ge 1) { ", $min min" } else { '' }
        if ($expectGb -gt 0) { Status ("{0}: {1:0.0} of about {2:0.0} GB{3}" -f $label, [math]::Min($gb, $expectGb), $expectGb, $time) ($gb / $expectGb) }
        else { Status ("{0}: {1:0.0} GB so far{2}" -f $label, $gb, $time) }
    }
    $p.WaitForExit()
    foreach ($f in $outFile, $errFile) {
        if (Test-Path -LiteralPath $f) { Get-Content -LiteralPath $f | ForEach-Object { Log "    $_" }; Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    }
    Log ("    measured: {0:0} MB added to {1}" -f (((Get-DirBytes $watch) - $base) / 1MB), $watch)
    if ($p.ExitCode -ne 0) { Fail "'$(Split-Path $exe -Leaf) $($argv[0])' exited with code $($p.ExitCode)" }
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
            try {
                $buf = New-Object byte[] 1048576; $got = $have; $last = Get-Date
                while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                    $out.Write($buf, 0, $n); $got += $n
                    if (((Get-Date) - $last).TotalSeconds -ge 1) {
                        $last = Get-Date
                        if (Test-Path -LiteralPath $CancelFile) { $out.Close(); $in.Close(); $resp.Close(); Cancelled }
                        $name = Split-Path $dest -Leaf
                        if ($expect -gt 0) { Status ("Downloading {0}: {1:0} of {2:0} MB" -f $name, ($got / 1MB), ($expect / 1MB)) ($got / $expect) }
                        else { Status ("Downloading {0}: {1:0} MB so far" -f $name, ($got / 1MB)) }
                    }
                }
            } finally { $out.Close(); $in.Close(); $resp.Close() }
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
}

# The download list (installer\downloads.tsv): group, dest, urls, sha256, bytes, pin. A row's file is in place when it
# is there at its size (the download page and Fetch check the SHA-256 before a file gets its name).
$Downloads = @(Import-Csv -LiteralPath (Join-Path $PSScriptRoot 'downloads.tsv') -Delimiter "`t")
function Rows([string[]]$groups) { @($Downloads | Where-Object { $groups -contains $_.group }) }
function CachePath($row) { Join-Path $Cache ($row.dest.Substring('cache/'.Length)) }
function InPlace($row) {
    $f = CachePath $row
    return (Test-Path -LiteralPath $f) -and ((Get-Item -LiteralPath $f).Length -eq [long]$row.bytes)
}
function CacheFile([string]$name) {   # a base tool's file in the cache, by its name
    $r = @($Downloads | Where-Object { $_.dest -eq "cache/$name" })
    if (-not $r.Count) { Fail "installer\downloads.tsv doesn't list $name" }
    return (CachePath $r[0])
}
# the files these groups need in cache\downloads: -Offline, each must be there (the installer fetched them); else the
# missing ones are fetched now, resumable, each checked against its SHA-256
function Get-Files([string[]]$groups, [string]$what) {
    $rows = @(Rows $groups | Where-Object { $_.dest -like 'cache/*' -and -not (Test-Installed $_ $Have $InstallDir $PythonVer) })
    $todo = @($rows | Where-Object { -not (InPlace $_) })
    if (-not $todo.Count) { Info "$what`: all $($rows.Count) files are here"; return }
    if ($Offline) {
        Fail ("$($todo.Count) of the files setup needs weren't downloaded ($($todo[0].dest.Substring(6))$(if ($todo.Count -gt 1) { ', ...' })). " +
              "Run the installer again: it downloads only what's missing")
    }
    $i = 0
    foreach ($r in $todo) {
        $i++
        Status ("Downloading {0} (file {1} of {2})" -f $what, $i, $todo.Count) ($i / ($todo.Count + 1))
        Fetch (($r.urls -split ' ')[0]) (CachePath $r) $r.sha256
    }
}

# ---------------------------------------------------------------------------------------------
Write-Host "UnderCover setup" -ForegroundColor White
Write-Host "Install folder: $InstallDir"
if (-not $DryRun) {
    Remove-Item -LiteralPath $ResultFile -Force -ErrorAction SilentlyContinue   # no answer from an earlier run
    Remove-Item -LiteralPath $CancelFile -Force -ErrorAction SilentlyContinue   # nor a Cancel from one
    Remove-Item -LiteralPath $StatusFile -Force -ErrorAction SilentlyContinue
}
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

# Space: what setup itself writes in the install folder now (the app, Python, PyTorch with uv's cache), with the
# margin. The wizard already warned about the models and the files that come later; this stops before downloading.
. (Join-Path $PSScriptRoot 'space.ps1')
$Sizes = Read-Sizes $PSScriptRoot
$StepGb = $Sizes.steps
# What an earlier run left (the venv, tools, uv's cache: a cancelled setup, a repair, a reinstall) is taken off, so a
# re-run is checked for what it still has to download, and isn't refused for room it already uses.
if (-not $DryRun) {
    $needs = Get-SpaceNeeds $InstallDir '' '' $ModelsDir $Backend $Sizes @() -SetupOnly -Have { param($k, $d) Get-HaveGB $k $d }
    $short = @(Find-Shortfalls $needs { param($r) Get-FreeGB $r })
    if ($short.Count) { Fail ("there isn't enough space. " + (Format-Shortfall $short[0]) + " Free some space, or install to another drive, then try again.") }
}
function SameDir([string]$a, [string]$b) {
    if (-not $a) { return $true }
    return ([IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [IO.Path]::GetFullPath($b).TrimEnd('\'))
}
# (the installer always passes the three folders: the step counts only when one of them is another folder)
if (-not ((SameDir $LibraryDir $InstallDir) -and (SameDir $WorkDir $InstallDir) -and (SameDir $ModelsDir $InstallDir))) { $script:StepsTotal += 1 }
if ($Addons) { $script:StepsTotal += 1 }
if (-not $Offline) { $script:StepsTotal += 1 }

# 2. The files: the tools and the packages for this graphics card, in cache\downloads -----------------------------
# What this install already has is neither needed in cache\downloads nor installed again (1.0.3: an update over 1.0.0
# downloaded 108 of 137 files that hadn't changed): installer\installed.ps1, the rule the installer's download page uses
# too. A repair reinstalls the packages as before (a package can be there at its pin and still be broken); -Force makes
# the venv again, so it has nothing.
. (Join-Path $PSScriptRoot 'installed.ps1')
$Have = if ($Repair -or $Force -or $DryRun) { @{} } else { Get-InstalledPins $InstallDir $PythonVer }
if ($Have.Count) { Info "$($Have.Count) packages already installed in .venv: the pinned ones among them are kept" }
$addonIds = @($Addons -split '[,; ]+' | Where-Object { $_ })
# (a repair fetches PyTorch and the packages only when they need fixing: a working setup tidied them away)
$groups = @('base') + $(if ($Fix.torch -or $Fix.packages) { @("torch-$Backend", 'packages') } else { @() }) + @($addonIds | ForEach-Object { "addon-$_" })
Step $(if ($Offline) { "Checking the downloaded files" } else { "Downloading what setup needs" })
if ($DryRun) { Info "[dry-run] $(@(Rows $groups | Where-Object { $_.dest -like 'cache/*' }).Count) files from installer\downloads.tsv into $Cache" }
else { Get-Files $groups 'the tools and packages' }

# 2b. uv (the package manager, one exe) -------------------------------------------------------
Step "Unpacking the package manager (uv $UvVersion)"
if ($Repair -and -not ($Fix.torch -or $Fix.packages)) { Info "not needed for this repair" }
elseif ((Test-Path $Uv) -and -not $DryRun) { Info "already there" }
else { Unzip (CacheFile 'uv-x86_64-pc-windows-msvc.zip') (Join-Path $Tools 'uv') }
# Keep uv's Python downloads and cache inside the install folder.
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Tools 'python'
$env:UV_CACHE_DIR          = Join-Path $InstallDir 'cache\uv'
$env:UV_NO_CONFIG          = '1'      # ignore any uv.toml the user may have elsewhere
$env:UV_OFFLINE            = '1'      # every package comes from cache\downloads: uv never goes online

# 3. Python + venv -----------------------------------------------------------------------------
# Python stays private to this install (installer\python.ps1): unpacked by setup itself from the pinned
# python-build-standalone archive (the one uv would install) into tools\python\cpython-<version>-windows-x86_64-none:
# no shim in %USERPROFILE%, nothing in the registry, no minor-version link (os error 448 can't happen).
. (Join-Path $PSScriptRoot 'python.ps1')
Step "Unpacking Python $PythonVer"
# a venv this setup made on uv's minor-version link (installer-fix, before this) is made again: it can't be used
$venvGood = (-not $DryRun) -and (Test-Path $Py) -and (Test-VenvBase $Venv $PythonVer)
if ($Repair -and $venvGood) { Info ".venv kept (repair never makes it again while it works)" }
else {
$pyHome = Join-Path $env:UV_PYTHON_INSTALL_DIR "cpython-$PythonVer-windows-x86_64-none"
$BasePy = Join-Path $pyHome 'python.exe'
if ($DryRun) { Info "[dry-run] unpack the Python archive into $pyHome" }
elseif (Test-OwnPython $BasePy $PythonVer) { Info "Python $PythonVer is already there" }
else {
    $archive = CacheFile 'cpython-3.12.10+20250529-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'
    $unpack = Join-Path $env:UV_PYTHON_INSTALL_DIR '.unpack'
    if (Test-Path -LiteralPath $unpack) { Remove-Item -LiteralPath $unpack -Recurse -Force }
    New-Item -ItemType Directory -Force $unpack | Out-Null
    Run (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xzf', $archive, '-C', $unpack)   # (the archive holds one folder, python\)
    if (-not (Test-Path -LiteralPath (Join-Path $unpack 'python\python.exe'))) { Fail "the Python archive didn't hold python.exe" }
    if (Test-Path -LiteralPath $pyHome) { Remove-Item -LiteralPath $pyHome -Recurse -Force }   # a broken earlier one: this install's own
    Move-Item -LiteralPath (Join-Path $unpack 'python') -Destination $pyHome
    Remove-Item -LiteralPath $unpack -Recurse -Force
    if (-not (Test-OwnPython $BasePy $PythonVer)) { Fail "Python $PythonVer didn't start after unpacking. The details are in the log." }
}
if (-not $DryRun) {
    Info "Python: $BasePy"
    foreach ($k in @(Remove-OwnPythonRegistry $InstallDir)) { Info "removed the registry entry an earlier setup made for its Python ($k)" }
    foreach ($l in @(Remove-MinorLink $env:UV_PYTHON_INSTALL_DIR $PythonVer)) { Info "removed $l, a link Windows won't let anything use" }
}
if ($venvGood -and -not $Force) { Info ".venv already exists" }
else {
    if ((Test-Path $Py) -and -not $DryRun) { Info ".venv is built on a folder Windows won't let it use: making it again" }
    # Python's own venv module, from the full-version python.exe: its home is that folder, never uv's minor-version link
    # (no pip in it: uv installs the packages, as before)
    Run $BasePy @('-m', 'venv', '--clear', '--without-pip', $Venv)
    if (-not $DryRun) { Set-Content -Path $Marker -Value "Made by UnderCover setup.ps1 on $(Get-Date -Format u)" -Encoding ascii }
}
}

# 4. PyTorch for this GPU and every other package, from cache\downloads -----------------------------------------
# One uv run: the pinned files downloads.tsv names (requirements\torch-<backend>.txt, common.txt, cadence.txt), each
# required to match its SHA-256, --no-deps (every dependency is pinned in the lists; the cadence set's metadata asks for
# torch 2.6 / numpy < 2, which would replace the GPU's torch), --offline from that folder only. The three pins that
# are source archives (openai-whisper, antlr4, AMD's rocm) are built here by uv with the pinned setuptools, offline too.
if (-not ($Fix.torch -or $Fix.packages)) { Info "PyTorch and the packages: as recorded, left alone" } else {
Step "Installing PyTorch and the packages"
$offlineReq = Join-Path $InstallDir 'cache\offline-requirements.txt'
$lines = @()
if (-not $DryRun) {
    $pinned = @(Rows @("torch-$Backend", 'packages') | Where-Object { $_.pin })
    $lines = @($pinned | Where-Object { -not (Test-Installed $_ $Have $InstallDir $PythonVer) } |
               ForEach-Object { "$($_.pin) --hash=sha256:$($_.sha256)" } | Select-Object -Unique)
    New-Item -ItemType Directory -Force (Split-Path $offlineReq) | Out-Null
    Set-Content -LiteralPath $offlineReq -Value $lines -Encoding ascii
    Info "$($lines.Count) packages, from $Cache$(if ($lines.Count -lt $pinned.Count) { "; $($pinned.Count - $lines.Count) already installed at their pins, kept" })"
}
# (the bar follows uv's cache as uv unpacks the files into it, against what that measured: PyTorch for this card, the
# rest; a card whose PyTorch hasn't been measured gets the largest measured, so the bar is a little slow, never stuck)
$torchGb = $StepGb.torch.$Backend
if (-not $torchGb) { $torchGb = ($StepGb.torch.PSObject.Properties | ForEach-Object { $_.Value } | Measure-Object -Maximum).Maximum }
if ($DryRun -or $lines.Count) {
    RunWatched $Uv @('pip', 'install', '--python', $Py, '--offline', '--no-index', '--find-links', $Cache, '--no-deps', '--require-hashes', '-r', $offlineReq) $env:UV_CACHE_DIR ([double]$torchGb + [double]$StepGb.common + [double]$StepGb.cadence) 'Unpacking PyTorch and the packages'
} else { Info "every pinned package is installed at its pin: nothing to unpack" }
if (-not $DryRun) {
    $ver = & $Py -c "import importlib.metadata as m; print(m.version('torch'))"
    if ($LASTEXITCODE -ne 0) { Fail "PyTorch didn't install correctly" }
    Info "PyTorch $ver"
}
}

# 5. ffmpeg + Deno (portable, inside tools\) ---------------------------------------------------
Step "Unpacking ffmpeg $FfmpegVer"
$ffBin = Join-Path $Tools 'ffmpeg\bin'
if (-not $Fix.ffmpeg) { Info "found and working: left alone" }
elseif ((Test-Path (Join-Path $ffBin 'ffmpeg.exe')) -and -not $DryRun) { Info "already there" }
else {
    $tmp = Join-Path $Tools '.ffmpeg-unzip'
    Unzip (CacheFile "ffmpeg-$FfmpegVer-essentials_build.zip") $tmp
    if (-not $DryRun) {
        # The zip holds one versioned folder (ffmpeg-8.x-essentials_build\bin\...).
        $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $ffBin | Out-Null
        Copy-Item (Join-Path $src.FullName 'bin\*.exe') $ffBin -Force
        Remove-Item -Recurse -Force $tmp
    }
}

Step "Unpacking Deno $DenoVersion (lets yt-dlp read YouTube)"
$denoDir = Join-Path $Tools 'deno'
if (-not $Fix.deno) { Info "found and working: left alone" }
elseif ((Test-Path (Join-Path $denoDir 'deno.exe')) -and -not $DryRun) { Info "already there" }
else { Unzip (CacheFile 'deno-x86_64-pc-windows-msvc.zip') $denoDir }

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

# 6c. The AI models every feature uses (rc-fix4b, the user: "it should be part of the install"): pinned, each checked
# against its SHA-256. The installer's download page put them in place (-Offline: only checked here); by hand, the
# missing ones are fetched now, resumed from their .part. After setup nothing downloads on first use.
Step $(if ($Offline) { "Checking the AI models" } else { "Downloading the AI models" })
if (-not $DryRun) { Push-Location $InstallDir }   # python -m finds the app's package from here
try {
    # (install --offline: audio-separator's model list put in place from the app, as setup always did; nothing fetched)
    if ($Offline) { $code = RunCode $Py @('-m', 'undercover.models', 'install', '--offline') }
    else { $code = RunStatus $Py @('-m', 'undercover.models', 'install', '--status', '--cancel-file', $CancelFile) 0 1 }
} finally { if (-not $DryRun) { Pop-Location } }
if ($code -eq 3) { Cancelled }
if ($Offline -and $code -ne 0) { Fail "some AI models weren't downloaded. Run the installer again: it downloads only what's missing" }
if ($code -ne 0) { Fail "the AI models couldn't be downloaded (code $code). Try again: what's downloaded is kept" }

# 6c'. Model files the record knew about that are missing or damaged (repair): fetched again from their pinned source,
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
# The installer (-Offline) skips it: nothing goes online after its download page, and the app runs the same self-heal
# once it starts.
$env:PATH = "$ffBin;$denoDir;$env:PATH"
if ($Offline) { Info "YouTube downloader: the pinned one; UnderCover brings it up to date once it runs" }
else {
Step "Bringing the YouTube downloader up to date"
if (-not $DryRun) { Push-Location $InstallDir }
try { Run $Py @('-m', 'undercover.ytdlp', 'install') } finally { if (-not $DryRun) { Pop-Location } }
}

# 7. Self-test --------------------------------------------------------------------------------
Step "Testing"
Run $Py @((Join-Path $PSScriptRoot 'selftest.py'), '--backend', $Backend, '--report', (Join-Path $InstallDir 'logs\selftest.json'))

# 7b. Optional add-ons the user accepted on the installer's add-ons page ----------------------
if ($addonIds.Count) {
    Step "Adding the add-ons you accepted: $($addonIds -join ', ')"
    $i = 0
    foreach ($id in $addonIds) {
        if (-not $DryRun) { Push-Location $InstallDir }   # python -m finds the app's package from here
        try {
            # (its packages from cache\downloads, its model files already in place: nothing goes online)
            $code = RunStatus $Py @('-m', 'undercover.addons', 'install', $id, '--accept-licence', '--status', '--cancel-file', $CancelFile, '--from', $Cache) ($i / $addonIds.Count) (1 / $addonIds.Count)
        } finally { if (-not $DryRun) { Pop-Location } }
        if ($code -eq 3) { Cancelled }
        if ($code -ne 0) { Fail "the add-on '$id' couldn't be added (code $code)" }
        $i++
    }
}

# 8. Launcher --------------------------------------------------------------------------------
# Same behaviour as scripts\UnderCover.bat (dev): from the Start menu / a shortcut (started by Explorer) it runs in the
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

# 10. The downloads are installed: cache\downloads (the tools and packages) and uv's unpacked copies go, so they don't
# keep gigabytes; a repair fetches what it needs again. (The models are in their place, not in the cache.)
if (-not $DryRun) {
    foreach ($d in @($Cache, $env:UV_CACHE_DIR)) {
        $full = [IO.Path]::GetFullPath($d)
        if ($full.StartsWith([IO.Path]::GetFullPath((Join-Path $InstallDir 'cache')) + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $full)) {
            Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction SilentlyContinue
            Log "tidied $full"
        }
    }
}

Result "ok"
Write-Host ""
Write-Host "Done. Start UnderCover from the Start menu or with UnderCover.cmd." -ForegroundColor Green
Write-Host "Its AI models are installed: nothing downloads later, except add-ons you choose or a repair."
if ($Pause) { Read-Host "Press Enter to close" | Out-Null }
