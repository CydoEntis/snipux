# Next: the destination migration, then Windows, then Wayland

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-127** is merged, and so is `fix/recording-flow`
(merge `97f129b`). The suite is green on Linux -- 1,542 passed, 0 failed.
It was last green on Windows at 1,457 passed / 14 skipped, which is
*before* the capture-flow work below; that number is stale.

The locked capture-flow handoff (`docs/design/flow/`) is part-built: the
recording bar and its stages are done, the chooser and the stills bar are
not. What was built differently, and why, is in that directory's
`divergences.md` -- read it before "fixing" anything back to the
handoff.

## Windows has now been watched, and region recording was broken

Run for real on Windows 11 on 2026-08-27, against the merged main.

**Region recording produced a 0-byte, unplayable file -- 0 runs out of 3
-- while full screen managed 3 of 3.** `_crop_frame()` built its output
format with `QVideoFrameFormat(size, pixel_format)`, which leaves
`streamFrameRate()` at 0. The encoder builds its output type from the
*first* frame it is handed, and Media Foundation's H264 encoder refuses a
type whose frame rate is 0: "could not set output type (80004005)" ->
"Could not initialize encoder" -> a recorder that dropped itself from
`RecordingState` to `StoppedState`. The source format's rate is carried
onto the crop now, and region measures 5.866s of video for 6.001s of wall
clock (0.977x -- no SNX-125 regression).

`setVideoFrameRate()` on the recorder did **not** cover this: it is only
called once `_RegionCropWorker` has 15 arrivals to average, long after the
encoder has had to commit. That race is why the failure was intermittent,
and it produced the most misleading evidence of the week -- a size sweep
"proved" 640x360 broken and 1280x720 fine, then proved the exact opposite
when the same sweep ran in reverse order. Nothing about the sizes
mattered; only what ran first did. **A sweep that varies one thing still
has to be run in more than one order.**

`stop()` now raises on a failure reported after `start()` returned. Both
start paths only ever checked for a *synchronous* failure, so this entire
fault went unreported: `start()` returned happily, the HUD counted up over
a recording that did not exist, and the toast named a file nobody had
written. `_stop_recording()` in app.py already catches and reports a
raising `stop()`, and already declines to land a file when one raises, so
this needed no app-side change.

Two smaller Windows faults, both fixed:

- `--list-backends` reported capture backends only -- no way to ask
  whether the machine could record at all, which on Windows is a
  single-backend yes/no question.
- `build.ps1` printed "Built ..." after a **failed** build.
  `$ErrorActionPreference` does not apply to a native executable's exit
  code, and python and pyinstaller are both native. Observed for real:
  PyInstaller died with "Access is denied" on `dist\snipux.exe` (a running
  snipux was holding its own image open), the script reported success, and
  left the stale exe in place.

And one that was Windows-only by accident: 11 `GnomeScreencastBackend`
tests failed here while passing on Linux, because `os.path.isabs()` is
`ntpath.isabs()` on a Windows dev box and since Python 3.13 that calls a
drive-less `/tmp/out.webm` **not** absolute. The check asks `posixpath`
now -- the answer comes from GNOME Shell and is a POSIX path whoever reads
it. Same family as SNX-126: a test that passes or fails by what the
machine running it happens to be.

**Still open on Windows:** the recording pill's placement was reported as
"doesn't stay stuck to the top". `_place_recording_hud` was run against a
real three-monitor layout (one of them at y=-1440) and every case lands
correctly top-centre of the right monitor, *except* the documented
fallback: a region covering the top-centre strip pushes the pill below the
recorded area, which on a 1440p screen is several hundred px down. That is
by design -- it keeps the pill out of the recording -- but it is what a
user reads as broken, and it is unresolved whether the report was that
case or a genuine bug. Worth knowing when picking this up: on Windows the
recorder only ever captures the **primary** screen, so on a multi-monitor
desktop any non-primary monitor is a guaranteed-unfilmed home for the
pill. Also note `_screen_for()` treats `geometries[0]` as the primary
screen for a full-screen recording, which `QGuiApplication.screens()` does
not actually guarantee.

## The capture flow: what is built, and the one thing left

The recording side of `docs/design/flow/` is built and driven end to end
on real GNOME. The stills side has rule 3 and nothing else from this
handoff.

**Built:** the recording bar (`flowbars.py`) with its four states, its
delay and audio dropdowns, the countdown numeral inside the region, the
red outline and live scrim around what is being filmed, cross-monitor
placement so a full-monitor recording still has a visible Stop, Window
recording, and the stills bar's action group leading the bar and carrying
the accent.

