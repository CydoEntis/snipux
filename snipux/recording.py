"""Recording-backend interface: `RecordingBackend` and `RecorderRegistry`.

Mirrors `capture.py`'s `CaptureBackend`/`BackendRegistry` pattern rather
than inventing a second one, per `docs/design/recording.md`'s ticket 1: try
backends in order, collect every failure, report them together, never let
one failure stop the next (CLAUDE.md's "a backend that fails must not stop
the next one" applies here too). Recording is stateful -- started and
stopped, not produced in one call -- so `start()`/`stop()` replace
`capture()`, but the try/collect/raise shape is unchanged. `GnomeScreencastBackend`
(SNX-117) is the Linux backend; `WindowsRecorderBackend` (SNX-118) is
Windows's, registered behind `build_windows_registry()` the same way
`build_linux_registry()` registers the GNOME one.
"""

from __future__ import annotations

import os
import posixpath
import sys
import time
from abc import ABC, abstractmethod

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    QRect,
    QRectF,
    Qt,
    QThread,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QGuiApplication, QImage
from PyQt6.QtMultimedia import (
    QMediaCaptureSession,
    QMediaFormat,
    QMediaRecorder,
    QScreenCapture,
    QVideoFrame,
    QVideoFrameFormat,
    QVideoFrameInput,
    QVideoSink,
)

from . import setup_desktop


class RecordingBackend(ABC):
    """A way of recording a region of the virtual desktop on a particular
    session type."""

    @abstractmethod
    def name(self) -> str:
        """A short, human-readable identifier, e.g. 'gnome-screencast'."""

    @abstractmethod
    def is_available(self) -> bool:
        """Whether this backend can plausibly run in the current session."""

    def unavailable_reason(self) -> str | None:
        """Why `is_available()` is False, or None if it is True.

        Kept as a separate accessor rather than folded into
        `is_available()` so `RecorderRegistry.available()`'s filter stays a
        plain boolean check -- same reason `CaptureBackend` keeps it
        separate.
        """
        return None

    @abstractmethod
    def start(self, rect: QRectF | None, path: str) -> str:
        """Begin recording to `path`: `rect` (absolute logical coordinates)
        if given, or the whole virtual desktop when `rect` is None.

        Returns the path actually written, which is not always `path`: on
        GNOME, Shell picks the container and renames to suit it, so `path`
        is a request and the return value is the answer. Callers must keep
        what comes back -- it is the only file that will exist when
        `stop()` returns.
        """

    @abstractmethod
    def stop(self) -> None:
        """End a recording started by this backend's `start()`."""


def _platform_name() -> str:
    """Human-readable platform name for `RecordingError`'s message.

    Deliberately not `capture.py`'s `_missing_backend_advice()` or
    `detect_session_type()`: that function's shape is Linux-package-advice
    specific ("install grim via apt"), and there is no such fix for
    recording on any platform yet -- this ticket adds no backend at all.
    Naming the platform is the honest thing `RecordingError` can say
    instead.
    """
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return "Linux"


class RecordingError(Exception):
    """Raised by `RecorderRegistry.start()` when every backend fails.

    Carries every `(backend_name, exception)` pair collected along the way,
    not just the last one, same as `CaptureError`, per CLAUDE.md's "a
    backend that fails must not stop the next one". `failures` is empty
    specifically when no backend was even available to try. Unlike
    `CaptureError`, that case cannot fall back to package-install advice --
    there is no such fix for recording yet -- so instead it names the
    platform and, given `unavailable` (name, reason) pairs from
    `RecorderRegistry.unavailable()`, enumerates every registered backend
    and why it couldn't be tried. With no backends registered at all (a
    bare, freshly-constructed registry -- this ticket's own end state),
    `unavailable` is itself empty and the message falls back to just the
    platform sentence, since there is nothing to enumerate.
    """

    def __init__(
        self,
        failures: "list[tuple[str, Exception]] | str",
        unavailable: list[tuple[str, str | None]] | None = None,
    ):
        if isinstance(failures, str):
            # `RecordingError("the display is locked")` is the obvious thing
            # to write, and a backend author will write it -- the Windows
            # and macOS recorders are the next ones to exist. Without this
            # the string is iterated as a list of (name, exception) pairs
            # and the *exception constructor* raises ValueError, burying
            # whatever actually went wrong.
            self.failures = []
            self.unavailable = unavailable or []
            super().__init__(failures)
            return

        self.failures = failures
        self.unavailable = unavailable or []
        if not failures:
            platform = _platform_name()
            if not self.unavailable:
                super().__init__(f"no recording backend is available on {platform}")
                return
            tried = "; ".join(f"{name}: {reason}" for name, reason in self.unavailable)
            super().__init__(
                f"no recording backend is available on {platform} (tried: {tried})"
            )
            return
        summary = "; ".join(f"{name}: {exc}" for name, exc in failures)
        super().__init__(f"all recording backends failed: {summary}")


class RecorderRegistry:
    """An ordered collection of `RecordingBackend`s, tried in order."""

    def __init__(self, backends: list[RecordingBackend] | None = None):
        self._backends = list(backends) if backends else []

    def __iter__(self):
        return iter(self._backends)

    def __len__(self) -> int:
        return len(self._backends)

    def add(self, backend: RecordingBackend) -> None:
        self._backends.append(backend)

    def available(self) -> list[RecordingBackend]:
        """Backends whose `is_available()` is True, in registration order."""
        return [b for b in self._backends if b.is_available()]

    def unavailable(self) -> list[tuple[str, str | None]]:
        """Name and `unavailable_reason()` for every registered backend
        *not* in `available()`, in registration order.

        `capture.py` has no equivalent of this -- `CaptureError` doesn't
        need one, since it falls back to session-type advice instead. This
        is what lets `RecordingError` report "which were tried and why"
        even when `available()` is empty and `start()`'s loop below never
        runs: without it, nothing in this module still knows about the
        unavailable backends by the time `RecordingError` is constructed.
        """
        return [(b.name(), b.unavailable_reason()) for b in self._backends if not b.is_available()]

    def start(self, rect: QRectF | None, path: str) -> tuple[RecordingBackend, str]:
        """Try available backends in order; return the first that starts
        recording `rect` (or the whole virtual desktop, when `rect` is
        None) to `path` successfully.

        Unlike `BackendRegistry.capture()`, which hands back the value a
        backend produced, this hands back *which backend* started, paired
        with the path it is actually writing -- recording is stateful, so
        the caller needs the backend to call `.stop()` later, and the path
        because a backend may not have honoured the one it was given (see
        `RecordingBackend.start()`). Returning them together makes the
        second impossible to forget. A backend raising in `start()` does not stop the
        next one from being tried. If every available backend fails, raises
        `RecordingError` carrying all collected failures. Always passes
        `unavailable=self.unavailable()` too, so a caller catching
        `RecordingError` never has to call back into the registry to see
        which backends existed and why each was unavailable -- both are
        already on the exception, whether `failures` ended up empty or not.
        """
        failures: list[tuple[str, Exception]] = []
        for backend in self.available():
            try:
                return backend, backend.start(rect, path)
            except Exception as exc:  # noqa: BLE001 - collected, not swallowed
                failures.append((backend.name(), exc))
        raise RecordingError(failures, unavailable=self.unavailable())


