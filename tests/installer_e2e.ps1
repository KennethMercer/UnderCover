<#
.SYNOPSIS
  Release check for the installer: the REAL installer, run by a normal user (no admin) into a fresh folder, clicked
  through as a person would, all the way to selftest passing and a first use. Run it before any installer release,
  and only when MasterPlan has cleared it (nobody at the PC: its wizard runs on this desktop, off-screen). It downloads
  about 11 GB and takes 30-60 minutes; the GPU is only touched by selftest's quick check and the first use.

  powershell -ExecutionPolicy Bypass -File tests\installer_e2e.ps1 [-Root D:\] [-Keep] [-CancelCheck] [-NoFirstUse]

  -Root     the drive or folder the test install goes in (default: this repo's drive; the user hit os error 448 on D:)
  -Keep     leave the test install in place (default: deleted after the checks, pass or fail)
  -CancelCheck  first a silent run whose download page's Stop is clicked once 3 files are in: the installer must end
                (as Inno does, exit 1, its log saying it aborted), keep what's downloaded (each file whole, its SHA-256
                right, nothing half-made in place) and install nothing; the real run after it must carry on from there,
                fetching none of those files again
  -NoFirstUse   skip the first cover and speech

  The installer is built here as its test variant (ISCC /DReleaseCheck on installer\UnderCover.iss): the same code
  under its own name ("UnderCover release check (test)") and AppId, with no uninstaller, no shortcuts and no
  registration. Its wizard is a tool window on no screen (not in the taskbar or Alt+Tab), its own message boxes are
  written to a file instead of shown, and it tells the driver its window handles and progress through files (/HOOKS=).
  A second process (the driver) clicks Next, Install and Finish with valid inputs only, and watches it.
  Checked afterwards:
    - the installer exited 0 and logs\setup-result.txt says "ok"; no message box came up at all;
    - a download that comes back short is tried again by itself (a local server answers the first file's first try
      with a 100 MB length, sends 1 MB and closes, as AMD's server once did);
    - a big file that arrives whole with the wrong bytes is caught by its SHA-256 (checked off Inno's thread, before
      anything uses it), deleted and fetched again by itself (the same server sends the first big file's exact size
      in zeros);
    - on the download page (rc-fix6, the user's install: the wizard refused every click for all of setup), every
      second: the wizard enabled and its Stop button there; it answered within a second (WM_NULL), never hung, moved
      when moved (SetWindowPos) and minimised and restored;
    - setup's offline steps: each one's time (setup.log), none over 5 minutes (the wizard is held still meanwhile);
      every status step from 1 to the last, in order;
    - logs\selftest.json has no problems; .venv's home is the full-version Python folder; no registry entry under
      HKCU\Software\Python points into the install; nothing new in %USERPROFILE%\.local\bin; no shortcut made; no
      uninstaller registered; cache\downloads tidied away;
    - every model is in place (python -m undercover.models check);
    - a first cover and speech on the fresh install download nothing (the installed copy runs on a free port with its
      own folders; its models folder and the Hugging Face, torch and Whisper caches are the same before and after, and
      its log says no download);
    - the graphics driver never reset during the run (no Display 4101, no LiveKernelEvent 141 / 117) and no log says
      the device was lost.
  Exit code 0 only when every check passes. The tail of setup.log is printed either way.
#>
param(
    [string]$Root = '',
    [switch]$Keep,
    [switch]$CancelCheck,
    [switch]$NoFirstUse,
    [string]$FirstUseOnly = ''   # an install made by an earlier run (-Keep): only its first-use check
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) { Write-Host "Run this without admin rights: the check is a normal user's install." -ForegroundColor Red; exit 2 }

if (-not $Root) { $Root = (Split-Path $repo -Qualifier) + '\' }
$dir = Join-Path $Root ('uc-e2e-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$runStart = Get-Date
if ($FirstUseOnly) { $dir = (Resolve-Path -LiteralPath $FirstUseOnly).Path }
$vpy = Join-Path $dir '.venv\Scripts\python.exe'

# a first cover and speech on the fresh install download nothing (the user: "we absolutely should not be downloading
# modules the first time they're called")
function CacheList {
    $roots = @((Join-Path $dir 'models'), $(if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $env:USERPROFILE '.cache\huggingface' }),
               (Join-Path $env:USERPROFILE '.cache\torch'), (Join-Path $env:USERPROFILE '.cache\whisper'))
    @(foreach ($r in $roots) { if (Test-Path -LiteralPath $r) { Get-ChildItem -LiteralPath $r -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '\.lock$' } | ForEach-Object { "$($_.FullName)|$($_.Length)" } } })
}
function FirstUse {
    $port = (7862..7869 | Where-Object { $c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', $_); $c.Close(); $false } catch { $true } } | Select-Object -First 1)
    if (-not $port) { return 'no free port in 7862-7869' }
    $base = "http://127.0.0.1:$port"
    $h = @{ Origin = $base }
    $before = CacheList
    $out = Join-Path $dir 'logs\firstuse-server.log'
    $saved = @{}
    foreach ($k in @('UNDERCOVER_CONFIG', 'UNDERCOVER_DATA_ROOT', 'UNDERCOVER_TEST_ROOTS', 'UNDERCOVER_SANDBOX_PORT')) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $null)
    }
    try {
        $srv = Start-Process -FilePath $vpy -ArgumentList '-m', 'undercover.server', '--no-browser', '--no-tray', '--port', "$port" -WorkingDirectory $dir `
                             -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError "$out.err" -PassThru
    } finally { foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) } }
    $said = @()
    try {
        $deadline = (Get-Date).AddMinutes(10); $st = $null
        while ((Get-Date) -lt $deadline -and -not $srv.HasExited) {
            Start-Sleep -Seconds 2
            try { $st = Invoke-RestMethod "$base/api/status" -TimeoutSec 5 } catch { continue }
            if ($st.models -eq 'ready' -or "$($st.models)".StartsWith('error')) { break }
        }
        if (-not $st -or $st.models -ne 'ready') { return "the installed copy's models didn't load: $($st.models)" }
        if ($st.first_run) { return "the installed copy's first start had models to fetch (models_mb $($st.models_mb))" }
        $jobs = @()
        $r = Invoke-RestMethod "$base/api/auto" -Method Post -Headers $h -ContentType 'application/json' -Body (@{ text = 'Hold On as Agent Smoke' } | ConvertTo-Json)
        $jobs += @{ id = $r.job; what = 'the first cover (one-shot)' }
        foreach ($cad in @($false, $true)) {
            try {
                $r = Invoke-RestMethod "$base/api/speak" -Method Post -Headers $h -ContentType 'application/json' `
                     -Body (@{ text = 'This is the first thing I say.'; voice = 'agent-smoke'; cadence = $cad } | ConvertTo-Json)
                $jobs += @{ id = $r.job; what = $(if ($cad) { 'speech with cadence (Chatterbox Turbo)' } else { 'speech (the Reader)' }) }
            } catch { $said += "speech (cadence $cad) refused: $($_.ErrorDetails.Message)" }
        }
        foreach ($j in $jobs) {
            $deadline = (Get-Date).AddMinutes(20); $snap = $null
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 3
                $all = Invoke-RestMethod "$base/api/jobs" -TimeoutSec 10   # (assigned first: PS 5.1 pipes a JSON array as one object)
                $snap = $all | Where-Object { $_.id -eq $j.id } | Select-Object -First 1
                if ($snap -and $snap.final) { break }
            }
            $state = if ($snap -and $snap.final) { $snap.final.state } else { 'still running' }
            $said += "$($j.what): $state"
            if ($state -ne 'done') { return "$($j.what) ended '$state': $($snap.final.message)" }
        }
    } finally {
        try { Invoke-RestMethod "$base/api/quit" -Method Post -Headers $h -ContentType 'application/json' -Body '{}' -TimeoutSec 10 | Out-Null } catch { }
        if (-not $srv.WaitForExit(60000)) { try { $srv.Kill() } catch { } }
        Write-Host ("First use: " + ($said -join '; '))
    }
    $after = CacheList
    # (not downloads: the first start seeds the demo voice into models\voices from the app's own assets, and diffusers
    # writes a tiny version marker on import; anything else new in the models folder or the caches is a download)
    $new = @(Compare-Object $before $after | Where-Object { $_.SideIndicator -eq '=>' } | ForEach-Object {
        $f, $n = $_.InputObject -split '\|'; [pscustomobject]@{ f = $f; n = [long]$n } } |
        Where-Object { $_.f -notlike (Join-Path $dir 'models\voices\*') -and -not ($_.n -le 1024 -and (Split-Path $_.f -Leaf) -like 'version_*.txt') } |
        ForEach-Object { $_.f })
    if ($new.Count) { return "files appeared during first use: $(($new | Select-Object -First 5) -join ', ')" }
    $log = (Get-Content -LiteralPath $out -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath "$out.err" -Raw -ErrorAction SilentlyContinue)
    if ($log -match 'Downloading|download_checks|iB/s') { return "the installed copy's log mentions a download" }
    return $null
}
if ($FirstUseOnly) {
    $runStart = Get-Date
    $why = FirstUse
    if ($why) { Write-Host "FAILED: first use: $why" -ForegroundColor Red; exit 1 }
    Write-Host "PASSED: first use downloaded nothing." -ForegroundColor Green; exit 0
}

