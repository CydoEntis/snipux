"""`tools/marketing/`: the made-up desktop and the shots taken over it.

Only that they render headless and land where they aim. What the shots
look like is for a person to judge; the clips need ffmpeg and minutes, so
they are not run here at all -- `python -m tools.marketing.shoot --clips`.
"""

import pytest
from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

from snipux import platform
from snipux.shapes import Redact
from tools.marketing import reel, scene, shots
from tools.marketing.stage import Stage, isolate_snipux


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ocr_restored(monkeypatch):
    """The shots install a fake OCR on the platform; put the real one back."""
    current = platform.current
    for name in ("recognizes_text", "text_recognition_unavailable_reason", "recognize_text"):
        monkeypatch.setattr(current, name, getattr(current, name))


def test_the_scene_paints_the_desktop_at_twice_its_logical_size():
    desk = scene.build_scene()

    assert (desk.image.width(), desk.image.height()) == (3200, 2000)
    assert not desk.image.isNull()


def test_the_scene_reports_where_it_drew_the_email():
    desk = scene.build_scene()

    [line] = [line for line in desk.chat_lines if any(word == scene.EMAIL for word, _ in line)]
    rect = dict(line)[scene.EMAIL]
    assert desk.places["email"] == rect
    assert scene.CHAT.contains(rect)


def test_the_hero_blacks_out_the_email(ocr_restored):
    desk = scene.build_scene()
    overlay = shots.annotated_overlay(desk)
    try:
        boxes = [mark for mark in overlay.marks if isinstance(mark, Redact)]
        image = shots.compose(overlay)
    finally:
        shots.close_all()

    assert len(boxes) == 1
    assert boxes[0].start.y() < desk.places["email"].center().y() < boxes[0].end.y()
    assert not image.isNull()


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """A shoot's isolation -- throwaway home and config, no clipboard, no
    probing of the real global shortcut -- undone after the test."""
    stage = Stage(root=tmp_path, home=tmp_path / "home")
    isolate_snipux(stage, replace=monkeypatch.setattr)
    return stage


# The player is left out: it needs a real video, which needs ffmpeg.
@pytest.mark.parametrize("name", sorted(shots.STILLS))
def test_every_still_renders_at_the_published_size(name, stage, ocr_restored):
    try:
        image = shots.STILLS[name](stage)
    finally:
        shots.close_all()

    assert not image.isNull()
    assert (image.width(), image.height()) == (3200, 2000)


def test_the_link_preview_renders_at_open_graph_size(stage, ocr_restored):
    try:
        image = shots.og_image(shots.hero(stage))
    finally:
        shots.close_all()

    assert (image.width(), image.height()) == (1200, 630)


def test_a_reel_without_ffmpeg_leaves_its_frames_as_pngs(tmp_path, monkeypatch):
    monkeypatch.setattr(reel, "ffmpeg_binary", lambda: None)
    desk = scene.build_scene()
    encoder = reel.Encoder(tmp_path, "clip")
    clip = reel.Reel(encoder, lambda: desk.image, QPointF(100, 100))

    clip.move(QPointF(400, 300), 2 / reel.FPS)
    written = encoder.close()

    assert written == []
    assert len(list((tmp_path / "frames" / "clip").glob("*.png"))) == 2