class GnomeScreencastBackend(RecordingBackend):
    """`org.gnome.Shell.Screencast`, following `GnomeShellHelperBackend` in
    capture.py: manual D-Bus calls over jeepney, no wrapper library.
    Records a region straight to a file and works on GNOME under both X11
    and Wayland -- the primary route, since it's the only one that answers
    "how do you record on Wayland at all" (docs/design/recording.md).

    Three facts about Shell's own behaviour drive the shape of this class.
    All three were measured against a live GNOME Shell 46 session; every
    one of them contradicts what this file previously assumed, and each
    alone was enough to stop a recording from ever working:

    * **The recording dies with the D-Bus connection that started it.**
      Shell keys the in-progress screencast to the calling connection, so
      closing it once `ScreencastArea` returns leaves a truncated,
      duration-less file (measured: frozen at 4029 bytes, `duration=N/A`)
      and makes a later `StopScreencast()` from a fresh connection answer
      `False`. `_connection` is therefore opened by `start()` and held
      until `stop()`. Do not "tidy" this back into the open-call-close
      shape `GnomeShellHelperBackend.capture()` uses: that shape is right
      for a one-shot call and fatal for a stateful one.

    * **Shell chooses the container, and so the file extension.** It
      appends `.webm` to any path not already ending that way, so the file
      that appears is not the file that was asked for. `start()` returns
      the `filename_used` Shell reports instead of rejecting it.

    * **The interface moved.** GNOME 41 split Screencast out of the main
      shell object into a service of its own; Shell 46 answers only at the
      new address and returns "No such interface" at the old one.
    """

    # (bus name, object path), current layout first, legacy second -- see
    # the class docstring. Tried in order rather than either being assumed,
    # the same collect-and-report habit CLAUDE.md asks of capture backends.
    _ADDRESSES = (
        ("org.gnome.Shell.Screencast", "/org/gnome/Shell/Screencast"),
        ("org.gnome.Shell", "/org/gnome/Shell"),
    )
    _INTERFACE = "org.gnome.Shell.Screencast"
    _PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def __init__(self):
        # The live recording's lifeline, held from start() to stop() -- see
        # the class docstring for why closing it early truncates the file.
        self._connection = None
        # Which of `_ADDRESSES` answered, remembered by the availability
        # probe so start()/stop() address the same object without probing
        # again -- and so both halves of one recording are guaranteed to
        # talk to the same one.
        self._address = None

    def name(self) -> str:
        return "gnome-screencast"

    def is_available(self) -> bool:
        return self._screencast_supported() is True

    def unavailable_reason(self) -> str | None:
        result = self._screencast_supported()
        return None if result is True else result

    @staticmethod
    def _reply_error(reply) -> str | None:
        """The D-Bus error text carried by `reply`, or None if `reply` is
        not an error.

        jeepney's blocking API *returns* an error message rather than
        raising it, and an error body is a bare `(message,)` -- the same
        arity a `Properties.Get` reply has. Unpacking one as the other is
        how "No such interface" surfaced as `ValueError: too many values
        to unpack (expected 2)`, discarding the one sentence that said
        what was actually wrong.
        """
        if getattr(reply.header, "message_type", None) is not MessageType.error:
            return None
        body = getattr(reply, "body", None) or ()
        return str(body[0]) if body else "unspecified D-Bus error"

    def _address_or_default(self) -> tuple[str, str]:
        """The address the availability probe settled on, or the current
        layout if nothing has probed yet.

        `start()` deliberately does not probe on its own: a probe costs a
        `Properties.Get` round trip, and `RecorderRegistry.start()` has
        always called `available()` (and so `is_available()`) immediately
        beforehand, which is what fills `_address` in.
        """
        return self._address or self._ADDRESSES[0]

    def _screencast_supported(self) -> bool | str:
        """True if `ScreencastSupported` is set at one of `_ADDRESSES`,
        otherwise a reason string naming what each address answered.

        Unlike `GnomeShellHelperBackend.is_available()`'s cheap
        `XDG_SESSION_TYPE` guess, this makes real `Properties.Get` round
        trips -- a Wayland-but-not-GNOME session would sail past a
        session-type gate and only fail once `start()` was already
        attempted, and the acceptance criterion is specifically about
        `org.gnome.Shell.Screencast`'s own availability, not the session
        type. `RecorderRegistry.start()` calls this once per attempt, not
        in a hot loop, so the round trip is worth paying for a real answer
        instead of a guess -- do not "fix" this back to an env-var check.

        Folds "why not" into the return value rather than a bare bool
        because a failure has several distinct shapes (connection refused,
        no such interface, no such property, a malformed reply) worth
        naming individually rather than collapsing into one message.
        """
        try:
            connection = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return f"{type(exc).__name__}: {exc}"
        reasons = []
        try:
            for bus_name, object_path in self._ADDRESSES:
                outcome = self._query_supported(connection, bus_name, object_path)
                if outcome is True:
                    self._address = (bus_name, object_path)
                    return True
                reasons.append(f"{object_path}: {outcome}")
        finally:
            connection.close()
        return "; ".join(reasons)

    def _query_supported(self, connection, bus_name: str, object_path: str) -> bool | str:
        """Ask one address whether screencasting is supported."""
        try:
            properties = DBusAddress(
                object_path,
                bus_name=bus_name,
                interface=self._PROPERTIES_INTERFACE,
            )
            message = new_method_call(
                properties, "Get", "ss", (self._INTERFACE, "ScreencastSupported")
            )
            reply = connection.send_and_get_reply(message)
            error = self._reply_error(reply)
            if error is not None:
                return error
            # A Properties.Get reply's body is a single variant, which
            # jeepney's low-level API represents as a (signature, value)
            # pair -- not a bare bool -- so this must unwrap one level
            # further than a plain method reply would.
            (variant,) = reply.body
            _signature, supported = variant
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return f"{type(exc).__name__}: {exc}"
        if not supported:
            return "org.gnome.Shell.Screencast reports ScreencastSupported=False"
        return True

    def start(self, rect: QRectF | None, path: str) -> str:
        """Begin recording to `path` -- `rect` via `ScreencastArea`, or the
        whole virtual desktop via bare `Screencast` when `rect` is None --
        and return the path Shell reports it is actually writing.

        `path` is a request, not a guarantee: Shell picks the container and
        renames accordingly (see the class docstring), so the return value
        is the only path that will exist when `stop()` returns. It must be
        absolute; older Shell versions resolve a relative filename against
        their own videos directory rather than the caller's working
        directory, and every downstream consumer (stat, move, clipboard
        reference) needs a path it can find.

        The connection opened here stays open -- it is what keeps the
        recording alive -- and is closed by `stop()`, or immediately if
        starting fails, so a failed start leaks neither a socket nor a
        half-started server-side recording.

        `rect` is None specifically for "the whole screen", not a
        monitor-sized region: a monitor-sized rect isn't reliably
        distinguishable from a large region, especially on a multi-monitor
        desktop where "whole screen" is one display, not the union of all
        of them, so callers that mean the whole virtual desktop must say so
        by omitting `rect` rather than by passing its dimensions.
        """
        bus_name, object_path = self._address_or_default()
        connection = open_dbus_connection(bus="SESSION")
        try:
            shell = DBusAddress(
                object_path, bus_name=bus_name, interface=self._INTERFACE
            )
            if rect is None:
                reply = self._call_screencast(connection, shell, path)
            else:
                reply = self._call_screencast_area(connection, shell, rect, path)
            actual_path = self._finish_start(reply, path)
        except Exception:
            connection.close()
            raise
        self._connection = connection
        return actual_path

    @staticmethod
    def _screencast_options() -> dict:
        """The `a{sv}` options dict shared by `ScreencastArea` and
        `Screencast` -- ticket 9's settings, read fresh on every call the
        same way `overlay.py` re-reads `load_hints_enabled()` per snip,
        rather than cached at construction: a Settings change must take
        effect on the next recording, not the next process restart.

        jeepney's low-level API wants each `a{sv}` entry as its own
        `(dbus-signature, value)` pair, not a bare value. `draw-cursor` is
        Shell's boolean option for whether the cursor is composited into
        the recording, `framerate` its integer frames-per-second request.
        Both spellings are confirmed accepted by a live GNOME Shell 46
        session, which recorded real-time video with them (5.05s of wall
        clock producing a 5028ms file).
        """
        return {
            "draw-cursor": ("b", setup_desktop.load_recording_draw_cursor()),
            "framerate": ("i", setup_desktop.load_recording_frame_rate()),
        }

    def _call_screencast_area(self, connection, shell, rect: QRectF, path: str):
        """Issue `ScreencastArea(iiiisa{sv})` for `rect` and return the reply."""
        # Round left/top/right/bottom independently and take the
        # difference for width/height, not independently-rounded
        # width/height, so adjacent regions stay pixel-aligned -- same
        # reasoning as Frame.crop() in capture.py.
        left = round(rect.left())
        top = round(rect.top())
        right = round(rect.left() + rect.width())
        bottom = round(rect.top() + rect.height())

        message = new_method_call(
            shell,
            "ScreencastArea",
            "iiiisa{sv}",
            (
                left,
                top,
                right - left,
                bottom - top,
                path,
                self._screencast_options(),
            ),
        )
        return connection.send_and_get_reply(message)

    def _call_screencast(self, connection, shell, path: str):
        """Issue whole-desktop `Screencast(sa{sv})` and return the reply."""
        message = new_method_call(
            shell,
            "Screencast",
            "sa{sv}",
            (
                path,
                self._screencast_options(),
            ),
        )
        return connection.send_and_get_reply(message)

    @staticmethod
    def _finish_start(reply, path: str) -> str:
        """Shared success/filename handling for both `start()` calls;
        returns the filename Shell says it is writing.

        That filename is authoritative and routinely *not* `path` -- Shell
        appends `.webm` to anything not already named that way. This check
        used to reject any mismatch, which meant every real GNOME
        recording failed here *after* Shell had already started one,
        orphaning both the server-side recording and the file. A
        non-absolute answer is still refused, for the reason `start()`
        gives -- asked of `posixpath`, not `os.path`, because the answer
        comes from GNOME Shell and is always a POSIX path whatever
        interpreter reads it. `os.path.isabs()` is `ntpath.isabs()` on a
        Windows dev box, which since Python 3.13 calls a drive-less
        `/tmp/out.webm` *not* absolute -- enough to fail this backend's
        tests there while they pass on the Linux box that actually runs it.
        """
        error = GnomeScreencastBackend._reply_error(reply)
        if error is not None:
            raise RuntimeError(f"gnome-screencast: {error}")
        success, filename = reply.body
        if not success:
            raise RuntimeError("gnome-screencast: recording start reported failure")
        if not filename or not posixpath.isabs(str(filename)):
            raise RuntimeError(
                f"gnome-screencast: reported a non-absolute recording path "
                f"{filename!r} for the requested {path!r}"
            )
        return str(filename)

    def stop(self) -> None:
        """End the recording via `StopScreencast()`, on the very connection
        `start()` opened.

        Reusing that connection is load-bearing, not tidiness: Shell keys
        the in-progress recording to the calling connection, and a
        `StopScreencast()` from any other one answers `False` while leaving
        the real recording running (measured on Shell 46). The connection
        is closed here whatever happens, since the recording is over either
        way and a held-open socket would otherwise outlive it.
        """
        connection = self._connection
        if connection is None:
            raise RuntimeError(
                "gnome-screencast: stop() called with no recording in progress"
            )
        bus_name, object_path = self._address_or_default()
        try:
            shell = DBusAddress(
                object_path, bus_name=bus_name, interface=self._INTERFACE
            )
            reply = connection.send_and_get_reply(
                new_method_call(shell, "StopScreencast")
            )
        finally:
            connection.close()
            self._connection = None

        error = self._reply_error(reply)
        if error is not None:
            raise RuntimeError(f"gnome-screencast: StopScreencast() failed: {error}")
        (success,) = reply.body
        if not success:
            raise RuntimeError("gnome-screencast: StopScreencast() reported failure")


