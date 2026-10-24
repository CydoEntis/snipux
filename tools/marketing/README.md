# Marketing shots

Screenshots and short demo clips of snipux for the website, rendered
headless from the real widgets over a made-up desktop. Re-run it whenever
the UI changes; nothing in the output carries a version number or a date.

Render the published set on Windows. Text is drawn in whatever fonts the
machine resolves, and some of what the shots show is Windows-only (Hide
sensitive's text recognition, the Windows shortcut note in Settings), so a
Linux run looks different.

Dev-only: `tools/` is outside the `snipux` package, so no wheel, installer
or bundle ships it.

## Running it

From the repository root:

```sh
python -m tools.marketing.shoot                 # every still
python -m tools.marketing.shoot hero settings   # just these
python -m tools.marketing.shoot --clips         # every still, then the clips
python -m tools.marketing.shoot --only-clips    # just the clips
python -m tools.marketing.shoot --out D:\site\media
```

Output goes to `build/marketing/`, which is ignored. The media belongs to
the website, not this repository: copy it from there.

Nothing to install beyond `requirements.txt`. The clips and the player shot
need a system `ffmpeg` on `PATH` (the same optional one snipux itself uses).
Without it the player shot is skipped and each clip is left as a PNG frame
sequence under `<out>/video/frames/<clip>/`, with a
message saying so. Stills take about half a minute, the clips about two
minutes.

It never touches the machine it runs on: Qt runs offscreen on its own
1600x1000 screen at 2x, snipux gets a throwaway home and config folder (so
every path a window shows is `~\...` and no username appears), Copy does not
reach the clipboard, and the shortcut check does not probe the hotkey a
running snipux holds. `SNIPUX_DEMO=1` is set, which takes the version out
of the Settings footer.

## What it makes

Every still is 3200x2000:

| File | Shows |
| --- | --- |
| `hero.png` | the overlay over a selection with a box, numbered steps, an arrow, a label, a highlight, and the email blacked out by Hide sensitive |
| `chooser.png` | the capture-mode menu open over the frozen screen |
| `record.png` | a region framed for recording, with the Record bar under it |
| `player.png` | the player on a real 12 s recording, trimmed, filmstrip and waveform drawn |
| `review.png` | the review window editing an annotated snip |
| `settings.png` | Settings, no version shown |
| `pin.png` | a pinned snip floating over the editor |
| `history.png` | the history panel with copies |
| `og-image.png` | 1200x630 link preview built from the hero |

In `<out>/video/`, each as `.mp4` (H.264, yuv420p, faststart), `.webm` (VP9) and
`.gif` (its own palette), 1600x1000 at 30 fps (the GIF 720 wide, 12 fps):

- `snip-and-annotate`: Ctrl+Alt+S, the screen freezes, drag a region, box
  (R), steps (S), arrow (A), Copy, and the system notification.
- `hide-sensitive`: one click on Hide sensitive, frame the chat, the email
  is blacked out.
- `record-and-trim`: switch to recording (set to open in the player, as
  Settings can), frame the editor, Record, the recording runs, Stop, set the
  trim in the player, Export.

## How it is built

- `stage.py`: the environment, set before Qt starts, and the stubs that keep
  a run off the real machine.
- `scene.py`: the desktop, painted with QPainter. It also reports where it
  drew things (`Scene.places`, `Scene.chat_lines`), which is what the
  shots aim marks at and what the fake OCR reads.
- `shots.py`: one function per still. Each builds the real window, drives
  it into the state the shot needs and returns its `grab()`. `compose()`
  paints popup menus and HUDs, which are windows of their own, over it.
- `reel.py`: frame-by-frame clips. A `Reel` grabs a frame, then paints the
  pointer, click ripples and keycaps, which no grab can see, and an `Encoder`
  pipes the frames to ffmpeg.
- `clips.py`: the three clips and the recording the player opens. Input goes
  in as real mouse and key events, so the overlay does what it would for a
  person.

Keep it honest: a shot or clip shows only what snipux really does. The
desktop, the pointer, the keys on screen and the OS notification balloon are
the only things painted in, and the only answers faked are the ones a real
Windows desktop would give: the windows on it (so Window, Active window and
Browser have something to take) and the text recognised in the chat. The
rest is snipux's own widgets.

## Adding a shot

1. Write a function in `shots.py` that takes the `Stage` and returns a
   `QImage`: build the widget, drive it through its own methods or through
   `clips.mouse`/`clips.key`, `settle()`, then `compose(widget)` (or
   `on_desktop(...)` for an ordinary window).
2. Add it to `STILLS`. It is written as `<name>.png`, and
   `tests/test_marketing.py` renders it on every test run.
3. Run `python -m tools.marketing.shoot <name>` and look at the result.
4. Anything that must aim at the desktop's content goes in `Scene.places`,
   not as hard-coded coordinates.

A clip is a function in `clips.py` added to `CLIPS`: make an `Encoder`, a
`Reel` whose `source` returns the screen as it is now, then `move`, `hold`,
`click` and `keys` through the story.
