; Inno Setup script - builds RelayCRM-Setup-<version>.exe
; Installs per-user (no admin rights needed), adds Desktop + Start menu shortcuts.
#define AppName "Relay CRM"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7C1E2B4A-5D3F-4E8B-9A61-2E5C1A7D0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Relay CRM
DefaultDirName={autopf}\Relay CRM
DefaultGroupName=Relay CRM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\Output
OutputBaseFilename=RelayCRM-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\RelayCRM.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\RelayCRM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}\docs"; DestName: "Relay CRM User Guide.txt"; Flags: ignoreversion
Source: "..\AZURE_SETUP.md"; DestDir: "{app}\docs"; DestName: "Microsoft 365 Setup for IT.txt"; Flags: ignoreversion

[Icons]
Name: "{group}\Relay CRM"; Filename: "{app}\RelayCRM.exe"
Name: "{group}\Relay CRM User Guide"; Filename: "{app}\docs\Relay CRM User Guide.txt"
Name: "{group}\Microsoft 365 Setup for IT"; Filename: "{app}\docs\Microsoft 365 Setup for IT.txt"
Name: "{group}\Uninstall Relay CRM"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Relay CRM"; Filename: "{app}\RelayCRM.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RelayCRM.exe"; Description: "Open Relay CRM now"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedLabel=Relay CRM is installed. Your data is stored only on this computer, in %LOCALAPPDATA%\RelayCRM (uninstalling never deletes it).