Write-Host "Installer end-to-end check: a real install into $dir" -ForegroundColor White

# what's there before, outside the install (it must stay the same)
$binDir = Join-Path $env:USERPROFILE '.local\bin'
function BinList { if (Test-Path $binDir) { @(Get-ChildItem -LiteralPath $binDir -File | ForEach-Object { "$($_.Name)|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)" }) } else { @() } }
$binBefore = BinList
function Shortcuts { @(foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    Get-ChildItem -LiteralPath $d -Recurse -Filter '*.lnk' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } }) }
$linksBefore = Shortcuts

# the installer itself, built as its test variant (UnderCover.iss with ReleaseCheck): its own name and AppId, no
# uninstaller, no shortcuts, no registration; its message boxes go to a file; its wizard out of the taskbar and Alt+Tab
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Host "Inno Setup 6 (ISCC.exe) isn't installed: it's needed to build the installer." -ForegroundColor Red; exit 2 }
$launch = Join-Path $env:TEMP ('uc-e2e-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$hooks = Join-Path $launch 'hooks'
New-Item -ItemType Directory -Force $hooks | Out-Null
& $iscc /Q /DReleaseCheck=1 "/O$launch" (Join-Path $repo 'installer\UnderCover.iss') | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't build the installer's test variant." -ForegroundColor Red; exit 2 }
$exe = Join-Path $launch 'UnderCover-release-check.exe'

# the driver: clicks the wizard through as a person would (Next, Install, Finish; valid inputs only) and watches it.
# While it downloads (Inno's download page) every second: the wizard and its Stop button are enabled; it answers within
# a second (WM_NULL); once, it is moved (SetWindowPos) and minimised and restored. While setup runs offline: how long,
# and how long Inno held the wizard still. -Mode abort (a silent run): Stop is clicked once 3 files are in, and the
# installer must end. Any message box is written down and closed (there should be none).
$measure = @'
param([string]$Hooks, [string]$Out, [string]$Mode)
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class W {
  [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, IntPtr w, IntPtr l, uint f, uint t, out IntPtr r);
  [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageTimeoutW")] public static extern IntPtr SendText(IntPtr h, uint m, IntPtr w, StringBuilder l, uint f, uint t, out IntPtr r);
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowEnabled(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint f);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
}
"@
$res = [ordered]@{}
function Quit([string]$why) { $res['error'] = $why; Save; exit }
function Save { @($res.Keys | ForEach-Object { "$_=$($res[$_])" }) | Set-Content -LiteralPath $Out }
function Read1([string]$name) { try { $f = Join-Path $Hooks $name; if (Test-Path $f) { return (Get-Content -LiteralPath $f -Raw).Trim() } } catch { }; return '' }
function ClassOf($w) { $c = New-Object Text.StringBuilder 64; [void][W]::GetClassName($w, $c, 64); $c.ToString() }
$t0 = Get-Date
while (-not (Read1 'handles.txt')) { Start-Sleep -Milliseconds 200; if (((Get-Date) - $t0).TotalMinutes -gt 5) { Quit 'the wizard never opened' } }
$h, $next, $cancel, $abort = (Read1 'handles.txt') -split ' ' | ForEach-Object { [IntPtr][long]$_ }
$script:wpid = [uint32]0; [void][W]::GetWindowThreadProcessId($h, [ref]$script:wpid)
$res['toolwindow'] = [bool]([W]::GetWindowLong($h, -20) -band 0x80)   # out of the taskbar and Alt+Tab
# a message box of the installer's process: written down, closed with No / Cancel (there should be none)
$script:boxes = @()
function Boxes {
    $logged = Read1 'boxes.txt'   # (the test variant writes its boxes down instead of showing them)
    if ($logged) { Quit "a box came up: $($logged -replace '\s+', ' ')" }
    $script:found = @()
    $cb = [W+EnumProc]{ param($w, $l)
        $p = [uint32]0; [void][W]::GetWindowThreadProcessId($w, [ref]$p)
        if ($p -eq $script:wpid -and [W]::IsWindowVisible($w) -and (ClassOf $w) -eq '#32770') { $script:found += $w }
        return $true }
    [void][W]::EnumWindows($cb, [IntPtr]::Zero)
    foreach ($b in $script:found) {
        $script:said = @()
        $cb2 = [W+EnumProc]{ param($w, $l)
            if ((ClassOf $w) -eq 'Static') { $t = New-Object Text.StringBuilder 1024; $r = [IntPtr]::Zero
                [void][W]::SendText($w, 0x0D, [IntPtr]1024, $t, 2, 2000, [ref]$r); if ($t.Length) { $script:said += $t.ToString() } }
            return $true }
        [void][W]::EnumChildWindows($b, $cb2, [IntPtr]::Zero)
        $script:boxes += (($script:said -join ' ') -replace '\s+', ' ').Trim()
        [void][W]::PostMessage($b, 0x111, [IntPtr]7, [IntPtr]::Zero); [void][W]::PostMessage($b, 0x111, [IntPtr]2, [IntPtr]::Zero)
    }
}
$lat = New-Object System.Collections.Generic.List[double]; $sw = New-Object Diagnostics.Stopwatch
$script:timeouts = 0; $script:hung = 0
$script:hangAt = [ordered]@{}
function Sample {
    $r = [IntPtr]::Zero
    $sw.Restart(); $ok = [W]::SendMessageTimeout($h, 0, [IntPtr]::Zero, [IntPtr]::Zero, 2, 5000, [ref]$r); $sw.Stop()
    if ($ok -eq [IntPtr]::Zero) { $script:timeouts++ } else { $lat.Add($sw.Elapsed.TotalMilliseconds) }
    if ([W]::IsHungAppWindow($h)) { $script:hung++ }
    if ($ok -eq [IntPtr]::Zero -or $sw.ElapsedMilliseconds -gt 1000) {   # where: the download (file k), or setup's page
        $at = if ((Read1 'page.txt') -eq '12') { 'setup' } else { 'file ' + ((Read1 'download.txt') -split ' ')[0] }
        $script:hangAt[$at] = 1 + [int]$script:hangAt[$at]
    }
}
# 1. the pages up to Install: Next once on each (again after 3 s if the page didn't move: a click as a page changes
#    can be lost); a silent run needs no clicks
$clicked = ''; $clickedAt = Get-Date
while ([W]::IsWindow($h) -and -not [W]::IsWindowVisible($abort) -and -not (Read1 'download.txt') -and (Read1 'page.txt') -notin @('12', '14')) {
    if (((Get-Date) - $t0).TotalMinutes -gt 10) { Quit "the wizard never reached its downloads (page $(Read1 'page.txt'))" }
    Boxes
    $p = Read1 'page.txt'
    if ($Mode -ne 'abort' -and $p -and $p -notin @('11', '12', '13', '14') -and [W]::IsWindowEnabled($next) -and
        ($p -ne $clicked -or ((Get-Date) - $clickedAt).TotalSeconds -gt 3)) {
        Start-Sleep -Milliseconds 300; [void][W]::PostMessage($next, 0xF5, [IntPtr]::Zero, [IntPtr]::Zero); $clicked = $p; $clickedAt = Get-Date
    }
    Start-Sleep -Milliseconds 200
}
# 2. the downloads
$dlSecs = 0; $wizOff = 0; $abortOff = 0; $moved = ''; $mini = ''; $tick = [Diagnostics.Stopwatch]::StartNew(); $dl0 = Get-Date
while ([W]::IsWindow($h) -and (Read1 'page.txt') -notin @('12', '14')) {
    Sample; Boxes
    if ($Mode -ne 'abort' -and (Read1 'download.txt') -and -not [W]::IsWindowVisible($abort) -and (Read1 'page.txt') -eq '10' -and
        ((Get-Date) - $dl0).TotalSeconds -gt 5) {
        Start-Sleep -Seconds 3   # (Stop is hidden for a moment between files: back on the Ready page for good?)
        if (-not [W]::IsWindowVisible($abort) -and (Read1 'page.txt') -eq '10') { Quit "the wizard went back to its Ready page during the downloads (file $(Read1 'download.txt'))" }
    }
    if ($tick.ElapsedMilliseconds -ge 1000) {
        $tick.Restart(); $dlSecs++
        if ($Mode -ne 'abort') {   # (a silent run's wizard is hidden until it installs)
            if (-not [W]::IsWindowEnabled($h)) { $wizOff++ }
            if (-not ([W]::IsWindowEnabled($abort) -and [W]::IsWindowVisible($abort))) { $abortOff++ }
            if ($dlSecs -eq 5) {   # it moves: by a person's drag, here by SetWindowPos, and back
                $a = New-Object W+RECT; [void][W]::GetWindowRect($h, [ref]$a)
                [void][W]::SetWindowPos($h, [IntPtr]::Zero, $a.L + 40, $a.T + 30, 0, 0, 0x15)   # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                $b = New-Object W+RECT; [void][W]::GetWindowRect($h, [ref]$b)
                $moved = "$(($b.L - $a.L) -eq 40 -and ($b.T - $a.T) -eq 30)"
                [void][W]::SetWindowPos($h, [IntPtr]::Zero, $a.L, $a.T, 0, 0, 0x15)
            }
            if ($dlSecs -eq 10) {
                [void][W]::PostMessage($h, 0x112, [IntPtr]0xF020, [IntPtr]::Zero); Start-Sleep -Milliseconds 500; $m = [W]::IsIconic($h)
                [void][W]::PostMessage($h, 0x112, [IntPtr]0xF120, [IntPtr]::Zero); Start-Sleep -Milliseconds 500
                $mini = "$m/$(-not [W]::IsIconic($h))"; $tick.Restart()
            }
        }
    }
    if ($Mode -eq 'abort' -and -not $res.Contains('abort')) {
        $d = (Read1 'download.txt') -split ' '
        if ($d.Count -eq 2 -and [int]$d[0] -ge 4) {   # 3 files are in: Stop (its "Stop downloading?" answers itself: /SUPPRESSMSGBOXES)
            [void][W]::PostMessage($abort, 0xF5, [IntPtr]::Zero, [IntPtr]::Zero)
            $res['abort'] = "clicked on file $($d[0]) of $($d[1])"; $abortAt = Get-Date
        }
    }
    if ($Mode -eq 'abort' -and $res.Contains('abort') -and ((Get-Date) - $abortAt).TotalSeconds -gt 120) { Quit "Stop was clicked and the installer kept going for 2 minutes ($($res['abort']))" }
    Start-Sleep -Milliseconds 100
}
$res['dlsecs'] = $dlSecs; $res['dlwizoff'] = $wizOff; $res['dlabortoff'] = $abortOff; $res['moved'] = $moved; $res['minimise'] = $mini
$res['dlmin'] = [math]::Round(((Get-Date) - $dl0).TotalMinutes, 1)
# 3. setup, offline (only in a run that got that far): how long, and how long the wizard was held still
$setupSecs = 0; $setupOff = 0; $tick.Restart()
while ([W]::IsWindow($h) -and (Read1 'page.txt') -ne '14') {
    Sample; Boxes
    if ($tick.ElapsedMilliseconds -ge 1000) { $tick.Restart(); $setupSecs++; if (-not [W]::IsWindowEnabled($h)) { $setupOff++ } }
    if ($setupSecs -gt 3600) { Quit 'setup ran for an hour' }
    Start-Sleep -Milliseconds 100
}
$res['setupsecs'] = $setupSecs; $res['setupwizoff'] = $setupOff
# 4. the last page: Finish (an interactive run)
$f0 = Get-Date
while ([W]::IsWindow($h)) {
    Boxes
    if ($Mode -ne 'abort' -and (Read1 'page.txt') -eq '14' -and [W]::IsWindowEnabled($next) -and ((Get-Date) - $f0).TotalSeconds -gt 2) {
        [void][W]::PostMessage($next, 0xF5, [IntPtr]::Zero, [IntPtr]::Zero); $f0 = Get-Date
    }
    if (((Get-Date) - $f0).TotalMinutes -gt 5) { Quit "the wizard didn't close" }
    Start-Sleep -Milliseconds 300
}
$s = @($lat | Sort-Object)
$res['samples'] = $lat.Count; $res['timeouts'] = $script:timeouts; $res['hung'] = $script:hung
$res['maxms'] = if ($s.Count) { [math]::Round($s[-1], 1) } else { -1 }
$res['boxes'] = ($script:boxes -join ' | ')
$res['hangs'] = (@($script:hangAt.Keys | ForEach-Object { "$_ x$($script:hangAt[$_])" }) -join ', ')
Save
'@
$measureFile = Join-Path $launch 'measure.ps1'
[IO.File]::WriteAllText($measureFile, $measure, (New-Object Text.UTF8Encoding $false))

# one run of the installer with the driver beside it -> @{ code; resp; log }
function RunInstaller([string]$Mode, [string[]]$More) {
    Get-ChildItem -LiteralPath $hooks -File -ErrorAction SilentlyContinue | Remove-Item -Force
    $resp = Join-Path $launch "driver-$Mode.txt"
    $innoLog = Join-Path $launch "inno-$Mode.log"
    $mp = Start-Process powershell.exe -ArgumentList '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$measureFile`"",
        "`"$hooks`"", "`"$resp`"", $Mode -WindowStyle Hidden -PassThru
    $ip = Start-Process -FilePath $exe -ArgumentList (@("/DIR=`"$dir`"", "/HOOKS=`"$hooks`"", "/LOG=`"$innoLog`"") + $More) -PassThru
    $null = $ip.Handle   # (without it, ExitCode stays empty: a Start-Process quirk)
    while (-not $ip.WaitForExit(2000)) {
        if ($mp.HasExited -and (Test-Path $resp) -and (Select-String -LiteralPath $resp -Pattern '^error=' -Quiet)) {
            Start-Process taskkill.exe -ArgumentList "/PID $($ip.Id) /T /F" -WindowStyle Hidden -Wait; break   # the driver gave up: no one would click on
        }
    }
    $ip.WaitForExit()
    [void]$mp.WaitForExit(120000)
    $r = @{}
    if (Test-Path $resp) { Get-Content -LiteralPath $resp | ForEach-Object { $k, $v = $_ -split '=', 2; $r[$k] = $v } }
    $boxFile = Join-Path $hooks 'boxes.txt'
    if (Test-Path $boxFile) { $r['ourboxes'] = ((Get-Content -LiteralPath $boxFile) -join ' | ') }
    return @{ code = $ip.ExitCode; resp = $r; log = $(if (Test-Path $innoLog) { Get-Content -LiteralPath $innoLog -Raw } else { '' }) }
}
function ToDownload($run) { if ($run.log -match '(\d+) files to download, ([0-9.]+) GB') { return [int]$Matches[1] } else { return -1 } }

$fails = @()
$cacheDir = Join-Path $dir 'cache\downloads'
$firstTodo = -1
if ($CancelCheck) {
    # Stop on the download page, silently (its "Stop downloading?" answers itself), once 3 files are in: the installer
    # ends, what's downloaded is kept, and the next run carries on from there
    Write-Host "Stop check: a silent run, Stop clicked during the downloads"
    $a = RunInstaller 'abort' @('/SILENT', '/SUPPRESSMSGBOXES')
    $firstTodo = ToDownload $a
    $kept = @(Get-ChildItem -LiteralPath $cacheDir -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -ne '.moving' })
    Write-Host "Stop check: $($a.resp['abort']); installer exit $($a.code); $($kept.Count) files kept of $firstTodo to download"
    if ($a.resp['error']) { $fails += "Stop check: $($a.resp['error'])" }
    if (-not $a.resp['abort']) { $fails += 'Stop check: Stop was never clicked' }
    # (Inno 6.7, silent: the suppressed "Stop downloading?" says Yes, the download aborts, and a NextButtonClick that
    # returns False ends setup with "Failed to proceed to next wizard page; aborting": exit code 1)
    if ($a.code -eq 0 -or $a.log -notmatch 'Need to abort download' -or $a.log -notmatch 'Failed to proceed to next wizard page; aborting') {
        $fails += "a stopped download didn't end the installer as Inno does (exit $($a.code); the log lacks its abort lines)"
    }
    if ($kept.Count -lt 3) { $fails += "a stopped download kept $($kept.Count) files, not the 3 or more that were in" }
    # every file kept is one of the list's, whole, with its SHA-256; nothing half-downloaded or half-moved in its place
    $listed = @{}
    Import-Csv -LiteralPath (Join-Path $repo 'installer\downloads.tsv') -Delimiter "`t" | ForEach-Object { if ($_.dest -like 'cache/*') { $listed[$_.dest.Substring(6)] = $_ } }
    foreach ($f in @(Get-ChildItem -LiteralPath $cacheDir -File -ErrorAction SilentlyContinue)) {
        $row = $listed[$f.Name]
        if (-not $row) { $fails += "a stopped download left $($f.Name) in its place (not a listed file)"; continue }
        if ($f.Length -ne [long]$row.bytes -or (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash.ToLower() -ne $row.sha256) {
            $fails += "a stopped download kept $($f.Name), but it isn't the listed file (size or SHA-256)"
        }
    }
    $keptNames = @($kept | ForEach-Object { $_.Name })
    if (Test-Path (Join-Path $dir 'undercover')) { $fails += 'a stopped download installed the app anyway' }
}

$t0 = Get-Date
Write-Host "Install: the wizard, clicked through"
# the proof that a short read is retried (AMD's server once closed a 645 MB download at 6 MB, with no error): a local
# server answers the first file's first try with a 100 MB length, sends 1 MB and closes; the installer must try again
# by itself and go on
$short = @'
param([string]$PortFile)
$l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0); $l.Start()
Set-Content -LiteralPath $PortFile $l.LocalEndpoint.Port
$z = New-Object byte[] 1048576
for ($i = 0; $i -lt 2; $i++) {
    $c = $l.AcceptTcpClient(); $s = $c.GetStream(); $b = New-Object byte[] 8192; $n = $s.Read($b, 0, $b.Length)
    $line = ([Text.Encoding]::ASCII.GetString($b, 0, $n) -split "`r`n")[0]   # GET /short HTTP/1.1, or GET /bad?bytes=N HTTP/1.1
    if ($line -match '/bad\?bytes=(\d+)') { $size = [long]$Matches[1]; $send = $size; $what = 'bad' } else { $size = 104857600; $send = 1048576; $what = 'short' }
    $head = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: application/octet-stream`r`nContent-Length: $size`r`nConnection: close`r`n`r`n")
    $s.Write($head, 0, $head.Length)
    for ($left = $send; $left -gt 0; $left -= $z.Length) { $s.Write($z, 0, [int][math]::Min($z.Length, $left)) }
    $s.Flush(); $c.Close()
    Set-Content -LiteralPath "$PortFile.$what" "$line"
}
$l.Stop()
'@
$shortFile = Join-Path $launch 'short.ps1'; $portFile = Join-Path $launch 'short-port.txt'
[IO.File]::WriteAllText($shortFile, $short, (New-Object Text.UTF8Encoding $false))
$sp = Start-Process powershell.exe -ArgumentList '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$shortFile`"", "`"$portFile`"" -WindowStyle Hidden -PassThru
$w0 = Get-Date; while (-not (Test-Path $portFile) -and ((Get-Date) - $w0).TotalSeconds -lt 30) { Start-Sleep -Milliseconds 200 }
$shortUrl = "http://127.0.0.1:$((Get-Content -LiteralPath $portFile -Raw).Trim())/short"
$badUrl = $shortUrl -replace '/short$', '/bad'
$run = RunInstaller 'full' @("/SHORTURL=$shortUrl", "/BADURL=$badUrl")
$served = Test-Path "$portFile.short"; $badServed = if (Test-Path "$portFile.bad") { (Get-Content -LiteralPath "$portFile.bad" -Raw).Trim() } else { '' }
if (-not $sp.HasExited) { Stop-Process -Id $sp.Id -Force -ErrorAction SilentlyContinue }
$resp = $run.resp
$code = $run.code
$todo = ToDownload $run
$retried = @([regex]::Matches($run.log, ': (.+); trying again by itself \(1 of 3\)') | ForEach-Object { $_.Value })
Write-Host "Short read: served $served; retried by itself: $($retried.Count) ($(($retried | Select-Object -First 1) -replace '\s+', ' '))"
if (-not $served) { $fails += "the short-read server was never asked (the test variant didn't use /SHORTURL)" }
elseif (-not $retried.Count) { $fails += "a download that came back short wasn't tried again by itself" }
# a big file of the right size and the wrong bytes: its SHA-256 (checked off Inno's thread) fails, it's deleted and
# fetched again by itself, and the install goes on
$badRetry = @([regex]::Matches($run.log, '(\S+): its SHA-256 is wrong; trying again by itself \(1 of 3\)') | ForEach-Object { $_.Groups[1].Value })
Write-Host "Bad hash: served '$badServed'; deleted and fetched again: $($badRetry -join ', ')"
if (-not $badServed) { $fails += "the bad-hash server was never asked (the test variant didn't use /BADURL)" }
elseif (-not $badRetry.Count) { $fails += "a big file with the wrong bytes wasn't caught by its SHA-256 and fetched again" }
if ($CancelCheck) {   # the run after Stop fetched none of the files the Stop run kept
    foreach ($n in $keptNames) {
        if ($run.log -match ("Downloading temporary file from \S+: \S+\\" + [regex]::Escape($n) + "\r?\n")) { $fails += "the run after Stop downloaded $n again" }
    }
}
Remove-Item -LiteralPath $launch -Recurse -Force -ErrorAction SilentlyContinue
$mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)