def build_linux_registry() -> RecorderRegistry:
    """The real Linux `RecorderRegistry`.

    One backend today; the shape exists so ticket 4's
    `Platform.build_recording_registry()` has something to call, mirroring
    `capture.build_linux_registry()`, and so this ticket's "does the
    registry actually pick this backend" behaviour has somewhere to be
    asserted without waiting on the platform seam.
    """
    registry = RecorderRegistry()
    registry.add(GnomeScreencastBackend())
    return registry


def _rect_to_screen_pixels(rect: QRectF, screen_geometry: QRectF, ratio: float) -> QRect:
    """Map `rect` (absolute logical virtual-desktop coordinates, per
    `RecordingBackend.start()`) into the pixel space of the `QVideoFrame`
    `QScreenCapture` delivers for the screen with logical geometry
    `screen_geometry` and device-pixel ratio `ratio`.

    A pure function of its arguments -- no `QScreen` touched -- so the
    crop-rect arithmetic is unit-testable without a display, the same
    reason `Frame.crop()` in capture.py stays a plain calculation. Same
    left/top/right/bottom-independently-rounded, width/height-as-difference
    pattern as `Frame.crop()` and `GnomeScreencastBackend
    ._call_screencast_area()`: an independently-rounded width/height can
    drift a pixel off the position-derived edges under fractional scaling.
    """
    local_left = rect.left() - screen_geometry.left()
    local_top = rect.top() - screen_geometry.top()

    left = round(local_left * ratio)
    top = round(local_top * ratio)
    right = round((local_left + rect.width()) * ratio)
    bottom = round((local_top + rect.height()) * ratio)

    return QRect(left, top, right - left, bottom - top)


