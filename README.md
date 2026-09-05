# Snipux

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
permission broker, and the tools that predate that constraint fight it. Snipux
treats it as the primary target rather than an afterthought.

## How it works

Capture the entire virtual desktop in a single shot, then run selection against
that frozen frame in our own overlay. The compositor is involved for exactly one
instant, which is what lets the same code path behave identically on X11 and
Wayland. Everything downstream — region select, annotation, export — is ordinary
drawing on an image already held in memory.

## Install

### Windows

Windows ships as a single standalone `snipux.exe` (SNX-96) — no Python, and
no terminal, required, and no installer to run first:

1. Download `snipux.exe` from the
   [Releases page](https://github.com/CydoEntis/snipux/releases) (newest
   release, under Assets).
2. Run it. The first run sets itself up — it relocates itself into your own
   user folder, adds a Start Menu shortcut and a Startup entry, and
   registers the Ctrl+Alt+S shortcut — the same as `snipux --setup` does
   for the pipx route below. No administrator prompt.

**Windows will interrupt that with a SmartScreen warning first**, and it's
worth knowing exactly what you'll see so it doesn't look like the download
failed: a blue box reading **"Windows protected your PC"**, with smaller
text underneath naming "an unrecognized app". This is Microsoft Defender
SmartScreen's standard reaction to *any* downloaded executable without a
paid code-signing certificate — it is not a virus warning and not a sign
the download is broken. Click **More info**, then **Run anyway**. (See
[Why the exe isn't signed](docs/releasing.md#why-the-exe-isnt-signed) for
the reasoning behind that certificate not existing.)

**There is deliberately no installer.** An earlier version of Snipux
shipped one (built with Inno Setup), and it ran straight into Smart App
Control — a stricter, opt-in Windows 11 feature that is nonetheless on by
default on a meaningful share of clean Windows 11 installs. Smart App
Control blocked that installer outright: the message read like the file
was corrupt or unsafe, not like a policy decision, and unlike SmartScreen
there was no "More info → Run anyway" to click through — so for those
users the installer did not just inconvenience, it did not work at all.
Signing would satisfy Smart App Control, but costs a few hundred dollars a
year and, since 2023, requires a hardware token (see
[docs/releasing.md](docs/releasing.md#why-the-exe-isnt-signed)); that has
been considered and declined. The portable exe above is not blocked by
Smart App Control, and since it installs itself on first run it delivers
what the installer was for without the thing that broke it — so don't
re-add one without re-reading this. If you ever do hit Smart App Control
blocking the exe itself, `pipx` (below) runs through the already-trusted
`python.exe` instead of a new unsigned executable.

Once running, Snipux's shortcut is **Ctrl+Alt+S**, not Windows'
Win+Shift+S — that combination already belongs to the Windows Snipping
Tool, and Windows won't hand the same combination to a second app. Change
it from tray → Settings…, the same as on Linux.

**Snipux has to actually be running for Ctrl+Alt+S to do anything.** Unlike
GNOME's shortcut on Linux, which the desktop itself owns, Windows' hotkey is
a registration the Snipux process holds only while it's alive. That's what
autostart is for: the first run's Startup-folder entry means Snipux is
already running by the time you'd want to press the shortcut, from the next
login onward.

Already have Python? `pipx` works identically on Windows — see below.

### Updating an existing install

Check which version is running first: **tray → Settings**, bottom-left of
the window, e.g. `Snipux 0.2.0 / Qt 6.11.0 · Windows`.

**If they run `snipux.exe`:** send them the new one and have them run it.
It copies itself over the old install at
`%LocalAppData%\snipux\snipux.exe`, so the shortcuts keep working and the
two versions never coexist.

> **Quit Snipux from the tray first.** A running Snipux holds that file
> open, the copy fails, and the update silently does not happen — the old
> version just keeps running. There is no error; the version line in
> Settings is the only way to tell.

**If they installed with pipx:** `--force` is required, not optional —
without it pipx sees the same package name and does nothing.

```powershell
pipx install --force C:\path\to\snipux-<version>-py3-none-any.whl
```

`snipux --setup` does **not** need re-running: the shortcut, Startup entry
and hotkey all point at a stable location that does not change between
versions.

**There is no update check.** Snipux never phones home and has no idea a
newer version exists, so someone has to tell each user. Adding one is not
a small change while this repository is private: GitHub's API answers 404
for a private repo without a token, and a token cannot be shipped inside a
binary that anyone can open.

### Sending it to someone else

The repository is private, so `pipx install git+https://…` only works for
people who have access to it. To put Snipux on someone else's Windows
machine without adding them as a collaborator — and without handing them an
executable that Smart App Control may refuse to run — send them a **wheel**:

```powershell
python -m pip install --upgrade build
python -m build --wheel
```

That writes `dist/snipux-<version>-py3-none-any.whl`. Send them that one
file. On their machine, with Python 3.10+ installed:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
pipx install C:\path\to\snipux-<version>-py3-none-any.whl
snipux --setup
```

`--setup` writes the Start Menu shortcut, the Startup entry and the
Ctrl+Alt+S binding, exactly as it does for a git install.

**Smart App Control does not block this route**, which is the whole reason
it exists alongside `snipux.exe`. The `snipux` launcher `pipx` puts on PATH
is generated on their machine by pip, from the `python.exe` they already
trust — nothing unsigned is ever downloaded and run, so there is no
unrecognised binary for Smart App Control or SmartScreen to object to. The
wheel is a zip of Python source and PNG/SVG assets; neither feature
inspects it.

The one thing they do need is Python, which the standalone `snipux.exe`
does not require. If they have it (or will install it from python.org or
the Microsoft Store), this is the smoothest path. If they don't and won't,
`snipux.exe` is the alternative — and if Smart App Control blocks *that*,
this section is the fallback.


### Linux

Snipux isn't published to PyPI — publishing there isn't planned. This is a
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

Either route gives Snipux and its dependencies their own isolated
environment and puts a `snipux` launcher on PATH. Then run:

```sh
snipux --setup
```

`--setup` writes the pieces `pipx install` can't: the `.desktop` entry, the
autostart entry, and the GNOME Super+Shift+S shortcut. Safe to re-run.

The same two `pipx install` commands work unchanged on Windows if you
already have Python — `--setup` there writes the Start Menu shortcut, the
Startup entry, and registers Ctrl+Alt+S instead, in place of the
`.desktop`/GNOME pieces above.

#### Using a different shortcut

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

## Using Snipux

Snipux runs resident in the background (with a tray icon, where one is
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

   If you often re-shoot the same area, click the **↻ toggle** right next
   to the mode control on that row. Region then opens with your previous
   snip's rectangle already framed, so a repeat capture needs no second
   drag — take it as-is, nudge an edge, or drag anywhere outside it to
   frame something else. It takes effect from the next snip, the rectangle
   is remembered between sessions, and it is trimmed or dropped if the
   monitor it came from is no longer there. Hover the toggle and the hint
   line under the row says which way it is set; to turn it off once a
   region is already framed, press `Esc` to bring the row back.
3. **Annotate in place**, directly on the frozen desktop — there's no
   separate editor window. The **pen is already armed** when the toolbar
   appears, so you can draw straight away; pick a different tool from the
   bar or its keyboard shortcut (below) at any point. You can keep reframing as you go:
   drag any edge or corner of the selection and the ink you've already
   drawn stays exactly where it was drawn, over the same pixels, instead of
   moving with the selection or getting clipped away.
4. **Copy or save.** Whichever way a snip ends, a tray notification
   confirms it — `Copied to clipboard`, or `Saved to snipux/<filename>`.
   That matters most for **Capture and finish**, which shows no overlay and
   no toolbar at all, so the notification is the only sign anything
   happened. A snip headed for the review window doesn't get one; the
   window itself is the confirmation.

   `Enter` (or the bar's Copy button) copies the
   annotated snip to the clipboard and closes the overlay. The bar's Save
   button writes it to disk and closes the overlay too. The bar's leading
   button is a split action: its face is whichever destination you chose
   before the snip, and its chevron offers the other two. Whatever you pick
   there sticks — the next snip opens on it, so the Settings default is a
   starting point rather than something you have to keep overriding.

### The review window

Copy and Save both dismiss the overlay immediately, so a snip saved to the
wrong place — or copied when you meant to save — means taking the capture
again. Turn on **Open each snip in a review window** in Settings and each
finished snip opens in a small window instead: the image, where it went,
and Copy / Save As… / Show in Folder.

Off by default. Press **Annotate** and it reveals the overlay's *own*
floating bar over the image — the same widget, the same tools, the same mark
model, so there is no second tool set to drift. The only differences are
that there's no capture-mode chip (nothing left to capture) and the bar's
trailing action is `Done`, since the footer already owns Copy and Save As.

Marks made here live in image coordinates rather than screen coordinates,
so they survive zooming and export exactly where they looked. Several snips
in a row leave several windows open.

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
GNOME Shell extension is installed and enabled. Without it, Snipux still
runs and still answers the shortcut — it just prints a notice to stdout on
startup and has no tray icon or Quit menu item, so quit it by killing the
process. Install the extension if you want the icon and menu back.

**Capture fails, or a permission prompt appears every time.** On Wayland,
capture goes through `xdg-desktop-portal`, which owns the permission
prompt itself. If a capture reports the request was cancelled, press the
shortcut again and approve the prompt when it appears. If it instead
reports the request failed outright, check that `xdg-desktop-portal` and a
desktop-appropriate portal backend (e.g. `xdg-desktop-portal-gnome`) are
installed and running — Snipux can't get pixels if the portal that owns
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
  pulls it in. Without it Snipux installs cleanly and then crashes on
  launch. `packaging/install.sh` checks for it before doing anything.
- Ubuntu 22.04+ (Wayland or X11); other desktops are expected to work but
  are not the primary target

## Licence

MIT
