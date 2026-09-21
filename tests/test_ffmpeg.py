"""`snipux.ffmpeg`: finding the system ffmpeg and what it can do for a
recording, without ever running a real one."""

from unittest.mock import Mock

import pytest

from snipux import ffmpeg

DEVICES = """Devices:
 D. = Demuxing supported
 .E = Muxing supported
 --
 DE alsa            ALSA audio output
 DE pulse           Pulse audio output
  E sdl,sdl2        SDL2 output device
"""
ENCODERS = """Encoders:
 A....D libopus              libopus Opus (codec opus)
 A....D libvorbis            libvorbis (codec vorbis)
"""


@pytest.fixture
def probing(monkeypatch):
    """Unprobed, on a machine whose ffmpeg answers with `listings`."""
    ffmpeg.reset()
    listings = {"-devices": DEVICES, "-encoders": ENCODERS}
    run = Mock(side_effect=lambda command, **_: Mock(stdout=listings[command[-1]]))
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg.subprocess, "run", run)
    return listings, run


def test_no_ffmpeg_on_the_path_is_none(monkeypatch):
    ffmpeg.reset()
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)

    assert ffmpeg.probe() is None


def test_an_ffmpeg_with_pulse_and_opus_records_sound(probing):
    capabilities = ffmpeg.probe()

    assert capabilities.binary == "/usr/bin/ffmpeg"
    assert capabilities.records_sound is True


def test_pulse_that_only_writes_is_no_way_to_record(probing):
    listings, _run = probing
    listings["-devices"] = DEVICES.replace(" DE pulse", "  E pulse")

    assert ffmpeg.probe().records_sound is False


def test_no_opus_encoder_is_no_sound(probing):
    listings, _run = probing
    listings["-encoders"] = ENCODERS.replace("libopus", "libfoo")

    capabilities = ffmpeg.probe()

    assert capabilities is not None and capabilities.records_sound is False


def test_an_ffmpeg_that_will_not_answer_is_none(monkeypatch):
    ffmpeg.reset()
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(ffmpeg.subprocess, "run", Mock(side_effect=OSError("exec format error")))

    assert ffmpeg.probe() is None


def test_it_is_probed_once_per_process(probing):
    _listings, run = probing

    ffmpeg.probe()
    ffmpeg.probe()

    assert run.call_count == 2  # -devices and -encoders, once each