def _bytes_per_pixel(pixel_format: "QVideoFrameFormat.PixelFormat") -> int:
    """Bytes per pixel for a `QVideoFrame` pixel format, via the `QImage`
    format Qt itself considers equivalent -- there is no direct
    bytes-per-pixel accessor on `QVideoFrameFormat`. Used to size the
    row-wise crop copy in `_crop_frame()`.
    """
    image_format = QVideoFrameFormat.imageFormatFromPixelFormat(pixel_format)
    depth = QImage(1, 1, image_format).depth()
    if depth <= 0 or depth % 8 != 0:
        raise RuntimeError(
            f"qt-native: pixel format {pixel_format!r} isn't a whole number of "
            "bytes per pixel, can't crop it row-wise"
        )
    return depth // 8


def _clamp_rect_to_frame(pixel_rect: QRect, frame_width: int, frame_height: int) -> QRect:
    """Clamp `pixel_rect` to `frame`'s actual pixel bounds.

    `_rect_to_screen_pixels()`'s screen-geometry-times-device-pixel-ratio
    arithmetic is an estimate of the frame `QScreenCapture` will actually
    deliver, not a guarantee of it -- a region dragged flush against a
    screen edge, combined with device-pixel-ratio rounding (CLAUDE.md:
    "coordinates are the sharp edge here"), can round the estimate one
    pixel past the frame's real buffer. `frame.width()`/`frame.height()`
    are the one place that actually knows the true bounds, so clamping
    against them here, right before `_crop_frame()` slices `src_bits` at
    the computed offsets, is what stops a one-pixel-over rect from reading
    (or writing) past the end of a row.
    """
    left = max(0, min(pixel_rect.left(), frame_width))
    top = max(0, min(pixel_rect.top(), frame_height))
    right = max(left, min(pixel_rect.left() + pixel_rect.width(), frame_width))
    bottom = max(top, min(pixel_rect.top() + pixel_rect.height(), frame_height))
    return QRect(left, top, right - left, bottom - top)


