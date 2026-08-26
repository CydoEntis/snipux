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
    def start(self, rect: QRectF, path: str) -> None:
        """Begin recording `rect` (absolute logical coordinates) to `path`."""

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

    def start(self, rect: QRectF, path: str) -> RecordingBackend:
        """Try available backends in order; return the first that starts
        recording `rect` to `path` successfully.

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
