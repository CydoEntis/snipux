# Next: get the recording branch onto Windows and Wayland

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-127** is merged. The recording work below is on
`fix/recording-flow`, five commits, **not yet merged**. The suite is green
on Linux for the first time: 1,464 passed, 0 failed, at both
`QT_SCALE_FACTOR=1` and `1.5`.

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

**Windows.** None of the flow work has run there. The arming pill, the
top-centre placement and the countdown are all ordinary Qt, but
`WindowsRecorderBackend.start()` now returns the path it wrote (it returns
its input unchanged, since `QMediaRecorder` honours it) and that contract
change has only been exercised by tests. A fresh `dist\snipux.exe` needs
building from this branch.

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

**No ffmpeg, and no editing in v1.** PyQt6 already ships FFmpeg inside the
wheel, which is why recording and playback need no external tool. Trimming
and format conversion would; they are deferred, and their research is kept
in `docs/design/recording.md` under a heading saying so. Bundling a real
`ffmpeg.exe` measured 121MB and was **blocked outright by Smart App Control**.

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
