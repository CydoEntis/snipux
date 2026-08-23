# Binding Print Screen to snipux on GNOME

Binding a key is the desktop's job, not the application's — snipux never
grabs keys globally (see `docs/SPEC.md`, "Resident process and hotkey"). This
page is the entire mechanism: there is no code-side fallback, and once the
binding below is set up GNOME runs `snipux --snip` for you.

`--snip` asks an *already-running* snipux instance to start a capture; it
does not start the resident process itself. Install snipux and make sure it
is running (e.g. via the desktop entry, or `snipux` from a terminal) before
you bind the key, otherwise the first press after login will report that no
instance is running instead of taking a snip.

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

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ binding 'Print'
```

`command` must be exactly `snipux --snip` — that is the flag `snipux/app.py`
parses to forward a capture request to the resident instance. `binding` is
`Print`, GNOME's keysym name for the Print Screen key.

## 4. Try it

Press Print Screen. If snipux is running, the screen freezes into the
selection overlay. If instead nothing happens, check that snipux is running
and that `snipux --snip` succeeds when run by hand from a terminal.

## Note

This is deliberately not a global-hotkey library, nor anything snipux grabs
itself. GNOME owns the key binding; snipux only answers the request once
GNOME runs the command above. If you use another desktop, look for its
equivalent custom-shortcut mechanism and point it at the same
`snipux --snip` command.
