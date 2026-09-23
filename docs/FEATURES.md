# Features

The living list of what snipux does and what it might do next. It is a
planning document, not a promise. What *shipped* and when is in
`CHANGELOG.md`; the working plan and settled decisions are in `TODO.md`.

An idea becomes a GitHub issue -- and so something Tugboat can pick up --
only once its scope, acceptance criteria and how to verify it are clear.

## Statuses

- `done` -- shipped and verified
- `dev` -- merged to `dev`, not yet released to `main`
- `now` -- an open issue being worked
- `next` -- likely after the current batch
- `later` -- wanted, deliberately deferred
- `idea` -- worth discussing, not committed
- `declined` -- not doing it, with the reason

## Capture

- [x] `done` Region, window, full screen, last region
- [x] `done` Active window (A) and Full screen following the pointer across monitors
- [x] `done` Browser mode: the page area of the browser in front, and full-page scroll-and-stitch
- [x] `done` Delay before capture, chosen on the chooser row
- [x] `done` Hide sensitive: black out passwords and keys found by OCR (Windows)
- [x] `done` Nudge the selection with the arrow keys
- [x] `done` Open an image you already have (`snipux shot.png`, or drop it on the review window)
- [ ] `now` Find the page inside a browser window on Linux X11 (#81, needs a real desktop)
- [ ] `later` Scrolling capture beyond the browser -- the capture layer must not assume one frame per session

## Annotate

- [x] `done` Pen, highlighter (snaps to text), rectangle, ellipse, line, arrow, text, numbered steps
- [x] `done` Per-tool style: colour, fill, solid/dashed/dotted
- [x] `done` Blur, pixelate and solid redaction
- [x] `done` Watermark, text or image
- [x] `done` Watermark text colour, font, and an optional background box
- [x] `done` Undo/redo shared by overlay and review
- [x] `done` Callout: a box with a tail and words in it
- [x] `done` Spotlight: dim everything but one region
- [x] `done` Eyedropper: pick a colour off the frozen frame
- [ ] `now` Point a callout at something, then place its words (#105)
- [x] `done` Keep the eyedropper's loupe clear of the bar
- [x] `declined` Crop as a shape -- it only drew a dashed box; crop by reselecting

## After a snip

- [x] `done` Copy, Save, Open (review window), with the choice remembered
- [x] `done` Instant saves, filename patterns, save folder
- [x] `done` Copy text: OCR the selection to the clipboard (Windows)
- [x] `done` Pin a snip on top of the screen (not on Wayland)
- [x] `done` Recent captures in the tray menu
- [ ] `now` Read text on Linux with a system tesseract (#92)
- [ ] `now` Recent captures as thumbnails in a window (#107)
- [ ] `idea` The stills destination model -- open product question, see TODO.md

## Record

- [x] `done` Record a region, window or screen (GNOME Screencast on Linux, QtMultimedia on Windows)
- [x] `done` Player with trim, and export to MP4, WebM, GIF or a single frame (H.264 via a system `ffmpeg` when present)
- [x] `done` Frame rate, cursor and destination settings
- [x] `done` Land a recording as GIF without opening the player
- [x] `done` Pause and resume (Windows)
- [x] `done` Pause and resume on Linux, with a system ffmpeg (#93)
- [x] `done` Record the pointer, or not, from the chooser row as well as
      Settings (Linux; Windows' recorder has no cursor option)
- [x] `done` Microphone audio in recordings (Windows)
- [ ] `later` Desktop (system) sound on Windows -- Qt cannot capture it without a new dependency
- [x] `done` System sound and mic on Linux, with a system ffmpeg

## Desktop integration

- [x] `done` Tray app, start at login, one-command setup and removal
- [x] `done` Global shortcut (Ctrl+Alt+S), rebindable in Settings
- [x] `done` Fast shortcut handoff to the running instance
- [x] `done` Standalone Windows exe that installs itself on first run
- [x] `done` `snipux --update`
- [x] `done` Crash log instead of a silent exit
- [x] `done` Windows installer, shipped beside the portable exe rather than
      instead of it (docs/releasing.md)
- [x] `done` `winget install snipux`
- [x] `done` A `.deb` and an AppImage, built and attached for every release
- [x] `done` "Check for updates" in the tray -- asked for, never automatic
- [x] `declined` Code signing -- recurring cost out of proportion for a free tool, for now (docs/releasing.md)

## Platforms

- [x] `done` Linux: Ubuntu 22.04+, GNOME, Wayland and X11
- [x] `done` Windows 10 2004+ / 11
- [ ] `later` macOS -- out of scope for 1.0; the seam exists, and a port needs a real Mac for Screen Recording and Accessibility permissions
- [ ] `later` Verified on other Linux desktops (KDE, Sway, Hyprland)

## Ideas

Add new ideas in this form:

```markdown
- [ ] `idea` Short name -- the problem it solves; constraints or open questions
```

## Planning rules

- Keep this file about capabilities and decisions, not implementation steps.
- Implementation-ready work is a GitHub issue, not a checklist here.
- Don't pull a `later` item forward because it is convenient.
- Record an expensive-to-reverse decision in TODO.md's "Decisions" section
  (or a design note under `docs/design/`) before building on it.
- When something ships, tick it here and write the user-facing line in
  `CHANGELOG.md`.
