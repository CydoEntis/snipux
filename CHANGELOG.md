# Changelog

What changed in each release of Snipux, newest first. Written for people who
use it; the commit history has the detail.

## Unreleased

### Added

- **A Windows installer, and `winget install snipux`.** Download
  `snipux-setup-<version>.exe` from the Releases page: it installs without an
  admin prompt, shows up in Add/Remove Programs, and starts Snipux when it
  finishes. The portable `snipux.exe` is still there and still works -- it is
  what to use if Smart App Control refuses the installer, since both are
  unsigned. An old 0.1.0 install from the installer Snipux used to ship is
  removed automatically if you still have one.

- **Record the pointer, or don't -- from the row itself.** The chooser row's
  record side now carries the flag beside Delay, so it is decided where the
  recording is being set up rather than in Settings, where it was easy to
  miss. Settings keeps the switch and the two stay in step. On Windows both
  are greyed with the reason: its recorder has no cursor option at all, so
  the switch there never did anything.

- **"Check for updates" in the tray menu.** Snipux never checked before, so
  a new release went unnoticed unless you happened to visit the repository.
  It only ever asks when you pick it -- there is no check at startup, no
  timer, and no traffic of any kind otherwise -- and it tells you how to
  update the build you are actually running.

### Changed

- **One green, not five.** The accent, the "free to register" tick, the
  "Saved" tick, the filename preview and the app icon were five greens
  within a few percent of each other, which read as an imprecise palette
  rather than five meanings. They are all the accent now -- what separates
  them is weight, not hue -- and the icon matches the app it opens.

### Fixed

- **A recording that fails to start no longer leaves the red outline on
  screen.** It could sit there over a recording that was not running, with
  no way to dismiss it, until Snipux was killed.
- **Pause really pauses when a recording has audio on Windows.** The bar
  could show paused while the microphone kept recording.
- **A failed recording start cleans up after itself**, instead of leaving an
  unplayable part-file where your recording was meant to be.
- **Step numbers stay put.** Deleting step 2 left 1 and 3 alone on screen but
  the export renumbered them anyway.
- **Hovering windows in Window mode cannot freeze the pointer** if the X
  server stops answering.
- **A highlighter no longer changes the opacity of marks drawn after it.**
- **An annotation that fails to draw cannot corrupt the exported image.**

## 1.0.1 — 2026-09-22

### Added

- **A `.deb` and an AppImage on the Releases page.** Installing Snipux on
  Linux no longer means having Python, pipx or a virtual environment: the
  `.deb` installs with `sudo apt install ./snipux_<version>_amd64.deb`, and
  the AppImage runs from wherever you save it after a `chmod +x`. Both carry
  their own Python and Qt, so neither cares what the distribution ships.
  Both are built for every release alongside the wheel.

### Changed

- **One colour for the whole application.** The overlay's bars, menus and
  hint pills were a warm grey while Settings, review and the player were a
  cool one, so the two halves read as different applications. They are one
  family now. Nothing changed weight -- every surface kept the exact
  lightness it had, so the bars still sit over a screenshot the way they did.

### Fixed

- **Clicking a menu's own control closes it again.** Every dropdown --
  capture mode, recording delay, audio, destination, the player's speed and
  export menus -- reopened instead of closing when you clicked the control
  that opened it.
- **Menus stay on screen.** The recording delay menu opened downward from
  a bar low on the screen, so its rows ran off the bottom edge and under
  the taskbar. Every menu now flips to the other side when there is no
  room, and stays inside the screen's edges.
- **An instant snip shows no annotation toolbar.** It finishes the moment
  you let go, so the toolbar only ever appeared for the length of the drag
  and then disappeared; the capture region is what you see now.

- **A copy from the review window now survives Snipux closing.** On Wayland
  it went through Qt's clipboard alone, so quitting took the image with it.
- **"Copy file" in the player pastes into Nautilus.** It was missing the one
  clipboard flavour GNOME's file manager reads, and left spaces in the
  filename unencoded -- which every default filename has.
- **Settings no longer says "Everything saved" when it saved nothing.** If
  the config file cannot be written, the window stays open and says so
  instead of closing on a success message.
- **A snip that cannot be written says why.** A full disk or a read-only
  folder used to toast "Saved to ..." and list a file that was never there;
  the image is still handed to Open/Review so it isn't lost with the write.
- **Save uses the folder you chose in Settings.** The overlay and a pinned
  snip both wrote to `~/Pictures/snipux` whatever the setting said.
- **The player shows the recording's real frame rate**, and its arrow keys
  step one of that recording's frames -- a 60 fps clip was labelled 30 fps
  and stepped two frames at a time.
