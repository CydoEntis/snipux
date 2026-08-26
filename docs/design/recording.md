# Screen recording — the plan

Not built. This is the shape of it, sliced into tickets, with the research
already done so the tickets can be written without redoing it.

**What it does in v1:** a switch between screenshotting and recording;
region or full-screen recording; and then copy the file or save it. Two
things the tool does, not two tools.

**What it deliberately does not do in v1:** trimming, and exporting to
mp4 / webm / gif. That is the whole reason there is no ffmpeg — see below.

## Decided

- **No ffmpeg, and therefore no editing in v1.** ffmpeg is 212MB on
  Windows, and the portable exe exists so one file can be handed to
  someone. Recording itself needs no ffmpeg on the routes that matter;
  only trimming and format conversion do. So v1 records and lands a file,
  and editing waits. This is a scope decision, not a technical one — the
  research below still stands if it is revisited.
- **Copy puts the file on the clipboard**, the way the Windows Snipping
  Tool does it — see "The clipboard" below, because the mechanism is worth
  knowing before ticket 6.
- **No audio in v1.** The UI should say so rather than leave people to
  discover it.

## Why this is cheap to build

The rule in CLAUDE.md — grab the whole desktop in one shot, run selection
against that frozen frame — looks like it rules recording out. It doesn't,
and the reason is what keeps the diff small:

> **The frozen frame is still how you choose. Recording starts once you
> have chosen.**

Freeze a still exactly as now, run the whole existing overlay against it —
same chooser, same drag, same handles, same rect — and when the selection
is committed, close the overlay and start a recorder on those coordinates.
The compositor is involved for one instant to freeze and again to record,
but never while the user is dragging, which is the part that would
otherwise need writing four times for four platforms.

So this adds **no lines to the selection code**. `_commit_selection()` —
the funnel added for instant capture — is already the one place a selection
stops being provisional and something happens to it. Recording is a fourth
thing that can happen there, next to copy, save and review.

## What it rides on that already exists

- **`_commit_selection()`** — one place, four capture modes, already the
  hook instant capture uses.
- **`capture.py`'s backend pattern** — `CaptureBackend` / `BackendRegistry`:
  try in order, collect every failure, report them together, never let one
  failure stop the next. Recording wants the same shape and should not
  invent a second one.
- **The platform seam** — `build_capture_registry()` already decides which
  backends an OS may even try. Recording gets the twin.
- **`review.py`** — a window that takes a finished artifact and offers what
  to do with it. The trim window is that role again with a different verb.
- **jeepney and two working D-Bus backends** — `PortalScreenshotBackend`
  and `GnomeShellHelperBackend`. The recorder's best Linux route is D-Bus
  too, and the handshake code has precedent to copy.
- **The chooser's three axes** — mode, "then", delay — and the
  Instant / Edit / Review vocabulary this branch just rebuilt.

## The backends — measured, not guessed

Checked on the dev box (Ubuntu GNOME, X11, PyQt6 6.11):

```
org.gnome.Shell.Screencast          ScreencastSupported = true
  .ScreencastArea   (iiiisa{sv}) -> (bs)     x, y, w, h, filename, options
  .Screencast       (sa{sv})     -> (bs)     whole screen
  .StopScreencast   ()           -> (b)
org.freedesktop.portal.ScreenCast   present
ffmpeg                              6.1.1 on PATH
PyQt6.QtMultimedia                  imports from the stock wheel
PyQt6.QtMultimediaWidgets           imports from the stock wheel
```

**`org.gnome.Shell.Screencast` is the find here.** It records a region
straight to a file, it works on GNOME under both X11 and Wayland, and it
needs no new dependency — jeepney is already a dependency and
`GnomeShellHelperBackend` is already talking to `org.gnome.Shell` next
door. It answers the question that would otherwise be this feature's
biggest risk: *how do you record on Wayland at all*.

The rest, in the order a registry should try them:

| Session | Backend | Notes |
| --- | --- | --- |
| GNOME (X11 + Wayland) | `org.gnome.Shell.Screencast` | one D-Bus call, no new dependency, primary session type |
| X11, any desktop | `ffmpeg -f x11grab` | external tool, same category as `maim`/`grim`/`wmctrl` |
| Windows | `ffmpeg -f gdigrab`, or Qt's `QScreenCapture` + `QMediaRecorder` | Qt route avoids the external tool entirely |
| Wayland, not GNOME | `org.freedesktop.portal.ScreenCast` | hands back a **PipeWire node**, and reading one needs something that speaks PipeWire — see below |

**`QScreenCapture` is not a Wayland answer.** Qt does not support screen
capture there, so the Qt route is Windows and X11 only.

## Why there is no ffmpeg, and what that costs

Recording does not need it. `org.gnome.Shell.Screencast` records a region
straight to a file on GNOME under both X11 and Wayland, and Qt's own
recorder should do the same on Windows. Playback needs nothing either —
`QMediaPlayer` is already in PyQt6.

