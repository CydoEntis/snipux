# Changelog

What changed in each release of Snipux, newest first. Written for people who
use it; the commit history has the detail.

## Unreleased

### Added

- **Open an image you already have.** `snipux path/to/shot.png` opens it in
  the review window with the full toolbar, instead of re-snipping it off
  your own screen at whatever size it happens to be displayed. Dropping an
  image file onto an open review window opens it there too, after
  confirming if the window has unsaved edits. Save writes back to the file
  it came from, in its own format, where Qt can write that format; where it
  cannot, Save opens Save As instead.
- **Copy text**, on the floating bar after a selection (Windows only for
  now). Reads the words out of the selection and puts them on the
  clipboard as plain text instead of a picture -- for text you cannot
  select yourself: a VM, a remote desktop, an error dialog, a screenshot
  someone sent you. Greyed with why wherever it cannot run. Does not
  change what the next snip's Copy, Save or Open does.
- **Pin a snip on top of everything.** Choose Pin from the destination menu
  next to Copy/Save/Open and the selection becomes a small frameless window,
  always on top, opened exactly where you snipped it. Drag it by the image,
  resize it (it keeps its own proportions), and close it with Escape or the
  × that appears on hover. Right-click for Copy and Save. Greyed on Wayland,
  where an app can't place its own window or keep it on top.
- **Eyedropper.** Pick a colour off the frozen frame: hover to preview it
  magnified with its hex underneath, click to copy the hex to the clipboard.
  The tool stays active, so several colours can be taken in a row.
- **Recent captures in the tray menu.** The last few things you saved --
  stills and recordings both -- now show up under a Recent section, newest
  first, named by their filename. Click one to open it in whatever your
  system opens that kind of file with. A capture that only went to the
  clipboard adds nothing; a row whose file has moved, been renamed or been
  deleted quietly drops out.
- **Land a recording straight to GIF**, no trip through the player. GIF now
  sits alongside Copy/Save/Open on the record side, in the chooser and as a
  default in Settings' Recording pane. Greyed with why on a machine with no
  system ffmpeg to convert with.
- **Pause and resume a recording** (Windows). Pause sits beside Stop on the
  recording bar; the clock stops with it, and Resume carries on in the same
  file with the paused time left out. Greyed with why on Linux for now.
- **Record your microphone** (Windows). Pick Mic from the audio menu on the
  recording bar and the recording carries what it hears, paused and resumed
  with the picture. Desktop sound (System) is greyed for now, with why, as
  is Mic on a machine with no microphone. Each recording starts muted.
- **Nudge the selection with the arrow keys** — one logical pixel a press,
  ten with Shift, for framing a region precisely without the mouse.
  Alt+arrow resizes it instead, from the bottom-right corner. Both are in
  the shortcuts overlay (`?`).
- **Callout**, a new shape in the shapes menu: a box with a tail, pointing
  at wherever the drag began. Click into it and type -- the text wraps to
  fit the box rather than spilling out of it. Colour, fill, line style and
  size come from the same style popover every other shape uses, and the
  whole thing -- box, tail and words -- undoes and erases as one mark.
- **Spotlight**, a fourth sibling on the redaction tool (B cycles to it
  after Blackout). Drag a rectangle and everything outside it dims instead
  of everything inside it being replaced -- the inverse of a redaction, for
  pointing at one part of a snip rather than hiding one. Its strength
  slider sets how strong the dim is. Several spotlights on one snip all
  stay lit together.

## 0.8.2 — 2026-09-15

### Fixed

- **The toolbar follows the region onto the monitor it is on.** A region
  drawn across the whole of another monitor, begun a few pixels the wrong
  side of the bezel, left its controls behind on the monitor the drag
  started on. The start still wins while a fair share of the region is on
  it, so a drag that spills a little past the bezel keeps its toolbar where
  it began.

## 0.8.1 — 2026-09-15

### Fixed

- **Highlights on tightly packed lines no longer overlap each other.** On a
  terminal or a dense editor, where a line's type is taller than the space
  between lines, each band now stops halfway to the line above and below.

## 0.8.0 — 2026-09-15

