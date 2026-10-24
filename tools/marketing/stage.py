"""Everything the shot script sets up before the first widget exists.

A headless Qt at a fixed screen size and scale, a throwaway home and config
folder, and the few machine-facing calls (clipboard, shortcut probes) turned
into no-ops, so a run never reads or changes anything of the person running
it -- their settings, their clipboard, the hotkey their own snipux holds --
and never shows their username in a path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The offscreen plugin's screen, in physical pixels, and the scale it runs
# at: a 1600x1000 logical desktop rendered at 2x.
SCREEN_PX = (3200, 2000)
SCALE = 2


@dataclass
class Stage:
    root: Path
    home: Path

    def pictures(self) -> Path:
        path = self.home / "Pictures" / "snipux"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def videos(self) -> Path:
        path = self.home / "Videos" / "snipux"
        path.mkdir(parents=True, exist_ok=True)
        return path


def configure_environment() -> Stage:
    """Set the process up for headless shots. Must run before QApplication."""
    root = Path(tempfile.mkdtemp(prefix="snipux-shoot-"))
    home = root / "home"
    home.mkdir()
    screen = root / "screen.json"
    screen.write_text(json.dumps({"screens": [{
        "name": "demo", "x": 0, "y": 0,
        "width": SCREEN_PX[0], "height": SCREEN_PX[1],
        "logicalDpi": 96, "logicalBaseDpi": 96,
    }]}))
    # Qt splits platform arguments on ':', so a Windows drive letter in the
    # path cuts it short; a path relative to here has none.
    try:
        config = os.path.relpath(screen).replace("\\", "/")
    except ValueError:
        sys.exit("Run shoot.py from a folder on the same drive as the temp folder.")
    os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={config}"
    os.environ["QT_SCALE_FACTOR"] = str(SCALE)
    if sys.platform == "win32":
        # The offscreen plugin finds no fonts on Windows without this
        # (see tests/conftest.py).
        os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"
    os.environ["SNIPUX_DEMO"] = "1"
    # Qt's FFmpeg media backend prints every file it opens, temp path and all.
    os.environ["QT_LOGGING_RULES"] = "qt.multimedia*=false"
    os.environ["QT_FFMPEG_DEBUG"] = "0"
    # Path.home() reads these; every path a window shows is then under a
    # home with no real username in it.
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    return Stage(root=root, home=home)


def isolate_snipux(stage: Stage, replace=setattr) -> None:
    """Point snipux at the throwaway config and stub what reaches the machine.

    `replace` does the patching; a test passes `monkeypatch.setattr` so all
    of it is undone afterwards.
    """
    from snipux import app, output, platform, setup_desktop

    real_config_path = setup_desktop.config_path
    throwaway = stage.root / "config"
    replace(setup_desktop, "config_path",
            lambda config_dir=None: real_config_path(throwaway if config_dir is None else config_dir))
    # The person's own snipux holds the real hotkey; probing for it would
    # report a conflict that only exists on this machine.
    replace(platform.current, "find_shortcut_conflict", lambda shortcut: None)
    replace(setup_desktop, "find_shortcut_conflicts_named", lambda shortcut: [])
    # A shot's Copy must not land on the person's clipboard (wl-copy on
    # Linux reaches the real one even from an offscreen Qt). These three
    # are every clipboard write snipux makes; app.py imports them by name,
    # so its copies are replaced too.
    for name in ("copy_image_to_clipboard", "copy_text_to_clipboard", "copy_file_to_clipboard"):
        replace(output, name, _no_clipboard)
        replace(app, name, _no_clipboard)


def _no_clipboard(value) -> None:
    """Stands in for every clipboard write during a shoot."""


def fake_text_recognition(words_for_image) -> None:
    """Stand in for the platform's OCR. `words_for_image(image)` returns
    lines of `sensitive.RecognizedWord` in that image's pixels."""
    from snipux import platform

    current = platform.current
    current.recognizes_text = lambda: True
    current.text_recognition_unavailable_reason = lambda: ""
    current.recognize_text = words_for_image