**Verified live**, not just in the suite -- which caught nothing that
mattered here. Twelve faults were found by using the app, every one of
them invisible to 1,500-odd passing tests: a bar shown behind a fullscreen
overlay for want of `Qt.WindowType.Tool`, a scrim whose panels were
present, sized, coloured and painting nothing, Enter copying a screenshot
of a region about to be recorded, Full screen filming all three monitors,
and the capture hotkey starting a recording rather than opening a snip.
**Photograph the screen before believing a UI change works.**

A full run, end to end: arm on a region, reframe it to 1100x560 with the
handles, record, stop. The file is 1100x560 -- the reframed size, not the
dragged one -- 2.96s, landed where the toast said, with the finished bar
carrying "0:02 - 0.2 MB - webm".

**Left: the destination migration.** `AFTER_CAPTURE`
(`instant`/`edit`/`review`) has to become the handoff's `DESTINATIONS`
(`Copy`/`Save`/`Open`). It is agreed, and it is deliberately not done in
the same pass as everything above, because it is the only remaining change
that:

- rewrites values already persisted in the user's config, so it needs a
  rename map like `_AFTER_CAPTURE_RENAMES` already is for `clip`/`file`;
- touches the Settings pane's radio cards, the chooser's rows and
  `_commit_selection`'s outcome branch at once;
- **drops a path that exists today.** `instant` finishes the snip with no
  overlay at all, and the handoff has no equivalent: every capture lands
  on the stills bar and fires its destination from there. `edit` likewise
  stops being a destination and becomes the thing that always happens.
  Whether the no-overlay path is worth keeping as a fourth option is a
  product question, not a merge conflict.

`DESTINATION_WORDING` in tokens.py already carries the per-kind Copy/Save
strings the record side uses, so the vocabulary exists; what is missing is
the model change behind it.

## Windows: three jobs, in this order

Written up from the Linux side on 2026-08-28. None of it can be checked
from here; all of it is reachable with the machine in front of you.

### 1 · The recorder always records the primary screen (a real bug)

`WindowsRecorderBackend._start_region` opens with
`screen = QGuiApplication.primaryScreen()` (`recording.py`, ~987) and maps
the requested rect against *that* screen's geometry and DPR.
`_start_full_screen` does the same. So on a multi-monitor desktop:

- a region dragged on a non-primary monitor is cropped out of the primary
  one -- wrong pixels, or an out-of-bounds crop, depending where it is;
- **Full screen on a non-primary monitor records the primary monitor.**

The second is newly visible rather than newly broken: Full screen used to
hand the backend `None`, which took the same primary-only path. It is now
an ordinary rect (see "Full screen means the monitor you are on"), so the
fix is the same one either way.

**The fix:** pick the screen the rect is actually on --
`QGuiApplication.screenAt(rect.center().toPoint())`, falling back to
`primaryScreen()` when it answers None (a rect whose centre is in the gap
between two staggered monitors). Then `QScreenCapture.setScreen()` that
one, and map through *its* geometry and DPR.

**How to know it worked:** record a region on your second monitor and look
at the file. Today it will contain the primary monitor's pixels. Note that
`_place_recording_hud` deliberately puts the bar on a *different* monitor
from the one being recorded, so on Windows the bar itself is a convenient
marker for which screen the recorder should not have chosen.

### 2 · Let the bar sit anywhere: WDA_EXCLUDEFROMCAPTURE

Asked directly, and the answer is yes on Windows and no on Linux.

`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` (user32, Windows
10 2004+) makes a window invisible to screen capture while still visible
on screen. Applied to `RecordingBar` (and `RegionFrame`), it removes the
whole reason placement has to work around the recorded area: the bar could
sit wherever it reads best, including over the region, and still be absent
from the file.

`WDA_EXCLUDEFROMCAPTURE` is `0x00000011`. The older `WDA_MONITOR` (`0x1`)
blanks the window to *black* in the capture rather than removing it, which
is worse than the current behaviour -- check the constant.

The hwnd is `int(widget.winId())`; call it after the widget is shown, and
re-apply if the window is ever recreated. Failure is a returned zero, not
an exception, so check it rather than assuming.

**Keep the geometry fallback.** Linux has no equivalent -- introspected:
`org.gnome.Shell.Screencast`'s `ScreencastArea` takes `draw-cursor` and
`framerate` and nothing else, and it captures the composited output. So
placement must stay correct without this; the affinity call is an
improvement on top, per platform, not a replacement for it.

### 3 · Watch the flow end to end

Only recording has been driven on Windows. Since then the whole
post-selection flow changed and none of it has run there:

- the overlay now **stays up** after a record selection is committed, so
  the region can be reframed, and closes when the backend starts;
- the bar has four states, dropdowns for delay and audio, and a countdown
  that goes *inside* the region;
