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
                 label|<plain sentences: what goes where, and each drive's free space>
                 short|<plain sentence>     (one per drive that won't fit)
#>
param(
    [string]$Out = '',
    [string]$InstallDir = '',
    [string]$LibraryDir = '',
    [string]$WorkDir = '',
    [string]$ModelsDir = '',
    [string]$Backend = 'auto',
    [string]$Addons = ''
)

function Read-Sizes([string]$Folder) {
    Get-Content -LiteralPath (Join-Path $Folder 'sizes.json') -Raw | ConvertFrom-Json
}

function Round-Up([double]$gb) { [math]::Ceiling($gb * 2) / 2 }   # to the next half GB

function Format-GB([double]$gb) { if ($gb -eq [math]::Floor($gb)) { '{0:0}' -f $gb } else { '{0:0.0}' -f $gb } }

function Get-SpaceNeeds([string]$InstallDir, [string]$LibraryDir, [string]$WorkDir, [string]$ModelsDir,
                        [string]$Backend, $Sizes, [string[]]$Addons = @(), [switch]$SetupOnly) {
    # -> [ordered]@{ "<drive root>" = @{ need = GB with the margin; items = @( @{ what; gb } ) } }
    $python = $Sizes.python.$Backend
    if (-not $python) { $python = ($Sizes.python.PSObject.Properties | ForEach-Object { $_.Value } | Measure-Object -Maximum).Maximum }
    $addonGb = 0.0
    foreach ($a in $Addons) { if ($a -and $Sizes.addons.$a) { $addonGb += $Sizes.addons.$a } }
    $parts = @(@{ dir = $InstallDir; gb = $Sizes.app_gb + $Sizes.tools_gb + $python; what = 'the app, Python and PyTorch' })
    if (-not $SetupOnly) {
        $parts += @{ dir = $(if ($ModelsDir) { $ModelsDir } else { $InstallDir }); gb = $Sizes.models_gb + $addonGb
                     what = 'the AI models, which UnderCover fetches the first time each feature is used' }
        $parts += @{ dir = $(if ($LibraryDir) { $LibraryDir } else { $InstallDir }); gb = $Sizes.library_gb
                     what = 'your songs, covers and voices to start with' }
        $parts += @{ dir = $(if ($WorkDir) { $WorkDir } else { $InstallDir }); gb = $Sizes.work_gb; what = 'working files to start with' }
    }
    $out = [ordered]@{}
    foreach ($p in $parts) {
        $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($p.dir)).ToUpperInvariant()
        if (-not $out.Contains($root)) { $out[$root] = @{ raw = 0.0; items = @() } }
        $out[$root].raw += $p.gb
        $out[$root].items += @{ what = $p.what; gb = Round-Up ($p.gb * $Sizes.margin) }
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

function Format-Needs($Needs, [scriptblock]$Free) {
    # the folder page's text: the total, then what goes on each drive and how much that drive has free
    $total = 0.0
    foreach ($k in $Needs.Keys) { $total += $Needs[$k].need }
    $text = "UnderCover needs about $(Format-GB $total) GB in all, more than this installer's size suggests."
    foreach ($k in $Needs.Keys) {
        $d = $k.TrimEnd('\')
        $list = @($Needs[$k].items | ForEach-Object { "about $(Format-GB $_.gb) GB for $($_.what)" })
        $items = if ($list.Count -gt 1) { ($list[0..($list.Count - 2)] -join '; ') + '; and ' + $list[-1] } else { $list[0] }
        $f = & $Free $k
        $text += " On $d it needs about $(Format-GB $Needs[$k].need) GB: $items."
        if ($f -ge 0) { $text += " $d has $(Format-GB $f) GB free." }
    }
    return $text
}

if ($Out) {
    $here = $PSScriptRoot
    $sizes = Read-Sizes $here
    if ($Backend -eq 'auto') {
        try { $Backend = (& (Join-Path $here 'detect_gpu.ps1') | Out-String | ConvertFrom-Json).backend }
        catch { $Backend = '' }   # unknown: the largest download is used (an overestimate, never an underestimate)
    }
    $needs = Get-SpaceNeeds $InstallDir $LibraryDir $WorkDir $ModelsDir $Backend $sizes @($Addons -split ',' | Where-Object { $_ })
    $free = { param($r) Get-FreeGB $r }
    $total = 0.0
    foreach ($k in $needs.Keys) { $total += $needs[$k].need }
    $lines = @("backend|$Backend", "total|$(Format-GB $total)", "label|$(Format-Needs $needs $free)")
    foreach ($s in (Find-Shortfalls $needs $free)) { $lines += "short|$(Format-Shortfall $s)" }
    [IO.File]::WriteAllLines($Out, [string[]]$lines)
}
