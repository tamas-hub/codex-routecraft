#!/usr/bin/env python3
"""RouteCraft persistent decision memory CLI launcher."""
from __future__ import annotations

import os
import sys


def configure_private_git_identity() -> None:
    """Prevent inherited private Git email addresses from leaking into memory commits.

    A dedicated RouteCraft memory repository does not need to inherit the user's
    global Git author identity. GitHub can reject command-line pushes when that
    inherited address is private (GH007), so memory commits use a neutral
    no-reply identity by default. Users may opt into their own public/no-reply
    identity with ROUTECRAFT_GIT_NAME and ROUTECRAFT_GIT_EMAIL.
    """

    name = os.environ.get("ROUTECRAFT_GIT_NAME", "RouteCraft Memory")
    email = os.environ.get(
        "ROUTECRAFT_GIT_EMAIL",
        "routecraft-memory@users.noreply.github.com",
    )
    os.environ["GIT_AUTHOR_NAME"] = name
    os.environ["GIT_AUTHOR_EMAIL"] = email
    os.environ["GIT_COMMITTER_NAME"] = name
    os.environ["GIT_COMMITTER_EMAIL"] = email


def configure_utf8_stdin() -> None:
    """Decode redirected JSON stdin as UTF-8, independent of the Windows code page.

    Codex and modern PowerShell write redirected process input as UTF-8, while a
    Windows Python process can otherwise inherit a legacy ANSI encoding for
    redirected stdin. Reconfiguring before the CLI imports or reads input avoids
    mojibake and UnicodeDecodeError for Japanese learning/promotion packets.
    `utf-8-sig` also accepts an optional UTF-8 BOM.
    """

    reconfigure = getattr(sys.stdin, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8-sig", errors="strict")
    except (OSError, ValueError):
        # Embedded runtimes may expose a stream that cannot be reconfigured.
        # The normal script and cross-platform wrappers use a TextIOWrapper.
        pass


configure_private_git_identity()
configure_utf8_stdin()

from routecraft_memory_lib.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
