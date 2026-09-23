#!/usr/bin/env bash
# Builds dist/Snipux-<version>-x86_64.AppImage: one file, no Python and no Qt
# needed on the machine that runs it, no install step and no root.
#
#     ./packaging/linux/build_appimage.sh
#
# Needs a Linux machine (the Ubuntu VM, or the release workflow's runner) --
# PyInstaller bundles the interpreter and libraries of the machine it runs
# on, so there is no cross-building this from Windows.

set -e

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Sets REPO_ROOT, SNIPUX_VERSION and BUNDLE_DIR, and does the platform and
# libxcb-cursor preflights -- see build_bundle.sh.
source "$_here/build_bundle.sh"

APPDIR="$REPO_ROOT/build/AppDir"
OUTPUT="$REPO_ROOT/dist/Snipux-$SNIPUX_VERSION-$APPIMAGE_ARCH.AppImage"

# appimagetool is fetched and then executed, so it is build input like any
# dependency: pinned to a tagged release and checked against a recorded
# digest rather than tracking `continuous`, which would mean two builds of
# the same snipux commit could be made by two different tools. The checksum
# is per architecture; adding one means recording its digest here.
APPIMAGETOOL_VERSION="1.9.1"
case "$APPIMAGE_ARCH" in
    x86_64)
        APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
        ;;
    *)
        echo "error: no recorded appimagetool checksum for $APPIMAGE_ARCH." >&2
        echo "Record one in packaging/linux/build_appimage.sh before building." >&2
        exit 1
        ;;
esac

echo "Assembling $APPDIR..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -a "$BUNDLE_DIR/." "$APPDIR/usr/bin/"

# AppRun is what the mounted image executes. `readlink -f` because $0 here is
# the mount point AppRun was invoked through, and the bundle beside it has to
# be found relative to the real path, not through whatever symlink chain the
# launcher used.
#
# $APPIMAGE is set by the AppImage runtime itself, not by this script, and
# setup_desktop.find_console_script() reads it to write a .desktop entry and
# GNOME shortcut that point at the .AppImage file the user keeps rather than
# at this run's throwaway /tmp mount.
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/snipux" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# appimagetool wants the .desktop file and the icon at the AppDir root, and
# refuses a desktop entry whose Exec names something it cannot find. The
# template's placeholder becomes a bare "snipux" here rather than an absolute
# path: inside the image the only snipux there is is ours, and an absolute
# /tmp/.mount_* path would be wrong by the time anything read it.
sed 's|^Exec=__SNIPUX_LAUNCHER__$|Exec=snipux|' \
    "$REPO_ROOT/snipux/snipux.desktop" > "$APPDIR/snipux.desktop"

# sed reports success when it matches nothing, so an unsubstituted template
# would otherwise ship as a launcher whose Exec line is the literal
# placeholder -- a build that "succeeded" and an app that cannot start.
if ! grep -q '^Exec=snipux$' "$APPDIR/snipux.desktop"; then
    echo "error: the Exec placeholder in snipux/snipux.desktop was not substituted." >&2
    exit 1
fi

# Icon=snipux in that entry is a *theme* name, so the sized PNGs go into a
# hicolor tree inside the image as well as the 256px copy appimagetool reads
# from the root -- a desktop that adopts the AppImage's icon looks for the
# theme path, and one that reads the embedded icon looks for the root file.
cp "$REPO_ROOT/snipux/design/logo/snipux-256.png" "$APPDIR/snipux.png"
for png in "$REPO_ROOT"/snipux/design/logo/snipux-*.png; do
    size="$(basename "$png" .png)"
    size="${size#snipux-}"
    mkdir -p "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps"
    cp "$png" "$APPDIR/usr/share/icons/hicolor/${size}x${size}/apps/snipux.png"
done

# appimagetool itself is an AppImage, fetched once into build/ rather than
# vendored: it is a build tool, not a dependency of snipux, and nothing about
# the artifact it writes depends on which build it was made with.
TOOL="$REPO_ROOT/build/appimagetool-$APPIMAGETOOL_VERSION-$APPIMAGE_ARCH.AppImage"
if [ ! -x "$TOOL" ]; then
    echo "Fetching appimagetool $APPIMAGETOOL_VERSION..."
    mkdir -p "$REPO_ROOT/build"
    curl -fsSL -o "$TOOL.part" \
        "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-$APPIMAGE_ARCH.AppImage"
    # Verified before it is made executable, and written under .part until
    # it passes, so a failed or tampered download cannot be left behind as a
    # runnable file that the next build would trust.
    echo "$APPIMAGETOOL_SHA256  $TOOL.part" | sha256sum --check --status || {
        echo "error: the downloaded appimagetool does not match its recorded checksum." >&2
        rm -f "$TOOL.part"
        exit 1
    }
    mv "$TOOL.part" "$TOOL"
    chmod +x "$TOOL"
fi

echo "Building $OUTPUT..."
mkdir -p "$REPO_ROOT/dist"
rm -f "$OUTPUT"
# APPIMAGE_EXTRACT_AND_RUN: appimagetool is itself an AppImage and would
# normally mount itself through FUSE, which a CI container and a fresh
# Ubuntu install (no libfuse2 since 22.04) do not have. Extracting instead is
# slower and needs nothing.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$APPIMAGE_ARCH" "$TOOL" "$APPDIR" "$OUTPUT"

echo
echo "Built $OUTPUT"
echo "Run it with: chmod +x $(basename "$OUTPUT") && ./$(basename "$OUTPUT")"