Only editing needs it: trimming, mp4/webm transcoding, and GIF, which
wants a two-pass `palettegen`/`paletteuse` or it looks terrible. There is
no version of *editing* where ffmpeg is optional, which is exactly why
dropping editing drops ffmpeg.

**What this costs, stated plainly:**

- **No trimming.** You record, and you keep what you recorded. Cutting the
  first few seconds off is usually the first thing anyone wants, and v1
  cannot do it.
- **No format choice.** You get whatever the backend produces — webm from
  GNOME, whatever Qt writes on Windows.
- **No GIF.**
- **Non-GNOME X11 cannot record at all.** `ffmpeg -f x11grab` was that
  route, and `QScreenCapture` does not work on Wayland. GNOME is the
  primary target, so this is acceptable — but it is a real gap, not a
  rounding error.

**The one thing this hangs on:** Windows recording now depends entirely on
Qt's `QScreenCapture` + `QMediaRecorder`, with no ffmpeg fallback behind
it. That route is unproven. Spike it before writing any other ticket — if
it does not work, v1 records on GNOME only, and that is worth knowing
before eight tickets are built on the assumption it does.

## Where it shows up in the UI

**The switch.** A new axis on a chooser panel that already carries mode,
"then" and delay. The two sides are not symmetric, so a switch (rather than
folding "Record region" into the mode list) is the recommendation — the
mode list and the "then" list both change meaning when it flips.

**The mode list narrows.** Region and Full screen only. Window recording is
possible on GNOME (`Screencast` vs `ScreencastArea`) but wasn't asked for;
Freeform recording is close to meaningless — video is rectangular. Grey
them out rather than hiding them, and let the hint say why.

**"Then" changes.** There is no annotate-in-place for a video, so `edit`
has no meaning on the recording side. Recording's two in v1 are **Instant**
and **Save**. `Trim` is where the third would go when editing lands.

**The clipboard: copy the file, not the frames.** This is what the Windows
Snipping Tool actually does with a recording, and it is why its result
pastes into Explorer, Teams and Word — the clipboard carries a reference to
the file, not video data.

Qt does the same thing through one API on both platforms:

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    QGuiApplication.clipboard().setMimeData(mime)

On Windows Qt maps that to `CF_HDROP`, the file-drop format every Windows
app understands as "a file was copied" — the same clipboard content
Explorer itself puts there on Ctrl+C. On Linux it becomes `text/uri-list`,
which Chrome, Slack and Discord accept as an attachment. GNOME's file
manager wants an extra `x-special/gnome-copied-files` flavour
(`copy\nfile:///path`) on the same `QMimeData` to paste as a file — cheap
to add, and worth adding.

Two consequences to be honest about, both of which are true of the Windows
Snipping Tool too: **the file has to exist first**, so copy means save-then-
copy and there is no such thing as an unsaved recording on the clipboard;
and an app that only takes bitmaps gets nothing. `app.copy_image_to_clipboard`
gets a sibling — `copy_file_to_clipboard(path)` — rather than growing a
branch.

**While recording is a whole new surface.** The overlay is gone by then —
it is showing a frozen picture of the thing being recorded. That leaves no
stop button, no elapsed time and no visible sign anything is happening. And
on a full-screen recording, any control drawn on screen is *in the
recording*. The workable answer is a tray-icon state plus the existing
global hotkey doubling as stop, with an optional floating pill placed
outside the recorded rect when there is room for one. This is its own
design question and its own ticket.

## The trim window — deferred, kept for when editing lands

**Not in v1.** Everything below needs ffmpeg and is recorded here so the
research is not redone. Same role `review.py` plays for stills.

- Preview with `QMediaPlayer` + `QVideoWidget` — no new dependency.
- A scrubber with in/out handles. Trim maths is ordinary and testable.
- **Lossless vs exact.** `-c copy` trims only on keyframe boundaries: it is
  instant but the cut lands where the nearest keyframe is. Re-encoding
  gives the exact frame and costs time. Pick one as the default and say
  which in the UI, rather than silently doing one of them.
- Export targets: **mp4** (h264), **webm** (vp9), **gif** (two-pass
  palette, plus an honest warning about size and frame rate).
- Where the raw recording lives before export — a temp file, who deletes
  it, and what happens to it if the app dies mid-recording.

## Settings this adds

Frame rate, quality/bitrate, draw-the-cursor or not, and where recordings
save. Ordinary `setup_desktop` config plus rows in the Settings pane,
following what's already there. Default export format waits for editing.

## Out of scope for v1

- **Editing** — decided. Trimming, mp4/webm/gif export and the trim window
  all wait, because all of them need ffmpeg and none of them is needed to
  record. The UI should not show a Trim destination it cannot honour.
- **Audio** — decided. Microphone and system audio mean device selection,
  sync and permissions, and roughly double the backend surface. v1 records
  silent and says so.
- **Non-GNOME X11 and non-GNOME Wayland** — no route without ffmpeg or a
  PipeWire dependency respectively.