if ($code -ne 0) { $fails += "the installer exited $code" }
if ($resp['error']) { $fails += "wizard check: $($resp['error'])" }
elseif (-not $resp.Count) { $fails += "the wizard wasn't measured" }
else {
    # the download page: the wizard enabled and Stop there every second, answering within a second, moved, minimised
    Write-Host ("Downloads: $todo files in $($resp['dlmin']) min; wizard disabled $($resp['dlwizoff']) of $($resp['dlsecs']) s, Stop " +
                "missing $($resp['dlabortoff']) s; moved $($resp['moved']); minimise/restore $($resp['minimise'])")
    Write-Host ("Setup (offline): $($resp['setupsecs']) s, the wizard held still $($resp['setupwizoff']) s; answers: $($resp['samples']), " +
                "slowest $($resp['maxms']) ms, $($resp['timeouts']) unanswered, hung $($resp['hung']) times")
    if ($resp['toolwindow'] -ne 'True') { $fails += "the test wizard was in the taskbar and Alt+Tab" }
    if ([int]$resp['dlsecs'] -lt 5) { $fails += "the download page was up $($resp['dlsecs']) s: too short to judge" }
    if ([int]$resp['dlwizoff'] -gt 0) { $fails += "the wizard was disabled $($resp['dlwizoff']) of $($resp['dlsecs']) s on the download page" }
    if ([int]$resp['dlabortoff'] -gt 0) { $fails += "the download page's Stop was disabled or hidden $($resp['dlabortoff']) s" }
    if ($resp['moved'] -ne 'True') { $fails += "the wizard didn't move on the download page ($($resp['moved']))" }
    if ($resp['minimise'] -ne 'True/True') { $fails += "minimise/restore on the download page: $($resp['minimise'])" }
    if ([int]$resp['timeouts'] -gt 0 -or [int]$resp['hung'] -gt 0) { $fails += "the wizard stopped answering ($($resp['timeouts']) timeouts, hung $($resp['hung']) times; during: $($resp['hangs']))" }
    if ([double]$resp['maxms'] -gt 1000) { $fails += "the wizard took $($resp['maxms']) ms to answer" }
    if ($resp['boxes'] -or $resp['ourboxes']) { $fails += "message boxes came up: $($resp['boxes']) $($resp['ourboxes'])" }
}
if ($CancelCheck -and $firstTodo -gt 0 -and ($todo -lt 0 -or $todo -ge $firstTodo)) { $fails += "the run after Stop downloaded $todo files again (of $firstTodo): it didn't carry on" }
if (Test-Path $cacheDir) { $fails += "cache\downloads is still there after a working setup" }
if (@(Compare-Object $linksBefore (Shortcuts)).Count) { $fails += 'the test install made shortcuts' }
if (Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -like '*3F1A9C52-6B7E-4D21-9A43-5C0E7D2B8F61*' }) {
    $fails += 'the test install registered an uninstaller'
}
# the status lines setup wrote (logs\setup-status.txt): every step from 1 to the last, never going back; and each
# step's time (setup.log's STEP lines), which a person waits through with the wizard held still
$statusFile = Join-Path $dir 'logs\setup-status.txt'
$seen = if (Test-Path $statusFile) { @(Get-Content -LiteralPath $statusFile | ForEach-Object { if ($_ -match '(@@ .*)$') { $Matches[1] } }) } else { @() }
$steps = @($seen | ForEach-Object { if ($_ -match '^@@ (\d+)\|(\d+)\|') { [int]$Matches[1] } })
$of = if ($seen.Count -and ($seen[-1] -match '^@@ \d+\|(\d+)\|')) { [int]$Matches[1] } else { 0 }
if (-not $steps.Count) { $fails += 'setup wrote no status lines (the wizard would show nothing)' }
else {
    $back = @(for ($i = 1; $i -lt $steps.Count; $i++) { if ($steps[$i] -lt $steps[$i - 1]) { $i } })
    $missing = @(1..$of | Where-Object { $steps -notcontains $_ })
    if ($back.Count) { $fails += 'the status went back a step' }
    if ($missing.Count) { $fails += "steps never reported: $($missing -join ', ') of $of" }
}
$setupLog = Join-Path $dir 'logs\setup.log'
if (Test-Path $setupLog) {
    # (setup.log's times are local: PowerShell's "u" format writes a Z without converting to UTC)
    $stepTimes = @(Get-Content -LiteralPath $setupLog | ForEach-Object { if ($_ -match '^(\S+ \S+)\s+STEP (.*)$') { [pscustomobject]@{ t = [datetime]::Parse($Matches[1].TrimEnd('Z')); s = $Matches[2] } } })
    $long = @()
    for ($i = 0; $i -lt $stepTimes.Count; $i++) {
        $end = if ($i + 1 -lt $stepTimes.Count) { $stepTimes[$i + 1].t } else { (Get-Item -LiteralPath $setupLog).LastWriteTime }
        $secs = [math]::Round(($end - $stepTimes[$i].t).TotalSeconds)
        Write-Host ("  {0,5} s  {1}" -f $secs, $stepTimes[$i].s)
        if ($secs -gt 300) { $long += "$($stepTimes[$i].s) ($secs s)" }
    }
    if ($long.Count) { $fails += "setup steps longer than 5 minutes, with the wizard held still: $($long -join '; ')" }
}
if ($run.log -notmatch 'Downloads for backend (\w+)') { $fails += "the installer's log doesn't say which downloads it chose" }
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
if ((-not $venvHome) -or ((Split-Path $venvHome -Leaf) -notmatch '^cpython-\d+\.\d+\.\d+-')) { $fails += ".venv's home is '$venvHome', not the full-version Python folder" }
$reg = @(Get-ChildItem 'HKCU:\Software\Python' -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -eq 'InstallPath' } |
    Where-Object { "$((Get-ItemProperty -LiteralPath $_.PSPath).'(default)')".StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) })
