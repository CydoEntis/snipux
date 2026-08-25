# snipux

A Windows Snipping Tool workalike for Linux.

Snip an area, a window, a freehand shape, or the whole screen — then draw on it,
blur out the parts that shouldn't be shared, and copy or save it. The workflow
Windows gives you for free, on Ubuntu.

## Status

Early. Being built ticket by ticket; nothing here is usable yet.

## Why

Linux has capable screenshot tools, but the Ubuntu/GNOME/Wayland combination is
where most of them get awkward — Wayland deliberately forbids applications from
reading the screen whenever they like, so every capture has to go through a
permission broker, and the tools that predate that constraint fight it. snipux
treats it as the primary target rather than an afterthought.

## Design in one paragraph

Capture the entire virtual desktop in a single shot, then run selection against
that frozen frame in our own overlay. The compositor is involved for exactly one
instant, which is what lets the same code path behave identically on X11 and
Wayland. Everything downstream — region select, annotation, export — is ordinary
drawing on an image we already hold.

## Install

```sh
pipx install snipux
snipux --setup
```

`pipx install` puts snipux and its dependencies in their own isolated
environment and a launcher on PATH — no repository checkout needed. `snipux
--setup` then writes the `.desktop` entry, the autostart entry, and the GNOME
Super+Shift+S shortcut, the pieces a wheel can't install on its own. Both
commands are safe to re-run.

## Requirements

- Python 3.10+
- PyQt6
- Ubuntu 22.04+ (X11 or Wayland); other desktops are expected to work but are
  not the primary target

## Licence

MIT
