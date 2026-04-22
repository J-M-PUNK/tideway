; Inno Setup script — builds an installer for the Windows PyInstaller
; output. Compile with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\Tideway.iss
;
; Prereq: pyinstaller Tideway-win.spec must have produced
; dist\Tideway\Tideway.exe (one-folder layout, not
; one-file — Inno Setup handles the extraction cost; one-file adds
; painful startup delay for the user).
;
; Output: dist\Tideway-setup-<version>.exe
;
; Registers a Start Menu entry and an optional desktop shortcut. The
; uninstaller is free from Inno. SmartScreen will still warn on first
; launch because the .exe is unsigned — acknowledged tradeoff, signing
; is a separate build step.

#define MyAppName "Tideway"
#define MyAppExeName "Tideway.exe"
#define MyAppPublisher "Tideway"
#define MyAppURL "https://github.com/yourname/tidal-downloader"

; VERSION is read from the repo-root VERSION file.
#define MyAppVersion ReadIni(SourcePath + "..\VERSION", "", "") == "" ? Trim(ReadFileString(SourcePath + "..\VERSION")) : "0.0.0"

[Setup]
AppId={{C2BFB2A0-1D3C-4B4E-AB17-5F8C3F1A7A2C}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; User install (not admin-required) is usually right for a desktop app.
; If the user opts for AllUsers, Inno elevates automatically.
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=Tideway-setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; Pull the entire one-folder PyInstaller output. recursesubdirs +
; createallsubdirs replicate the staging layout exactly so PyAV's
; and pystray's dynamic-loaded shared libraries resolve correctly
; from any install location.
Source: "..\dist\Tideway\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
