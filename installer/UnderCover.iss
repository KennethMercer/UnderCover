; UnderCover Windows installer (Inno Setup 6, https://jrsoftware.org/isinfo.php).
; Build:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\UnderCover.iss
; Output: installer\dist\UnderCover-Setup-<version>.exe  (~1 MB; everything heavy is downloaded by setup.ps1)
;
; Per-user install to %LOCALAPPDATA%\UnderCover: no admin prompt, and the app can write its models/cache/output
; next to itself (config.toml paths are relative to the app folder).

#define AppName "UnderCover"
#define AppVersion "0.1.0"

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
FinishedLabel=UnderCover is installed. Start it from the Start menu. The first start downloads the voice and separation models (about 1-3 GB, depending on which features you use).

[Files]
; App code. __pycache__ and anything personal (models, cache, output, uploads, logs, test, .venv) stay out.
Source: "..\undercover\*"; DestDir: "{app}\undercover"; Flags: recursesubdirs ignoreversion; Excludes: "__pycache__,*.pyc"
Source: "..\check_env.py"; DestDir: "{app}"; Flags: ignoreversion
; Keep the user's settings on reinstall/update.
Source: "..\config.toml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
; Setup scripts.
Source: "setup.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "detect_gpu.ps1"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "selftest.py"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "requirements\*.txt"; DestDir: "{app}\installer\requirements"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\UnderCover.cmd"; WorkingDir: "{app}"
Name: "{group}\Repair {#AppName} (re-run setup)"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\setup.ps1"" -InstallDir ""{app}"" -Force -Pause"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\UnderCover.cmd"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
; Visible window so people can watch the downloads (PyTorch alone is 2-3 GB).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installer\setup.ps1"" -InstallDir ""{app}"" -Pause"; WorkingDir: "{app}"; StatusMsg: "Setting up Python, PyTorch and tools for your graphics card (this takes a while)..."; Flags: waituntilterminated
Filename: "{app}\UnderCover.cmd"; Description: "Start UnderCover now"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; Downloaded runtime pieces. Voices, covers and uploads (models, output, uploads) are the user's and are kept.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\logs"
Type: files; Name: "{app}\UnderCover.cmd"