- audio is a live control on Windows (`records_audio()` returns True
  there) and its menu has never been opened on a machine that can act on
  it -- this is the only platform where all three sources are selectable,
  and nothing has ever verified that choosing one changes what is
  recorded;
- stopping leaves the bar up for six seconds with a summary and Discard.

## The recording flow was fixed, and so was the recorder underneath it

The three flow complaints from 2026-08-27 are done, and fixing them meant
first finding out that **the GNOME backend had never worked at all**. It
was run against a live GNOME Shell 46 session for the first time on
2026-08-27 and had four independent faults, each alone fatal:

- **The interface moved.** GNOME 41 split Screencast out of the main shell
  object into a service of its own. Shell 46 answers only at
  `org.gnome.Shell.Screencast` `/org/gnome/Shell/Screencast` and returns
  "No such interface" at the old address, so `is_available()` was False on
  every modern GNOME and the only Wayland route was never even tried.
- **D-Bus error replies were read as success.** jeepney *returns* an error
  message rather than raising, and its body is a bare `(message,)` -- the
  same arity as a `Properties.Get` variant. Unpacking one as the other
  turned "No such interface" into `ValueError: too many values to unpack`.
- **Shell picks the container, so it picks the filename.** It appends
  `.webm` to any path not already named that way, and the app always asked
  for a `.mp4` temp file, so the `filename != path` check rejected every
  recording *after* Shell had already started one.
- **The recording dies with the connection that started it.** `stop()`
  deliberately opened a fresh one, on the stated belief that Shell keys the
  recording to the caller's unique name. It keys it to the *connection*:
  closing it froze the file at 4029 bytes with `duration=N/A`, and a later
  `StopScreencast()` answered `False` while the real recording ran on.

Two tests asserted the last two beliefs. Both are inverted now, with the
measurement in the comment.

The flow itself: committing a selection now **arms** a recording instead of
starting one. One pill carries it from armed to stopped and its label always
names what a click does -- "Start recording", "Cancel · 3", "Stop · 0:12" --
sitting top-centre of the monitor the recording is on, moving below the
recorded area only when the recording covers that strip. A 3s/5s/10s delay
became a visible count; "Off" starts immediately, which is safe because
placement now guarantees the pill is outside the recorded area.

**The file does land where the toast says.** Driven end to end through
`AppController` against real GNOME: the toast named
`…/Videos/Screenshot from 2026-08-27 09-33-58.webm` and the file was exactly
there, 4.024s of video for 4.05s of wall clock. No settling delay is needed;
Shell finalises before `StopScreencast()` returns.

## Still unwatched

**Windows.** Done -- see the section above. `start()`'s returned-path
contract is verified for real (`QMediaRecorder` honours the path it is
given), both recording paths land a playable file at the right speed, and
`dist\snipux.exe` is rebuilt from main. What remains unwatched here is the
pill placement question above and the overlay/annotation flow end to end;
only recording was driven.

**Wayland at all.** Still true, and not checkable from the machine this was
done on -- it has no Wayland socket. What *is* now known is that the GNOME
screencast route works, and that route is D-Bus, not session-dependent. The
overlay and capture path under a real Wayland session remain unwatched.

**Pasting into Nautilus or a chat app.** The mime data is now verified
correct *on the wire* -- read back off a live X11 clipboard with xclip --
but nobody has watched a paste actually land. Getting there found a real
bug: `x-special/gnome-copied-files` carried a raw space and raw UTF-8
because `QUrl.toString()` returns the pretty, decoded form, not the escaped
one. `text/uri-list` on the same `QMimeData` had it right, so the two
flavours named one file two different ways. Not a corner case either: the
default filename pattern always contains spaces.

**macOS**, entirely. The seam exists so it slots in without rework; it needs
a real Mac for the Screen Recording and Accessibility permissions.

## Fractional display scaling: the code was fine, the suite wasn't

This has now been run. Under `QT_SCALE_FACTOR=1.5`, 26 tests failed and the
painting they check was provably correct -- at 1.5x the magnifier's marker
is absent at the logical coordinate and exactly present at the physical one.
`QWidget.grab()` returns a pixmap at the device pixel ratio, and every pixel
assertion in `test_overlay.py` was written in logical coordinates and read
straight off it, which pinned the whole file to 1.0. They go through a
`pixel()` helper now.

1.25 and 2.0 still leave one or two failures, all of them exact-row sampling
against a stroke that antialiases across a fractional pixel boundary. That
is inherent to sampling a 1px line at 1.25x, not a defect -- but it does
mean **1.5 is the factor to keep green**, and a real machine at 1.25 has
never been tried.

## Decisions -- deliberate, do not revert

