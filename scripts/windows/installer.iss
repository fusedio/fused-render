#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef BundleDir
  #error "BundleDir must be provided"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseName
  #define OutputBaseName "FusedRender-setup"
#endif

#define AppUserModelId "Fused.FusedRender.Desktop"
#define InstalledIcon "{app}\payload\assets\icons\fused-render.ico"
#define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{0C8B8297-F699-45C0-9B49-F01CED3C6986}_is1"

[Setup]
AppId={{0C8B8297-F699-45C0-9B49-F01CED3C6986}
AppName=FusedRender
AppVersion={#AppVersion}
AppPublisher=Fused
AppPublisherURL=https://fused.io
DefaultDirName={localappdata}\Programs\FusedRender
DefaultGroupName=FusedRender
DisableProgramGroupPage=yes
DisableDirPage=yes
UsePreviousAppDir=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.10240
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
SetupIconFile={#BundleDir}\assets\icons\fused-render.ico
UninstallDisplayIcon={#InstalledIcon}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
ChangesAssociations=yes
SetupLogging=yes

[Files]
Source: "{#BundleDir}\FusedRender.exe"; DestDir: "{app}\next"; Flags: ignoreversion
Source: "{#BundleDir}\python\*"; DestDir: "{app}\next\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundleDir}\assets\*"; DestDir: "{app}\next\assets"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundleDir}\payload.complete"; DestDir: "{app}\next"; Flags: ignoreversion; AfterInstall: ActivatePayload

[InstallDelete]
Type: filesandordirs; Name: "{app}\next"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\cache"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\runtime"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\temp"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\logs"

[Icons]
Name: "{group}\FusedRender"; Filename: "{app}\payload\FusedRender.exe"; IconFilename: "{#InstalledIcon}"; AppUserModelID: "{#AppUserModelId}"
Name: "{group}\Uninstall FusedRender"; Filename: "{uninstallexe}"

[Registry]
#include BundleDir + "\registry.iss"

[Run]
Filename: "{app}\payload\FusedRender.exe"; Description: "Launch FusedRender"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\cache"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\runtime"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\temp"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\logs"

[Code]
function NextVersionPart(var Version: String): Integer;
var
  Separator: Integer;
  Part: String;
begin
  Separator := Pos('.', Version);
  if Separator = 0 then
  begin
    Part := Version;
    Version := '';
  end
  else
  begin
    Part := Copy(Version, 1, Separator - 1);
    Delete(Version, 1, Separator);
  end;
  Result := StrToIntDef(Part, 0);
end;

function CompareVersions(Left, Right: String): Integer;
var
  Index: Integer;
  LeftPart: Integer;
  RightPart: Integer;
begin
  Result := 0;
  for Index := 1 to 4 do
  begin
    LeftPart := NextVersionPart(Left);
    RightPart := NextVersionPart(Right);
    if LeftPart < RightPart then
    begin
      Result := -1;
      Exit;
    end;
    if LeftPart > RightPart then
    begin
      Result := 1;
      Exit;
    end;
  end;
end;

function InitializeSetup(): Boolean;
var
  InstalledVersion: String;
begin
  Result := True;
  if RegQueryStringValue(HKCU, '{#UninstallKey}', 'DisplayVersion', InstalledVersion) and
    (CompareVersions('{#AppVersion}', InstalledVersion) < 0) then
  begin
    MsgBox('A newer FusedRender version is already installed.', mbError, MB_OK);
    Result := False;
  end;
end;

function ShutdownSupervisor(): Boolean;
var
  ResultCode: Integer;
  Supervisor: String;
begin
  Supervisor := ExpandConstant('{app}\payload\FusedRender.exe');
  if not FileExists(Supervisor) then
    Supervisor := ExpandConstant('{app}\FusedRender.exe');
  Result := (not FileExists(Supervisor)) or
    (Exec(Supervisor, '--shutdown-for-upgrade', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0));
end;

procedure RecoverPayload();
var
  CurrentPayload: String;
  PreviousPayload: String;
begin
  CurrentPayload := ExpandConstant('{app}\payload');
  PreviousPayload := ExpandConstant('{app}\previous');
  if not DirExists(CurrentPayload) and DirExists(PreviousPayload) and
    not RenameFile(PreviousPayload, CurrentPayload) then
    RaiseException('The previous FusedRender payload could not be recovered.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CompareText(ExpandConstant('{app}'),
    ExpandConstant('{localappdata}\Programs\FusedRender')) <> 0 then
    Result := 'FusedRender must be installed in its private application directory.'
  else
  begin
    RecoverPayload();
    if not ShutdownSupervisor() then
    Result := 'FusedRender could not be stopped. Exit it from the tray and retry setup.';
  end;
end;

procedure ActivatePayload();
var
  CurrentPayload: String;
  NewPayload: String;
  PreviousPayload: String;
begin
  CurrentPayload := ExpandConstant('{app}\payload');
  NewPayload := ExpandConstant('{app}\next');
  PreviousPayload := ExpandConstant('{app}\previous');
  if not FileExists(NewPayload + '\FusedRender.exe') or
    not FileExists(NewPayload + '\python\python.exe') or
    not FileExists(NewPayload + '\python\pythonw.exe') or
    not FileExists(NewPayload + '\python\uv.exe') or
    not FileExists(NewPayload + '\python\python312._pth') or
    not FileExists(NewPayload + '\python\Lib\site-packages\fused_render\__init__.py') or
    not FileExists(NewPayload + '\python\Lib\site-packages\fused_render\static\shell-dist\index.html') then
    RaiseException('The new FusedRender payload is incomplete.');
  DelTree(PreviousPayload, True, True, True);
  if DirExists(CurrentPayload) and not RenameFile(CurrentPayload, PreviousPayload) then
    RaiseException('The installed FusedRender payload could not be moved.');
  if not RenameFile(NewPayload, CurrentPayload) then
  begin
    if DirExists(PreviousPayload) then
      RenameFile(PreviousPayload, CurrentPayload);
    RaiseException('The new FusedRender payload could not be activated.');
  end;
end;

function InitializeUninstall(): Boolean;
begin
  RecoverPayload();
  Result := ShutdownSupervisor();
  if not Result then
    MsgBox('FusedRender could not be stopped. Exit it from the tray and retry uninstall.',
      mbError, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    DelTree(ExpandConstant('{app}\previous'), True, True, True);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupCommand: String;
  Supervisor: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Supervisor := ExpandConstant('{app}\payload\FusedRender.exe');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'FusedRenderDesktop', StartupCommand) and
      (CompareText(StartupCommand, '"' + Supervisor + '"') = 0) then
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
        'FusedRenderDesktop');
  end;
end;