def _crop_frame(frame: QVideoFrame, pixel_rect: QRect) -> QVideoFrame:
    """Return a new `QVideoFrame` covering `pixel_rect` of `frame`, copied
    row-wise in `frame`'s own native pixel format.

    The spike measured this at 1.52ms/frame, versus 5.47ms going through
    `toImage()`/`convertToFormat()` -- the difference between sustaining
    30fps and not, so this must never round-trip through `QImage`.
    `frame` is unmapped in a `finally`, mirroring CLAUDE.md's "never leave
    a QPainter open across a read of the pixmap it is painting" -- an
    unmapped-too-late or never-unmapped `QVideoFrame` is the same class of
    bug.

    `pixel_rect` is clamped to `frame`'s own `width()`/`height()` before
    any byte-slicing happens -- see `_clamp_rect_to_frame()`.
    """
    pixel_rect = _clamp_rect_to_frame(pixel_rect, frame.width(), frame.height())
    source_format = frame.surfaceFormat()
    pixel_format = source_format.pixelFormat()
    bytes_per_pixel = _bytes_per_pixel(pixel_format)
    row_bytes = pixel_rect.width() * bytes_per_pixel

    # The size/pixel-format constructor copies neither the stream frame rate
    # nor the colour metadata, and the frame rate is load-bearing: the
    # encoder builds its output type from the *first* frame pushed, and
    # Media Foundation's H264 encoder refuses a type whose frame rate is 0
    # ("could not set output type (80004005)" -> "Could not initialize
    # encoder"). The recording then lands as a 0-byte file while start()
    # has already returned successfully. `_apply_measured_frame_rate` does
    # tell the *recorder* a real rate, but only after
    # _FRAME_RATE_SAMPLE_FRAMES arrivals -- long after the encoder has
    # already had to commit -- which is why this failed intermittently
    # rather than always, depending on which won the race. Carrying the
    # source's own rate here means the very first frame already describes
    # itself correctly. The colour fields are copied for the same
    # "describe the frame honestly" reason; QScreenCapture reports them
    # Undefined/Unknown today, so they cost nothing and stop this from
    # silently mattering if it ever reports otherwise.
    out_format = QVideoFrameFormat(pixel_rect.size(), pixel_format)
    out_format.setStreamFrameRate(source_format.streamFrameRate())
    out_format.setColorSpace(source_format.colorSpace())
    out_format.setColorRange(source_format.colorRange())
    out_format.setColorTransfer(source_format.colorTransfer())

    out_frame = QVideoFrame(out_format)
    # startTime()/endTime() are metadata accessors, not buffer accessors --
    # copying them here has no interaction with the map/unmap ordering
    # below. This is the fix for SNX-125: a freshly constructed QVideoFrame
    # defaults both to -1 (untimed), and nothing else in this module ever
    # carried `frame`'s real capture timestamps onto the cropped copy, so
    # QMediaRecorder laid whatever it received down at its own nominal
    # spacing instead of the rate frames actually arrived at.
    out_frame.setStartTime(frame.startTime())
    out_frame.setEndTime(frame.endTime())
    if not out_frame.map(QVideoFrame.MapMode.WriteOnly):
        raise RuntimeError("qt-native: could not map cropped video frame for writing")

    if not frame.map(QVideoFrame.MapMode.ReadOnly):
        out_frame.unmap()
        raise RuntimeError("qt-native: could not map source video frame for reading")
    try:
        src_stride = frame.bytesPerLine(0)
        dst_stride = out_frame.bytesPerLine(0)
        src_bits = frame.bits(0)
        src_bits.setsize(src_stride * frame.height())
        dst_bits = out_frame.bits(0)
        dst_bits.setsize(dst_stride * pixel_rect.height())

        left_offset = pixel_rect.left() * bytes_per_pixel
        for row in range(pixel_rect.height()):
            src_start = (pixel_rect.top() + row) * src_stride + left_offset
            dst_start = row * dst_stride
            dst_bits[dst_start : dst_start + row_bytes] = bytes(
                src_bits[src_start : src_start + row_bytes]
            )
    finally:
        frame.unmap()
        out_frame.unmap()
    return out_frame


class _RegionCropWorker(QObject):
    """Crops and forwards frames off the frame-delivery thread.

    Lives on its own `QThread` (see `WindowsRecorderBackend._start_region`)
    -- the spike measured cropping and sending inline in the
    `QVideoSink.videoFrameChanged` handler halving throughput, 29fps to
    17fps. `on_frame` and `on_ready_to_send` are connected to
    `videoFrameChanged`/`readyToSendVideoFrame` with an explicit
    `Qt.ConnectionType.QueuedConnection`, so both run on this worker's
    thread regardless of which thread emits the signal.

    Holds at most one pending frame: an arriving frame *replaces* whatever
    is already pending rather than queuing behind it, since the goal is a
    smooth 30fps *output*, not encoding every captured frame at whatever
    rate the encoder can chew through. `sendVideoFrame()` is only ever
    called once `readyToSendVideoFrame` has actually fired -- never
    speculatively -- which is what respects `QVideoFrameInput`'s
    backpressure: the spike's own failure mode, calling `sendVideoFrame()`
    while the encoder was still busy, dropped 47 of 48 frames.

    Tracks readiness explicitly (`_ready`) rather than sending only from
    inside `on_ready_to_send`, confirmed necessary by hand:
    `readyToSendVideoFrame` is edge-triggered and, in practice, fires once
    immediately on construction -- *before* the first frame has arrived.
    A version that only ever sent from `on_ready_to_send` (checking for a
    pending frame and giving up if there wasn't one yet) missed that first
    edge, found no frame waiting, and then waited forever: nothing calls
    `sendVideoFrame()`, so no further `readyToSendVideoFrame` ever fires,
    and zero frames ever reach the recorder -- a real, reproduced stall,
    not a hypothetical one. Recording readiness and consuming it from
    *both* `on_frame` and `on_ready_to_send` (whichever of the two events
    happens second triggers the send) fixes that regardless of which
    order they arrive in, while still never sending unless `_ready` says
    the encoder actually asked for a frame.

    Coalescing to at most one pending frame stays correct now that
    `_crop_frame()` carries real timing (SNX-125): a frame dropped here
    simply leaves a bigger, honest gap between its surviving neighbours'
    `startTime()`s, rather than the silently-wrong nominal spacing the
    encoder used to fabricate for whatever it received. Don't mistake this
    coalescing itself for that bug -- the bug was the missing timing on the
    frames that *did* survive, not the dropping.

    Also measures the real inter-arrival rate of source frames and emits it
    once via `frame_rate_measured`, so `WindowsRecorderBackend` can declare
    a frame rate the container header actually matches (the ticket's other
    half: per-frame timing alone fixes duration/speed, but a container's
    stream-level frame-rate metadata is separate from per-frame PTS and
    nothing else in this class touches it). The measurement is taken in
    `on_frame`, before `_send_pending()`'s coalescing drops anything --
    deliberately: it wants the rate frames genuinely arrived at from
    `QScreenCapture`, not the smaller rate that survives to the encoder,
    since coalescing is a deliberate output-side choice that must not feed
    back into what rate this claims frames arrived at.
    """

    # How many genuine arrivals to average over before emitting
    # frame_rate_measured -- large enough to smooth out one-off jitter
    # between individual frames, small enough to produce an answer well
    # before a short recording ends. Exposed as a class attribute so tests
    # can drive exactly this many synthetic arrivals rather than a magic
    # number duplicated on both sides.
    _FRAME_RATE_SAMPLE_FRAMES = 15

    frame_rate_measured = pyqtSignal(float)

    def __init__(self, frame_input: QVideoFrameInput, pixel_rect: QRect):
        super().__init__()
        self._frame_input = frame_input
        self._pixel_rect = pixel_rect
        self._pending: QVideoFrame | None = None
        self._ready = False
        self._arrival_start_times: list[int] = []
        self._rate_measured = False

    def on_frame(self, frame: QVideoFrame) -> None:
        self._record_arrival(frame)
        self._pending = frame
        if self._ready:
            self._send_pending()

    def on_ready_to_send(self) -> None:
        self._ready = True
        if self._pending is not None:
            self._send_pending()

    def _record_arrival(self, frame: QVideoFrame) -> None:
        """Accumulate real arrival timestamps toward a one-shot frame-rate
        estimate; see the class docstring for why this runs here rather
        than in `_send_pending()`.
        """
        if self._rate_measured:
            return
        start_time = frame.startTime()
        if start_time < 0:
            return  # untimed source frame -- nothing to measure from
        self._arrival_start_times.append(start_time)
        if len(self._arrival_start_times) < self._FRAME_RATE_SAMPLE_FRAMES:
            return
        span = self._arrival_start_times[-1] - self._arrival_start_times[0]
        intervals = len(self._arrival_start_times) - 1
        if span <= 0:
            return  # clock didn't advance across the sample -- try again later
        self._rate_measured = True
        self.frame_rate_measured.emit(intervals * 1_000_000 / span)

    def _send_pending(self) -> None:
        frame, self._pending = self._pending, None
        self._ready = False
        self._frame_input.sendVideoFrame(_crop_frame(frame, self._pixel_rect))


