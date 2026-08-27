# Next: try recording on a real screen

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-127** is merged. `main` runs 1,431 tests, 14 skipped.
**Zero open tickets.** Linux and Windows are both implemented; macOS is stubbed.

## Recording is in, and no human has watched it work

Backends, the chooser switch, commit-to-record, the HUD and file landing all
merged. A fresh `dist\snipux.exe` (48MB) is built from this commit.

    dist\snipux.exe

Then Ctrl+Alt+S, flip the chooser to **record**, drag a region, stop with the
same shortcut.

**Verified, and how:** a region records to a playable mp4 on Windows at ~30fps
with real-time playback -- 10 seconds recorded gives a 9893ms file, and five
runs in a row all produced files. The whole user path was driven end to end
through `AppController`: overlay opens, chooser switches to record, committing
a selection requests a recording with the right rect, delay and destination.

**Not verified:** any of it on screen. Nothing proves the HUD appears, that the
stop control is reachable, or that the file lands where the toast says. That is
the ten minutes this needs.

## Unverified on Linux, and uncheckable from Windows

**The GNOME recorder backend** (`org.gnome.Shell.Screencast`) has never run. It
is the only Wayland route and the primary target.

**Pasting a recording into Nautilus or a chat app** -- the
`x-special/gnome-copied-files` flavour was written to spec, never watched.

**Wayland at all**, since the platform seam went in.

## Decisions -- deliberate, do not revert

**No ffmpeg, and no editing in v1.** PyQt6 already ships FFmpeg inside the
wheel, which is why recording and playback need no external tool. Trimming and
format conversion would; they are deferred, and their research is kept in
`docs/design/recording.md` under a heading saying so. Bundling a real
`ffmpeg.exe` measured 121MB and was **blocked outright by Smart App Control**.

**No Windows installer**, same reason -- SAC refuses unsigned installers. The
portable exe is not blocked and installs itself on first run.

**Non-GNOME Linux cannot record.** `ffmpeg -f x11grab` was that route, and
`QScreenCapture` does not work on Wayland. Acceptable while GNOME is the target.

**30fps is the ceiling.** `QScreenCapture` delivers ~28.8 and exposes no rate
control. 60 needs a different capture API -- DXGI or `Windows.Graphics.Capture`
-- which is a backend swap, not a setting.

**Windows uses Ctrl+Alt+S.** Win+Shift+S belongs to the Windows Snipping Tool
and `RegisterHotKey` refuses it.

**Snipux is capitalised in display text, lowercase in anything typed.**

## The trap, restated -- it caught three more things this week

A green suite here is weak evidence, and lately it has been wrong in *both*
directions:

- A ticket claimed 30fps, passed every criterion, and produced video that
  played at **double speed**. The header said 30; the file was half the wall
  clock. A number in a header is not the thing working.
- A branch was nearly condemned as broken when the fault was a **test harness**
  reusing one filename while a media player still held it open. The recorder
  was fine.
- 24 tests went red on main with no code change, because the suite read the
  **real user config** and a stray script had written `kind: record` into it.
  SNX-126 fixed it: the suite now passes against a deliberately hostile config
  and leaves it untouched.

Habits that actually catch things here:

- **Measure the artifact, not the assertion.** Record for a known interval and
  check the file's duration.
- **Do not seed state the app never sets**; start from what `app.py` builds.
- **For anything interaction-shaped, get a screen recording.** The chooser bug
  took four wrong guesses from reading code and two minutes from a video.

## Never tested

**Fractional display scaling.** Multi-monitor is covered -- three displays
including one at negative coordinates, on real Windows hardware -- but nothing
here has ever run at a scale factor other than 1.0.

**macOS**, entirely. The seam exists so it slots in without rework; it needs a
real Mac for the Screen Recording and Accessibility permissions.

## Housekeeping

`punch.config.yaml` became `hopper.config.yaml` (one key, `profile: Snipux`).
The `hopper` on PATH at `C:\nvm4w\nodejs\hopper` is a **stale launcher** that
points at a `node_modules` with no `@cydo/hopper` in it; the working binary is
`~/.bun/bin/hopper`. Worth cleaning up, or every session trips on it.