- Window recording and freeform recording.
- Pause/resume mid-recording.
- Annotating a video.

## What a green suite will and will not prove

The trap section in TODO.md applies double here, because a recorder is
almost entirely the part the headless suite cannot see.

**Testable headless:** which backend gets chosen and why; the exact
arguments or D-Bus payload each backend builds; the recorder state machine
(idle → recording → stopped → trimming); trim arithmetic; the export
command built for each format; filename patterns; settings persistence.

**Not testable at all:** that any of it produces a file that plays. That
needs a real session on GNOME/Wayland, GNOME/X11 and Windows.

Every ticket below that touches a backend should say what was watched
happen, on which session type, as its done-when. "The suite is green" is
not a done-when for any of them.

## The tickets

Nine, in dependency order, and a spike before any of them. 1-4 are
plumbing, 5-9 make it record and land a file. Editing's tickets are gone
with ffmpeg; the trim window and export are written up above for whoever
picks them back up.

**0. Spike: can Qt record on Windows?** Throwaway. `QScreenCapture` +
   `QMediaRecorder` against a region of a real desktop, to a file that
   plays. Not a ticket, not merged - an answer.
   *Decides:* whether Windows is in v1 at all. There is no ffmpeg fallback
   behind it any more, so if this fails, v1 is GNOME-only and that is a
   scope conversation, not a bug.

1. **`recording.py`: the recorder seam.** `RecordingBackend` ABC +
   `RecorderRegistry`, mirroring `capture.py`'s pattern exactly - try in
   order, collect failures, report together. No backend behind it yet.
   *Done when:* the registry picks and reports with no real recorder, the
   way `UnsupportedPlatformBackend` already does for capture.

2. **The GNOME Shell recorder backend.** `ScreencastArea` / `Screencast` /
   `StopScreencast` over jeepney, following `GnomeShellHelperBackend`.
   *Done when:* a region records to a file on GNOME/Wayland **and**
   GNOME/X11, both watched.

3. **The Windows recorder backend.** Qt's `QScreenCapture` +
   `QMediaRecorder`, on whatever the spike learned. Reports itself
   unavailable with a reason where Qt cannot do it - notably Wayland.
   *Done when:* a region records to a playable file on Windows.

4. **`Platform.build_recording_registry()`.** The seam decides which
   recorders an OS may try, same as capture. Linux and Windows real; macOS
   raises `UnimplementedPlatformError` naming itself.

5. **The stills/record switch in the chooser.** UI and state only, nothing
   wired to a recorder yet. Includes narrowing the mode list and swapping
   the "then" list per side. The "then" list is Instant and Save; do not
   offer Trim.

6. **Recording's own "then" vocabulary.** Instant / Save, plus
   `copy_file_to_clipboard(path)` next to the existing image one.
   *Done when:* a recording pastes as a file into Nautilus and as an
   attachment into a chat app on Linux, and into Explorer on Windows -
   Qt's `CF_HDROP` mapping is the part to actually watch happen rather
   than assume.

7. **Commit -> record.** `_commit_selection` starts the recorder for the
   committed rect and closes the overlay. The delay setting should apply
   here too - a countdown before recording starts is more useful than
   before a still.

8. **The recording HUD.** Stop control, elapsed time, the hotkey doubling
   as stop, tray-icon state. Includes the "the control is inside the
   recording" problem for full-screen.

9. **Landing the file.** Save folder, filename pattern, the toast, and
   cleaning up the temp file on discard or crash. Plus the fps, cursor and
   folder settings rows.

**Dropped with ffmpeg**, and what they were: the ffmpeg `x11grab` backend
(non-GNOME X11 recording), the trim window, and export to mp4/webm/gif.
**Never scheduled:** non-GNOME Wayland, which needs portal ScreenCast plus
a real PipeWire dependency and is worth nothing until everything above
works.

## Where this will hurt

- **Windows rests on one unproven route.** Dropping ffmpeg removed the
  fallback behind Qt's recorder. Ticket 0 exists because of that, and it
  is the only thing in this plan that can invalidate the rest of it.
- **Non-GNOME Linux cannot record.** X11 without GNOME lost its only
  route with ffmpeg; non-GNOME Wayland never had one. Acceptable while
  GNOME is the target, and a real gap if that changes.
- **The control surface is inside the recording** on full-screen. There is
  no clean answer, only trade-offs.
- **Disk.** A long full-screen recording is large, it is being written
  while the app is otherwise idle, and nothing in this codebase has ever
  had to think about running out of space. Worse without trimming: what
  you record is what you keep.
- **The missing trim will be felt immediately.** Cutting the start off a
  clip is the first thing anyone reaches for. v1 not having it is a
  deliberate trade for not shipping 212MB, and it should be revisited once
  recording actually works rather than treated as settled forever.
- **A green suite proves very little here** — more than anywhere else in
  this project, which is already a project where that is the warning at the
  bottom of TODO.md.
