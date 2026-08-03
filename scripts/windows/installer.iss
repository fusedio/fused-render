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
  #define OutputBaseName "FusedRenderPy-setup"
#endif
; FusedRenderPy: experiment/python-supervisor's own product identity — a
; distinct exe name, install dir, ProgID prefix, and AppId GUID from the
; shipping "FusedRender" product (Rust supervisor, feat/windows-desktop-
; foundation), so this experimental build can be installed side by side
; without colliding with a real install's files or registry entries.
; docs/PYTHON_SUPERVISOR_SPEC.md's must-match behaviors (mutex/pipe names,
; AppUserModelID, Desktop paths under %LOCALAPPDATA%\FusedRender\Desktop)
; stay identical to the Rust contract on purpose — those live in
; fused_render/supervisor/*.py, not here.
#define AppUserModelId "Fused.FusedRender.Desktop"
#define ExeName "FusedRenderPy.exe"
#define InstalledIcon "{app}\payload\assets\icons\fused-render.ico"
#define UninstallKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{9F1D3C2A-6B4E-4A8F-9C3D-2E7B5A1F8D6C}_is1"

[Setup]
AppId={{9F1D3C2A-6B4E-4A8F-9C3D-2E7B5A1F8D6C}
AppName=FusedRender
AppVersion={#AppVersion}
AppPublisher=Fused
AppPublisherURL=https://fused.io
DefaultDirName={localappdata}\Programs\FusedRenderPy
DefaultGroupName=FusedRenderPy
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
; Serialize installer runs — an auto-update prompt launching setup can race a
; manual reinstall through ActivatePayload's rename dance otherwise.
SetupMutex=FusedRenderPySetup

[Files]
Source: "{#BundleDir}\{#ExeName}"; DestDir: "{app}\next"; Flags: ignoreversion
Source: "{#BundleDir}\python\*"; DestDir: "{app}\next\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundleDir}\assets\*"; DestDir: "{app}\next\assets"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#BundleDir}\payload.complete"; DestDir: "{app}\next"; Flags: ignoreversion; AfterInstall: ActivatePayload
; WinFsp MSI (D133): extracted on demand by InstallWinFsp, never persisted in
; the payload — {tmp} is deleted when setup exits.
Source: "{#BundleDir}\winfsp.msi"; Flags: dontcopy

[InstallDelete]
Type: filesandordirs; Name: "{app}\next"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\cache"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\runtime"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\temp"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\logs"
; the [Icons] entries below were renamed from "FusedRender (Python Supervisor)"
; — Inno's uninstall log only tracks additions per run, so upgrading over an
; older install would otherwise leave these old .lnk files orphaned in the
; Start Menu.
Type: files; Name: "{group}\FusedRender (Python Supervisor).lnk"
Type: files; Name: "{group}\Uninstall FusedRender (Python Supervisor).lnk"

[Icons]
Name: "{group}\FusedRender"; Filename: "{app}\payload\{#ExeName}"; WorkingDir: "{localappdata}"; IconFilename: "{#InstalledIcon}"; AppUserModelID: "{#AppUserModelId}"
Name: "{group}\Uninstall FusedRender"; Filename: "{uninstallexe}"

[Registry]
#include BundleDir + "\registry.iss"

[Run]
Filename: "{app}\payload\{#ExeName}"; WorkingDir: "{localappdata}"; Description: "Launch FusedRender"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\cache"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\runtime"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\temp"
Type: filesandordirs; Name: "{localappdata}\FusedRender\Desktop\logs"

[Code]
var
  PayloadPrepared: Boolean;
  PayloadActivated: Boolean;

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
  { Older updaters launch Setup without lpDirectory, so Setup inherits the
    running app's payload\ working directory and locks the very directory the
    upgrade must rename. Release that inherited directory before doing anything
    else. Newer updaters also pass a safe lpDirectory, but the installer must
    remain self-sufficient for upgrades from already-shipped versions. }
  Result := SetCurrentDir(ExpandConstant('{tmp}'));
  if not Result then
  begin
    MsgBox('Setup could not switch to its temporary working directory.',
      mbError, MB_OK);
    Exit;
  end;
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
  Supervisor := ExpandConstant('{app}\payload\{#ExeName}');
  if not FileExists(Supervisor) then
    Supervisor := ExpandConstant('{app}\{#ExeName}');
  Result := (not FileExists(Supervisor)) or
    (Exec(Supervisor, '--shutdown-for-upgrade', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0));
end;

function RenameWithRetry(const OldDir, NewDir: String): Boolean;
var
  Attempt: Integer;
begin
  { The supervisor's --shutdown-for-upgrade sweeps its process tree before this
    runs, but a terminated process's handles release asynchronously (and AV /
    the indexer may briefly hold a freshly written dir). Retry ~15s so the swap
    rides out that residue instead of failing the whole upgrade. }
  for Attempt := 1 to 30 do
  begin
    Result := RenameFile(OldDir, NewDir);
    if Result then
      Exit;
    Sleep(500);
  end;
end;

procedure RecoverPayload();
var
  CurrentPayload: String;
  PreviousPayload: String;
begin
  CurrentPayload := ExpandConstant('{app}\payload');
  PreviousPayload := ExpandConstant('{app}\previous');
  if not DirExists(CurrentPayload) and DirExists(PreviousPayload) and
    not RenameWithRetry(PreviousPayload, CurrentPayload) then
    RaiseException('The previous FusedRender payload could not be recovered.');
end;

procedure KillPayloadStragglers();
var
  Locator, WMI, Results: Variant;
  Pattern: String;
  I: Integer;
begin
  { Detached workers (template daemons, rclone rcd) outlive the app and hold
    payload\ locked; old payloads' --shutdown-for-upgrade exits 0 without
    sweeping them when no supervisor is running. Best-effort: a WMI failure
    just leaves RenameWithRetry to ride out whatever survives. }
  try
    Pattern := ExpandConstant('{app}\');
    StringChangeEx(Pattern, '\', '\\', True);
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    WMI := Locator.ConnectServer('.', 'root\CIMV2');
    Results := WMI.ExecQuery(Format(
      'SELECT * FROM Win32_Process WHERE ExecutablePath LIKE ''%s%%''', [Pattern]));
    for I := 0 to Results.Count - 1 do
      try
        Results.ItemIndex(I).Terminate(1);
      except
      end;
  except
  end;
end;

function PreparePayloadForInstall(): String;
var
  CurrentPayload: String;
  PreviousPayload: String;
begin
  Result := '';
  CurrentPayload := ExpandConstant('{app}\payload');
  PreviousPayload := ExpandConstant('{app}\previous');
  if not DirExists(CurrentPayload) then
    Exit;

  { Move the installed tree before Inno begins applying files or metadata. A
    lock is therefore a PrepareToInstall failure (exit code 7), not an
    AfterInstall expression error that Inno reports but then continues past. }
  if DirExists(PreviousPayload) then
  begin
    DelTree(PreviousPayload, True, True, True);
    if DirExists(PreviousPayload) then
    begin
      Result := 'The previous FusedRender payload could not be removed.';
      Exit;
    end;
  end;
  if not RenameWithRetry(CurrentPayload, PreviousPayload) then
  begin
    Result := 'The installed FusedRender payload could not be moved. Exit it from the tray and retry setup.';
    Exit;
  end;
  PayloadPrepared := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if CompareText(ExpandConstant('{app}'),
    ExpandConstant('{localappdata}\Programs\FusedRenderPy')) <> 0 then
    Result := 'FusedRender must be installed in its private application directory.'
  else
  begin
    RecoverPayload();
    if not ShutdownSupervisor() then
      Result := 'FusedRender could not be stopped. Exit it from the tray and retry setup.'
    else
    begin
      KillPayloadStragglers();
      Result := PreparePayloadForInstall();
    end;
  end;
end;

procedure RestorePreviousPayload();
var
  CurrentPayload: String;
  PreviousPayload: String;
begin
  CurrentPayload := ExpandConstant('{app}\payload');
  PreviousPayload := ExpandConstant('{app}\previous');
  if not DirExists(CurrentPayload) and DirExists(PreviousPayload) then
    RenameWithRetry(PreviousPayload, CurrentPayload);
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
  if not FileExists(NewPayload + '\{#ExeName}') or
    not FileExists(NewPayload + '\python\python.exe') or
    not FileExists(NewPayload + '\python\pythonw.exe') or
    not FileExists(NewPayload + '\python\uv.exe') or
    not FileExists(NewPayload + '\python\python312._pth') or
    not FileExists(NewPayload + '\python\Lib\site-packages\fused_render\__init__.py') or
    not FileExists(NewPayload + '\python\Lib\site-packages\win32\win32job.pyd') or
    not FileExists(NewPayload + '\python\Lib\site-packages\fused_render\static\shell-dist\index.html') then
  begin
    RestorePreviousPayload();
    RaiseException('The new FusedRender payload is incomplete.');
  end;
  { Fresh installs have no prepared payload. Keep the old fallback for repair
    installs whose tree appeared between PrepareToInstall and extraction. }
  if DirExists(CurrentPayload) and not RenameWithRetry(CurrentPayload, PreviousPayload) then
    RaiseException('The installed FusedRender payload could not be moved.');
  if not RenameWithRetry(NewPayload, CurrentPayload) then
  begin
    { Roll back with the same retry: the compensation rename races the identical
      transient locks, so a plain RenameFile here could leave no payload dir. }
    RestorePreviousPayload();
    RaiseException('The new FusedRender payload could not be activated.');
  end;
  PayloadActivated := True;
end;

function InitializeUninstall(): Boolean;
begin
  RecoverPayload();
  Result := ShutdownSupervisor();
  if Result then
    KillPayloadStragglers()
  else
    MsgBox('FusedRender could not be stopped. Exit it from the tray and retry uninstall.',
      mbError, MB_OK);
end;

function WinFspInstalled(): Boolean;
begin
  { Mirrors shell/mounts.py's _winfsp_available(): WinFsp installs its system
    DLL under %ProgramFiles(x86)%\WinFsp\bin — winfsp-x64.dll on x64,
    winfsp-a64.dll on ARM64. }
  Result :=
    FileExists(ExpandConstant('{commonpf32}\WinFsp\bin\winfsp-x64.dll')) or
    FileExists(ExpandConstant('{commonpf32}\WinFsp\bin\winfsp-a64.dll'));
end;

procedure InstallWinFsp();
var
  ErrorCode: Integer;
begin
  { Chain-install the bundled WinFsp MSI so mounts work with zero user setup
    (D133). The app itself stays per-user (PrivilegesRequired=lowest); only
    this MSI elevates, through the one UAC prompt the runas verb raises.
    Declining that prompt (or an MSI failure) must never fail setup — mounts
    then surface shell/mounts.py's "install WinFsp" message until the driver
    is installed, so it degrades, not breaks. }
  if WinFspInstalled() then
    Exit;
  ExtractTemporaryFile('winfsp.msi');
  if not ShellExec('runas', 'msiexec.exe',
    '/i "' + ExpandConstant('{tmp}\winfsp.msi') + '" /qn /norestart',
    '', SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
    Log('WinFsp install did not run (UAC declined?): ' + SysErrorMessage(ErrorCode))
  else if (ErrorCode <> 0) and (ErrorCode <> 3010) then
    { 3010 = ERROR_SUCCESS_REBOOT_REQUIRED — installed fine under /norestart. }
    Log(Format('WinFsp MSI exited with %d', [ErrorCode]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if PayloadActivated then
    begin
      DelTree(ExpandConstant('{app}\previous'), True, True, True);
      InstallWinFsp();
    end;
  end;
end;

procedure DeinitializeSetup();
begin
  { Cancel, extraction failure, or a failed activation must not strand the old
    install under previous\. RecoverPayload on the next setup run is the second
    safety net for hard process termination or power loss. }
  if PayloadPrepared and not PayloadActivated then
    RestorePreviousPayload();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  StartupCommand: String;
  Supervisor: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { startup.py writes '"<exe>" --startup' — match on the quoted exe prefix
      so the entry is removed whatever arguments follow it. }
    Supervisor := ExpandConstant('{app}\payload\{#ExeName}');
    if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'FusedRenderDesktop', StartupCommand) and
      (Pos(Lowercase('"' + Supervisor + '"'), Lowercase(StartupCommand)) = 1) then
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
        'FusedRenderDesktop');
  end;
end;
