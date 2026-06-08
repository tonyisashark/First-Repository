; Inno Setup script for the Kalshi Temperature Bot.
; Produces a Windows installer that:
;   * installs the standalone GUI executable (no Python required on the target),
;   * creates a Start Menu entry  -> makes the app searchable in Windows Search,
;   * optionally creates a Desktop shortcut,
;   * registers an uninstaller (appears in "Apps & features").
;
; Build:  compile this with Inno Setup 6 (ISCC.exe installer\kalshi_temp_bot.iss)
; after PyInstaller has produced dist\KalshiTempBot.exe.

#define MyAppName "Kalshi Temperature Bot"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Kalshi Temperature Bot"
#define MyAppExeName "KalshiTempBot.exe"

[Setup]
; A stable AppId keeps upgrades/uninstall consistent across versions.
AppId={{8F2B9A1C-4D3E-4A77-9C2E-1B6F0D5A7E34}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\KalshiTempBot
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=KalshiTempBotSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Per-user install -> no administrator rights required.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
; Start Menu shortcut -> indexed by Windows Search (searchable by name).
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; Optional Desktop shortcut (controlled by the task above).
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
