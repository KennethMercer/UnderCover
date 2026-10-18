; UnderCover Windows installer (Inno Setup 6, https://jrsoftware.org/isinfo.php).
; Build:  powershell -ExecutionPolicy Bypass -File installer\build.ps1
;         (writes installer\addons.isi from the add-ons registry, then runs ISCC on this file)
; Output: installer\dist\UnderCover-Setup-<version>.exe  (~2 MB; everything heavy is downloaded by setup.ps1)
;
; Per-user install to %LOCALAPPDATA%\UnderCover: no admin prompt, and the app can write its models/cache/output
; next to itself (config.toml paths are relative to the app folder), or in the folders picked on the
; "Where should UnderCover keep your files?" page (phase 4b).

#define AppName "UnderCover"
; The version is written in one place, undercover/VERSION (the app reads it too: core/about.py).
#define VersionHandle FileOpen(AddBackslash(SourcePath) + "..\undercover\VERSION")
#if VersionHandle == 0
  #error undercover\VERSION is missing: it holds the app's version
#endif
#define AppVersion Trim(FileRead(VersionHandle))
#expr FileClose(VersionHandle)
#if AppVersion == ""
  #error undercover\VERSION is empty: it holds the app's version
#endif

[Setup]
AppId={{E6B05D70-0109-4D65-8AEC-89622D0F5C91}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=UnderCover
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=UnderCover-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
UninstallDisplayName={#AppName}

[Messages]
FinishedLabel=UnderCover is installed. Start it from the Start menu: it runs in the background, and you quit it from SafeHouse. The first start downloads the voice and separation models (about 1-3 GB, depending on which features you use).

[Files]
; App code. __pycache__ and anything personal (models, cache, output, uploads, logs, test, .venv) stay out.
Source: "..\undercover\*"; DestDir: "{app}\undercover"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__,*.pyc"
; (scripts\, not tools\: an installed app's tools\ holds uv, ffmpeg and Deno)
Source: "..\scripts\check_env.py"; DestDir: "{app}\scripts"; Flags: ignoreversion
; Package pins: add-ons install their packages --no-deps against these (core/addons.py: config.CONSTRAINTS).
Source: "constraints.txt"; DestDir: "{app}\installer"; Flags: ignoreversion
; The user's own config.toml isn't shipped: the defaults are undercover\config.default.toml (in the app code above), and
; setup writes the user's file (the GPU, where the files live). An update keeps it; uninstall leaves it (not ours).
; Setup scripts + the pinned, hashed requirement files (installer/pin_hashes.py).
Source: "setup.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "detect_gpu.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "python.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "selftest.py"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "requirements\*.txt"; DestDir: "{app}\installer\requirements"; Flags: ignoreversion
; The friend-facing briefing (installer/FRIENDS.md): what to expect, Repair, the optional extras, uninstall
Source: "FRIENDS.md"; DestDir: "{app}"; DestName: "READ ME.md"; Flags: ignoreversion

[Icons]
; UnderCover.cmd started from a shortcut runs windowless in the background (quit it from SafeHouse).
Name: "{group}\{#AppName}"; Filename: "{app}\UnderCover.cmd"; WorkingDir: "{app}"
; Repair: the Doctor checks the install against its record and setup fixes only what's wrong (files and settings kept)
Name: "{group}\Repair {#AppName}"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\setup.ps1"" -InstallDir ""{app}"" -Repair -Pause"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\UnderCover.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
; Visible window so people can watch the downloads (PyTorch alone is 2-3 GB).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\setup.ps1"" -InstallDir ""{app}"" -LibraryDir ""{code:GetLibraryDir}"" -WorkDir ""{code:GetWorkDir}"" -ModelsDir ""{code:GetModelsDir}"" -Addons ""{code:GetAddons}"" -Pause"; WorkingDir: "{app}"; StatusMsg: "Setting up Python, PyTorch and tools for your graphics card (this takes a while)..."; Flags: waituntilterminated
Filename: "{app}\UnderCover.cmd"; Parameters: "--background"; Description: "Start UnderCover now"; Flags: postinstall nowait skipifsilent unchecked runhidden

[UninstallDelete]
; Downloaded runtime pieces and caches that are re-made when needed. Kept: voices, covers, songs (models, output,
; uploads) and the training projects in cache\training (hours of collected clips), all the user's.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\cache\uv"
Type: filesandordirs; Name: "{app}\cache\converted"
Type: filesandordirs; Name: "{app}\cache\keyshift"
Type: filesandordirs; Name: "{app}\cache\previews"
Type: filesandordirs; Name: "{app}\cache\stems"
Type: filesandordirs; Name: "{app}\logs"
; Away from home (phase 20): Tailscale's binaries (fetched on first use, fetched again if needed) and this PC's node keys
; (uninstalling ends the private link; the node then shows as offline in the user's Tailscale admin page)
Type: filesandordirs; Name: "{app}\models\tailscale"
Type: filesandordirs; Name: "{app}\cache\tailscale-fetch"
Type: filesandordirs; Name: "{app}\cache\tailscale-state"
Type: files; Name: "{app}\UnderCover.cmd"

[Code]
// The optional add-ons (generated from undercover/core/addons.py by build.ps1): ADDON_COUNT, AddonId(i),
// AddonCaption(i), AddonUrl(i).
#include "addons.isi"

var
  DataPage: TInputDirWizardPage;
  AddonPage: TWizardPage;
  AddonBoxes: array of TNewCheckBox;
  LastDefault: String;

// one add-on's licence, from its own "Read the licence" link (the link's Tag is the add-on's number)
procedure OpenLicence(Sender: TObject);
var
  Code: Integer;
begin
  ShellExec('open', AddonUrl(TNewStaticText(Sender).Tag), '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure InitializeWizard;
var
  I, Top, Cut: Integer;
  Intro, Link, Desc: TNewStaticText;
  Box: TNewCheckBox;
  Caption, Name, Rest: String;
begin
  DataPage := CreateInputDirPage(wpSelectDir, 'Where should UnderCover keep your files?',
    'Keep UnderCover on C: and its growing files somewhere roomier, if you like.',
    'Each folder gets its own sub-folders. Leave them as they are to keep everything in the install folder. ' +
    'You can move them later in SafeHouse, Storage.', False, '');
  DataPage.Add('Your library: songs, covers, speech and voices (yours: they can''t be downloaded again)');
  DataPage.Add('Working files: training projects (your collected clips), song stems and caches');
  DataPage.Add('Model files: the AI models UnderCover downloads (they can be downloaded again)');

  AddonPage := CreateCustomPage(DataPage.ID, 'Optional add-ons',
    'Extra parts with their own licence. UnderCover works fully without them.');
  Intro := TNewStaticText.Create(AddonPage);
  Intro.Parent := AddonPage.Surface;
  Intro.WordWrap := True;
  Intro.Width := AddonPage.SurfaceWidth;
  Intro.Caption := 'Ticking an add-on accepts its licence. You can add or remove add-ons later in SafeHouse, Add-ons.';
  Top := Intro.Top + Intro.Height + ScaleY(14);
  SetArrayLength(AddonBoxes, ADDON_COUNT);
  for I := 0 to ADDON_COUNT - 1 do begin
    // "F5-TTS, 1.5 GB, what it adds, who may use it" (middle dots): the name on the checkbox, the rest under it
    Caption := AddonCaption(I);
    Cut := Pos(' ' + #183 + ' ', Caption);   // #183: the middle dot (the .iss stays ASCII)
    if Cut > 0 then begin
      Name := Copy(Caption, 1, Cut - 1);
      Rest := Copy(Caption, Cut + 3, Length(Caption));
    end else begin
      Name := Caption;
      Rest := '';
    end;
    Box := TNewCheckBox.Create(AddonPage);
    Box.Parent := AddonPage.Surface;
    Box.Top := Top;
    Box.Left := 0;
    Box.Width := AddonPage.SurfaceWidth - ScaleX(120);
    Box.Height := ScaleY(17);
    Box.Caption := 'Add ' + Name + ' and accept its licence';
    Box.Checked := False;
    AddonBoxes[I] := Box;
    Desc := TNewStaticText.Create(AddonPage);
    Desc.Parent := AddonPage.Surface;
    Desc.Top := Top + Box.Height + ScaleY(2);
    Desc.Left := ScaleX(18);
    Desc.Width := AddonPage.SurfaceWidth - ScaleX(18);
    Desc.WordWrap := True;
    Desc.Caption := Rest;
    Link := TNewStaticText.Create(AddonPage);
    Link.Parent := AddonPage.Surface;
    Link.Top := Top + ScaleY(2);
    Link.Left := AddonPage.SurfaceWidth - ScaleX(110);
    Link.Caption := 'Read the licence';
    Link.Font.Color := clBlue;
    Link.Font.Style := [fsUnderline];
    Link.Cursor := crHand;
    Link.Tag := I;
    Link.OnClick := @OpenLicence;
    Top := Desc.Top + Desc.Height + ScaleY(14);
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
begin
  // the folders default to the install folder; follow it if it changes, unless the user typed something else
  if CurPageID = DataPage.ID then begin
    for I := 0 to 2 do
      if (DataPage.Values[I] = '') or (DataPage.Values[I] = LastDefault) then
        DataPage.Values[I] := WizardDirValue;
    LastDefault := WizardDirValue;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = AddonPage.ID) and (ADDON_COUNT = 0);
end;


// "E:\" would end the quoted argument with \", which escapes the quote: pass "E:\." instead
function DirArg(S: String): String;
begin
  Result := S;
  if (Length(Result) > 0) and (Result[Length(Result)] = '\') then Result := Result + '.';
end;

function GetLibraryDir(Param: String): String; begin Result := DirArg(DataPage.Values[0]); end;
function GetWorkDir(Param: String): String; begin Result := DirArg(DataPage.Values[1]); end;
function GetModelsDir(Param: String): String; begin Result := DirArg(DataPage.Values[2]); end;

function GetAddons(Param: String): String;
var
  I: Integer;
begin
  Result := '';
  for I := 0 to ADDON_COUNT - 1 do
    if AddonBoxes[I].Checked then begin   // ticked = added, its licence accepted (one checkbox, user 2026-10-18)
      if Result <> '' then Result := Result + ',';
      Result := Result + AddonId(I);
    end;
end;