- **Paths read the same way throughout on Windows** -- one of them mixed
  `/` and `\` in a single line.
- **`snipux --update` tells you how to update the build you actually
  have.** Every standalone build was told to download `snipux.exe` — on
  Linux, a file that does not exist.
- **An AppImage binds its shortcut to a path that survives the run.** The
  desktop entry and the Ctrl+Alt+S shortcut named the temporary location the
  AppImage unpacks itself to, which is gone the moment it exits, so both
  would have stopped working the first time you closed it.

## 1.0.0 — 2026-09-21

The first release since 0.8.2. 0.9.0 was prepared but never published,
so everything it would have carried is here too.

### Added

- **Choose the tool a snip opens with.** A dropdown in Settings →
  Annotation lists every tool the toolbar can start on, plus "No tool" for
  a toolbar that arms nothing until you pick. It was always the pen.
- **A default ink colour.** Also in Settings → Annotation: one colour every
  tool that draws in a colour starts with, picked from the swatches or
  typed as a hex. "Each tool's own" keeps the colours they ship with.
- **Per-tool defaults.** Below that, pick any tool and set what it starts
  with -- its colour, size, line style, fill, blur strength or highlighter
  sweep, whichever it has. "Reset all tools" puts them back. A change made
  mid-snip still lasts only for that session.
- **Pick what happens to a recording right beside Record.** The recording
  bar now has a Copy / Save / Open / GIF menu next to the Record button.
  It shows what Stop will do, and changing it applies to this recording
  only, without changing your default.
- **Pause and sound when recording on Linux.** With ffmpeg installed, Pause
  works on Linux as it does on Windows -- the paused time is simply left
  out -- and the audio menu records what you hear or your microphone.
  Without ffmpeg both are greyed and say what they need.
- **Style your watermark.** Settings → Watermark now picks the text's
  colour (a preset or any colour you like), its font, and whether it sits
  on a dark box. Without the box the text gets a thin edge in the opposite
  shade so it still reads on light and dark snips, and a preview shows it
  on both before you save.
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

### Changed

- **Settings shows just the Snipux version** at the foot of its sidebar,
  without the Qt version and session type after it.
- **The recording bar sits under the region, like the screenshot
  toolbar,** and moves with it while you resize it, instead of staying at
  the top of the screen. If there is no room below the region, it goes to
  the top of the screen.
- **A tidier recording bar.** It matches the screenshot toolbar now. Audio
  is an icon with a small corner marker instead of a labelled dropdown (on
  Linux it stays greyed, with the reason as its tooltip). A delay you set
  shows its seconds on the bar. The desktop no longer shows through the bar
  behind its buttons.

### Fixed

- **"Remember my last tool instead" does something.** The switch in
  Settings → Annotation was saved and never read. With it on, a snip opens
  with the tool the last one ended on.
- **The recording bar's audio menu opens where you can see it.** It always
  opened upward, so with the bar at the top of the screen it opened
  off-screen. It now opens below when there is no room above.
- **The audio source can't be changed mid-recording any more.** The menu
  still opened while recording, but the choice only applied to the next
  recording.
- **Recording on Wayland no longer films Snipux's own chrome.** GNOME
  placed the recording bar and the red outline wherever it liked, which
  could be inside the area being recorded. On Wayland the bar now stays
  under the region until you press Record. While recording, nothing of
  Snipux's is on screen. Stop with your shortcut or the tray, whose Snip
  item reads "Stop recording" while a recording runs.
- **Enter copies your snip on Wayland.** It closed the snip instead: the
  close button had the keyboard, so Enter pressed it, and Space would have
  too. Every button over the snip now leaves the keys to it.
- **What you copy on Wayland stays on the clipboard** when the snip closes
  straight after.
- **A clearer message when GNOME refuses a screenshot on Wayland.** It now
  says to allow Snipux when GNOME asks, instead of suggesting the portal
  is not installed.
- **Snipux says why when it can't start.** If Snipux couldn't open the
  socket your shortcut talks to, for example because `$TMPDIR` pointed at a
  folder that doesn't exist, pressing the shortcut did nothing and said
  nothing. Now it shows a message naming the folder to check.
- **Keyboard shortcuts work straight away on two monitors under Wayland.**
  With more than one monitor, Enter, Esc and the tool keys did nothing
  until you clicked the snip first.
- **Settings saves again on Windows.** If Snipux could not claim its own
  shortcut when it started, Save refused with "Shortcut already in use"
  and nothing was kept -- not the watermark, not the hide list, nothing.
  Save now only checks a shortcut you have changed.
- **The eyedropper's magnifier no longer hides behind the toolbar.** It
  opens in whichever corner around the pointer is clear of the toolbar,
  its menus and the hint, and stays on the monitor you are pointing at.
- **The toolbar fades while a tool works right under it** -- the
  eyedropper reading next to it, or a stroke passing beneath it -- and
  comes back as soon as you move away or reach for it. It is never part
  of the snip either way.
- **The eyedropper looks like an eyedropper.** Its button showed a plain
  slanted bar, easy to mistake for the pen beside it; it is a pipette now.
- **Snipux's own icon in the Windows taskbar.** Its windows showed the
  Python logo there. Run `snipux --setup` once after updating so the Start
  Menu and Startup shortcuts match.

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
