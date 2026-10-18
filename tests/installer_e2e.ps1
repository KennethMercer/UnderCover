<#
.SYNOPSIS
  Release check for the installer: a REAL install by a normal user (no admin) into a fresh folder, all the way to
  selftest passing. Run it before any installer release. It downloads about 3 GB and takes 20-40 minutes; the GPU is
  only touched by selftest's quick check at the end.

  powershell -ExecutionPolicy Bypass -File tests\installer_e2e.ps1 [-Root D:\] [-Backend auto] [-Keep] [-CancelCheck]

  -Root     the drive or folder the temp install goes in (default: this repo's drive; the user hit os error 448 on D:)
  -Backend  as setup.ps1's (auto: detected)
  -Keep     leave the temp install in place (default: deleted after the checks, pass or fail)
  -CancelCheck  first cancel setup mid-download (as the wizard's Cancel does): it must stop with code 3 and say what
                it kept; then the same folder is set up again and must finish (a re-run resumes)

  The app is laid out as the installer lays it out (UnderCover.iss [Files]); then installer\setup.ps1 runs for real,
  started from inside an Inno Setup process, as the installer starts it. That matters: Inno Setup blocks untrusted
  junctions in its own process and the processes it starts inherit that (a junction they make can't be traversed:
  the user's os error 448, which a run from a normal console never shows). The launcher is a throwaway Inno program
  compiled here: it installs nothing, registers nothing, and only runs setup.ps1, hidden, with the wizard's own way of
  reading its output (ExecAndLogOutput), and writes the status lines it received and the exit code.
  Checked afterwards:
    - setup exited 0 and logs\setup-result.txt says "ok";
    - logs\selftest.json has no problems;
    - .venv\pyvenv.cfg's home is the full-version Python folder (cpython-<version>-...), not uv's minor-version link;
    - no registry entry under HKCU\Software\Python points into the temp install;
    - nothing new in %USERPROFILE%\.local\bin;
    - the status lines reached the launcher (as the wizard reads them) and went through every step, in order;
    - the wizard stayed responsive the whole time (installer-fix6, the user: the window couldn't be moved during setup):
      the final run is a shown wizard (off-screen) that runs setup from its install step with UnderCover.iss's own
      SetupLine, and a second process times its answers (WM_NULL), hit-tests its title bar and minimises and restores
      it mid-run.
  Exit code 0 only when every check passes. The tail of setup.log is printed either way.
#>
param(
    [string]$Root = '',
    [string]$Backend = 'auto',
    [switch]$Keep,
    [switch]$CancelCheck
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
# the files the installer puts in {app}\installer, read from UnderCover.iss [Files] itself (a hand-kept list here
# missed space.ps1 once, and the release check failed for a reason the real installer doesn't have)
$issFiles = @(Select-String -LiteralPath (Join-Path $repo 'installer\UnderCover.iss') -Pattern '^Source: "([^"*\\]+)"; DestDir: "\{app\}\\installer";' |
    ForEach-Object { $_.Matches[0].Groups[1].Value })
if ($issFiles -notcontains 'setup.ps1') { throw "couldn't read the installer's file list from UnderCover.iss" }
foreach ($f in $issFiles) {
    Copy-Item (Join-Path $repo "installer\$f") (Join-Path $dir 'installer')
}
Copy-Item (Join-Path $repo 'installer\requirements\*.txt') (Join-Path $dir 'installer\requirements')

# the launcher: setup.ps1 as a child of an Inno Setup process (as UnderCover.iss runs it), its exit code to a file
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Host "Inno Setup 6 (ISCC.exe) isn't installed: it's needed to run setup the way the installer does." -ForegroundColor Red; exit 2 }
$launch = Join-Path $env:TEMP ('uc-e2e-launcher-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force $launch | Out-Null
$codeFile = Join-Path $launch 'exit.txt'
$linesFile = Join-Path $launch 'lines.txt'
$hwndFile = Join-Path $launch 'hwnd.txt'
$doneFile = Join-Path $launch 'done.txt'
$respFile = Join-Path $launch 'responsive.txt'
# the installer's own handling of setup's lines (both bars, the labels), copied from UnderCover.iss, so the wizard run
# below does exactly what the installer's wizard does with them
$src = [IO.File]::ReadAllText((Join-Path $repo 'installer\UnderCover.iss'))
function Cut([string]$from, [string]$to) {
    $a = $src.IndexOf($from); $b = $src.IndexOf($to)
    if ($a -lt 0 -or $b -le $a) { Write-Host "Couldn't find '$from' in UnderCover.iss." -ForegroundColor Red; exit 2 }
    return $src.Substring($a, $b - $a)
}
$fieldCode = Cut '// the N-th field (from 0)' "// one line of setup's output."
$setupLineCode = (Cut 'procedure SetupLine(' "// setup.ps1, hidden: the wizard's own bar").Replace("ExpandConstant('{app}\logs\setup-cancel')", "'" + (Join-Path $dir 'logs\setup-cancel') + "'")
$params = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$(Join-Path $dir 'installer\setup.ps1')`" -InstallDir `"$dir`" -Backend $Backend"
$iss = @'
[Setup]
AppName=UnderCover installer check
AppVersion=0
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=launcher
DisableWelcomePage=yes

[Code]
var
  Progress: Integer;
  CancelAsked: Boolean;
  StepGauge: TNewProgressBar;
  Code: Integer;

procedure OnLine(const S: String; const Error, FirstLine: Boolean);
begin
  if Copy(S, 1, 3) = '@@ ' then SaveStringToFile('@LINESFILE@', S + #13#10, True);
end;

@FIELD@
@SETUPLINE@
procedure WizardLine(const S: String; const Error, FirstLine: Boolean);
begin
  OnLine(S, Error, FirstLine);
  SetupLine(S, Error, FirstLine);
end;

// /WIZARD=1: a shown wizard that runs setup from its install step, as UnderCover.iss does; else no wizard at all
function InitializeSetup: Boolean;
begin
  Result := ExpandConstant('{param:wizard|0}') = '1';
  if Result then Exit;
  if not ExecAndLogOutput('powershell.exe', '@PARAMS@', '@DIR@', SW_HIDE, ewWaitUntilTerminated, Code, @OnLine) then Code := -1;
  SaveStringToFile('@CODEFILE@', IntToStr(Code), False);
end;

procedure InitializeWizard;
begin
  StepGauge := TNewProgressBar.Create(WizardForm);
  StepGauge.Parent := WizardForm.InstallingPage;
  StepGauge.Left := WizardForm.ProgressGauge.Left;
  StepGauge.Width := WizardForm.ProgressGauge.Width;
  StepGauge.Top := WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height + ScaleY(10);
  StepGauge.Height := WizardForm.ProgressGauge.Height - ScaleY(6);
  StepGauge.Max := 1000;
  StepGauge.Visible := False;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then WizardForm.Left := -6000;   // shown, as the installer's is, but on no screen
  if CurStep = ssPostInstall then begin
    WizardForm.Left := -6000;
    SaveStringToFile('@HWNDFILE@', IntToStr(WizardForm.Handle), False);
    if not ExecAndLogOutput('powershell.exe', '@PARAMS@', '@DIR@', SW_HIDE, ewWaitUntilTerminated, Code, @WizardLine) then Code := -1;
    SaveStringToFile('@CODEFILE@', IntToStr(Code), False);
    SaveStringToFile('@DONEFILE@', 'done', False);
  end;
end;
'@
$iss = $iss.Replace('@PARAMS@', $params.Replace("'", "''")).Replace('@DIR@', $dir).Replace('@CODEFILE@', $codeFile).Replace('@LINESFILE@', $linesFile).Replace('@HWNDFILE@', $hwndFile).Replace('@DONEFILE@', $doneFile).Replace('@FIELD@', $fieldCode).Replace('@SETUPLINE@', $setupLineCode)
[IO.File]::WriteAllText((Join-Path $launch 'launcher.iss'), $iss, (New-Object Text.UTF8Encoding $false))
& $iscc /Q "/O$launch" (Join-Path $launch 'launcher.iss') | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't compile the launcher." -ForegroundColor Red; exit 2 }

$fails = @()
$statusFile = Join-Path $dir 'logs\setup-status.txt'
if ($CancelCheck) {
    # cancel mid-download, as the wizard's Cancel does (logs\setup-cancel), once the PyTorch step has bytes moving
    Write-Host "Cancel check: starting, then cancelling during the PyTorch download"
    $lp = Start-Process -FilePath (Join-Path $launch 'launcher.exe') -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES' -PassThru
    $deadline = (Get-Date).AddMinutes(20)
    while (-not $lp.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ((Test-Path $statusFile) -and (Select-String -LiteralPath $statusFile -Pattern 'PyTorch: ([1-9]|0.[1-9])' -Quiet)) {
            Set-Content -LiteralPath (Join-Path $dir 'logs\setup-cancel') -Value 'stop'; break
        }
    }
    $lp.WaitForExit()
    $ccode = if (Test-Path $codeFile) { [int](Get-Content -LiteralPath $codeFile -Raw).Trim() } else { -2 }
    $cres = if (Test-Path (Join-Path $dir 'logs\setup-result.txt')) { (Get-Content -LiteralPath (Join-Path $dir 'logs\setup-result.txt') -Raw).Trim() } else { '' }
    Write-Host "Cancel check: exit $ccode, said: $cres"
    if ($ccode -ne 3) { $fails += "a cancelled setup exited $ccode, not 3" }
    if ($cres -notmatch '^cancelled: kept: .*Python') { $fails += "a cancelled setup said: $cres" }
    if (Test-Path (Join-Path $dir 'logs\setup-cancel')) { $fails += 'the cancel file was left behind' }
    Remove-Item -LiteralPath $codeFile, $linesFile -Force -ErrorAction SilentlyContinue
    Write-Host "Cancel check: setting the same folder up again (it should resume)"
}

# the wizard's responsiveness, timed from outside while setup runs in it
$measure = @'
param([string]$HwndFile, [string]$DoneFile, [string]$Out)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class W {
  [DllImport("user32.dll")] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, IntPtr w, IntPtr l, uint f, uint t, out IntPtr r);
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
}
"@
$t0 = Get-Date
while (-not (Test-Path $HwndFile)) { Start-Sleep -Milliseconds 100; if (((Get-Date) - $t0).TotalMinutes -gt 5) { "error=the wizard never started setup" | Set-Content $Out; exit } }
$h = [IntPtr][long](Get-Content $HwndFile -Raw).Trim()
$visible = [W]::IsWindowVisible($h)
$lat = New-Object System.Collections.Generic.List[double]; $hung = 0; $fails = 0; $caption = 0; $n = 0; $mini = @()
$sw = New-Object Diagnostics.Stopwatch
while (-not (Test-Path $DoneFile) -and [W]::IsWindow($h)) {
    $r = [IntPtr]::Zero
    $sw.Restart(); $ok = [W]::SendMessageTimeout($h, 0, [IntPtr]::Zero, [IntPtr]::Zero, 2, 5000, [ref]$r); $sw.Stop()
    if ($ok -eq [IntPtr]::Zero) { $fails++ } else { $lat.Add($sw.Elapsed.TotalMilliseconds) }
    if ([W]::IsHungAppWindow($h)) { $hung++ }
    $rc = New-Object W+RECT; [void][W]::GetWindowRect($h, [ref]$rc)
    $lp = [IntPtr]((($rc.T + 12) -band 0xFFFF) -shl 16 -bor (($rc.L + 60) -band 0xFFFF))   # a point in the title bar
    [void][W]::SendMessageTimeout($h, 0x84, [IntPtr]::Zero, $lp, 2, 5000, [ref]$r)
    if ([int]$r -eq 2) { $caption++ }   # HTCAPTION: a press there starts a move
    $n++
    if ($n % 600 -eq 100) {   # minimise and restore, posted as the title bar's buttons post them
        [void][W]::PostMessage($h, 0x112, [IntPtr]0xF020, [IntPtr]::Zero); Start-Sleep -Milliseconds 500; $m = [W]::IsIconic($h)
        [void][W]::PostMessage($h, 0x112, [IntPtr]0xF120, [IntPtr]::Zero); Start-Sleep -Milliseconds 500
        $mini += "$m/$(-not [W]::IsIconic($h))"
    }
    Start-Sleep -Milliseconds 100
}
$s = @($lat | Sort-Object)
@("visible=$visible", "samples=$($lat.Count)", "timeouts=$fails", "hung=$hung", "caption=$caption", "n=$n",
  "maxms=$(if ($s.Count) { [math]::Round($s[-1], 1) } else { -1 })",
  "p99ms=$(if ($s.Count) { [math]::Round($s[[int][math]::Floor($s.Count * 0.99)], 1) } else { -1 })",
  "minimise=$($mini -join ',')") | Set-Content $Out
'@
$measureFile = Join-Path $launch 'measure.ps1'
[IO.File]::WriteAllText($measureFile, $measure, (New-Object Text.UTF8Encoding $false))

$t0 = Get-Date
$mp = Start-Process powershell.exe -ArgumentList '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$measureFile`"", "`"$hwndFile`"", "`"$doneFile`"", "`"$respFile`"" -WindowStyle Hidden -PassThru
Start-Process -FilePath (Join-Path $launch 'launcher.exe') -ArgumentList '/SILENT', '/SUPPRESSMSGBOXES', '/WIZARD=1' -Wait
[void]$mp.WaitForExit(60000)
$resp = @{}
if (Test-Path $respFile) { Get-Content -LiteralPath $respFile | ForEach-Object { $k, $v = $_ -split '=', 2; $resp[$k] = $v } }
$code = if (Test-Path $codeFile) { [int](Get-Content -LiteralPath $codeFile -Raw).Trim() } else { -2 }
$seen = if (Test-Path $linesFile) { @(Get-Content -LiteralPath $linesFile) } else { @() }
Remove-Item -LiteralPath $launch -Recurse -Force -ErrorAction SilentlyContinue
$mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)

if ($code -ne 0) { $fails += "setup exited $code" }
# the wizard stayed responsive: it answered within a second every time, never counted as hung, its title bar was a title
# bar throughout, and it minimised and restored when asked mid-run
Write-Host ("Wizard: shown $($resp['visible']), $($resp['samples']) answers, slowest $($resp['maxms']) ms (99 % within " +
            "$($resp['p99ms']) ms), $($resp['timeouts']) unanswered, hung $($resp['hung']) times, title bar $($resp['caption']) of " +
            "$($resp['n']), minimise/restore $($resp['minimise'])")
if ($resp['error']) { $fails += "wizard check: $($resp['error'])" }
elseif (-not $resp.Count) { $fails += "the wizard's responsiveness wasn't measured" }
else {
    if ($resp['visible'] -ne 'True') { $fails += 'the wizard wasn''t shown' }
    if ([int]$resp['samples'] -lt 100) { $fails += "too few answers to judge the wizard ($($resp['samples']))" }
    if ([int]$resp['timeouts'] -gt 0 -or [int]$resp['hung'] -gt 0) { $fails += "the wizard stopped answering ($($resp['timeouts']) timeouts, hung $($resp['hung']) times)" }
    if ([double]$resp['maxms'] -gt 1000) { $fails += "the wizard took $($resp['maxms']) ms to answer" }
    if ($resp['caption'] -ne $resp['n']) { $fails += "the title bar wasn't a title bar $([int]$resp['n'] - [int]$resp['caption']) times" }
    if (-not $resp['minimise'] -or $resp['minimise'] -match 'False') { $fails += "minimise/restore mid-run: $($resp['minimise'])" }
}
# the status lines, as the wizard reads them: every step from 1 to the last, never going back
$steps = @($seen | ForEach-Object { if ($_ -match '^@@ (\d+)\|(\d+)\|') { [int]$Matches[1] } })
$of = if ($seen.Count -and ($seen[-1] -match '^@@ \d+\|(\d+)\|')) { [int]$Matches[1] } else { 0 }
if (-not $steps.Count) { $fails += 'no status lines reached the launcher (the wizard would show nothing)' }
else {
    $back = @(for ($i = 1; $i -lt $steps.Count; $i++) { if ($steps[$i] -lt $steps[$i - 1]) { $i } })
    $missing = @(1..$of | Where-Object { $steps -notcontains $_ })
    if ($back.Count) { $fails += 'the status went back a step' }
    if ($missing.Count) { $fails += "steps never reported: $($missing -join ', ') of $of" }
    $moving = @($seen | Where-Object { $_ -match 'PyTorch: [0-9.]+ ' })
    Write-Host "Status: $($seen.Count) lines, steps 1-$of, $($moving.Count) progress lines during the PyTorch download"
    if (-not $moving.Count -and -not $CancelCheck) { $fails += 'no progress lines during the PyTorch download' }
}
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
