"""Controller, tray, and CLI entry point.

This ticket only establishes the CLI skeleton and the `--list-backends`
diagnostic — no real capture backends exist yet (those land in the X11 and
Wayland tickets), and no overlay/editor exists yet either, so there is
nothing meaningful for a bare invocation to do beyond print usage.
"""

from __future__ import annotations

import argparse
import sys

from snipux.capture import BackendRegistry


def build_default_registry() -> BackendRegistry:
    """Construct the `BackendRegistry` the real app uses.

    Empty for now — no real `CaptureBackend` implementations exist yet.
    Later tickets extend this by appending real backend instances; its
    shape (a `BackendRegistry` with no arguments) does not change.
    """
    return BackendRegistry()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snipux",
        description="A Windows Snipping Tool workalike for Linux.",
    )
    parser.add_argument(
        "--list-backends",
        action="store_true",
        help="print each registered capture backend's name, availability, "
        "and reason if unavailable",
    )
    return parser


def _print_backends(registry: BackendRegistry) -> None:
    if len(registry) == 0:
        # An empty registry is expected today (no real backends exist yet),
        # but the output must never be silently empty in a way that looks
        # like a bug.
        print("no backends registered")
        return
    for backend in registry:
        if backend.is_available():
            print(f"{backend.name()}: available")
        else:
            reason = backend.unavailable_reason()
            if reason:
                print(f"{backend.name()}: unavailable ({reason})")
            else:
                print(f"{backend.name()}: unavailable")


def main(argv: list[str] | None = None, registry: BackendRegistry | None = None) -> int:
    """CLI entry point. Accepts an optional `registry` to stay testable
    without needing real backend availability on the machine running the
    tests.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()

    if not argv:
        # No arguments: nothing to capture with yet (no overlay, no real
        # backends), so print usage and exit cleanly rather than doing
        # nothing silently or trying to launch a display.
        parser.print_help()
        return 0

    args = parser.parse_args(argv)

    if registry is None:
        registry = build_default_registry()

    if args.list_backends:
        _print_backends(registry)
        return 0

    parser.print_help()
    return 0
