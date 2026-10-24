"""Render snipux's marketing screenshots and demo clips, headless.

    python -m tools.marketing.shoot              # every still
    python -m tools.marketing.shoot hero record  # just these
    python -m tools.marketing.shoot --clips      # stills and the demo clips

Run from the repository root. Output goes to build/marketing/ (ignored by
git) unless --out says otherwise. See tools/marketing/README.md.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .stage import configure_environment, isolate_snipux

REPO = Path(__file__).resolve().parents[2]
# Ignored by git: the media is for the website and is re-rendered whenever
# the UI changes, so committing it would grow the repository on every run.
DEFAULT_OUT = REPO / "build" / "marketing"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("shots", nargs="*", help="stills to render (default: all)")
    parser.add_argument("--clips", action="store_true", help="also render the demo clips")
    parser.add_argument("--only-clips", action="store_true", help="render the demo clips and no stills")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args(argv)

    stage = configure_environment()
    try:
        return render(parser, args, stage)
    finally:
        try:
            shutil.rmtree(stage.root)
        except OSError as error:
            print(f"could not remove the temporary folder {stage.root}: {error}")


def render(parser, args, stage) -> int:
    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    isolate_snipux(stage)
    from . import clips, shots

    named = set(shots.STILLS) | {"player", "og"}
    wanted = args.shots or list(shots.STILLS) + ["player", "og"]
    unknown = set(wanted) - named
    if unknown:
        parser.error(f"unknown shot(s): {', '.join(sorted(unknown))}")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    if not args.only_clips:
        for name in wanted:
            if name == "og":
                write(shots.og_image(shots.hero(stage)), out / "og-image.png")
            elif name == "player":
                image = clips.player_still(stage)
                if image is not None:
                    write(image, out / "player.png")
            else:
                write(shots.STILLS[name](stage), out / f"{name}.png")
    if args.clips or args.only_clips:
        clips.render_all(stage, out / "video")
    shots.close_all()
    app.quit()
    return 0


def write(image, path: Path) -> None:
    # Quality 0 is the PNG writer's strongest compression; it is lossless.
    if not image.save(str(path), "PNG", 0):
        raise OSError(f"could not write {path}")
    print(f"wrote {path.name}")


if __name__ == "__main__":
    sys.exit(main())