if ($reg.Count) { $fails += "registry entries point into the install: $(($reg | ForEach-Object { $_.Name }) -join ', ')" }
$binAfter = BinList
if (@(Compare-Object $binBefore $binAfter).Count) { $fails += "$binDir changed" }

# every model the app's features use is in place (rc-fix4b, the user: "it should be part of the install")
$vpy = Join-Path $dir '.venv\Scripts\python.exe'
if ($code -eq 0 -and (Test-Path $vpy)) {   # (after a failed setup the venv may be half made: its failure is already said)
    Push-Location $dir
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'   # (PS 5.1: a native stderr line is not a stop)
    try { $mcheck = (& $vpy -m undercover.models check 2>&1 | Out-String).Trim(); $mcode = $LASTEXITCODE } finally { $ErrorActionPreference = $eap; Pop-Location }
    Write-Host "Model check: $mcheck"
    if ($mcode -ne 0) { $fails += "models missing after setup: $mcheck" }
}

# the graphics driver never reset and the device was never lost, through setup, the selftest and the first use (the
# machine once came back from a reboot with LiveKernelEvent 141 reports; a release check must catch that, not pass it)
function GpuTrouble {
    $out = @()
    $wer = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1001; StartTime = $runStart } -ErrorAction SilentlyContinue |
        Where-Object { $_.Message -match 'LiveKernelEvent' -and $_.Message -match 'P1: (141|117)\b' })
    if ($wer.Count) { $out += "$($wer.Count) LiveKernelEvent 141/117 report(s) (the GPU stopped answering) at $(($wer | ForEach-Object { $_.TimeCreated.ToString('HH:mm:ss') }) -join ', ')" }
    $tdr = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 4101; StartTime = $runStart } -ErrorAction SilentlyContinue)
    if ($tdr.Count) { $out += "$($tdr.Count) display driver reset(s) (Display 4101) at $(($tdr | ForEach-Object { $_.TimeCreated.ToString('HH:mm:ss') }) -join ', ')" }
    foreach ($f in @('logs\setup.log', 'logs\firstuse-server.log', 'logs\firstuse-server.log.err', 'logs\selftest.json')) {
        $p = Join-Path $dir $f
        if ((Test-Path -LiteralPath $p) -and (Select-String -LiteralPath $p -Pattern 'device.?lost|hipErrorLaunchFailure|hipErrorIllegalAddress|unspecified launch failure|stopped answering' -Quiet)) {
            $out += "$f says the graphics device was lost"
        }
    }
    return $out
}

if (-not $fails.Count -and -not $NoFirstUse -and (Test-Path $vpy)) {
    $why = FirstUse
    if ($why) { $fails += "first use: $why" } else { Write-Host "First use: nothing downloaded." }
}

$gpu = @(GpuTrouble)
if ($gpu.Count) { $fails += $gpu | ForEach-Object { "GPU: $_" } } else { Write-Host "GPU: no driver reset or lost device during the run." }

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
