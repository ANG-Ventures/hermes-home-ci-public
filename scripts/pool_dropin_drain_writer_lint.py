#!/usr/bin/env python3
"""pool-dropin-drain-writer-lint: nothing in this repo may DRAIN a CI pool with a systemd drop-in (t_92988772).

A past incident: one drain landed as `zz-drain.conf` / `Environment=POOL_SIZE=0` drop-ins in every
ci-runner-pool@ unit dir, a second as drain files, and the two undrains then fought (ci-runner-host
refuses to undrain beside a legacy zz-drain.conf). A drop-in is also INERT on a running pool:
`Environment=` binds at process start, so the supervisor keeps its old POOL_SIZE (measured
2026-10-15: six units, runners 3 -> 62 after a "drain"). ONE mechanism remains:
`ci-runner-host drain|undrain <host>` (drain FILE + `zz-drain-owner` marker), reached from here via
`fleet-host drain|add`.

A WRITER is a code line (comments stripped) that
  * carries a literal `Environment=POOL_SIZE=0` drop-in body, or
  * names `zz-drain.conf` AND writes it (tee, a `>` redirect, write_text/open(..."w"), atomic_put,
    install/cp/mv onto it).
Reading or detecting a legacy drop-in is fine, and so is a POOL_SIZE=N SIZING drop-in (enrolment). Scope: scripts/, fleet/, hooks/, skills-shared/**/scripts/;
tests and fixtures are exempt (they build such files on purpose).

  python3 scripts/pool_dropin_drain_writer_lint.py [ROOT]   # rc 0 clean, 1 writers found
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOTS = ("scripts", "fleet", "hooks", "skills-shared")
CODE_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ""}
EXEMPT_PARTS = {"tests", "fixtures", "retired", "__pycache__", "node_modules", ".venv"}
SELF = Path(__file__).name

DROPIN_BODY = re.compile(r"Environment=\"?POOL_SIZE=0(?![0-9])")
TARGET = re.compile(r"zz-drain\.conf")
WRITE = re.compile(r"\btee\b|(?<![<>=!-])>{1,2}\s*[\"'$/{A-Za-z]|write_text|write_bytes|atomic_put|"
                   r"open\([^)]*[\"'][wa]\+?b?[\"']|\b(?:install|cp|mv)\s")


def _code(line: str) -> str:
    s = line.strip()
    return "" if s.startswith("#") or s.startswith("//") else line


def is_writer(line: str) -> bool:
    code = _code(line)
    return bool(code) and bool(DROPIN_BODY.search(code) or (TARGET.search(code) and WRITE.search(code)))


def candidates(root: Path):
    for top in ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root)
            if not p.is_file() or EXEMPT_PARTS & set(rel.parts) or p.name == SELF:
                continue
            if top == "skills-shared" and "scripts" not in rel.parts:
                continue
            if p.suffix not in CODE_SUFFIXES or ".bak" in p.name:
                continue
            yield p, rel


def scan(root: Path) -> list[str]:
    hits = []
    for p, rel in candidates(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if p.suffix == "" and not text.startswith("#!"):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if is_writer(line):
                hits.append(f"{rel}:{n}: {line.strip()[:160]}")
    return hits


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    hits = scan(root)
    for h in hits:
        print(f"pool-dropin-drain-writer: {h}\n  -> drain a CI pool with `ci-runner-host drain <host>` (or "
              f"`fleet-host drain <host>`), never a POOL_SIZE=0 / zz-drain.conf drop-in (t_92988772)")
    print(f"pool-dropin-drain-writer-lint: {len(hits)} writer(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
