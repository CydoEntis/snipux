"""snipux — a Windows Snipping Tool workalike, on Linux, Windows and macOS."""

import os as _os

# Qt Multimedia builds its list of hardware encoders once, the first time
# anything touches it, and never revisits it -- so this has to be decided
# here, before any module imports QtMultimedia, and cannot be corrected
# later when an encode fails.
#
# The bundled FFmpeg (7.1.5, LGPL) carries *only* hardware H.264 encoders --
# `h264_nvenc` and `h264_vaapi`, no software x264 -- and when neither has a
# capable device Qt does not fall back: `QMediaRecorder` reports "Could not
# initialize encoder" and writes a zero-length file. Measured on this box,
# which has an NVIDIA card whose NVENC reports "no capable devices" and a
# VAAPI node that fails the same way.
#
# Disabling hardware encoding therefore trades H.264 for an export that
# always produces a playable file (MPEG-4 Part 2), which is the better
# default: a file in a slightly older codec beats no file at all. Anyone
# with a working encoder can set this variable themselves and get H.264 --
# which is why an existing value is left alone.
#
# Global rather than behind the platform seam, and measured rather than
# assumed: on Windows 11 this variable changes nothing. Qt reaches
# `h264_mf`, Media Foundation's H.264 encoder, which is not a hwaccel
# *device* encoder and so is not in the list this names. Driving 45 frames
# through the same QVideoFrameInput -> QMediaRecorder path the region
# recorder uses produced h264 (Constrained Baseline, yuv420p, avc1) both
# with the variable set to "" and with it unset -- byte-identical files,
# same md5. So there is no good Windows path being thrown away here, and
# nothing for a per-platform override to buy.
_os.environ.setdefault("QT_FFMPEG_ENCODING_HW_DEVICE_TYPES", "")

__version__ = "0.4.3"