The highlighter snaps to the text you sweep, and recording is always chosen
on purpose.

### Added

- **The highlighter snaps to text.** Sweep roughly over a line and it becomes
  a clean band over the words you covered. Across several lines, swipe each
  one or drag diagonally from the first word to the last, the way you would
  select text. Switch it back to freehand from the highlighter's style
  button.

### Changed

- **The highlighter is yellow.** It started out orange.
- **Snipping and recording: a camcorder icon, and a fresh start every time.**
  The record side of the capture row now shows a camcorder rather than a dot,
  and every snip opens on stills. Recording is chosen for the snip you are
  taking, so the one after a recording can no longer start filming by
  accident.

## 0.7.2 — 2026-09-15

The toolbar stays on the monitor you are snipping.

### Changed

- **The toolbar stays on the monitor you are snipping.** When a selection
  filled most of a monitor, the toolbar jumped to the next monitor over. Now
  it sits along the bottom of your selection. Drag it anywhere, another monitor
  included, and it remembers the spot.

## 0.7.1 — 2026-09-15

The shapes and redaction menus open from a second click, Crop is gone, and
there are patch notes now.

### New

- **Patch notes.** What changed in each release is written up in
  CHANGELOG.md, back to 0.2.0.

### Changed

- **The shapes and redaction buttons open their menu on a second click.** The
  first click still picks the shape the button shows; click the same button
  again to choose a different one, and once more to close the menu. The small
  triangle in the corner still opens the menu too, and is easier to hit.

### Removed

- **Crop**, which only drew a dashed box and cropped nothing. To crop a snip,
  drag the handles on the selection; for a dashed box, draw a rectangle with a
  dashed line.

## 0.7.0 — 2026-09-15

A redesigned capture flow, and snips that open about four times faster.

### New

- **A one-row chooser.** The row across the top of a snip is slimmer: capture
  or record, the mode, where the capture goes, Hide sensitive and a delay. Once
  something is selected it folds into a small tab; click it or press Space to
  open it again.
- **Last region is a mode.** Pick it from the mode menu, or press Shift+R, to
  reuse the rectangle from your previous capture. The menu shows its size.
- **Active window** (A) captures the window you were using, with nothing to aim
  at.
- **Full screen on several monitors** highlights the monitor under the pointer;
  click the one you want.
- **A one-row toolbar.** Rectangle, ellipse, line, arrow and crop share one
  button, and blur, pixelate and blackout share another. R, O, L, A and B still
  pick them.
- **A style for each tool.** The dot on the toolbar opens colour, fill, solid,
  dashed or dotted lines, and size, and every tool remembers its own. 1–7 pick a
  colour, [ and ] change the size, and D changes the line style.
- **Filled rectangles and ellipses, and dashed or dotted lines** on any shape.
- **Watermark.** Stamp a line of text or an image in a corner of your captures.
  Set it up in Settings, then switch it on from the toolbar.
- **Frosted bars.** The chooser, the toolbar and their menus sit on a blur of
  the screenshot behind them.
- **Move the toolbar.** Drag it by its edges when it is in the way, and it
  remembers the spot. On a desk with several monitors, a snip that fills a
  monitor puts the toolbar on the next one, where it covers nothing.
- **Your hide list as rows.** The words, field names and patterns Hide sensitive
  looks for are edited one per row in Settings, with Edit as text for pasting a
  whole list.
- **A one-line installer** for Windows and Linux.

### Changed

- The redaction tool called Solid is now called Blackout.

### Faster

- **Snips open in about 110ms instead of about 475ms**, measured on Linux (X11,
  GNOME). The shortcut passes its request to the running Snipux without loading
  the whole app first, and a snip no longer waits for GNOME's window animation.

### Fixed

- Window mode could crash Snipux as soon as the pointer moved.
- The toolbar and its menus could sit under the dock or taskbar, where nothing
  on them could be clicked.
- Controls could open on a different monitor from the one you were working on.
- A delay picked on the chooser row did not delay the capture.
- On a display scaled above 100%, saved snips had thinner lines than you drew.
- A shape menu's first row could not be clicked while a tool's name hint was
  showing.
