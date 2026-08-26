"""Recording-backend interface: `RecordingBackend` and `RecorderRegistry`.

Mirrors `capture.py`'s `CaptureBackend`/`BackendRegistry` pattern rather
than inventing a second one, per `docs/design/recording.md`'s ticket 1: try
backends in order, collect every failure, report them together, never let
one failure stop the next (CLAUDE.md's "a backend that fails must not stop
the next one" applies here too). Recording is stateful -- started and
stopped, not produced in one call -- so `start()`/`stop()` replace
`capture()`, but the try/collect/raise shape is unchanged. No real backend
lives here yet; later tickets register one behind the platform seam, the
same way `build_x11_registry()`/`build_wayland_registry()` do for capture.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod

from jeepney import DBusAddress, new_method_call
from jeepney.io.blocking import open_dbus_connection

from PyQt6.QtCore import QRectF


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
    def start(self, rect: QRectF | None, path: str) -> None:
        """Begin recording to `path`: `rect` (absolute logical coordinates)
        if given, or the whole virtual desktop when `rect` is None."""

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
        failures: list[tuple[str, Exception]],
        unavailable: list[tuple[str, str | None]] | None = None,
    ):
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

    def start(self, rect: QRectF | None, path: str) -> RecordingBackend:
        """Try available backends in order; return the first that starts
        recording `rect` (or the whole virtual desktop, when `rect` is
        None) to `path` successfully.

        Unlike `BackendRegistry.capture()`, which hands back the value a
        backend produced, this hands back *which backend* started --
        recording is stateful, so the caller needs to hold onto it to call
        `.stop()` later. A backend raising in `start()` does not stop the
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
                backend.start(rect, path)
                return backend
            except Exception as exc:  # noqa: BLE001 - collected, not swallowed
                failures.append((backend.name(), exc))
        raise RecordingError(failures, unavailable=self.unavailable())


