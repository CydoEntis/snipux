# Binding Super+Shift+S to snipux on GNOME

Binding a key is the desktop's job, not the application's — snipux never
grabs keys globally (see `docs/SPEC.md`, "Resident process and hotkey"). This
page is the entire mechanism: there is no code-side fallback, and once the
binding is set up GNOME runs `snipux --snip` for you.

`--snip` asks an *already-running* snipux instance to start a capture; it
does not start the resident process itself. Install snipux and make sure it
is running (e.g. via the desktop entry, or `snipux` from a terminal) before
you use the shortcut, otherwise the first press after login will report that
no instance is running instead of taking a snip.

## `packaging/install.sh` does this for you

As of the last step of install, `install.sh` runs exactly the mechanism
below itself: it appends a `custom-keybindings` slot named `snipux`, sets its
`command` to the installed launcher's *absolute* path plus `--snip` (not the
bare `snipux` name — `~/.local/bin` is not reliably on `PATH` in a graphical
GNOME session, which the script checks and warns about separately), and sets
its `binding` to `<Super><Shift>s`. It reads the existing
`custom-keybindings` list first and appends to it, so any shortcuts you
already had configured are kept. Re-running `install.sh` reuses the same
`snipux` slot rather than adding a second one.

If `gsettings` isn't available, or setting the shortcut otherwise fails,
`install.sh` says so and leaves the tool installed and usable — it does not
fail the install. Follow the manual steps below in that case.

**To undo the automatic binding**, remove the `snipux` slot from the list and
reset its keys:

```sh
# Drop '/org/.../custom-keybindings/snipux/' from the list, keeping any
# other entries -- e.g. if it's the only one:
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "[]"

gsettings reset org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ name
gsettings reset org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ command
gsettings reset org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ binding
```

The rest of this page describes the same mechanism by hand, for other
desktops or if you'd rather not have install.sh touch GNOME settings at all.

## Why Super+Shift+S

snipux exists to restore the Windows Snipping Tool workflow, and that
workflow's muscle memory is Windows key + Shift + S. On Linux the key
labelled with the Windows logo is called **Super**, so the GNOME binding
string for that combination is `<Super><Shift>s`. It also does not collide
with anything: GNOME itself binds Print for its own screenshot UI, so
Super+Shift+S stays out of its way entirely.

## 1. Install first

Run `packaging/install.sh` (or otherwise make sure `pip install .` has been
run) so that `snipux` is resolvable on `PATH`. The `command` set in step 3
below only works once this is done.

## 2. Read the existing custom-keybindings list

GNOME keeps custom shortcuts as a list of D-Bus object paths. Read the
current list first so a new one can be *appended* rather than overwriting
any custom shortcuts already set up:

```sh
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```

If nothing else has been bound yet, this prints `@as []`. Otherwise it
prints something like:

```
['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']
```

Append a new, unused slot path to whatever that list already contains and
write the whole list back. For an empty starting list:

```sh
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/']"
```

If the list already had entries, include them too — e.g. if step 1 printed
`['.../custom0/']`, write back
`"['.../custom0/', '.../snipux/']"`.

## 3. Set the new slot's name, command, and binding

```sh
gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ name 'snipux'

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ command 'snipux --snip'

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ binding '<Super><Shift>s'
```

`command` must be exactly `snipux --snip` — that is the flag `snipux/app.py`
parses to forward a capture request to the resident instance. `binding` is
`<Super><Shift>s`, GNOME's keysym string for Super (the key labelled with the
Windows logo) plus Shift plus S — the same combination Windows Snipping Tool
uses.

## 4. Try it

Press Super+Shift+S. If snipux is running, the screen freezes into the
selection overlay. If instead nothing happens, check that snipux is running
and that `snipux --snip` succeeds when run by hand from a terminal.

## Prefer Print Screen instead?

If you'd rather bind Print Screen — note GNOME also uses it for its own
screenshot UI, so binding it here means overriding that — set `binding` to
`'Print'` in step 3 instead of `'<Super><Shift>s'`. Everything else in this
page is unchanged.

## Note

This is deliberately not a global-hotkey library, nor anything snipux grabs
itself. GNOME owns the key binding; snipux only answers the request once
GNOME runs the command above. If you use another desktop, look for its
equivalent custom-shortcut mechanism and point it at the same
`snipux --snip` command.
