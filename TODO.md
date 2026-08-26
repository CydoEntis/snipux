# Next: a decision, then screen recording

Status lives in Linear, not here. This file holds what Linear cannot: the
shape of the plan, the decisions already made, and how to pick it up.

Everything through **SNX-115** is merged. `main` runs 1,241 tests, 14 skipped,
green on Windows. **Linux and Windows are both implemented.** macOS is stubbed.

## Done since you left

- **SNX-109** - the suite was red on both platforms. On Windows a test was
  doing a real `RegisterHotKey`, so it passed or failed depending on what else
  held Ctrl+Alt+S; two Snipux instances were running, and it failed. Stubbed,
  with no production code changed.
- **SNX-111** - the dead "Always copy to clipboard too" switch is now a real
  instant-saves-vs-copies preference, defaulting to copy so an upgrade does not
  silently start writing files.
- **SNX-110** - `reserved_top()` now returns the real `availableGeometry()`
  query on Wayland rather than a hardcoded zero, so a compositor that does
  reserve space is honoured.
- **Linear squared up.** SNX-108 closed; SNX-112-115 filed and closed for the
  four changes that shipped with no ticket. Filing those open would have let a
  run rebuild already-merged work.

**One correction worth knowing about.** SNX-110's comment claimed GNOME hiding
its top bar for a fullscreen window had been "watched happen on a real GNOME
Wayland session ... the first launch of this codebase on Wayland at all". It
had not - that ticket was built on Windows. Corrected in `839bf26`. The code
was right; only the claim was invented. **Wayland remains unverified.**

## Pick up here

**A rebuilt exe is waiting at `dist-new`.** It could not be written into
`dist` because two running Snipux processes hold that file. Close them, then
move it across.

**Then start recording.** The scope question is settled: **v1 records, it
does not edit, and there is no ffmpeg.** ffmpeg is 212MB on Windows and the
portable exe exists so one file can be handed over; recording never needed
it, only trimming and format conversion did.

Start with **ticket 0, the spike** in `docs/design/recording.md`: can Qt's
`QScreenCapture` + `QMediaRecorder` record a region of a real Windows
desktop to a playable file? Dropping ffmpeg removed the fallback behind it,
so that answer decides whether Windows is in v1 at all. Throwaway code, not
a ticket.

## The next feature: screen recording

Planned, not built, in **`docs/design/recording.md`** — now nine tickets
plus a spike, with the backend research already done so they can be written
without redoing it.

The two things to read first if you read nothing else: recording does *not*
break the one architectural rule (the frozen frame stays how you choose;
recording starts once you have chosen, so selection code is untouched), and
`org.gnome.Shell.Screencast` records a region to a file on GNOME under both
X11 and Wayland with no new dependency — which is the answer to what would
otherwise be the feature's biggest risk.

Decisions made in it: **no ffmpeg and therefore no editing in v1**, **copy
puts the file on the clipboard** the way the Windows Snipping Tool does —
`CF_HDROP` on Windows, `text/uri-list` on Linux, one Qt call for both — and
**v1 records no audio**.

Two consequences of dropping ffmpeg, written down rather than discovered:
Windows now rests entirely on Qt's recorder with no fallback, and non-GNOME
X11 can no longer record at all. The trim window's research stays in the
doc, marked deferred, so it is not redone when editing comes back.

## Left open by that branch

**The top bar fix is still unverified on Wayland**, and so is everything else
about Wayland since the platform seam went in. One launch settles it.

## Installing

    pipx install git+https://github.com/CydoEntis/snipux.git
    snipux

First launch sets itself up. `snipux --remove` undoes it before
`pipx uninstall snipux`.

**Windows without Python:** build `dist\snipux.exe` with
`packaging\windows\build.ps1` and hand over that one file. It installs itself
on first run. **The prebuilt exe in `dist/` predates SNX-108** — rebuild it from
`punch/SNX-108`.

## Decisions — deliberate, do not revert

**No Windows installer.** Smart App Control blocks unsigned installers
outright on default Windows 11 — a refusal, not a warning that can be clicked
through. The portable exe is not blocked. Signing is ~$200-400/yr plus a
hardware token, and was declined. Do not rebuild the Inno Setup script without
knowing this.

**Capture never uses `QScreen.grabWindow(0)` on Linux** — black on Wayland.

**Minimum selection is 16x16**, not the handoff's 200x140.

**Eleven tools, not the handoff's eight.** Ellipse, Line and Crop live in a
popover off the rect button.

**The hint HUD is off by default.**

**Windows uses Ctrl+Alt+S.** Win+Shift+S belongs to the Windows Snipping Tool
and `RegisterHotKey` refuses it outright.

**Snipux is capitalised in display text, lowercase in anything typed** — the
command, package, imports and repo stay `snipux`.

## The trap this project keeps falling into

A green suite here is weak evidence. All 1,242 tests run headless — no
compositor, no window manager, no keyboard. That suite was green while the app
shipped: a package that could not import, an overlay with no way to select,
invisible blur, a toolbar clipped to single letters, a terminal window on
every launch, an unreadable icon, and now a chooser whose buttons start a
capture.

Three habits that catch these:

- **Do not seed state the app never sets.** A check that calls
  `set_selection()` before testing drawing proves nothing about the path that
  creates a selection. Start from what `app.py` constructs.
- **Grep for "later ticket" after any big change.** Several real bugs were
  agents deferring work in a comment where no such ticket existed.
- **For anything interaction-shaped, get a screen recording.** SNX-108 took
  four wrong guesses from reading code and two minutes from a video.

## What has never been tested

**Fractional display scaling.** Multi-monitor is covered — three displays
including one at negative coordinates, on real Windows hardware — but no
display here has ever had a scale factor other than 1.0.

**macOS**, entirely. The seam exists so it slots in without rework; it needs a
real Mac for the Screen Recording and Accessibility permissions.

**Wayland, since the Windows port.** X11 has now been run from
`punch/SNX-108` and it took one launch to surface the top bar hiding the
chooser -- something the whole headless suite had nothing to say about.
Wayland is the primary session type and has not been run since the platform
seam went in.
