#!/usr/bin/env python3
"""RouteCraft Memory Local v1.0 launcher."""

from __future__ import annotations

import sys


def _configure_text_streams() -> None:
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if callable(stdin_reconfigure):
        try:
            stdin_reconfigure(encoding="utf-8-sig", errors="strict")
        except (OSError, ValueError):
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


_configure_text_streams()

from routecraft_local.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
