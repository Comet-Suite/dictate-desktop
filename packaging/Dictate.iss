; SPDX-License-Identifier: GPL-3.0-only
#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#define AppName "Dictate"
#define AppExeName "Dictate.exe"

[Setup]
AppId={{FCFDEEC6-10A9-41D4-9263-FEFD00F42E66}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Comet-Suite
AppPublisherURL=https://github.com/Comet-Suite/dictate-desktop
AppSupportURL=https://github.com/Comet-Suite/dictate-desktop/issues
AppUpdatesURL=https://github.com/Comet-Suite/dictate-desktop/releases
DefaultDirName={localappdata}\Programs\Dictate
DefaultGroupName=Dictate
AllowNoIcons=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir=..\release
OutputBaseFilename=Dictate-Setup-{#AppVersion}-x64
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\Dictate.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=Dictate.exe
RestartApplications=no
VersionInfoVersion={#AppVersion}.0
VersionInfoDescription=Dictate Windows Installer
VersionInfoCompany=Comet-Suite
VersionInfoProductName=Dictate
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\Dictate\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Dictate"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "CometSuite.Dictate.Desktop"
Name: "{autodesktop}\Dictate"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "CometSuite.Dictate.Desktop"; Tasks: desktopicon

[Registry]
; The app manages opt-in startup. Remove its entry when this installation is removed.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue

[Run]
; Apply a saved enabled/disabled preference even on an upgrade or silent install.
; This does not enable startup for users who have left the existing switch off.
Filename: "{app}\{#AppExeName}"; Parameters: "--sync-startup"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExeName}"; Description: "Launch Dictate"; Flags: nowait postinstall skipifsilent
