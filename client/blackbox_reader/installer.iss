; Inno Setup script for the Neglect Black Box reader (built by .github/workflows/blackbox-reader.yml on a Windows runner).
; Per-user install (no admin rights), starts at every Windows login (no window until Ctrl+Shift+N), removes its autostart entry when uninstalled.
; Compile:  iscc /DAppVersion=0.4.0 client\blackbox_reader\installer.iss   (expects dist\NeglectBlackBox.exe)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1E2C54-9B0A-4D63-8A59-3E6C1B7A94D2}
AppName=Neglect Black Box
AppVersion={#AppVersion}
AppVerName=Neglect Black Box {#AppVersion}
AppPublisher=Neglect
DefaultDirName={localappdata}\NeglectBlackBox
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=NeglectBlackBox-Setup
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=Neglect Black Box
UninstallDisplayIcon={app}\NeglectBlackBox.exe
SetupIconFile=assets\logo.ico
WizardStyle=modern
WizardImageFile=assets\wizard.bmp
WizardSmallImageFile=assets\wizard_small.bmp
WizardImageBackColor=$040305
ShowLanguageDialog=no

[Messages]
WelcomeLabel1=Neglect Black Box
WelcomeLabel2=This program reads the VRChat log on this PC and tells the Neglect bot who joins, leaves and gets roles in Neglect instances.%n%nIt never sends the log itself, never reads passwords or chats, and does nothing in other instances.%n%nA window with a "Stop logging" button opens with Ctrl+Shift+N. Closing that window does not stop the logger. The program updates itself.
FinishedHeadingLabel=Neglect Black Box is installed
FinishedLabel=The logger starts with Windows. Press Ctrl+Shift+N to see what it is doing, stop it or check for updates.

[Files]
Source: "..\..\dist\NeglectBlackBox.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\Neglect Black Box"; Filename: "{app}\NeglectBlackBox.exe"; Parameters: "show"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NeglectBlackBox"; ValueData: """{app}\NeglectBlackBox.exe"" run"; Flags: uninsdeletevalue

[Run]
; a normal installation offers to open the window; a silent one (the self-update) just starts the logger again, without a window
Filename: "{app}\NeglectBlackBox.exe"; Parameters: "show"; Description: "Start Neglect Black Box and open its window"; Flags: nowait postinstall skipifsilent
Filename: "{app}\NeglectBlackBox.exe"; Parameters: "run"; Flags: nowait runhidden; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM NeglectBlackBox.exe"; Flags: runhidden; RunOnceId: "StopReader"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  { a running reader holds the "only one copy" lock: end it before the files are replaced }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM NeglectBlackBox.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
