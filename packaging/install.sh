#!/usr/bin/env bash
# Installs snipux for the current user: Python dependencies, the package
# itself, then the .desktop file so it shows up in GNOME's application list.
#
# User-local throughout (pip installs into the user site-packages implicitly
# used by `pip install .` outside a venv, and the .desktop file goes under
# ~/.local/share/applications) -- no sudo, nothing under /usr/share.

# Platform check first, before any other command runs: a failed step further
# down must not be what a non-Linux user sees, this must be the first thing
# that runs.
if [ "$(uname -s)" != "Linux" ]; then
    echo "error: snipux only installs on Linux (found: $(uname -s))" >&2
    exit 1
fi

set -e

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"

# `pip install .` alone resolves runtime dependencies from pyproject.toml's
# own [project] metadata -- installing requirements.txt first would also
# pull in pytest (a dev/test-only dependency there), leaking the test
# framework into every end-user install.
echo "Installing snipux and its runtime dependencies..."
pip install "$repo_root"

applications_dir="$HOME/.local/share/applications"
echo "Installing desktop entry into $applications_dir..."
mkdir -p "$applications_dir"
cp "$script_dir/snipux.desktop" "$applications_dir/snipux.desktop"

# `pip install .` outside a venv falls back to a user install, putting the
# entry point in ~/.local/bin -- and whether that directory is on PATH for
# a *graphical* GNOME session (as opposed to the login shell this script
# runs under) depends on how the display manager builds the session
# environment. Check rather than assume, so a broken PATH shows up here
# instead of as a silently dead tray icon / Print Screen binding later.
if command -v snipux >/dev/null 2>&1; then
    echo "Done. snipux is on PATH and listed in GNOME's application list."
else
    echo "Done. snipux is installed and listed in GNOME's application list,"
    echo "but it is not on PATH in this shell (checked: command -v snipux)."
    echo "The .desktop entry's Exec=snipux line and the Print Screen binding"
    echo "both need it resolvable in a graphical session too -- if it was"
    echo "installed to ~/.local/bin, make sure that directory is on PATH"
    echo "there, e.g. by logging out and back in."
fi
echo "Next: bind Print Screen to it -- see docs/print-screen-gnome.md."
