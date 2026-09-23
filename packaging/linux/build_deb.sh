#!/usr/bin/env bash
# Builds dist/snipux_<version>_<arch>.deb: the same PyInstaller bundle as the
# AppImage, laid out for dpkg.
#
#     ./packaging/linux/build_deb.sh
#     sudo apt install ./dist/snipux_1.0.0_amd64.deb
#
# **Uninstalling wants `snipux --remove` first.** dpkg removes what this
# package put on disk, and nothing else -- but Snipux's first launch writes
# a user-level .desktop entry, an autostart entry, hicolor icons and a GNOME
# keybinding into the user's own home, all pointing at /opt/snipux/snipux.
# A maintainer script cannot reach into another user's home to clean those
# up, so `apt remove` on its own leaves a dead launcher and a shortcut that
# does nothing. `--remove` is the counterpart that clears them (prerm says
# so too, for anyone who finds out the hard way).
#
# The bundle goes under /opt/snipux rather than /usr/lib/snipux because it
# carries its own copy of Qt: /opt is where the Filesystem Hierarchy Standard
# puts self-contained software that is not part of the distribution, and
# keeping a private Qt out of /usr means it can never be mistaken for, or
# conflict with, the system's own.
#
# Why bundle Qt at all, when Debian ships python3-pyqt6: snipux needs PyQt6
# 6.8 (requirements.txt explains the floor -- QVideoFrameInput, for Windows
# region recording), and Ubuntu 24.04 packages an older one. Depending on the
# distribution's Qt would make the package uninstallable on the LTS this
# targets.

set -e

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Sets REPO_ROOT, SNIPUX_VERSION and BUNDLE_DIR -- see build_bundle.sh.
source "$_here/build_bundle.sh"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "error: dpkg-deb is not installed." >&2
    echo "Install it with: sudo apt install dpkg-dev" >&2
    exit 1
fi

STAGE="$REPO_ROOT/build/deb"
OUTPUT="$REPO_ROOT/dist/snipux_${SNIPUX_VERSION}_${DEB_ARCH}.deb"

echo "Staging the package tree in $STAGE..."
rm -rf "$STAGE"
mkdir -p "$STAGE/opt/snipux" "$STAGE/usr/bin" "$STAGE/usr/share/applications" "$STAGE/DEBIAN"
cp -a "$BUNDLE_DIR/." "$STAGE/opt/snipux/"

# A relative symlink, so it still resolves if the tree is ever relocated or
# inspected from outside /.
ln -s ../../opt/snipux/snipux "$STAGE/usr/bin/snipux"

# A system-level entry, so snipux is in the application list the moment it is
# installed rather than only after its first run. The user-level entry that
# first launch writes (app.py's run_first_launch_setup) lands on the same
# filename in $XDG_DATA_HOME, which XDG resolves ahead of this one -- so the
# two never show as duplicate launchers, and the later, more specific one
# wins. That first run is still what binds the GNOME shortcut and the
# autostart entry, neither of which a package can write for a user.
sed 's|^Exec=__SNIPUX_LAUNCHER__$|Exec=/usr/bin/snipux|' \
    "$REPO_ROOT/snipux/snipux.desktop" > "$STAGE/usr/share/applications/snipux.desktop"

# sed reports success when it matches nothing, so an unsubstituted template
# would otherwise ship as a launcher whose Exec line is the literal
# placeholder -- a package that installs cleanly and cannot start.
if ! grep -q '^Exec=/usr/bin/snipux$' "$STAGE/usr/share/applications/snipux.desktop"; then
    echo "error: the Exec placeholder in snipux/snipux.desktop was not substituted." >&2
    exit 1
fi

# Icon=snipux is a theme name, so every vendored size goes into the system
# hicolor tree -- the same sizes and the same destination shape that
# setup_desktop.install_icons() writes per-user.
for png in "$REPO_ROOT"/snipux/design/logo/snipux-*.png; do
    size="$(basename "$png" .png)"
    size="${size#snipux-}"
    mkdir -p "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps"
    cp "$png" "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/snipux.png"
done

# Depends: the libraries Qt loads at runtime that are not inside the bundle.
# PyInstaller collects what the Qt libraries link directly, but the platform
# and multimedia plugins are dlopen()ed, and a machine that has never run a Qt
# application may have none of these -- libxcb-cursor0 in particular, which
# Ubuntu pulls in for nothing else (packaging/install.sh preflights for the
# same library for the same reason). Naming them here turns "installs, then
# aborts on launch" into "apt tells you what it is fetching".
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: snipux
Version: $SNIPUX_VERSION
Section: graphics
Priority: optional
Architecture: $DEB_ARCH
Maintainer: Cody <cydoentis@gmail.com>
Homepage: https://github.com/CydoEntis/snipux
Depends: libc6, libegl1, libgl1, libxkbcommon-x11-0, libxcb-cursor0, libdbus-1-3, libpulse0, libgstreamer1.0-0, libgstreamer-plugins-base1.0-0
Description: Snip, annotate and record your screen
 A Windows Snipping Tool workalike: snip an area, a window or the whole
 screen, annotate it, then copy or save it -- or record the same selection
 to video and trim it in the built-in player.
 .
 This package carries its own Python and Qt, so it depends on no system
 Python at all.
CONTROL

# update-desktop-database and the icon cache are how GNOME notices a new
# launcher without a logout. Both are guarded: they belong to packages
# (desktop-file-utils, gtk-update-icon-cache) that a minimal system may not
# have, and neither is worth failing an install over -- the entry is on disk
# either way and the next login picks it up.
cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi

exit 0
POSTINST
chmod 755 "$STAGE/DEBIAN/postinst"

# A package cannot clean another user's home, so the one thing it can do is
# say so before the files go -- while `snipux --remove` still exists to run.
# Printed on removal only: an upgrade runs prerm too, and telling someone
# mid-upgrade to undo their desktop integration would be wrong.
cat > "$STAGE/DEBIAN/prerm" <<'PRERM'
#!/bin/sh
set -e

if [ "$1" = "remove" ]; then
    echo "Note: run 'snipux --remove' as each user who ran Snipux to clear"
    echo "their desktop entry, autostart entry, icons and Ctrl+Alt+S shortcut."
    echo "Removing this package does not reach into a user's home directory."
fi

exit 0
PRERM
chmod 755 "$STAGE/DEBIAN/prerm"

cat > "$STAGE/DEBIAN/postrm" <<'POSTRM'
#!/bin/sh
set -e

# Only on a real removal: an upgrade calls this too, between unpacking the
# new version and configuring it, and refreshing the caches there would
# describe a state that no longer exists a second later.
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi

exit 0
POSTRM
chmod 755 "$STAGE/DEBIAN/postrm"

echo "Building $OUTPUT..."
mkdir -p "$REPO_ROOT/dist"
rm -f "$OUTPUT"
# --root-owner-group: without it every file in the package is owned by
# whichever uid happened to run this build, which dpkg then reproduces on the
# installing machine.
dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT"

echo
echo "Built $OUTPUT"
echo "Install it with: sudo apt install $OUTPUT"
