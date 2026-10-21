<#
.SYNOPSIS
  How much disk UnderCover really needs, per drive, and whether it fits. One implementation for the wizard (it runs
  this from its temp folder) and for setup.ps1 (dot-sourced); tests\smoke.py checks it with the free space stubbed.

  The sizes are measured (installer\sizes.json) and the margin (20 %) is added on top. What counts:
    - the install folder: the app, Python and PyTorch for the graphics card, and uv's download cache (it stays: a
      repair or a re-run reuses it);
    - the models folder: the AI models UnderCover fetches the first time each feature is used (and any add-ons);
    - the library and the working files: room to start with (they grow with use).

  As a command (the wizard):
    powershell -File space.ps1 -Out <file> -InstallDir <dir> [-LibraryDir ..] [-WorkDir ..] [-ModelsDir ..]
                               [-Backend auto|rocm|cuda128|cuda126|cpu] [-Addons f5,...]
  writes lines:  backend|<backend>
                 total|<GB>
                 need|<one short line per drive: "UnderCover needs about 22 GB on D: (188 GB free).">
                 parts|App 9 GB|AI models 10.5 GB|your files 2.5 GB   (the wizard joins them with a middle dot)
                 short|<plain sentence>     (one per drive that won't fit)
#>
# (the variables have names of their own: setup.ps1 dot-sources this file, and a parameter called $InstallDir here
# would replace setup's own; the command line still takes -InstallDir and the rest)
param(
    [Alias('Out')][string]$SpOut = '',
    [Alias('InstallDir')][string]$SpInstallDir = '',
    [Alias('LibraryDir')][string]$SpLibraryDir = '',
    [Alias('WorkDir')][string]$SpWorkDir = '',
    [Alias('ModelsDir')][string]$SpModelsDir = '',
    [Alias('Backend')][string]$SpBackend = 'auto',
    [Alias('Addons')][string]$SpAddons = ''
)
# Started from PowerShell 7 (a terminal, or anything started from one), Windows PowerShell inherits PowerShell 7's
# module path and can't load its own modules: Get-FileHash, Expand-Archive, ConvertFrom-Json are "not recognized". Its
# own path, then (only in Windows PowerShell: a PowerShell 7 session that dot-sources this keeps its own).
if ($PSVersionTable.PSEdition -ne 'Core') { $env:PSModulePath = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') }

function Read-Sizes([string]$Folder) {
    Get-Content -LiteralPath (Join-Path $Folder 'sizes.json') -Raw | ConvertFrom-Json
}

function Round-Up([double]$gb) { [math]::Ceiling($gb * 2) / 2 }   # to the next half GB

function Format-GB([double]$gb) { if ($gb -eq [math]::Floor($gb)) { '{0:0}' -f $gb } else { '{0:0.0}' -f $gb } }

# the bytes under a folder (0 when it isn't there; what can't be read isn't counted)
function Get-DirBytes([string]$Dir) {
    $n = [long]0
    try {
        if (-not [IO.Directory]::Exists($Dir)) { return $n }
        foreach ($f in ([IO.DirectoryInfo]::new($Dir)).EnumerateFiles('*', [IO.SearchOption]::AllDirectories)) { $n += $f.Length }
    } catch { }
    return $n
}

# what an earlier (or cancelled) setup already put in a folder, in GB, so a re-run or a reinstall isn't asked for room
# it already uses. $Kind: 'setup' (the install folder's .venv, tools and uv's cache) or 'models' (the models folder in it)
function Get-HaveGB([string]$Kind, [string]$Dir) {
    if (-not $Dir) { return 0 }
    $subs = if ($Kind -eq 'setup') { @('.venv', 'tools', (Join-Path 'cache' 'uv')) } else { @('models') }
    $n = [long]0
    foreach ($s in $subs) { $n += Get-DirBytes (Join-Path $Dir $s) }
    return $n / 1GB
}

function Get-SpaceNeeds([string]$InstallDir, [string]$LibraryDir, [string]$WorkDir, [string]$ModelsDir,
                        [string]$Backend, $Sizes, [string[]]$Addons = @(), [switch]$SetupOnly, [scriptblock]$Have = $null) {
    # -> [ordered]@{ "<drive root>" = @{ need = GB with the margin; items = @( @{ what; gb } ) } }
    # $Have: { param($kind, $dir) GB already there } (Get-HaveGB; tests stub it); what's there is taken off its part
    $python = $Sizes.python.$Backend
    if (-not $python) { $python = ($Sizes.python.PSObject.Properties | ForEach-Object { $_.Value } | Measure-Object -Maximum).Maximum }
    $addonGb = 0.0
    foreach ($a in $Addons) { if ($a -and $Sizes.addons.$a) { $addonGb += $Sizes.addons.$a } }
    $setupHave = if ($Have) { [double](& $Have 'setup' $InstallDir) } else { 0.0 }
    $parts = @(@{ dir = $InstallDir; gb = [math]::Max([double]0, [double]($Sizes.app_gb + $Sizes.tools_gb + $python - $setupHave)); what = 'the app, Python and PyTorch'; short = 'App' })
    # the AI models: setup downloads them (rc-fix4b), so setup's own check counts them too
    $mdir = if ($ModelsDir) { $ModelsDir } else { $InstallDir }
    $modelsHave = if ($Have) { [double](& $Have 'models' $mdir) } else { 0.0 }
    $parts += @{ dir = $mdir; gb = [math]::Max([double]0, [double]($Sizes.models_gb + $addonGb - $modelsHave))
                 what = 'the AI models, which setup downloads'; short = 'AI models' }
    if (-not $SetupOnly) {
        $parts += @{ dir = $(if ($LibraryDir) { $LibraryDir } else { $InstallDir }); gb = $Sizes.library_gb
                     what = 'your songs, covers and voices to start with'; short = 'your files' }
        $parts += @{ dir = $(if ($WorkDir) { $WorkDir } else { $InstallDir }); gb = $Sizes.work_gb; what = 'working files to start with'; short = 'your files' }
    }
    $out = [ordered]@{}
    foreach ($p in $parts) {
        $gb = Round-Up ($p.gb * $Sizes.margin)
        if ($gb -le 0) { continue }   # all there already
        $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($p.dir)).ToUpperInvariant()
        if (-not $out.Contains($root)) { $out[$root] = @{ raw = 0.0; items = @() } }
        $out[$root].raw += $p.gb
        $out[$root].items += @{ what = $p.what; short = $p.short; gb = $gb }
    }
    foreach ($k in @($out.Keys)) {   # the sum of the parts it lists, as said
        $n = 0.0; foreach ($i in $out[$k].items) { $n += $i.gb }; $out[$k].need = $n
    }
    return $out
}

function Get-FreeGB([string]$Root) {
    try { return [math]::Floor(([IO.DriveInfo]::new($Root).AvailableFreeSpace / 1GB) * 10) / 10 } catch { return -1 }
}

function Find-Shortfalls($Needs, [scriptblock]$Free) {
    # -> @( @{ drive; need; free } ) for every drive that won't fit. $Free: { param($root) GB } (tests stub it)
    $short = @()
    foreach ($k in $Needs.Keys) {
        $f = & $Free $k
        if ($f -ge 0 -and $f -lt $Needs[$k].need) { $short += @{ drive = $k; need = $Needs[$k].need; free = $f } }
    }
    return $short
}

function Format-Shortfall($s) {
    "$($s.drive.TrimEnd('\')) has $(Format-GB $s.free) GB free, and UnderCover needs about $(Format-GB $s.need) GB there."
}

# Free space as said on a page: whole GB from 10 up ("188 GB"), one decimal below
function Format-Free([double]$gb) { if ($gb -ge 10) { Format-GB ([math]::Floor($gb)) } else { Format-GB ([math]::Floor($gb * 10) / 10) } }

# the pages' first line, one per drive (the user: the paragraph was "excessively verbose, people will skip")
function Format-NeedLines($Needs, [scriptblock]$Free) {
    if (-not $Needs.Count) { return @("UnderCover is already set up in these folders, so it needs no more room.") }
    $lines = @()
    foreach ($k in $Needs.Keys) {
        $d = $k.TrimEnd('\')
        $f = & $Free $k
        $lines += "UnderCover needs about $(Format-GB $Needs[$k].need) GB on $d" + $(if ($f -ge 0) { " ($(Format-Free $f) GB free)." } else { "." })
    }
    return $lines
}

# the second, smaller line: what it's for, summed over the drives ("App 9 GB", "AI models 10.5 GB", "your files 2.5 GB")
function Format-Parts($Needs) {
    $sum = [ordered]@{ 'App' = 0.0; 'AI models' = 0.0; 'your files' = 0.0 }
    foreach ($k in $Needs.Keys) { foreach ($i in $Needs[$k].items) { $sum[$i.short] += $i.gb } }
    return @($sum.Keys | Where-Object { $sum[$_] -gt 0 } | ForEach-Object { "$_ $(Format-GB $sum[$_]) GB" })
}

if ($SpOut) {
    $here = $PSScriptRoot
    $sizes = Read-Sizes $here
    if ($SpBackend -eq 'auto') {
        try { $SpBackend = (& (Join-Path $here 'detect_gpu.ps1') | Out-String | ConvertFrom-Json).backend }
        catch { $SpBackend = '' }   # unknown: the largest download is used (an overestimate, never an underestimate)
    }
    $needs = Get-SpaceNeeds $SpInstallDir $SpLibraryDir $SpWorkDir $SpModelsDir $SpBackend $sizes @($SpAddons -split ',' | Where-Object { $_ }) `
                            -Have { param($k, $d) Get-HaveGB $k $d }
    $free = { param($r) Get-FreeGB $r }
    $total = 0.0
    foreach ($k in $needs.Keys) { $total += $needs[$k].need }
    $lines = @("backend|$SpBackend", "total|$(Format-GB $total)")
    foreach ($l in (Format-NeedLines $needs $free)) { $lines += "need|$l" }
    $parts = @(Format-Parts $needs)
    if ($parts.Count) { $lines += "parts|" + ($parts -join '|') }
    foreach ($s in (Find-Shortfalls $needs $free)) { $lines += "short|$(Format-Shortfall $s)" }
    [IO.File]::WriteAllLines($SpOut, [string[]]$lines)
}