**No ffmpeg *binary*, and that does not block editing.** *(Corrected
2026-08-28 -- the earlier form of this decision said trimming and format
conversion were deferred because they needed an external ffmpeg. They do
not, and the correction was the owner's: "i thought we could do it cause
pyqt bundles ffmpeg itself".)*

Bundling a real `ffmpeg.exe` measured 121MB and was **blocked outright by
Smart App Control**, so it stays unbundled. But the wheel ships
`libavcodec`/`libavformat`/`libavutil` and the FFmpeg media plugin, and Qt
exposes the *encoder* through its own API -- so trimming needs no binary.

Measured, not assumed: `QMediaPlayer` -> `QVideoSink` decodes, and
`QVideoFrameInput` -> `QMediaCaptureSession` -> `QMediaRecorder` encodes,
which is the identical pipeline `WindowsRecorderBackend._start_region`
already uses for a cropped screen recording. Trimming 1.0s-2.5s out of a
3.54s WebM produced a 1.521s H.264 MP4 at 900x400, `Error.NoError`. It
needs nothing bundled, so Smart App Control has nothing to object to on
Windows either.

`QMediaFormat` will encode to MPEG4/Matroska/QuickTime/AVI with
H264/H265/MPEG4/MotionJPEG here. **GIF is not in that list**, so the
player handoff's GIF export is the one row that still needs something
else; the other three do not.

**No Windows installer**, same reason -- SAC refuses unsigned installers. The
portable exe is not blocked and installs itself on first run.

**Non-GNOME Linux cannot record.** `ffmpeg -f x11grab` was that route, and
`QScreenCapture` does not work on Wayland. Acceptable while GNOME is the target.

**GNOME writes WebM, and that is now what lands.** The recorder does not get
to choose its container, so `RecordingBackend.start()` returns the path
actually written and the landed file takes its extension from the file that
exists. Do not put `extension="mp4"` back.

**The GNOME connection is held from start to stop.** It is the recording's
lifeline, not tidiness. Do not "fix" it back to the open-call-close shape
`GnomeShellHelperBackend.capture()` uses -- that shape is right for a
one-shot call and fatal for a stateful one.

**30fps is the ceiling.** `QScreenCapture` delivers ~28.8 and exposes no rate
control. 60 needs a different capture API -- DXGI or `Windows.Graphics.Capture`
-- which is a backend swap, not a setting.

**Windows uses Ctrl+Alt+S.** Win+Shift+S belongs to the Windows Snipping Tool
and `RegisterHotKey` refuses it.

**Snipux is capitalised in display text, lowercase in anything typed.**

## The trap, restated -- it is worse than it looked

A green suite here is weak evidence, and this week it was wrong in *both*
directions. The recording session added the strongest case yet: **1,444 tests
passed while the primary Linux and Wayland recording path could not have
worked even once.** Every one of the four faults was invisible to a suite
that mocked the D-Bus connection.

Worse, the tests actively defended two of them. `stop()` opening a fresh
connection had a test named for it, with a comment explaining the wrong
belief. And both clipboard tests computed their expected value by calling
the same `QUrl.toString()` the implementation called -- comparing the
implementation to itself, so they would have passed whatever it did.

Earlier entries, still true:

- A ticket claimed 30fps, passed every criterion, and produced video that
  played at **double speed**. A number in a header is not the thing working.
- A branch was nearly condemned as broken when the fault was a **test harness**
  reusing one filename while a media player still held it open.
- 24 tests went red on main with no code change, because the suite read the
  **real user config**. SNX-126 fixed it. The nav-rail footer test was the
  last survivor of that class -- it asserted this machine's own version line
  was too wide for the rail, and an X11 session reports the short "x11", so
  it fit and the test failed with nothing wrong.

Habits that actually catch things here:

- **Measure the artifact, not the assertion.** Record for a known interval and
  check the file's duration.
- **Never compute a test's expected value with the call under test.** That is
  not a test, it is a restatement.
- **Read the thing back out of the real system.** xclip on the actual
  clipboard, ffprobe on the actual file, D-Bus introspection on the actual
  session -- each of those found a bug that mocks had hidden.
- **Do not seed state the app never sets**; start from what `app.py` builds.
- **For anything interaction-shaped, get a screen recording.** The chooser bug
  took four wrong guesses from reading code and two minutes from a video.

## Housekeeping

`punch.config.yaml` became `hopper.config.yaml` (one key, `profile: Snipux`).
The `hopper` on PATH at `C:\nvm4w\nodejs\hopper` is a **stale launcher** that
points at a `node_modules` with no `@cydo/hopper` in it; the working binary is
`~/.bun/bin/hopper`. Worth cleaning up, or every session trips on it. Windows
only -- there is no `hopper` on the Linux box at all.

Development on Linux needs a venv (`python -m venv .venv`, then
`pip install -r requirements.txt`); there is no system PyQt6.