class GnomeScreencastBackend(RecordingBackend):
    """`org.gnome.Shell.Screencast`, following `GnomeShellHelperBackend` in
    capture.py: manual D-Bus calls over jeepney, no wrapper library, same
    object path (`/org/gnome/Shell`) but a different interface living at
    it. Records a region straight to a file and works on GNOME under both
    X11 and Wayland -- the primary route, since it's the only one that
    answers "how do you record on Wayland at all"
    (docs/design/recording.md).
    """

    _BUS_NAME = "org.gnome.Shell"
    _OBJECT_PATH = "/org/gnome/Shell"
    _INTERFACE = "org.gnome.Shell.Screencast"
    _PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def name(self) -> str:
        return "gnome-screencast"

    def is_available(self) -> bool:
        return self._screencast_supported() is True

    def unavailable_reason(self) -> str | None:
        result = self._screencast_supported()
        return None if result is True else result

    def _screencast_supported(self) -> bool | str:
        """True if `ScreencastSupported` is set, otherwise a reason string.

        Unlike `GnomeShellHelperBackend.is_available()`'s cheap
        `XDG_SESSION_TYPE` guess, this makes a real
        `org.freedesktop.DBus.Properties.Get` round trip on every call --
        a Wayland-but-not-GNOME session would sail past a session-type gate
        and only fail once `start()` was already attempted, and this
        ticket's acceptance criterion is specifically about
        `org.gnome.Shell.Screencast`'s own availability, not the session
        type. `RecorderRegistry.start()` calls `available()` (and so this)
        once per attempt, not in a hot loop, so the round trip is worth
        paying for a real answer instead of a guess -- do not "fix" this
        back to an env-var check.

        Folds "why not" into the return value rather than a bare bool
        because a `Properties.Get` failure has several distinct shapes
        (connection refused, no such interface, no such property, a
        malformed reply) worth naming individually rather than collapsing
        into one generic message.
        """
        try:
            connection = open_dbus_connection(bus="SESSION")
            try:
                properties = DBusAddress(
                    self._OBJECT_PATH,
                    bus_name=self._BUS_NAME,
                    interface=self._PROPERTIES_INTERFACE,
                )
                message = new_method_call(
                    properties, "Get", "ss", (self._INTERFACE, "ScreencastSupported")
                )
                reply = connection.send_and_get_reply(message)
            finally:
                connection.close()
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

    def start(self, rect: QRectF | None, path: str) -> None:
        """Begin recording to `path`: `rect` via `ScreencastArea`, or the
        whole virtual desktop via bare `Screencast` when `rect` is None.

        `path` must be absolute. Older Shell versions are known to resolve
        a non-absolute filename against their own videos directory instead
        of the caller's working directory, and every downstream consumer
        of this path (stat, move, clipboard reference) needs it to be the
        exact file GNOME wrote -- so the filename each method hands back is
        compared against `path` in `_finish_start()` rather than trusted
        blindly, the same way `GnomeShellHelperBackend.capture()` reads its
        reply's filename back rather than trusting its own input.

        `rect` is None specifically for "the whole screen", not a
        monitor-sized region: a monitor-sized rect isn't reliably
        distinguishable from a large region, especially on a multi-monitor
        desktop where "whole screen" is one display, not the union of all
        of them, so callers that mean the whole virtual desktop must say so
        by omitting `rect` rather than by passing its dimensions.
        """
        if rect is None:
            reply = self._call_screencast(path)
        else:
            reply = self._call_screencast_area(rect, path)
        self._finish_start(reply, path)

    def _call_screencast_area(self, rect: QRectF, path: str):
        """Issue `ScreencastArea(iiiisa{sv})` for `rect` and return the reply."""
        # Round left/top/right/bottom independently and take the
        # difference for width/height, not independently-rounded
        # width/height, so adjacent regions stay pixel-aligned -- same
        # reasoning as Frame.crop() in capture.py.
        left = round(rect.left())
        top = round(rect.top())
        right = round(rect.left() + rect.width())
        bottom = round(rect.top() + rect.height())

        connection = open_dbus_connection(bus="SESSION")
        try:
            shell = DBusAddress(
                self._OBJECT_PATH, bus_name=self._BUS_NAME, interface=self._INTERFACE
            )
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
                    # draw-cursor / framerate / pipeline plug in here once
                    # ticket 9's Settings pane exists to set them; nothing
                    # upstream has an opinion on them yet.
                    {},
                ),
            )
            return connection.send_and_get_reply(message)
        finally:
            connection.close()

    def _call_screencast(self, path: str):
        """Issue whole-desktop `Screencast(sa{sv})` and return the reply."""
        connection = open_dbus_connection(bus="SESSION")
        try:
            shell = DBusAddress(
                self._OBJECT_PATH, bus_name=self._BUS_NAME, interface=self._INTERFACE
            )
            message = new_method_call(
                shell,
                "Screencast",
                "sa{sv}",
                (
                    path,
                    # Same options placeholder as ScreencastArea -- see
                    # the comment there.
                    {},
                ),
            )
            return connection.send_and_get_reply(message)
        finally:
            connection.close()

    def _finish_start(self, reply, path: str) -> None:
        """Shared success/filename check for both `start()` D-Bus calls."""
        success, filename = reply.body
        if not success:
            raise RuntimeError("gnome-screencast: recording start reported failure")
        if filename != path:
            raise RuntimeError(
                f"gnome-screencast: recorded to {filename!r} instead of the "
                f"requested {path!r}"
            )

    def stop(self) -> None:
        """End the recording via `StopScreencast()`.

        Opens its own connection rather than reusing one held from
        `start()` -- GNOME Shell tracks the in-progress recording
        server-side, keyed to the caller's D-Bus unique name, not to any
        handle this process holds, so there's nothing to keep open between
        the two calls. Same open-call-close shape as
        `GnomeShellHelperBackend.capture()`.
        """
        connection = open_dbus_connection(bus="SESSION")
        try:
            shell = DBusAddress(
                self._OBJECT_PATH, bus_name=self._BUS_NAME, interface=self._INTERFACE
            )
            message = new_method_call(shell, "StopScreencast")
            reply = connection.send_and_get_reply(message)
        finally:
            connection.close()

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
