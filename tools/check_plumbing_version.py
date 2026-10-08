#!/usr/bin/env python3
"""CI guard: host files changed since the last tag need a plumbing-version bump.

Update never rewrites the host's unit files (ADR 0005); a release that
needs changed ones raises ``deploy/plumbing-version``, so bases are asked
to re-run ``install.sh`` instead of being offered an Update that would
leave code and host files apart.

The host files are those ``deploy/install.sh`` lays down with
``fetch_deploy_file`` (at the last tag or now, so a removed one counts).
If any changed between the last ``v*`` tag and ``HEAD``, ``HEAD``'s
plumbing version must be higher than the tag's (a tag without the file
has 0). Needs the full history and tags: a shallow clone is an error.

Usage: ``python tools/check_plumbing_version.py [--repo DIR] [--base REF]``.
Exits 0 when the guard passes, 1 when it fails, 2 when it can't tell.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

INSTALL_SH = "deploy/install.sh"
PLUMBING = "deploy/plumbing-version"
_FETCHED = re.compile(r"^\s*fetch_deploy_file\s+([A-Za-z0-9._/-]+)\s", re.MULTILINE)


class GuardError(Exception):
    """The guard can't tell (no history, an unreadable plumbing version)."""


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


def _show(repo: Path, ref: str, path: str) -> str | None:
    done = _git(repo, "show", f"{ref}:{path}", check=False)
    return done.stdout if done.returncode == 0 else None


def host_files(install_sh: str) -> set[str]:
    """``deploy/<path>`` for every file ``install.sh`` lays down."""
    return {
        f"deploy/{rel}"
        for rel in _FETCHED.findall(install_sh)
        if f"deploy/{rel}" != PLUMBING
    }


def _plumbing(repo: Path, ref: str) -> int:
    text = _show(repo, ref, PLUMBING)
    if text is None:
        return 0
    try:
        return int(text.strip())
    except ValueError as exc:
        raise GuardError(f"{PLUMBING} at {ref} isn't a number: {text!r}") from exc


def last_tag(repo: Path) -> str | None:
    """The newest ``v*`` tag reachable from ``HEAD``, or ``None``."""
    done = _git(repo, "describe", "--tags", "--abbrev=0", "--match", "v*", check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def check(repo: Path, base: str | None = None) -> tuple[bool, str]:
    """``(passed, why)``."""
    if _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise GuardError("Shallow clone: fetch the full history and tags first.")
    base = base or last_tag(repo)
    if base is None:
        return True, "No v* tag yet; nothing to compare against."
    files = host_files(_show(repo, "HEAD", INSTALL_SH) or "") | host_files(
        _show(repo, base, INSTALL_SH) or ""
    )
    changed = sorted(
        _git(repo, "diff", "--name-only", base, "HEAD", "--", *files)
        .stdout.strip()
        .splitlines()
    )
    if not changed:
        return True, f"No host file changed since {base}."
    old, new = _plumbing(repo, base), _plumbing(repo, "HEAD")
    if new > old:
        return True, (
            f"Host files changed since {base}; {PLUMBING} went {old} → {new}."
        )
    return False, (
        f"Host files changed since {base} without a bump to {PLUMBING} "
        f"(still {new}; must be more than {old}):\n"
        + "\n".join(f"  {path}" for path in changed)
        + "\nBases can't get these through Update: raise the plumbing version, "
        "so they are asked to re-run install.sh."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="compare against this ref, not the last tag")
    args = parser.parse_args(argv)
    try:
        passed, why = check(args.repo, args.base)
    except GuardError as exc:
        print(f"plumbing-version guard: {exc}", file=sys.stderr)
        return 2
    print(f"plumbing-version guard: {why}", file=sys.stdout if passed else sys.stderr)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
