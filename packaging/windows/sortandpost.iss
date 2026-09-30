; Inno Setup script for the Windows installer (V2 P6). Built by build.ps1:
;   ISCC /DAppVersion=<v> /DSourceDir=<dist\sortandpost> /DIconFile=<ico> /DOutputDir=<dist> sortandpost.iss
;
; Per-user by default (no admin prompt): installs to %LOCALAPPDATA%\Programs\sortandpost.
; The installer only ever touches the program folder, the Start menu and the
; optional desktop shortcut. The user's library, the shared folder and
; %LOCALAPPDATA%\sortandpost (machine data) are never installed or removed here.

#ifndef AppVersion
  #error Pass /DAppVersion=<version>
#endif

[Setup]
; Never change AppId: it's how upgrades and the uninstaller find this app.
AppId={{6F1B7C2E-4D3A-4E8B-9A51-2C7D5E0B8F14}
AppName=sortandpost
AppVersion={#AppVersion}
AppVerName=sortandpost {#AppVersion}
AppPublisher=sortandpost
DefaultDirName={autopf}\sortandpost
DefaultGroupName=sortandpost
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=sortandpost-{#AppVersion}-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\sortandpost.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; A running copy is closed by [Code] below before this check; Restart Manager
; is only the fallback (it can't close the windowless server child itself).
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; A new version's bundle replaces the old one wholesale (stale DLLs from an
; older torch must not linger).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\sortandpost"; Filename: "{app}\sortandpost.exe"
Name: "{autodesktop}\sortandpost"; Filename: "{app}\sortandpost.exe"; Tasks: desktopicon

[Registry]
; The tray's "Start at login" value (sortandpost.desktop.login_item): never
; written here, only removed on uninstall so it can't point at a deleted exe.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "sortandpost"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\sortandpost.exe"; Description: "{cm:LaunchProgram,sortandpost}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WM_ENDSESSION = $0016;
  ENDSESSION_CLOSEAPP = 1;
  // sortandpost.desktop._windows.WINDOW_TITLE / APP_MUTEX
  TrayWindowTitle = 'sortandpost tray';
  AppMutex = 'sortandpost-app';
  StillRunning = 'sortandpost is still running. Quit it from its tray icon (right-click, Quit sortandpost), then try again.';

// Ask a running sortandpost to quit the clean way: the tray stops its server
// (final publish + write-lock release), then exits. SendMessage returns once
// the server has stopped; then wait for the tray process to go.
procedure CloseRunningApp();
var
  Wnd: HWND;
  I: Integer;
begin
  Wnd := FindWindowByWindowName(TrayWindowTitle);
  if Wnd = 0 then
    Exit;
  Log('Asking the running sortandpost to quit');
  SendMessage(Wnd, WM_ENDSESSION, 1, ENDSESSION_CLOSEAPP);
  // The mutex goes when the tray process has fully exited.
  for I := 1 to 60 do
  begin
    if not CheckForMutexes(AppMutex) then
      Break;
    Sleep(500);
  end;
  Sleep(500);  // the server child exited first; let the OS release its files
end;

// Never replace or remove files under a running app: a half-replaced bundle
// breaks the copy that is still running (and the install itself).
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseRunningApp();
  if CheckForMutexes(AppMutex) then
    Result := StillRunning
  else
    Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CloseRunningApp();
  Result := not CheckForMutexes(AppMutex);
  if not Result then
    MsgBox(StillRunning, mbError, MB_OK);
end;
