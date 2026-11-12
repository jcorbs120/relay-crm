; Inno Setup script for the Relay CRM team server.
; Built by build_windows.ps1 (or GitHub Actions). Needs Inno Setup 6.
;   iscc /DAppVersion=2.0.0 deploy\windows\RelayCRM.iss
; Expects: dist\RelayCRM\ (PyInstaller output) and deploy\windows\nssm.exe

#ifndef AppVersion
  #define AppVersion "2.0.0"
#endif
#define AppName "Relay CRM"
#define SvcName "RelayCRM"
#define Port "5055"

[Setup]
AppId={{6F3C2A0E-5B7D-4C1E-9A44-2E1A7C0D0002}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Relay CRM
DefaultDirName={autopf}\Relay CRM
DefaultGroupName=Relay CRM
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=RelayCRM-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
UninstallDisplayName={#AppName} (team server)
SetupLogging=yes
#ifexist "relay.ico"
SetupIconFile=relay.ico
#endif

[Tasks]
Name: "service";  Description: "Run Relay as a Windows service (starts automatically with the server; recommended)"; GroupDescription: "Server:"
Name: "firewall"; Description: "Allow teammates to connect through Windows Firewall (TCP port {#Port})"; GroupDescription: "Server:"
Name: "desktop";  Description: "Desktop shortcut to open Relay"; GroupDescription: "Shortcuts:"

[Dirs]
; Database, keys, logs and backups. Locked down to SYSTEM + Administrators below.
Name: "{commonappdata}\RelayCRM"; Flags: uninsneveruninstall

[Files]
Source: "..\..\dist\RelayCRM\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\ADMIN_GUIDE.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\..\AZURE_SETUP.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\Open Relay CRM"; Filename: "http://localhost:{#Port}/"
Name: "{group}\Relay data folder"; Filename: "{commonappdata}\RelayCRM"
Name: "{group}\Admin guide"; Filename: "{app}\docs\ADMIN_GUIDE.md"
Name: "{group}\Run Relay in a console window (if not using the service)"; Filename: "{app}\RelayCRM.exe"; Parameters: "--data-dir ""{commonappdata}\RelayCRM"""
Name: "{group}\Uninstall Relay CRM"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Relay CRM"; Filename: "http://localhost:{#Port}/"; Tasks: desktop

[Run]
; Restrict the data folder: it holds the database, encryption keys and mailbox tokens.
Filename: "{sys}\icacls.exe"; Parameters: """{commonappdata}\RelayCRM"" /inheritance:r /grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F"; Flags: runhidden; StatusMsg: "Securing the data folder..."
; Service (NSSM wraps the console exe; logs rotate in the data folder)
Filename: "{app}\nssm.exe"; Parameters: "install {#SvcName} ""{app}\RelayCRM.exe"" --service --data-dir ""{commonappdata}\RelayCRM"""; Flags: runhidden; Tasks: service; StatusMsg: "Installing the Relay service..."
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} DisplayName ""Relay CRM"""; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} Description ""Relay CRM team server (contacts, pipeline and Microsoft 365 email sync)"""; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppDirectory ""{app}"""; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} Start SERVICE_AUTO_START"; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppStdout ""{commonappdata}\RelayCRM\service.log"""; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppStderr ""{commonappdata}\RelayCRM\service.log"""; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppRotateFiles 1"; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "set {#SvcName} AppRotateBytes 5000000"; Flags: runhidden; Tasks: service
Filename: "{app}\nssm.exe"; Parameters: "start {#SvcName}"; Flags: runhidden; Tasks: service; StatusMsg: "Starting Relay..."
; Firewall
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Relay CRM"" dir=in action=allow protocol=TCP localport={#Port} profile=domain,private"; Flags: runhidden; Tasks: firewall
; Finish
Filename: "http://localhost:{#Port}/"; Description: "Open Relay CRM and create the first administrator"; Flags: postinstall shellexec nowait skipifsilent; Tasks: service

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop {#SvcName}"; Flags: runhidden; RunOnceId: "StopSvc"
Filename: "{app}\nssm.exe"; Parameters: "remove {#SvcName} confirm"; Flags: runhidden; RunOnceId: "RemoveSvc"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Relay CRM"""; Flags: runhidden; RunOnceId: "DelFw"

[Code]
// Stop a running service before files are replaced during an upgrade.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\nssm.exe')) then
    Exec(ExpandConstant('{app}\nssm.exe'), 'stop {#SvcName}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer;
begin
  // On upgrade the service already exists, so "install" fails harmlessly; just restart it.
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('service') then
    Exec(ExpandConstant('{app}\nssm.exe'), 'restart {#SvcName}', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Relay was removed. Your database and backups were kept in ' + ExpandConstant('{commonappdata}\RelayCRM') +
           '. Delete that folder yourself if you no longer need the data.', mbInformation, MB_OK);
end;
