<#
.SYNOPSIS
  Detects the GPUs in this PC and picks the PyTorch build UnderCover should install.

.DESCRIPTION
  Read-only: queries Windows (Win32_VideoController) and, if present, nvidia-smi.
  Prints one JSON object (or a readable summary with -Human).

  backend values:
    cuda128  NVIDIA, compute capability 7.5+ (RTX 20 series and newer, including RTX 50)
    cuda126  NVIDIA, compute capability 5.0-7.4 (GTX 900/10 series, older Quadro/Titan)
    rocm     AMD on AMD's Windows ROCm support list (RX 7000 / RX 9000, see $RocmSupported)
    cpu      anything else (older AMD, Intel, no GPU) -- works, but slowly

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File detect_gpu.ps1 -Human
#>
param([switch]$Human)

$ErrorActionPreference = 'Stop'

# AMD ROCm 7.2.1 for Windows supports gfx1200/gfx1201/gfx1100/gfx1101 (checked 2026-10-09:
# rocm.docs.amd.com > Radeon/Ryzen > Windows compatibility). Windows only reports product names,
# so match by name. RX 7600 (gfx1102) is NOT on the list.
$RocmSupported = @(
    @{ Pattern = 'RX 9070';            Gfx = 'gfx1201' },
    @{ Pattern = 'AI PRO R9700';       Gfx = 'gfx1201' },
    @{ Pattern = 'RX 9060';            Gfx = 'gfx1200' },
    @{ Pattern = 'RX 7900';            Gfx = 'gfx1100' },
    @{ Pattern = 'PRO W7900';          Gfx = 'gfx1100' },
    @{ Pattern = 'PRO W7800';          Gfx = 'gfx1100' },
    @{ Pattern = 'RX 7800';            Gfx = 'gfx1101' },
    @{ Pattern = 'RX 7700';            Gfx = 'gfx1101' }
)

function Get-NvidiaGpus {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        $fallback = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
        if (Test-Path $fallback) { $smi = $fallback } else { return @() }
    } else { $smi = $smi.Source }
    try {
        $lines = & $smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>$null
    } catch { return @() }
    foreach ($l in $lines) {
        $f = $l -split '\s*,\s*'
        if ($f.Count -lt 4) { continue }
        [pscustomobject]@{
            Vendor = 'NVIDIA'; Name = $f[0]; VramGB = [math]::Round([double]$f[1] / 1024, 1)
            ComputeCap = $f[2]; Driver = $f[3]
        }
    }
}

function Get-RegistryVramGB([string]$name) {
    $base = 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    foreach ($k in (Get-ChildItem $base -ErrorAction SilentlyContinue)) {
        $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
        if ($p.DriverDesc -eq $name -and $p.'HardwareInformation.qwMemorySize') {
            return [math]::Round([double]$p.'HardwareInformation.qwMemorySize' / 1GB, 1)
        }
    }
    return $null
}

$nvidia = @(Get-NvidiaGpus)
$all = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    $vendor = switch -Regex ($_.PNPDeviceID) {
        'VEN_10DE' { 'NVIDIA' } 'VEN_1002' { 'AMD' } 'VEN_8086' { 'Intel' } default { 'Other' }
    }
    [pscustomobject]@{ Vendor = $vendor; Name = $_.Name.Trim(); VramGB = (Get-RegistryVramGB $_.Name); Driver = $_.DriverVersion }
})

$choice = $null
# 1) NVIDIA wins when nvidia-smi works (it means the driver is installed and healthy). Pick the most VRAM.
if ($nvidia.Count) {
    $g = $nvidia | Sort-Object VramGB -Descending | Select-Object -First 1
    $cc = [double]$g.ComputeCap
    $backend = if ($cc -ge 7.5) { 'cuda128' } elseif ($cc -ge 5.0) { 'cuda126' } else { 'cpu' }
    $why = if ($backend -eq 'cpu') { "compute capability $($g.ComputeCap) is too old for current PyTorch" } else { "NVIDIA, compute capability $($g.ComputeCap)" }
    $choice = [pscustomobject]@{ Backend = $backend; Gpu = $g.Name; VramGB = $g.VramGB; Gfx = $null; Reason = $why }
}
# 2) AMD on the ROCm list. The name substring goes into config [gpu].device_name so the iGPU is never picked.
if (-not $choice) {
    foreach ($g in ($all | Where-Object Vendor -eq 'AMD' | Sort-Object VramGB -Descending)) {
        $hit = $RocmSupported | Where-Object { $g.Name -like "*$($_.Pattern)*" } | Select-Object -First 1
        if ($hit) {
            $choice = [pscustomobject]@{ Backend = 'rocm'; Gpu = $g.Name; VramGB = $g.VramGB; Gfx = $hit.Gfx; Reason = "AMD $($hit.Gfx), on AMD's Windows ROCm list" }
            break
        }
    }
}
# 3) Everything else runs on the CPU.
if (-not $choice) {
    $best = $all | Where-Object { $_.Vendor -in 'AMD', 'Intel', 'NVIDIA' } | Sort-Object VramGB -Descending | Select-Object -First 1
    $why = if ($best) { "$($best.Name) has no supported PyTorch GPU build on Windows" } else { 'no GPU found' }
    $choice = [pscustomobject]@{ Backend = 'cpu'; Gpu = $best.Name; VramGB = $best.VramGB; Gfx = $null; Reason = $why }
}

$result = [pscustomobject]@{
    backend = $choice.Backend; gpu = $choice.Gpu; vram_gb = $choice.VramGB; gfx = $choice.Gfx; reason = $choice.Reason
    adapters = $all; nvidia_smi = $nvidia
}

if ($Human) {
    "Adapters found:"
    $all | ForEach-Object { "  - [$($_.Vendor)] $($_.Name)  VRAM: $(if ($_.VramGB) { "$($_.VramGB) GB" } else { 'unknown' })  driver $($_.Driver)" }
    ""
    "Chosen: $($choice.Backend)  ->  $($choice.Gpu)"
    "Why:    $($choice.Reason)"
} else {
    $result | ConvertTo-Json -Depth 4
}