- On Windows, Snipux ran behind a console window, and closing that window closed
  Snipux. The Start Menu and Startup shortcuts now start it with no console.
- If something unexpected goes wrong, Snipux keeps running and writes the
  details to `~/.config/snipux/crash.log`.

### Removed

- Freeform (lasso) capture.

## 0.6.0 — 2026-09-15

The first release on PyPI.

### New

- **Install from PyPI** with `pip install snipux` (or `pipx install snipux` on
  Linux), and update with `snipux --update`.
- **Hide sensitive**, a switch on the capture row that blacks out passwords, API
  keys, card numbers, IDs and personal details before a screenshot leaves your
  screen, using the text recognition built into Windows. Add your own words,
  field names and patterns in Settings.
- **Solid**, a third way to obscure part of a snip beside blur and pixelate.
  Blur can be reversed; a solid fill cannot.
- Clicking the tray icon opens Settings.

### Changed

- A readable font when IBM Plex is not installed, a softer accent green, and
  scrollbars that match the rest of the app.

## 0.5.0 — 2026-09-05

### Changed

- **Browser** (B) captures just your browser's page — none of the tabs, address
  bar or bookmarks — from whichever browser you were last in, on whichever
  monitor it is on, every time.

### Removed

- Full-page capture. It failed on pages that change while they scroll — live
  charts, animated ads, video — and a mode that only sometimes works is worse
  than none.

## 0.4.5 — 2026-09-05

### Fixed

- A capture indicator could be left on screen after a full-page capture.

## 0.4.4 — 2026-09-05

### Fixed

- Full-page capture failed with "frames 0 and 1 do not overlap" on long pages,
  because it started before the page had finished scrolling to the top.
- On Windows, the browser sometimes would not come to the front for a
  full-page capture.

## 0.4.3 — 2026-09-05

### Changed

- Full-page capture keeps a green outline on the page while it scrolls, counts
  the screens it has taken, and says how it ended — including when it stops on
  an endless feed.

## 0.4.2 — 2026-09-05

### Fixed

- In Browser mode the Visible / Full page switch, the toolbar and the colour
  tray piled up on top of each other and over the browser's tab bar.

## 0.4.1 — 2026-09-05

### Fixed

- Marks drawn before a full-page capture were silently dropped. The drawing
  tools now step aside while Full page is chosen; annotate the result in the
  review window instead.
- With *Capture and finish*, choosing Browser captured straight away, before the
  Visible / Full page switch could be used.

### Changed

- The overlay says "Scrolling the page…" before it steps aside.

## 0.4.0 — 2026-09-05

### Changed

- **Tab and Full page are one Browser mode.** The page is outlined first, then a
  Visible / Full page switch decides how much of it to take. Visible never
  touches your browser.

## 0.3.0 — 2026-09-05

### New

- **Tab** captures the page area of the browser in front, without its tab strip,
  address bar or bookmarks bar.
- **Full page** scrolls the browser to the end and joins the page into one tall
  image, and stops with a message on an endless feed.
- Both are greyed out, with the reason, when there is no browser to capture.

### Fixed

- On a scaled Windows display, window outlines were measured in the wrong units.

## 0.2.1 — 2026-09-05

### Fixed

- The toolbar covered the selection when the selection was near the bottom of
  the screen. It now moves above the selection instead.

## 0.2.0 — 2026-09-04

The first tagged release, for Windows and Linux, installed with pip.

- Snip a region, a window, a whole monitor or a freeform shape from a frozen copy
  of your screen, and annotate it in place: pen, highlighter, rectangle,
  ellipse, line, arrow, numbered steps, text, blur, pixelate and crop.
- Copy or save straight away, or open the review window afterwards.
- Record the same kind of selection to video, and trim it in the built-in
  player.
- Last region, a chooser row that follows the pointer across monitors,
  destinations that are remembered, and a tray notification when a capture is
  taken.
- Ctrl+Alt+S from anywhere, with Start Menu and Startup entries on Windows and a
  desktop entry and shortcut on GNOME.
