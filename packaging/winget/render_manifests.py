"""Write the three winget manifests for a release, ready to submit.

    python packaging/winget/render_manifests.py --version 1.0.1 \
        --installer dist/snipux-setup-1.0.1.exe

They land in `dist/winget/<version>/` and are what a pull request to
microsoft/winget-pkgs carries -- see docs/releasing.md for that half.
Generated rather than kept in the repository as three hand-edited files:
every one of them names the version, and one of them carries the
installer's SHA256, so a stale copy is not a stale document but a manifest
that winget will refuse or, worse, one that points at the wrong binary.

The installer is the winget target rather than the portable exe, because
winget's whole promise is `winget install` and `winget uninstall` behaving
like every other package: an Add/Remove Programs entry to remove, and a
silent switch to install with. Inno gives both, and winget knows Inno's
switches without being told (`InstallerType: inno`).

PyQt6 is not imported here -- this is build tooling and runs with no Qt,
no display and nothing installed but the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

PACKAGE_IDENTIFIER = "CydoEntis.Snipux"
REPOSITORY = "https://github.com/CydoEntis/snipux"

# The schema version the three files declare. Bumping it is a decision --
# winget validates against it and the fields move between versions.
MANIFEST_VERSION = "1.6.0"

_VERSION_MANIFEST = """# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.{manifest}.schema.json
PackageIdentifier: {identifier}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: {manifest}
"""

_INSTALLER_MANIFEST = """# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.{manifest}.schema.json
PackageIdentifier: {identifier}
PackageVersion: {version}
InstallerLocale: en-US
MinimumOSVersion: 10.0.19041.0
InstallerType: inno
# Per-user, matching the installer itself: snipux installs into
# %LOCALAPPDATA% and never asks for elevation, so a machine-scope claim
# here would be winget promising something the installer does not do.
Scope: user
InstallModes:
  - interactive
  - silent
  - silentWithProgress
UpgradeBehavior: install
ReleaseDate: {release_date}
Installers:
  - Architecture: x64
    InstallerUrl: {url}
    InstallerSha256: {sha256}
ManifestType: installer
ManifestVersion: {manifest}
"""

_LOCALE_MANIFEST = """# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.{manifest}.schema.json
PackageIdentifier: {identifier}
PackageVersion: {version}
PackageLocale: en-US
Publisher: Cody
PublisherUrl: {repository}
PublisherSupportUrl: {repository}/issues
PackageName: Snipux
PackageUrl: {repository}
License: MIT
LicenseUrl: {repository}/blob/main/LICENSE
ShortDescription: Snip, annotate and record your screen.
Description: |-
  A Windows Snipping Tool workalike: snip an area, a window or the whole
  screen, annotate it, then copy or save it -- or record the same selection
  to video and trim it in the built-in player.
Moniker: snipux
Tags:
  - screenshot
  - screen-capture
  - screen-recorder
  - annotation
  - productivity
ReleaseNotesUrl: {repository}/releases/tag/v{version}
ManifestType: defaultLocale
ManifestVersion: {manifest}
"""


def sha256_of(path: pathlib.Path) -> str:
    """The installer's digest, in the uppercase hex winget's own tooling
    writes. Read in chunks: the installer is ~50 MB and there is no reason
    for this to hold all of it."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def installer_url(version: str, installer_name: str) -> str:
    return f"{REPOSITORY}/releases/download/v{version}/{installer_name}"


def render(version: str, installer: pathlib.Path, release_date: str) -> dict[str, str]:
    """The three manifests, keyed by the filename each has to be written
    under -- winget-pkgs requires those exact names."""
    common = {
        "identifier": PACKAGE_IDENTIFIER,
        "version": version,
        "manifest": MANIFEST_VERSION,
        "repository": REPOSITORY,
    }
    return {
        f"{PACKAGE_IDENTIFIER}.yaml": _VERSION_MANIFEST.format(**common),
        f"{PACKAGE_IDENTIFIER}.installer.yaml": _INSTALLER_MANIFEST.format(
            **common,
            url=installer_url(version, installer.name),
            sha256=sha256_of(installer),
            release_date=release_date,
        ),
        f"{PACKAGE_IDENTIFIER}.locale.en-US.yaml": _LOCALE_MANIFEST.format(**common),
    }


def main(argv: list[str] | None = None) -> int:
    import datetime

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="the release, e.g. 1.0.1")
    parser.add_argument(
        "--installer", required=True, type=pathlib.Path,
        help="the built snipux-setup-<version>.exe, for its SHA256",
    )
    parser.add_argument(
        "--out", type=pathlib.Path, default=None,
        help="where to write them (default: dist/winget/<version>)",
    )
    parser.add_argument(
        "--release-date", default=datetime.date.today().isoformat(),
        help="YYYY-MM-DD, defaulting to today",
    )
    args = parser.parse_args(argv)

    if not args.installer.is_file():
        print(f"error: {args.installer} does not exist -- build it first", file=sys.stderr)
        return 1

    out = args.out or pathlib.Path("dist") / "winget" / args.version
    out.mkdir(parents=True, exist_ok=True)
    for name, body in render(args.version, args.installer, args.release_date).items():
        (out / name).write_text(body, encoding="utf-8", newline="\n")
        print(f"wrote {out / name}")

    print()
    print("Check them, then submit with either:")
    print(f"    wingetcreate submit --token <pat> {out}")
    print("  or a pull request to microsoft/winget-pkgs adding them under")
    print(f"    manifests/c/CydoEntis/Snipux/{args.version}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