class WindowsRecorderBackend(RecordingBackend):
    """Qt's `QScreenCapture` + `QMediaRecorder`, per the SNX-118 spike
    (measured on Windows 11, PyQt6 6.11) and `docs/design/recording.md`'s
    ticket 3.

    Two genuinely different paths, branching in `start()` on whether `rect`
    is given, not one pipeline with cropping made a no-op -- the acceptance
    criterion that full screen must not pay for interception has to be true
    structurally, not by a fast no-op branch inside a shared path:

    * **Full screen** (`rect is None`): one `QMediaCaptureSession` wiring
      `QScreenCapture` straight to `QMediaRecorder`. No `QVideoSink`, no
      `QVideoFrameInput`, no worker thread -- none of that machinery is
      even constructed.
    * **Region**: the spike's two-session design. `QScreenCapture` ->
      `QVideoSink` on one session (interception only, never touches the
      recorder); `QVideoFrameInput` -> `QMediaRecorder` on a second
      session (encoding only, never touches the raw screen), with
      `_RegionCropWorker` on its own `QThread` between them.

    Records the primary screen -- `QScreenCapture` has no region support of
    its own (confirmed by the spike: no rect/crop/area method, it captures
    a whole `QScreen`), and there is no ticket yet for choosing which
    monitor a region on a multi-monitor desktop belongs to, so, like
    `QtNativeX11Backend.capture()` in capture.py, one session-wide screen
    and device-pixel ratio is what's used. A region that doesn't lie on the
    primary screen will record the wrong pixels; that is a real gap, not
    an oversight, and belongs to whichever ticket adds monitor selection.

    Deliberately does not read either of ticket 9's Settings rows the way
    `GnomeScreencastBackend` does. Frame rate: SNX-125 measures the real
    inter-arrival rate off `_RegionCropWorker` and declares exactly that to
    the container (see `_apply_measured_frame_rate` in `_start_region`) --
    a nominal request from Settings would be a second, conflicting opinion
    about a number this class has already gone to real effort to get right
    from measurement instead of a guess. Draw-cursor: `QScreenCapture`
    exposes no such toggle at all -- there is nothing here to wire it to,
    not an oversight.

    Every Qt object involved is built by a constructor-injectable factory
    (`screen_capture_factory`, `capture_session_factory`,
    `recorder_factory`, `video_sink_factory`, `video_frame_input_factory`,
    `thread_factory`), all defaulting to the real Qt classes, so tests can
    substitute fakes -- there is no way to make `QScreenCapture` produce a
    real frame stream under `QT_QPA_PLATFORM=offscreen` with no real
    desktop behind it, the same reason `GnomeScreencastBackend`'s tests
    monkeypatch `open_dbus_connection` rather than talk to a real session
    bus.
    """

    def __init__(
        self,
        screen_capture_factory=QScreenCapture,
        capture_session_factory=QMediaCaptureSession,
        recorder_factory=QMediaRecorder,
        video_sink_factory=QVideoSink,
        video_frame_input_factory=QVideoFrameInput,
        thread_factory=QThread,
        stop_settle_seconds: float = 1.0,
    ):
        self._screen_capture_factory = screen_capture_factory
        self._capture_session_factory = capture_session_factory
        self._recorder_factory = recorder_factory
        self._video_sink_factory = video_sink_factory
        self._video_frame_input_factory = video_frame_input_factory
        self._thread_factory = thread_factory
        # See _wait_for_stopped()'s docstring: real, load-bearing settle
        # time for the muxer's asynchronous finalization, not a cosmetic
        # default -- tests that don't care about it pass 0 here rather
        # than eating the real delay.
        self._stop_settle_seconds = stop_settle_seconds

        # Full screen: one session, no interception.
        self._session = None
        self._screen_capture = None
        self._recorder = None

        # Region: capture-only session, encode-only session, and the
        # worker thread that bridges them. `_worker_thread` being set is
        # what `stop()` uses to tell which path is live.
        self._capture_session = None
        self._video_sink = None
        self._encode_session = None
        self._frame_input = None
        self._worker = None
        self._worker_thread = None

        # Failures reported *after* start() returned. Both start paths
        # check this list once, immediately after record(), which catches
        # only what Qt reported synchronously -- and the encoder failure
        # that mattered most did not arrive until later (see stop()).
        self._errors: list[str] = []

    def name(self) -> str:
        return "qt-native"

    def is_available(self) -> bool:
        return sys.platform == "win32"

    def unavailable_reason(self) -> str | None:
        return None if self.is_available() else "qt-native recording is Windows-only"

    def _media_format(self) -> QMediaFormat:
        """MPEG4/H264, explicitly -- not left to default-construct.

        This exact combination is what the spike verified plays back with
        no external ffmpeg, using PyQt6's bundled FFmpeg-backend media
        plugin. An unset/auto format is how a build would quietly stop
        being the thing the spike actually validated.
        """
        media_format = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
        media_format.setVideoCodec(QMediaFormat.VideoCodec.H264)
        return media_format

    def _wire_errors(self, screen_capture, recorder, errors: list[str]) -> None:
        """Collect failures from `screen_capture` and `recorder` into
        `errors`, so `_start_full_screen`/`_start_region` can raise instead
        of returning normally on failure -- same shape as
        `GnomeScreencastBackend.start()` turning a `(success, filename)`
        reply into a `RuntimeError`.

        `QScreenCapture.errorOccurred` connects directly. `QMediaRecorder
        .errorOccurred` does not: connecting *anything* to it -- even a
        bare `print`, with no lambda involved -- raises `TypeError:
        connect() failed between (QMediaRecorder::Error,QString) and
        unislot()` in this PyQt6 6.11.0 / Qt 6.11.1 QtMultimedia build,
        reproduced standalone and independent of the slot's own signature.
        That is a binding-level failure on Qt's side, not a mistake in the
        slot passed here, so the actual plan (wire the signal Qt exposes
        for "the recorder failed") is followed via `errorChanged` instead:
        a plain no-arg NOTIFY signal that connects fine and, confirmed by
        triggering a real recorder error, fires for the exact same
        underlying failure `errorOccurred` would have reported --
        `error()`/`errorString()` read back the same information
        `errorOccurred`'s arguments would have carried.
        """
        screen_capture.errorOccurred.connect(lambda _err, msg: errors.append(msg))

        def _on_recorder_error_changed() -> None:
            if recorder.error() != QMediaRecorder.Error.NoError:
                errors.append(recorder.errorString())

        recorder.errorChanged.connect(_on_recorder_error_changed)

    def start(self, rect: QRectF | None, path: str) -> str:
        """Returns `path` unchanged: unlike GNOME, `QMediaRecorder` writes
        exactly the file it is handed, so there is nothing to correct.
        """
        if rect is None:
            self._start_full_screen(path)
        else:
            self._start_region(rect, path)
        return path

    def _start_full_screen(self, path: str) -> None:
        session = self._capture_session_factory()
        screen_capture = self._screen_capture_factory()
        recorder = self._recorder_factory()
        recorder.setMediaFormat(self._media_format())
        recorder.setOutputLocation(QUrl.fromLocalFile(path))

        errors: list[str] = self._errors
        errors.clear()
        self._wire_errors(screen_capture, recorder, errors)

        session.setScreenCapture(screen_capture)
        session.setRecorder(recorder)
        screen_capture.setActive(True)
        recorder.record()

        if errors:
            raise RuntimeError(f"qt-native: {errors[0]}")

        self._session = session
        self._screen_capture = screen_capture
        self._recorder = recorder

    def _start_region(self, rect: QRectF, path: str) -> None:
        """Wire and start the region (crop) path -- see the class
        docstring's two-session description.

        `recorder.setVideoFrameRate()` is called late, from
        `_apply_measured_frame_rate()` below, once `_RegionCropWorker` has
        actually measured a real inter-arrival rate -- not up front here.
        There is no real rate to declare before any source frame has
        arrived, and `QScreen.refreshRate()` was deliberately rejected as a
        substitute (SNX-125): it's the display's nominal Hz, not what
        `QScreenCapture` delivers, and the two are already known to differ
        (~28.8fps measured against a 30fps-class request). `record()` is
        still called immediately, same as before -- recording starts with
        no visible delay, and the declared rate updates in place once the
        measurement is ready, a few frames in.
        """
        screen = QGuiApplication.primaryScreen()
        pixel_rect = _rect_to_screen_pixels(
            rect, QRectF(screen.geometry()), screen.devicePixelRatio()
        )

        capture_session = self._capture_session_factory()
        screen_capture = self._screen_capture_factory()
        video_sink = self._video_sink_factory()

        encode_session = self._capture_session_factory()
        frame_input = self._video_frame_input_factory()
        recorder = self._recorder_factory()
        recorder.setMediaFormat(self._media_format())
        recorder.setOutputLocation(QUrl.fromLocalFile(path))

        errors: list[str] = self._errors
        errors.clear()
        self._wire_errors(screen_capture, recorder, errors)

        worker = _RegionCropWorker(frame_input, pixel_rect)
        worker_thread = self._thread_factory()
        worker.moveToThread(worker_thread)
        # Explicit QueuedConnection rather than relying on Qt's implicit
        # auto-connect (which would already queue, since emitter and
        # worker end up on different threads) -- so a later refactor that
        # accidentally constructs the worker on the wrong thread doesn't
        # silently regress the crop back onto the delivery thread.
        video_sink.videoFrameChanged.connect(
            worker.on_frame, Qt.ConnectionType.QueuedConnection
        )
        frame_input.readyToSendVideoFrame.connect(
            worker.on_ready_to_send, Qt.ConnectionType.QueuedConnection
        )

        # Declares the container's frame rate from the worker's own
        # measurement of real inter-arrival timing, once it has enough
        # samples -- not a hardcoded nominal value, and not called until
        # genuine data exists. `_apply_measured_frame_rate` is a plain
        # function rather than a bound method so this doesn't need
        # `WindowsRecorderBackend` itself to be a QObject; queued the same
        # explicit way as the other two connections, so
        # `recorder.setVideoFrameRate()` only ever runs on this object's
        # own thread even though the measurement happens on the worker's.
        def _apply_measured_frame_rate(fps: float) -> None:
            recorder.setVideoFrameRate(fps)

        worker.frame_rate_measured.connect(
            _apply_measured_frame_rate, Qt.ConnectionType.QueuedConnection
        )
        worker_thread.start()

        capture_session.setScreenCapture(screen_capture)
        capture_session.setVideoSink(video_sink)
        encode_session.setVideoFrameInput(frame_input)
        encode_session.setRecorder(recorder)

        screen_capture.setActive(True)
        recorder.record()

        if errors:
            worker_thread.quit()
            worker_thread.wait()
            raise RuntimeError(f"qt-native: {errors[0]}")

        self._capture_session = capture_session
        self._screen_capture = screen_capture
        self._video_sink = video_sink
        self._encode_session = encode_session
        self._frame_input = frame_input
        self._recorder = recorder
        self._worker = worker
        self._worker_thread = worker_thread

    def stop(self) -> None:
        """End the recording, and raise if the recorder reported a failure
        at any point -- not just in the instant after `record()`.

        Both start paths already check for a synchronous failure, but a
        recorder can fail well after `record()` returns and go on reporting
        `RecordingState` for a while before dropping to `StoppedState` on
        its own. That is not hypothetical: a cropped frame carrying a
        stream frame rate of 0 made Media Foundation's H264 encoder refuse
        to initialize on the *first* frame, and the whole failure surfaced
        as `ResourceError: Could not initialize encoder` some milliseconds
        after a `start()` that had already returned happily. The result was
        a 0-byte file, an app that showed a HUD counting up over a
        recording that did not exist, and a toast naming a file nobody had
        written. Whatever else is wrong, a recording that failed must not
        end quietly.
        """
        if self._worker_thread is not None:
            self._stop_region()
        else:
            self._stop_full_screen()

        if self._errors:
            failure = self._errors[0]
            self._errors = []
            raise RuntimeError(f"qt-native: {failure}")

    def _stop_full_screen(self) -> None:
        if self._screen_capture is not None:
            self._screen_capture.setActive(False)
        if self._recorder is not None:
            self._recorder.stop()
            self._wait_for_stopped(self._recorder)
        self._session = None
        self._screen_capture = None
        self._recorder = None

    def _stop_region(self) -> None:
        # Opposite order from _start_region's wiring: stop new frames from
        # arriving first, then unwind the worker thread (so no crop is
        # left mid-flight when the recorder goes away), then stop the
        # recorder last. Stopping the recorder first would race a queued
        # frame still landing after encoding has ended.
        if self._screen_capture is not None:
            self._screen_capture.setActive(False)
        if self._video_sink is not None and self._worker is not None:
            try:
                self._video_sink.videoFrameChanged.disconnect(self._worker.on_frame)
            except TypeError:
                pass  # already disconnected -- nothing left to do
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait()
        if self._recorder is not None:
            self._recorder.stop()
            self._wait_for_stopped(self._recorder)

        self._capture_session = None
        self._screen_capture = None
        self._video_sink = None
        self._encode_session = None
        self._frame_input = None
        self._recorder = None
        self._worker = None
        self._worker_thread = None

    def _wait_for_stopped(
        self,
        recorder: QMediaRecorder,
        stopped_timeout: float = 3.0,
        poll_interval: float = 0.02,
    ) -> None:
        """Block until `recorder.recorderState()` actually reaches
        `StoppedState`, then hold for a further, real, wall-clock settle
        period before returning.

        `QMediaRecorder.stop()` is not documented as synchronous, and the
        "playable file" acceptance criterion needs the container's moov
        atom actually written before `stop()` returns, not just the call
        having been made -- so the first loop checks state rather than
        assuming. Polling needs an actual `time.sleep()` between checks,
        not just repeated `processEvents()` calls with no pause between
        them -- confirmed by hand that spinning `processEvents()` back to
        back with no sleep never once observes the state change, even
        after 50 calls, because the real transition happens on a Windows
        Media Foundation worker thread that needs actual elapsed wall time
        to post its result back, not just event-loop turns to *deliver*
        one if it were already queued.

        Reaching `StoppedState` alone turned out not to be enough either,
        confirmed the same way: a file opened with `QMediaPlayer`
        immediately after `stop()` returned came back "moov atom not
        found" and unplayable, even though the state machine already said
        stopped, while the same file opened a couple of seconds later (a
        fresh process, plenty more wall time elapsed) played fine. There
        is no more precise signal to wait on instead of a further timed
        hold: `recorderStateChanged` and `errorOccurred` both carry a
        nested `QMediaRecorder` enum argument, and connecting *anything*
        to either -- confirmed independent of the slot's own signature --
        raises `TypeError: connect() failed ... and unislot()` in this
        exact PyQt6 6.11.0 / Qt 6.11.1 build (see `_wire_errors()`), and
        `actualLocationChanged` did not fire at all in the same manual
        check. So this holds, still pumping the event loop, for
        `self._stop_settle_seconds` (empirically ~1s was enough) after
        `StoppedState` first appears -- a real, load-bearing wait, not a
        cosmetic one; shortening or dropping it reintroduces the
        unplayable-file failure this was written to fix.

        Bounded rather than looped forever throughout: with no
        `QCoreApplication` instance, or a recorder that never reports
        `StoppedState` within `stopped_timeout`, there's nothing more this
        can usefully wait on, and a bounded wait fails visibly (an
        unplayable file) instead of hanging the process.
        """
        app = QCoreApplication.instance()
        if app is None:
            return

        deadline = time.monotonic() + stopped_timeout
        reached_stopped = False
        while time.monotonic() < deadline:
            app.processEvents()
            if recorder.recorderState() == QMediaRecorder.RecorderState.StoppedState:
                reached_stopped = True
                break
            time.sleep(poll_interval)

        if not reached_stopped:
            return

        settle_deadline = time.monotonic() + self._stop_settle_seconds
        while time.monotonic() < settle_deadline:
            app.processEvents()
            time.sleep(poll_interval)


def build_windows_registry() -> RecorderRegistry:
    """The real Windows `RecorderRegistry`.

    One backend today, mirroring `build_linux_registry()`'s shape.
    """
    registry = RecorderRegistry()
    registry.add(WindowsRecorderBackend())
    return registry
