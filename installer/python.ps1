<#
  The installer's own Python, private to this install (dot-sourced by setup.ps1; tests/smoke.py runs the finder on
  folders of its own). uv installs it into tools\python with --no-bin (no python.exe shim in %USERPROFILE%\.local\bin)
  and --no-registry (nothing under HKCU\Software\Python).

  uv also makes a "minor version" link beside the install (cpython-3.12-..., a directory junction). Windows can refuse
  a junction made without admin rights ("untrusted mount point", os error 448: the user's 1.0 installer test), and uv
  then exits with an error although the full install is there. So setup finds the full-version folder itself
  (cpython-3.12.10-...), checks that its python.exe runs and is that version, and makes the venv from it: never the
  link.
#>

function Get-OwnPython([string]$InstallDir, [string]$Version) {
    # the full-version folder only: "cpython-3.12.10-*" never matches the minor link "cpython-3.12-*"
    $d = Get-ChildItem -LiteralPath $InstallDir -Directory -Filter "cpython-$Version-*" -ErrorAction SilentlyContinue |
        Sort-Object Name | Select-Object -First 1
    if ($d -and (Test-Path -LiteralPath (Join-Path $d.FullName 'python.exe'))) { return (Join-Path $d.FullName 'python.exe') }
    return $null
}

function Test-OwnPython([string]$Exe, [string]$Version) {
    # it runs, and it's the version asked for
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $v = (& $Exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null | Out-String).Trim() }
    catch { $v = '' }
    finally { $ErrorActionPreference = $old }
    return ($v -eq $Version)
}

function Test-VenvBase([string]$Venv, [string]$Version) {
    # The venv is built on a full-version folder (pyvenv.cfg's home is "...\cpython-3.12.10-..." and its python.exe is
    # there). uv's own venvs are built on the minor-version link instead (so a patch upgrade carries over), and where
    # Windows refused that link (os error 448) every use of the venv fails the same way: the user's 1.0 test stopped at
    # PyTorch with it. Setup makes the venv with Python's own venv module from the full-version python.exe.
    $cfg = Join-Path $Venv 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg)) { return $false }
    $line = Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    if (-not $line) { return $false }
    $venvHome = ($line -replace '^\s*home\s*=\s*', '').Trim()
    return ((Split-Path $venvHome -Leaf) -like "cpython-$Version-*") -and (Test-Path -LiteralPath (Join-Path $venvHome 'python.exe'))
}

function Remove-MinorLink([string]$PythonDir, [string]$Version, [switch]$EvenIfUsable) {
    # uv's minor-version link (cpython-3.12-..., a junction beside the full install). Where Windows won't let it be
    # traversed it's removed, so nothing can pick it up: the link only (a non-recursive delete of a reparse point),
    # never what it points at, and never a real folder. -EvenIfUsable: tests. -> the links removed
    $mm = (($Version -split '\.')[0..1]) -join '.'
    $gone = @()
    foreach ($d in @(Get-ChildItem -LiteralPath $PythonDir -Directory -Force -Filter "cpython-$mm-*" -ErrorAction SilentlyContinue)) {
        if ($d.Name -like "cpython-$Version-*") { continue }                               # the full install itself
        if (-not ($d.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }      # a real folder: not a link
        $usable = $false
        try { $usable = [bool](Test-Path -LiteralPath (Join-Path $d.FullName 'python.exe') -ErrorAction Stop) } catch { $usable = $false }
        if ($usable -and -not $EvenIfUsable) { continue }
        try { [IO.Directory]::Delete($d.FullName, $false); $gone += $d.Name } catch { }
    }
    return $gone
}

function Remove-OwnPythonRegistry([string]$InstallDir) {
    # An earlier setup (before --no-registry) registered this install's Python under HKCU\Software\Python\Astral.
    # Only keys whose InstallPath is inside this install's tools\python are removed: anything else is someone else's.
    $base = 'HKCU:\Software\Python\Astral'
    if (-not (Test-Path $base)) { return @() }
    $root = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
    $gone = @()
    foreach ($k in @(Get-ChildItem $base -ErrorAction SilentlyContinue)) {
        $ip = (Get-ItemProperty -LiteralPath (Join-Path $k.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
        if (-not $ip) { continue }
        $at = [IO.Path]::GetFullPath($ip).TrimEnd('\') + '\'
        if ($at.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $k.PSPath -Recurse -Force -ErrorAction SilentlyContinue
            $gone += $k.PSChildName
        }
    }
    return $gone
}
