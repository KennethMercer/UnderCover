# The installer's check of the files an earlier run kept (1.0.1): each listed file against its SHA-256, before the
# download page trusts it. A file that fails is deleted, so the download page fetches it again (a kept file used to be
# trusted by its size alone). The wizard starts this hidden and doesn't wait: it reads -Done (how many are checked so
# far) while it runs and -Marker (written whole, then named) when it ends: "checked: <the names that failed>".
#   powershell -File keptcheck.ps1 -List <path<TAB>sha256 per line> -Done <file> -Marker <file>
param([Parameter(Mandatory)][string]$List, [Parameter(Mandatory)][string]$Done, [Parameter(Mandatory)][string]$Marker)
if ($PSVersionTable.PSEdition -ne 'Core') { $env:PSModulePath = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') }
$bad = @()
$n = 0
foreach ($line in Get-Content -LiteralPath $List) {
    if (-not $line.Trim()) { continue }
    $file, $sha = $line -split "`t"
    try {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $file -ErrorAction Stop).Hash.ToLower() -ne $sha.Trim().ToLower()) {
            Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
            $bad += (Split-Path $file -Leaf)
        }
    } catch {
        $bad += (Split-Path $file -Leaf)   # (unreadable: not trusted; the download page fetches it again if it's gone)
    }
    $n++
    Set-Content -LiteralPath $Done $n
}
Set-Content -LiteralPath "$Marker.part" ('checked: ' + ($bad -join ', '))
Move-Item -LiteralPath "$Marker.part" -Destination $Marker -Force
