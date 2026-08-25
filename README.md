# snipux

A Windows Snipping Tool workalike for Linux.

Snip an area, a window, a freehand shape, or the whole screen — then draw on it,
blur out the parts that shouldn't be shared, and copy or save it. The workflow
Windows gives you for free, on Ubuntu.

Targets **Ubuntu 22.04+ with GNOME**. Wayland is the primary target and X11
must also work — the session type is detected at runtime, never assumed.
Other desktops are expected to work but aren't what the tool is tested
against.

## Why

Linux has capable screenshot tools, but the Ubuntu/GNOME/Wayland combination is
where most of them get awkward — Wayland deliberately forbids applications from
reading the screen whenever they like, so every capture has to go through a
permission broker, and the tools that predate that constraint fight it. snipux
treats it as the primary target rather than an afterthought.

## How it works

Capture the entire virtual desktop in a single shot, then run selection against
that frozen frame in our own overlay. The compositor is involved for exactly one
instant, which is what lets the same code path behave identically on X11 and
Wayland. Everything downstream — region select, annotation, export — is ordinary
drawing on an image already held in memory.

## Install

snipux isn't published to PyPI — publishing there isn't planned. This is a
private repository, so install straight from it with
[pipx](https://pipx.pypa.io/):

**Over SSH**, if you already have an SSH key on your GitHub account:

```sh
pipx install git+ssh://git@github.com/CydoEntis/snipux.git
```

**Over HTTPS**, using the [GitHub CLI](https://cli.github.com/) to
authenticate instead:

```sh
gh auth login
gh auth setup-git
pipx install git+https://github.com/CydoEntis/snipux.git
```

`gh auth setup-git` registers `gh` as git's credential helper for
github.com, so the HTTPS clone `pipx` does under the hood authenticates
automatically. Don't paste a personal access token into the URL — the `gh`
credential helper is the supported way to reach a private repo over HTTPS.

Either route gives snipux and its dependencies their own isolated
environment and puts a `snipux` launcher on PATH. Then run:

```sh
snipux --setup
```

`--setup` writes the pieces `pipx install` can't: the `.desktop` entry, the
autostart entry, and the GNOME Super+Shift+S shortcut. Safe to re-run.

### Using a different shortcut

Super+Shift+S is a popular key combination, and whichever app claimed it
first wins — GNOME binds it without complaint and yours simply never fires.
Pick another:

```sh
snipux --setup --shortcut '<Super><Shift>x'
```

GNOME's accelerator syntax, not the `Super+Shift+X` form the docs render
for humans: modifiers in angle brackets, then the key —
`'<Super><Shift>x'`, `'<Alt>Print'`, `'Print'`, `'F9'`. Anything else is
rejected with an explanation rather than bound and silently ignored.

The choice is remembered in `~/.config/snipux/config.json`, so later
`snipux --setup` runs — including the one `packaging/install.sh` performs on
every install — keep it instead of reverting to the default. `snipux
--remove` deletes it along with everything else `--setup` wrote.

Or set it from **tray → Settings…**, which records the combination you press
rather than making you spell out the angle-bracket syntax, and warns you if
GNOME already uses it. That warning only sees GNOME's own shortcuts — an
application that grabs a key directly owns it just as effectively and can't
be detected, so "No GNOME shortcut uses this" is not a promise the key is
free.

## Using snipux

snipux runs resident in the background (with a tray icon, where one is
available) so the shortcut reaches an already-warm process instead of
paying startup cost on every snip.

1. **Press Super+Shift+S** (wired up by `snipux --setup`; see
   [Using a different shortcut](#using-a-different-shortcut) above, or
   [docs/super-shift-s-gnome.md](docs/super-shift-s-gnome.md) for other
   desktops). The whole virtual desktop freezes into a
   full-screen overlay — this single frozen frame is what selection and
   annotation both work against, which is why the flow is identical on
   Wayland and X11.
2. **Pick a mode and drag.** The mode chip on the floating bar switches
   between **Region** (drag any rectangle, the default), **Window** (hover
   highlights the window under the cursor, click accepts it), **Full
   screen** (the whole display), and **Freeform** (lasso an odd shape).
3. **Annotate in place**, directly on the frozen desktop — there's no
   separate editor window. Pick a tool from the floating bar or its
   keyboard shortcut (below) and draw. You can keep reframing as you go:
   drag any edge or corner of the selection and the ink you've already
   drawn stays exactly where it was drawn, over the same pixels, instead of
   moving with the selection or getting clipped away.
4. **Copy or save.** `Enter` (or the bar's Copy button) copies the
   annotated snip to the clipboard and closes the overlay. The bar's Save
   button writes it to disk and closes the overlay too.

## Tools and shortcuts

| Key | Tool | What it does |
|-----|------|--------------|
| `P` | Pen | Drag to draw freehand |
| `H` | Highlighter | Sweep over the line that matters |
| `A` | Arrow | Drag from tail to head |
| `R` | Rectangle | Drag to box something in. Its button opens a popover with three more shapes that share its colour/stroke tray: Ellipse, Line, and Crop |
| `S` | Step | Click to drop the next numbered marker |
| `T` | Text | Click, then type into the label |
| `B` | Blur | Drag over anything private to obscure it |
| `E` | Eraser | Click a mark to remove it |
| `Ctrl+Z` / `Ctrl+Shift+Z` | — | Undo / redo |
| `Enter` | — | Copy the annotated snip to the clipboard and close the overlay |
| `Esc` | — | First press discards all ink and leaves the overlay open; press again (once there's nothing left to discard) to close without capturing |
| `?` | — | Toggle the on-screen shortcut hint bar |

Colour and stroke width are chosen from the tray that appears once a
drawing tool is selected — the eraser has none. Tool shortcuts and `Enter`
are suppressed while a text label or a slider has keyboard focus; `Esc` and
undo/redo always work regardless.

## Where saves go

Save writes a timestamped PNG to `~/Pictures/snipux`, creating that
directory if it doesn't exist yet, named `Screenshot from
YYYY-MM-DD HH-MM-SS.png`. A toast in the overlay confirms the path each
time.

## Uninstall

Run `snipux --remove` **before** `pipx uninstall snipux`, so the desktop
entry, the autostart entry, the installed icons, and the GNOME
Super+Shift+S shortcut all go with it:

```sh
snipux --remove
pipx uninstall snipux
```

`pipx uninstall` only removes the installed package — it has no idea
`--setup` also wrote files outside it, so skipping `--remove` first leaves
an autostart entry pointing at a binary that no longer exists, a dead
keyboard shortcut, and a ghost entry in GNOME's application list. `--remove`
only ever splices its own custom-keybinding slot out of GNOME's list, so any
other shortcuts you've set up by hand are left alone. Safe to re-run, same
as `--setup`.

## Troubleshooting

**No tray icon.** Stock Ubuntu/GNOME ships no legacy tray icon support at
all unless the
[AppIndicator and KStatusNotifierItem Support](https://extensions.gnome.org/extension/615/appindicator-support/)
GNOME Shell extension is installed and enabled. Without it, snipux still
runs and still answers the shortcut — it just prints a notice to stdout on
startup and has no tray icon or Quit menu item, so quit it by killing the
process. Install the extension if you want the icon and menu back.

**Capture fails, or a permission prompt appears every time.** On Wayland,
capture goes through `xdg-desktop-portal`, which owns the permission
prompt itself. If a capture reports the request was cancelled, press the
shortcut again and approve the prompt when it appears. If it instead
reports the request failed outright, check that `xdg-desktop-portal` and a
desktop-appropriate portal backend (e.g. `xdg-desktop-portal-gnome`) are
installed and running — snipux can't get pixels if the portal that owns
them refuses, or isn't there at all. `snipux --list-backends` shows which
capture backends this session has available and why the others aren't.

## What hasn't been tested yet

Development so far has happened on Windows and in a single-monitor Ubuntu
VM. These are believed to work from the design but haven't been verified on
real hardware:

- Multiple monitors, including ones positioned above or left of the primary
- Fractional display scaling
- X11 sessions — Wayland is what's actually been exercised end to end

If one of these is where something goes wrong, that's a known gap, not a
mystery.

## Requirements

- Python 3.10+
- PyQt6, jeepney
- `libxcb-cursor0` (`sudo apt install libxcb-cursor0`) — Qt 6.5+ needs it to
  load its xcb platform plugin, and nothing else on a stock Ubuntu desktop
  pulls it in. Without it snipux installs cleanly and then crashes on
  launch. `packaging/install.sh` checks for it before doing anything.
- Ubuntu 22.04+ (Wayland or X11); other desktops are expected to work but
  are not the primary target

## Licence

MIT
