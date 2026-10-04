#!/usr/bin/env python3
"""Run the GitNexus CLI from a git hook; a no-op where GitNexus is not set up.

GitNexus (https://github.com/abhigyanpatwari/GitNexus) keeps a code knowledge graph of the
repository in .gitnexus/ (excluded through .git/info/exclude; .gitnexusignore keeps tests and data
out of it). It is opt-in per clone: the hooks do something only after
`gitnexus analyze --index-only --pdg` has been run once. Usage from .pre-commit-config.yaml:

    python scripts/gitnexus_hook.py analyze --index-only --pdg --no-stats   # refresh the graph
    python scripts/gitnexus_hook.py check --cycles                          # fail on import cycles
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SKIPPED = "gitnexus: .gitnexus/ exists but neither node nor gitnexus is on PATH; skipped"


def command(root: Path) -> list[str] | None:
    """The GitNexus CLI: the runner that `analyze` leaves in .gitnexus/, else a global install."""
    node = shutil.which("node")
    runner = root / ".gitnexus" / "run.cjs"
    if node and runner.is_file():
        return [node, str(runner)]
    gitnexus = shutil.which("gitnexus")
    return [gitnexus] if gitnexus else None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path.cwd()
    if not (root / ".gitnexus").is_dir():
        return 0  # not opted in on this clone
    # post-checkout also fires for `git checkout -- <file>`; only branch switches change the code.
    if os.environ.get("PRE_COMMIT_CHECKOUT_TYPE") == "0":
        return 0
    cli = command(root)
    if cli is None:
        print(SKIPPED)
        return 0
    return subprocess.call([*cli, *args])  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
