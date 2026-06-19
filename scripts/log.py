"""GitHub Actions workflow commands and step summary helpers.

Outside of a GitHub Actions runner the annotation commands are printed as-is,
which is harmless in a terminal - the ``::`` prefix makes them visually
distinct from regular output.
"""

from __future__ import annotations

import os


def _annotate(level: str, title: str, message: str) -> None:
    t = f" title={title}" if title else ""
    print(f"::{level}{t}::{message}", flush=True)


def error(message: str, title: str = "") -> None:
    _annotate("error", title, message)


def warning(message: str, title: str = "") -> None:
    _annotate("warning", title, message)


def notice(message: str, title: str = "") -> None:
    _annotate("notice", title, message)


def info(message: str) -> None:
    print(message, flush=True)


def group(title: str) -> None:
    print(f"::group::{title}", flush=True)


def endgroup() -> None:
    print("::endgroup::", flush=True)


def append_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(markdown + "\n")
