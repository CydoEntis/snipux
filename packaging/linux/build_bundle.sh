#!/usr/bin/env bash
# Builds the PyInstaller bundle that both Linux artifacts package:
# `dist/snipux/`, a directory containing the snipux executable, Qt, and the
# design assets. Sourced (not run) by build_appimage.sh and build_deb.sh,
# which each turn that directory into their own format -- so a bug in the
# bundle is one bug, not two, and an AppImage and a .deb cut from the same
# commit contain byte-identical payloads.
#
# Run directly to build just the bundle, e.g. to test it without packaging:
#
#     ./packaging/linux/build_bundle.sh && ./dist/snipux/snipux --list-backends

set -e

if [ "$(uname -s)" != "Linux" ]; then
    echo "error: this builds the Linux bundle and must run on Linux (found: $(uname -s))" >&2
    echo "Build it in the Ubuntu VM, or let .github/workflows/release.yml do it." >&2
    exit 1
fi

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$_here/../.." && pwd)"

# PyInstaller bundles binaries for the machine it runs on, so the artifact's
# architecture is a fact about this build rather than a choice the packagers
# get to make. Named once here because the two of them spell it differently
# (dpkg says amd64, AppImage says x86_64): hardcoding either would let a
# bundle built on an arm64 machine ship labelled x86_64, which dpkg installs
# happily and which then fails to launch on the user's machine -- an
# install-time failure, exactly what the preflights below exist to prevent.
MACHINE_ARCH="$(uname -m)"
case "$MACHINE_ARCH" in
    x86_64)
        DEB_ARCH="amd64"
        APPIMAGE_ARCH="x86_64"
        ;;
    aarch64)
        DEB_ARCH="arm64"
        APPIMAGE_ARCH="aarch64"
        ;;
    *)
        echo "error: snipux has no packaging for $MACHINE_ARCH yet." >&2
        echo "Add it to the case in packaging/linux/build_bundle.sh." >&2
        exit 1
        ;;
esac

# __init__.py, not pyproject.toml: it is the version the running app reports,
# and read with a regex rather than by importing snipux, which would need
# PyQt6 present in whatever interpreter runs this script.
SNIPUX_VERSION="$(
    python3 - "$REPO_ROOT/snipux/__init__.py" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE)
if match is None:
    sys.exit("could not find __version__ in snipux/__init__.py")
print(match.group(1))
PY
)"

# Qt 6.5+ dlopen()s libxcb-cursor from its xcb platform plugin, and PyInstaller
# only bundles a library it can find on the machine doing the build. Missing
# here, the build still "succeeds" and produces an artifact that aborts on
# launch on every machine that installs it -- the same failure
# packaging/install.sh preflights for, moved to build time where it is one
# person's problem instead of every user's.
if ! ldconfig -p 2>/dev/null | grep -q 'libxcb-cursor\.so\.0'; then
    echo "error: libxcb-cursor.so.0 is not installed on this build machine." >&2
    echo "Qt's xcb platform plugin links it, and PyInstaller can only bundle" >&2
    echo "what it can find -- without it the artifact crashes on launch." >&2
    echo "Install it with: sudo apt install libxcb-cursor0" >&2
    exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "error: the python3 venv module is not available." >&2
    echo "Install it with: sudo apt install python3-venv" >&2
    exit 1
fi

# A venv of the build's own, for the same PEP 668 reason packaging/install.sh
# builds one: Ubuntu 23.04+ marks the system site-packages externally managed,
# and PyInstaller is a build-time dependency that has no business being
# installed there anyway.
BUILD_VENV="$REPO_ROOT/build/venv-linux"
echo "Creating the build environment in $BUILD_VENV..."
rm -rf "$BUILD_VENV"
python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --quiet --upgrade pip

# .[build-linux] rather than a bare `pip install pyinstaller`: PyInstaller
# has to import snipux and PyQt6 to analyse them, so the project and its
# runtime dependencies belong in this environment too, at the versions
# pyproject.toml pins.
echo "Installing snipux and PyInstaller (from this checkout)..."
"$BUILD_VENV/bin/python" -m pip install --quiet "$REPO_ROOT[build-linux]"

# --noconfirm: dist/snipux/ is rebuilt from scratch every run rather than
# asking, because a stale file left from a previous build is exactly the kind
# of thing that ships.
echo "Building dist/snipux/ with PyInstaller..."
(
    cd "$REPO_ROOT"
    "$BUILD_VENV/bin/pyinstaller" \
        --noconfirm \
        --distpath "$REPO_ROOT/dist" \
        --workpath "$REPO_ROOT/build/pyinstaller" \
        "$REPO_ROOT/packaging/linux/snipux.spec"
)

BUNDLE_DIR="$REPO_ROOT/dist/snipux"
if [ ! -x "$BUNDLE_DIR/snipux" ]; then
    echo "error: PyInstaller finished but $BUNDLE_DIR/snipux is missing." >&2
    exit 1
fi

echo "Built $BUNDLE_DIR (snipux $SNIPUX_VERSION)."
