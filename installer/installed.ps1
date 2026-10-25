# What this install already has of the download list (1.0.3): an update downloads only what changed. One rule, used in
# two places: the installer runs this file hidden before its download page (-InstallDir -List -Out: the destinations
# it needn't fetch, one a line), and setup.ps1 dot-sources it to install only the pins that differ.
#   a package row (a pin, name==version): that exact version is installed in this install's own .venv (one setup made,
#     on the full-version Python), read from the venv itself, not from the record;
#   a tool row (uv, Python, ffmpeg, Deno): the tool is where setup would reuse it (setup leaves a present one alone);
#   anything else (a model): not decided here (the download page counts a model file in its place already).
# No venv setup made, one on the wrong Python, or one that won't start: nothing counts as installed.
#   powershell -File installed.ps1 -InstallDir <app> -List <downloads.tsv> -Out <file>
param([string]$InstallDir = '', [string]$List = '', [string]$Out = '')

. (Join-Path $PSScriptRoot 'python.ps1')

function Get-PinName([string]$pin) { (($pin -split '==')[0] -replace '[-_.]+', '-').ToLower() }

function Get-PythonVersion($rows) {
    # the Python setup unpacks, from the archive's name in the download list (cpython-3.12.10+...)
    $r = @($rows | Where-Object { $_.dest -match '^cache/cpython-(\d+\.\d+\.\d+)\+' } | Select-Object -First 1)
    if ($r.Count -and $r[0].dest -match '^cache/cpython-(\d+\.\d+\.\d+)\+') { return $Matches[1] }
    return ''
}

function Get-InstalledPins([string]$Dir, [string]$PythonVer) {
    # {name==version: $true} of what this install's .venv holds (names as pip normalises them), or an empty table
    $have = @{}
    $venv = Join-Path $Dir '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $venv 'undercover-setup.txt'))) { return $have }   # (not a venv setup made)
    if (-not $PythonVer -or -not (Test-VenvBase $venv $PythonVer) -or -not (Test-Path -LiteralPath $py)) { return $have }
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $py -I -c "import importlib.metadata as m`nfor d in m.distributions():`n    print(d.metadata['Name'], d.version, sep='==')" 2>$null)
        if ($LASTEXITCODE -ne 0) { return @{} }
    } catch { return @{} } finally { $ErrorActionPreference = $eap }
    foreach ($l in $lines) {
        if ("$l" -notmatch '==') { continue }
        $have["$(Get-PinName $l)==$(("$l" -split '==', 2)[1].Trim())"] = $true
    }
    return $have
}

function Test-Installed($row, $have, [string]$Dir, [string]$PythonVer) {
    if ($row.pin) { return [bool]$have["$(Get-PinName $row.pin)==$((($row.pin -split '==', 2)[1]).Trim())"] }
    $tools = Join-Path $Dir 'tools'
    switch -Regex ($row.dest) {
        '^cache/uv-'      { return (Test-Path -LiteralPath (Join-Path $tools 'uv\uv.exe')) }
        '^cache/cpython-' { return [bool]$PythonVer -and (Test-OwnPython (Join-Path $tools "python\cpython-$PythonVer-windows-x86_64-none\python.exe") $PythonVer) }
        '^cache/ffmpeg-'  { return (Test-Path -LiteralPath (Join-Path $tools 'ffmpeg\bin\ffmpeg.exe')) }
        '^cache/deno-'    { return (Test-Path -LiteralPath (Join-Path $tools 'deno\deno.exe')) }
    }
    return $false
}

if ($InstallDir -and $List -and $Out) {
    $rows = @(Import-Csv -LiteralPath $List -Delimiter "`t")
    $pv = Get-PythonVersion $rows
    $have = Get-InstalledPins $InstallDir $pv
    $skip = @($rows | Where-Object { $_.dest -like 'cache/*' -and (Test-Installed $_ $have $InstallDir $pv) } | ForEach-Object { $_.dest })
    Set-Content -LiteralPath "$Out.part" -Value $skip -Encoding ascii
    Move-Item -LiteralPath "$Out.part" -Destination $Out -Force
}
