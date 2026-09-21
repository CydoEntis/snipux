# Platform capabilities

Every OS facility snipux reaches for, what it is used for, and what the user
sees. This is the list to check before adding a new one -- and a new one
belongs on this page in the same change.

Rule of thumb: ask the OS for as little as possible, as late as possible,
and only for something the user just asked for.

## Everywhere

| Facility | Used for | Notes |
| --- | --- | --- |
| Clipboard (`QClipboard`) | Copy, Copy text, player copy | Written only when the user clicks Copy. Never read in the background; no clipboard history. |
| Local socket (`QLocalServer`/`QLocalSocket`) | a second `snipux --snip` hands its request to the running one | Local only, fixed server name. Carries a command, never pixels. |
| Files | saving captures, `config.json` and friends | Only the save/recording folders, the config folder, and a file the user opened. |
| `pip` (subprocess) | `snipux --update` | Only when the user runs it. Refused in the standalone exe. |
| `ffmpeg` (subprocess) | H.264 and GIF export | Optional, found on `PATH`, never installed by us. |

**Not used anywhere:** network requests, telemetry, crash upload, automatic
updates, accounts, camera, location, keylogging. The microphone is used
only on Windows, only when the user picks Mic for a recording. The
global hotkey registers one chord; snipux does not see other keystrokes.

## Linux

| Facility | Used for | What the user sees |
| --- | --- | --- |
| `org.freedesktop.portal.Screenshot` (D-Bus, via jeepney) | the one-shot grab on Wayland | GNOME asks once, before the first snip -- "Allow Snipux to Take Screenshots?" -- and remembers the answer. It names the app by the systemd scope it runs in: started from its desktop entry or at login, that is Snipux; started from a terminal, it is asked as that terminal. The prompt gives up after 25 seconds. |
| `org.gnome.Shell.Screenshot` | a grab on GNOME older than 41, which answered any caller | Nothing. GNOME 41 and later refuse callers not on its allowlist ("Screenshot is not allowed"), so the portal is the route there. |
| `org.gnome.Shell.Screencast` / `ScreencastArea` | recording | GNOME's recording indicator. WebM; the interface has no audio and no pause. |
| system `ffmpeg` (optional), PulseAudio input | recording sound, and joining a paused recording's pieces | Nothing. Without it, Pause and the System/Mic sources are greyed and say why. |
| `grim`, `scrot`, ImageMagick `import` | capture backends when present | Nothing. Each is one backend among several. |
| `xprop` | window geometry on X11 (Window/Browser modes) | Nothing. |
| `wl-copy` | clipboard on Wayland when Qt's is not enough | Nothing. |
| `gsettings` (`org.gnome.settings-daemon.plugins.media-keys` custom keybindings; `shell`/`mutter`/`desktop.wm` keybindings to find clashes) | binding the global shortcut and clearing chords that clash | Written by `--setup`; undone by `--remove`. |
| `.desktop` files, autostart, hicolor icons | app launcher and start at login | Written by `--setup`; undone by `--remove`. |

Pin is greyed on Wayland: a client cannot place its own window or keep it on
top there. Copy text is greyed on Linux until a system OCR route exists
(issue #92).

## Windows

| Facility | Used for | What the user sees |
| --- | --- | --- |
| `QScreenCapture` (QtMultimedia) | the one-shot grab | Nothing. |
| `QScreenCapture` -> `QMediaRecorder` | recording, pause/resume | Windows' own recording indicator. H.264 video. |
| `QAudioInput` (default input device) | Mic audio in a recording | Only when the user picks Mic; each recording starts Muted. Windows may ask for microphone access. System (desktop) sound is greyed: Qt cannot capture it. |
| `user32.RegisterHotKey` / `UnregisterHotKey` | the global shortcut | One chord (`MOD_NOREPEAT`). |
| `user32.SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` | keeping snipux's own bars out of a recording | Nothing. Windows 10 2004+. |
| `kernel32.AttachConsole` | printing `--help`/`--update` output from the windowless launcher | Nothing. |
| COM `IShellLinkW` / `IPersistFile` (raw `ctypes`) | Start Menu and Startup shortcuts | Written by `--setup` / first run of the exe; undone by `--remove`. |
| `Windows.Media.Ocr` via `powershell.exe` | Copy text, Hide sensitive | On-device. The image goes to a temp `.bmp`, which is deleted after the call. Needs an OCR language installed; greyed with the reason if not. |
| Self-relocation to `%LOCALAPPDATA%\snipux` | the standalone exe survives a Downloads cleanup | First run only. |

The exe is unsigned, so SmartScreen warns on first run (see
`docs/releasing.md` for why).

## macOS

Nothing yet. `platform/darwin.py` raises `UnimplementedPlatformError` for
every operation. The port will need **Screen Recording** permission for
capture and recording and **Accessibility** for the global shortcut, both
granted by the user in System Settings; this table gets filled in when it
lands.

## Adding a capability

1. Put the call in `snipux/platform/` (or a backend the platform chooses).
2. Add a `can_<x>()` / `<x>_unavailable_reason()` pair (or a per-option
   reason, like `audio_source_unavailable_reason()`) if some OSes can't do
   it, so the control is greyed with a reason rather than hidden.
3. Add its row here: what it is, what it is for, what the user sees.
4. Call out anything new that touches the network, the clipboard in the
   background, the microphone, or other apps' windows in the PR description.
   Those need the owner's explicit yes.
