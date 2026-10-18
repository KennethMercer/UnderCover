<#
.SYNOPSIS
  Release check for the installer: a REAL install by a normal user (no admin) into a fresh folder, all the way to
  selftest passing. Run it before any installer release. It downloads about 3 GB and takes 20-40 minutes; the GPU is
  only touched by selftest's quick check at the end.

  powershell -ExecutionPolicy Bypass -File tests\installer_e2e.ps1 [-Root D:\] [-Backend auto] [-Keep]

  -Root     the drive or folder the temp install goes in (default: this repo's drive; the user hit os error 448 on D:)
  -Backend  as setup.ps1's (auto: detected)
  -Keep     leave the temp install in place (default: deleted after the checks, pass or fail)

  The app is laid out as the installer lays it out (UnderCover.iss [Files]); then installer\setup.ps1 runs for real.
  Checked afterwards:
    - setup exited 0 and logs\setup-result.txt says "ok";
    - logs\selftest.json has no problems;
    - .venv\pyvenv.cfg's home is the full-version Python folder (cpython-<version>-...), not uv's minor-version link;
    - no registry entry under HKCU\Software\Python points into the temp install;
    - nothing new in %USERPROFILE%\.local\bin.
  Exit code 0 only when every check passes. The tail of setup.log is printed either way.
#>
param(
    [string]$Root = '',
    [string]$Backend = 'auto',
    [switch]$Keep
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) { Write-Host "Run this without admin rights: the check is a normal user's install." -ForegroundColor Red; exit 2 }

if (-not $Root) { $Root = (Split-Path $repo -Qualifier) + '\' }
$dir = Join-Path $Root ('uc-e2e-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Write-Host "Installer end-to-end check: a real install into $dir" -ForegroundColor White

# what's there before, outside the install (it must stay the same)
$binDir = Join-Path $env:USERPROFILE '.local\bin'
function BinList { if (Test-Path $binDir) { @(Get-ChildItem -LiteralPath $binDir -File | ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) } else { @() } }
$binBefore = BinList

# the installer's layout (UnderCover.iss [Files])
function CopyTree([string]$from, [string]$to) {
    New-Item -ItemType Directory -Force $to | Out-Null
    Get-ChildItem -LiteralPath $from -Recurse -File | Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.Extension -ne '.pyc' } |
        ForEach-Object {
            $rel = $_.FullName.Substring($from.Length).TrimStart('\')
            $dest = Join-Path $to $rel
            New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $dest
        }
}
New-Item -ItemType Directory -Force $dir | Out-Null
CopyTree (Join-Path $repo 'undercover') (Join-Path $dir 'undercover')
New-Item -ItemType Directory -Force (Join-Path $dir 'scripts'), (Join-Path $dir 'installer\requirements') | Out-Null
Copy-Item (Join-Path $repo 'scripts\check_env.py') (Join-Path $dir 'scripts')
foreach ($f in 'setup.ps1', 'detect_gpu.ps1', 'python.ps1', 'selftest.py', 'constraints.txt') {
    Copy-Item (Join-Path $repo "installer\$f") (Join-Path $dir 'installer')
}
Copy-Item (Join-Path $repo 'installer\requirements\*.txt') (Join-Path $dir 'installer\requirements')

$t0 = Get-Date
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dir 'installer\setup.ps1') -InstallDir $dir -Backend $Backend
$code = $LASTEXITCODE
$mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)

$fails = @()
if ($code -ne 0) { $fails += "setup exited $code" }
$result = Join-Path $dir 'logs\setup-result.txt'
$said = if (Test-Path $result) { (Get-Content -LiteralPath $result -Raw).Trim() } else { '(no result line)' }
if ($said -ne 'ok') { $fails += "setup-result.txt says: $said" }
$st = Join-Path $dir 'logs\selftest.json'
if (Test-Path $st) {
    $rep = Get-Content -LiteralPath $st -Raw | ConvertFrom-Json
    if (@($rep.problems).Count) { $fails += "selftest problems: $(@($rep.problems) -join '; ')" }
    if (@($rep.warnings).Count) { Write-Host "selftest warnings: $(@($rep.warnings) -join '; ')" -ForegroundColor Yellow }
} else { $fails += 'no selftest.json (selftest never ran)' }
$cfg = Join-Path $dir '.venv\pyvenv.cfg'
$venvHome = if (Test-Path $cfg) { ((Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1) -replace '^\s*home\s*=\s*', '').Trim() } else { '' }
if ((Split-Path $venvHome -Leaf) -notmatch '^cpython-\d+\.\d+\.\d+-') { $fails += ".venv's home is '$venvHome', not the full-version Python folder" }
$reg = @(Get-ChildItem 'HKCU:\Software\Python' -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -eq 'InstallPath' } |
    Where-Object { "$((Get-ItemProperty -LiteralPath $_.PSPath).'(default)')".StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) })
if ($reg.Count) { $fails += "registry entries point into the install: $(($reg | ForEach-Object { $_.Name }) -join ', ')" }
$binAfter = BinList
if (@(Compare-Object $binBefore $binAfter).Count) { $fails += "$binDir changed" }

Write-Host ""
Write-Host "---- setup.log, last 25 lines ----"
$log = Join-Path $dir 'logs\setup.log'
if (Test-Path $log) { Get-Content -LiteralPath $log -Tail 25 }
Write-Host "----"
Write-Host "Took $mins min. venv home: $venvHome"

if (-not $Keep) {
    # junctions first, as links (a recursive delete must never walk through one), then the folder
    Get-ChildItem -LiteralPath $dir -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
        ForEach-Object { try { [IO.Directory]::Delete($_.FullName, $false) } catch { } }
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $dir) { Write-Host "Couldn't delete all of $dir" -ForegroundColor Yellow } else { Write-Host "Deleted $dir" }
}

if ($fails.Count) {
    Write-Host "FAILED:" -ForegroundColor Red
    $fails | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
Write-Host "PASSED: a normal user's install, all the way to selftest." -ForegroundColor Green
exit 0
