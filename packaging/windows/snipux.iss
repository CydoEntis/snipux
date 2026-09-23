; Inno Setup script for snipux-setup.exe -- the Windows installer.
;
; Built by packaging/windows/build_installer.ps1, which builds snipux.exe
; first and passes the version in as /DAppVersion. Running ISCC on this file
; by hand works too, provided dist\snipux.exe already exists.
;
; SNX-97 built an installer, SNX-104 deleted it, and this brings one back
; deliberately. The reason it was deleted still stands and is worth stating
; plainly: Smart App Control, on by default on some Windows 11 installs,
; blocks an unsigned installer outright -- no "Run anyway", just a message
; that reads as if the file is corrupt. Signing is what fixes that, and for
; a tool with a handful of users a certificate is not a trade worth making.
;
; What makes this safe to ship anyway is that it is an *addition*. The
; portable snipux.exe stays on the Releases page exactly as it was, and it
; is not blocked by Smart App Control -- so the people this installer cannot
; serve still have the route they have always had. Deleting the installer
; last time removed the only option for everyone in order to protect the
; few; keeping both costs nobody anything.
;
; Three decisions worth knowing, all of them about not fighting the
; application's own behaviour:
;
; 1. It installs into %LOCALAPPDATA%\snipux, which is not an arbitrary
;    choice -- it is `platform.windows._portable_exe_path()`, the location a
;    frozen snipux.exe copies itself to on first run (SNX-103). Install
;    anywhere else and `_ensure_stable_copy()` would duplicate the exe on
;    first launch, leave the shortcuts pointing at its copy rather than at
;    the installed file, and leave that copy behind at uninstall.
;
; 2. Per-user, so there is no UAC prompt and no admin account needed. A
;    screen-capture tool that demands elevation to install is asking for
;    more trust than it needs.
;
; 3. It creates no shortcuts of its own. snipux writes its own Start Menu
;    entry, Startup entry, icon and Ctrl+Alt+S binding on first launch
;    (`run_first_launch_setup`), and a second set written here would be a
;    second thing to keep in step with it. The installer launches snipux
;    instead, which does all four.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C0D3A1E-6E5B-4E3F-9C1A-2F6D5B0A7E41}
AppName=Snipux
AppVersion={#AppVersion}
AppPublisher=Cody
AppPublisherURL=https://github.com/CydoEntis/snipux
AppSupportURL=https://github.com/CydoEntis/snipux/issues
AppUpdatesURL=https://github.com/CydoEntis/snipux/releases
; See decision 1 above: this is _portable_exe_path()'s directory.
DefaultDirName={localappdata}\snipux
DisableDirPage=yes
DisableProgramGroupPage=yes
; See decision 2: per-user, so no elevation is ever requested.
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=snipux-setup
SetupIconFile=..\..\build\snipux.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Snipux is a tray application, so the file being replaced is very likely
; running. Without this the install fails on a locked file with nothing
; useful to say about it.
CloseApplications=yes
CloseApplicationsFilter=snipux.exe
RestartApplications=no
UninstallDisplayName=Snipux
UninstallDisplayIcon={app}\snipux.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\snipux.exe"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; Launching is what performs setup: the Start Menu entry, the Startup entry,
; the icon and the hotkey are all written by snipux itself on first launch,
; so there is nothing for [Icons] to duplicate here.
Filename: "{app}\snipux.exe"; Description: "Start Snipux"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; `--remove` undoes exactly what that first launch wrote -- the shortcuts,
; the icon and the Ctrl+Alt+S binding -- none of which the installer put
; there, so none of which Inno knows to remove. RunOnceId keeps it to one
; call across a repeated uninstall.
Filename: "{app}\snipux.exe"; Parameters: "--remove"; RunOnceId: "RemoveIntegration"; Flags: runhidden

[UninstallDelete]
; The generated .ico and the single-instance socket live beside the exe and
; are written at runtime, so Inno has no record of them.
Type: filesandordirs; Name: "{app}"

[Code]
// SNX-97's installer is still on machines that took it before SNX-104
// removed it: a different AppId, a different directory
// (%LOCALAPPDATA%\Programs\snipux) and a stale "snipux version 0.1.0" row
// in Add/Remove Programs that nothing has replaced since. Its files are not
// where this installs, so without this they simply stay there -- an
// orphaned copy of an old snipux, and an uninstall entry that outlives the
// thing it uninstalls.
//
// **Its own uninstaller is deliberately not run.** That was the first
// version of this and it was wrong: the old uninstaller calls the old
// snipux's `--remove`, which tears down *per-user* state that is not the
// old install's to remove -- the Start Menu and Startup shortcuts, the
// generated icon under %LOCALAPPDATA%\snipux, and config.json. All of that
// belongs to whichever snipux is current, so cleaning up a stale 0.1.0
// deleted the settings and the autostart of the copy the user is actually
// running. Tested, and it did exactly that.
//
// What is left is the part that is genuinely the old install's: its own
// directory, and its own row in Add/Remove Programs. Nothing shared is
// touched, and a failure at any step is silent -- someone installing
// snipux asked for snipux, not for a dialog about a version they had
// forgotten they had.
const
  OldAppKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
              '{C826F985-2988-4278-AF8F-4BBDAA5DF4BF}_is1';

procedure RemoveOldInstall();
var
  Location: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, OldAppKey, 'InstallLocation', Location) then
    Exit;
  // Its directory, only if it still names one and that one is not where
  // this installs -- an InstallLocation pointing here would mean deleting
  // what was just written.
  Location := RemoveBackslashUnlessRoot(RemoveQuotes(Location));
  if (Location <> '') and DirExists(Location) and
     (CompareText(Location, RemoveBackslashUnlessRoot(ExpandConstant('{app}'))) <> 0) then
    DelTree(Location, True, True, True);
  RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, OldAppKey);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // After the new files are in place, so a failure here cannot leave the
  // machine with neither version.
  if CurStep = ssPostInstall then
    RemoveOldInstall();
end;
