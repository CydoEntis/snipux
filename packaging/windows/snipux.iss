; Inno Setup script for the snipux Windows installer (SNX-97). Wraps the
; standalone snipux.exe packaging/windows/build.ps1 already produces
; (SNX-96) -- this script's only job is to place that one file and
; register the Add/Remove Programs entry. It deliberately does *not*
; create a Start Menu shortcut, a Startup entry, or a hotkey registration
; of its own: snipux writes all three itself, the first time it actually
; runs (SNX-95), and duplicating that here would leave two copies of each
; behind an ordinary install/uninstall cycle. The uninstaller's own job
; mirrors that split -- see [UninstallRun] below.
;
; Built by packaging/windows/build.ps1, the documented command (see
; docs/releasing.md); running `iscc` on this file directly also works,
; provided dist\snipux.exe already exists (build.ps1's earlier PyInstaller
; step) and MyAppVersion is supplied on the command line, e.g.:
;
;   iscc /DMyAppVersion=0.1.0 packaging\windows\snipux.iss

#ifndef MyAppVersion
  ; Only hit when this script is compiled directly without build.ps1's
  ; /DMyAppVersion -- good enough to smoke-test the script itself, but
  ; build.ps1 always overrides it with the real `version` from
  ; pyproject.toml so a released installer never reports "0.0.0".
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "snipux"
#define MyAppPublisher "snipux"
#define MyAppURL "https://github.com/CydoEntis/snipux"
; Fixed across every release. Inno Setup uses this GUID, not the app name
; or install path, to recognise "this is the same product" from one
; version to the next -- a fixed AppId is what makes installing a new
; version overwrite the previous one's files and reuse its single
; Add/Remove Programs entry, rather than registering a second one
; alongside it (the acceptance criterion that upgrading must not leave
; two copies behind). Generated once for this ticket; never regenerate it
; for a later release.
#define MyAppId "{C826F985-2988-4278-AF8F-4BBDAA5DF4BF}"

; SourcePath is Inno's own name for this file's directory (always ends in
; a backslash) -- used instead of a relative "..\..\dist\..." everywhere
; so every path here resolves the same way regardless of the working
; directory `iscc` happens to be invoked from, the same reasoning
; snipux.spec's own SPECPATH comment gives for the PyInstaller build.
#define DistDir SourcePath + "..\..\dist"
#define IconFile SourcePath + "..\..\build\snipux.ico"

[Setup]
AppId={{#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}
; Per-user by default: {autopf} is Program Files when the installer is
; elevated and %LocalAppData%\Programs (the per-user equivalent) when it
; isn't, matching PrivilegesRequired below -- neither snipux nor this
; installer needs a machine-wide install, and the acceptance criterion is
; that administrator rights are only asked for when installing outside
; the user's own directories.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
; No Start Menu program group of our own -- see the file header on why
; the installer leaves shortcuts to snipux's own first-launch setup.
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Still lets someone pick a machine-wide install (Program Files) from the
; installer's own UI, or with `/ALLUSERS` on the command line -- the only
; case that should ever prompt for elevation.
PrivilegesRequiredOverridesAllowed=commandline dialog
OutputDir={#DistDir}
OutputBaseFilename=snipux-setup
Compression=lzma
SolidCompression=yes
; Shown in Add/Remove Programs next to the version this .iss already
; reports via AppVersion above.
UninstallDisplayIcon={app}\snipux.exe
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Files]
; The one file this installer places -- SNX-96's standalone, one-file
; PyInstaller build, which already bundles the interpreter, PyQt6 and
; every vendored asset snipux needs, so there is nothing else to copy.
Source: "{#DistDir}\snipux.exe"; DestDir: "{app}"; Flags: ignoreversion

[Run]
; So the app's own first-launch setup (SNX-95) -- the Start Menu
; shortcut, Startup entry and hotkey binding this installer deliberately
; doesn't create -- actually gets to run once, rather than leaving the
; user to go find and double-click the exe themselves before any of that
; exists. Unchecked by default is not appropriate here: skipping this is
; how a Start Menu entry never appears at all.
Filename: "{app}\snipux.exe"; Description: "Launch snipux"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; A running snipux holds the exe file open (this is a single-file
; PyInstaller bundle, not a DLL Windows can replace out from under a live
; process) and, on Windows, the RegisterHotKey registration
; WindowsPlatform.bind_shortcut() made -- that registration lives only in
; the running process and is released the moment it exits, clean or not
; (see platform/windows.py's unbind_shortcut), so ending it here is both
; what frees the file for deletion below and what releases the hotkey.
; `|| exit 0`: taskkill exits non-zero when nothing matches, which is the
; ordinary case (most uninstalls happen with snipux idle, not killed),
; and that must not be read as this step failing.
Filename: "{cmd}"; Parameters: "/C taskkill /IM snipux.exe /F || exit 0"; \
    Flags: runhidden; RunOnceId: "KillSnipux"
; The exact counterpart to [Run] above: undoes SNX-95's first-launch
; setup -- the Start Menu shortcut, Startup shortcut, generated .ico, and
; remembered shortcut choice (WindowsPlatform.remove_desktop_integration)
; -- run from the still-installed exe, before Inno deletes {app} below
; it. `skipifdoesntexist` covers the case where the user already deleted
; the exe by hand; `--remove` would have nothing left to run.
Filename: "{app}\snipux.exe"; Parameters: "--remove"; \
    Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "RemoveSnipux"
