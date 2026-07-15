; Inno Setup script for the fused-render Windows installer.
; Compiled by scripts/build_windows_installer.ps1, which stages a relocatable
; CPython + the fused-render wheel[bundled,fused] under BundleDir and passes the
; values below via ISCC /D. Not meant to be compiled by hand.
;
; Per-user, no elevation: installs into %LOCALAPPDATA%\Programs, registers the
; Explorer "Open with" associations through the bundled interpreter on install,
; and unregisters them on uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppNameSuffix
  #define AppNameSuffix ""
#endif
#ifndef BundleDir
  #error "BundleDir must be passed with /DBundleDir=..."
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseName
  #define OutputBaseName "FusedRender-setup"
#endif
#ifndef IconFile
  #define IconFile BundleDir + "\python\Lib\site-packages\fused_render\assets\fused-render.ico"
#endif

#define AppName "FusedRender" + AppNameSuffix
; A stable per-name GUID so upgrades replace in place; the suffix keeps
; branch builds from colliding with the baseline install.
#define AppId "{{FA7D9E2C-3B4A-4C1D-9E6F-FUSEDRENDER" + AppNameSuffix + "}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Fused
AppPublisherURL=https://fused.io
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\python\pythonw.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; A large bundle (~1 GB) — give the copy step room and disable needless prompts.
DisableDirPage=auto
DisableReadyPage=no

[Files]
; The whole relocatable runtime lands under {app}\python. recursesubdirs +
; createallsubdirs copies the deep pxr/matplotlib data trees intact.
Source: "{#BundleDir}\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; Upgrades: wipe the old runtime first so stale .dist-info / removed modules
; don't accumulate (Inno overlays files, it never deletes them on its own).
Type: filesandordirs; Name: "{app}\python"

[Icons]
; Windowless launch (pythonw) so the Start Menu entry never flashes a console.
; The tray starts (or reuses) the server, opens the browser, and offers a
; Stop-and-quit; the file associations still use winopen directly.
Name: "{group}\{#AppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: "-m fused_render.wintray"; IconFilename: "{#IconFile}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

[Run]
; Register the Explorer associations via the bundled interpreter. python.exe
; (not pythonw) so it has a real stdout — winopen prints instead of popping a
; MessageBox; runhidden keeps the console off-screen during a silent install.
Filename: "{app}\python\python.exe"; Parameters: "-m fused_render.winopen --register"; Flags: runhidden; StatusMsg: "Registering file associations..."

[UninstallRun]
; Runs before the files are removed (Inno's default ordering), so the
; interpreter is still present to undo the registration.
Filename: "{app}\python\python.exe"; Parameters: "-m fused_render.winopen --unregister"; Flags: runhidden; RunOnceId: "UnregisterFusedRender"

[UninstallDelete]
; Inno only removes files it installed; the bundled interpreter writes .pyc
; caches under {app}\python at runtime that aren't in the log. Sweep the whole
; install tree so uninstall leaves nothing behind.
Type: filesandordirs; Name: "{app}"
; Runtime-created state Inno's uninstall log doesn't track (pidfiles, logs).
Type: filesandordirs; Name: "{localappdata}\fused-render"
