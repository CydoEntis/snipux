# Binding the Snipux shortcut on GNOME

Binding a key is the desktop's job, not the application's — Snipux never
grabs keys globally (see `docs/SPEC.md`, "Resident process and hotkey"). This
page is the entire mechanism: there is no code-side fallback, and once the
binding is set up GNOME runs `snipux --snip` for you.

`--snip` asks an *already-running* Snipux instance to start a capture, and
starts one itself first if nothing was running yet -- so the very first press
after login (or after a crash) still shows the capture overlay, rather than
depending on something else having started Snipux first. Either way, install
Snipux (this page assumes `packaging/install.sh` has already been run) so
`snipux --snip` is resolvable when GNOME runs the command below.

## `snipux --setup` does this for you

`packaging/install.sh` runs `snipux --setup` (SNX-73) right after installing
the package into its venv, and that command runs exactly the mechanism below
itself: it appends a `custom-keybindings` slot named `snipux`, sets its
`command` to the installed console script's *absolute* path plus `--snip`
(not the bare `snipux` name — `~/.local/bin` is not reliably on `PATH` in a
graphical GNOME session), and sets its `binding` to `<Control><Alt>s`. It
reads the existing `custom-keybindings` list first and appends to it, so any
shortcuts you already had configured are kept. Running `snipux --setup`
again (via a re-run of `install.sh`, or directly once Snipux is installed)
reuses the same `snipux` slot rather than adding a second one.

If `gsettings` isn't available, or setting the shortcut otherwise fails,
`--setup` says so and leaves the tool installed and usable — it does not
fail the install. Follow the manual steps below in that case.

`--setup` also writes its bundled `.desktop` template (shipped inside the
`snipux` package itself, so this works from a `pip`/`pipx` install with no
repository checkout present) into `~/.config/autostart/` (creating the
directory if needed) — the freedesktop-standard location GNOME (and every
other compliant desktop) reads at login to bring applications back
automatically, so the binding above still has something to talk to after a
reboot without you doing anything by hand. Running `--setup` again just
overwrites the same filename, so this never produces a second entry.

Finally, `install.sh` itself (not `--setup`, which only handles the desktop
entry, autostart entry, and shortcut) starts Snipux at the end of a
successful install, so the keybinding works immediately rather than only
after the next login. It skips this — printing that it could not start the
app, rather than failing the install — on a machine with no graphical
session (neither `DISPLAY` nor `WAYLAND_DISPLAY` set), and it leaves an
already-running instance alone rather than starting a second one or
nudging it into an unwanted capture.

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

## Why Control+Alt+S, and not Super+Shift+S

The obvious choice is the one Snipux is imitating: the Windows Snipping
Tool's Win+Shift+S, which on Linux would be `<Super><Shift>s`. Snipux used
that default, and no longer does.

**Windows will not give that combination to another application.** Win+Shift+S
belongs to the Windows Snipping Tool, and `RegisterHotKey` refuses it outright
rather than sharing it. So on Windows the shortcut had to be something else
regardless, and keeping Super+Shift+S on Linux would have meant Snipux
answering a different key on each platform — two things to document, two
things to remember, for no gain.

`Control+Alt+S` is what both platforms use instead: free on GNOME (which binds
Print for its own screenshot UI, not this) and accepted by `RegisterHotKey` on
Windows. It is the default, not a requirement — see "Using a different
shortcut" in the README, or the recorder in Settings, to change it.

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

gsettings set org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/snipux/ binding '<Control><Alt>s'
```

`command` must be exactly `snipux --snip` — that is the flag `snipux/app.py`
parses to forward a capture request to the resident instance. `binding` is
`<Control><Alt>s`, GNOME's keysym string for Control plus Alt plus S — see
"Why Control+Alt+S" above for why this and not the Snipping Tool's own
combination.

## 4. Try it

Press Control+Alt+S. The screen freezes into the selection overlay, whether
or not Snipux happened to be running already. If instead nothing happens,
check that `snipux --snip` succeeds when run by hand from a terminal.

## Prefer Print Screen instead?

If you'd rather bind Print Screen — note GNOME also uses it for its own
screenshot UI, so binding it here means overriding that — set `binding` to
`'Print'` in step 3 instead of `'<Control><Alt>s'`. Everything else in this
page is unchanged.

## Note

This is deliberately not a global-hotkey library, nor anything Snipux grabs
itself. GNOME owns the key binding; Snipux only answers the request once
GNOME runs the command above. If you use another desktop, look for its
equivalent custom-shortcut mechanism and point it at the same
`snipux --snip` command.
