#!/usr/bin/env python3
# SCHEDULE: manual (runs inside the CI-only mirror's workflow, .github/ci/ci-public/workflow.yml; card t_c09d9dcb)
"""Run hermes-home suites (.github/ci/suites.json `run:` rows) on a GitHub-HOSTED runner in the CI-only mirror.

Modes
  census  every `run:` suite of this shard (card step 2): each suite runs in its own bash with a timeout, a red suite
          never stops the next, and one JSON line per suite goes to --out:
          {"suite", "rc", "secs", "class", "tail"}  class = pass | fail-hermeticity | fail-real | timeout
          Exit 0 (the census is a measurement, not a gate).
  gate    the suites listed in .github/ci/ci-public/hermetic.txt (the hermetic set step 2 measured); exit 1 if any
          is red. An empty or missing hermetic.txt, or a shard that selects 0 suites, is exit 1: a gate with nothing in
          it is never green (fail closed). Each suite's shell leads its own process group; the group is killed at the
          timeout and after the suite ends, so no child outlives its suite.

fail-hermeticity = the output names a fleet dependency (see HERMETIC_MARKERS): a fleet host, 1Password, LAN, a live
~/.hermes path, the test gate, a missing private file. Anything else red is fail-real. The classification is a
heuristic over the log tail; the census table lists the matching marker so a human can check it.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERMETIC = ROOT / ".github/ci/ci-public/hermetic.txt"
HERMETIC_MARKERS = re.compile(
    r"(\.hermes|/Users/|op(?: read|://)|1password|OP_SERVICE_ACCOUNT|test-gate|ssh: |"
    r"Could not resolve hostname|tailscale|\.ts\.net|192\.168\.|"
    r"No such file or directory: '(?:state|var|cron|fleet|config|plans|skills-shared|profiles|kanban)/|"
    r"(?:state|var|cron|fleet|config|plans|skills-shared|profiles|kanban)/[\w./-]+: No such file|"
    r"can't open file|FileNotFoundError|ModuleNotFoundError|command not found|launchctl|systemctl|"
    r"sqlite3\.OperationalError)",
    re.I)


# Prism 972291b90ed2: the same per-entry contract as the private runner (.github/workflows/ci.yml, registered
# suites): ONE shared shell, each non-final entry's status checked explicitly (errexit exempts `a && b`), and a
# completion marker written just before the final entry, so an `exec` / early `exit 0` that skips entries is red.
ENTRY_CHECK = '__rc=$?; if [ "$__rc" -ne 0 ]; then exit "$__rc"; fi'
INCOMPLETE_RC = 125   # rc 0 but the shell ended before every entry ran


def suite_script(lines, done):
    return ("".join("%s\n%s\n" % (ln, ENTRY_CHECK) for ln in lines[:-1])
            + ": > %s\n%s\n" % (shlex.quote(done), lines[-1]))


def _kill_group(pgid):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        if sig == signal.SIGTERM:
            time.sleep(2)


def run_group(argv, env, timeout_s):
    """(rc, combined output). The suite shell leads its OWN process group (Prism c830d90e0be4: subprocess.run's timeout
    kills only bash, so a child that holds the pipe keeps the runner blocked and keeps running into the next suite).
    On timeout the whole group gets SIGTERM then SIGKILL; after a normal exit any straggler in the group is killed too,
    so no suite's children outlive it. Output goes to a temp file, never a pipe a grandchild can hold open."""
    with tempfile.TemporaryFile() as fh:
        p = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                             start_new_session=True)
        try:
            rc = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(p.pid)
            p.wait()
            rc = 124
        else:
            _kill_group(p.pid)   # stragglers a passing suite left behind
        fh.seek(0)
        return rc, fh.read().decode("utf-8", "replace")


def run_suite(name, lines, timeout_s, env):
    t0 = time.monotonic()
    mark = tempfile.mkdtemp(prefix="ci-public-suite-")
    done = os.path.join(mark, "done")
    try:
        if not lines:
            rc, out = 1, "ci-public: suite %s has no run: entries\n" % name
        else:
            rc, out = run_group(["bash", "-e", "-o", "pipefail", "-c", suite_script(lines, done)], env, timeout_s)
            if rc == 0 and not os.path.exists(done):
                rc = INCOMPLETE_RC
                out += "\nci-public: shell ended before every run: entry ran (exec / early exit); rc=0 is not a pass\n"
    finally:
        shutil.rmtree(mark, True)
    secs = round(time.monotonic() - t0, 1)
    tail = out[-3000:]
    if rc == 0:
        cls, marker = "pass", ""
    elif rc == 124:
        cls, marker = "timeout", ""
    else:
        m = HERMETIC_MARKERS.search(out)
        cls, marker = ("fail-hermeticity", m.group(0)) if m else ("fail-real", "")
    return {"suite": name, "rc": rc, "secs": secs, "class": cls, "marker": marker, "tail": tail}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("census", "gate"), required=True)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--timeout-s", type=int, default=900)
    ap.add_argument("--out", default="ci-public-results.jsonl")
    a = ap.parse_args(argv)
    if a.shards < 1 or not 0 <= a.shard < a.shards:
        print("ci-public: --shard %d is not in 0..%d" % (a.shard, a.shards - 1), file=sys.stderr)
        return 1
    suites = json.load(open(ROOT / ".github/ci/suites.json"))["suites"]
    if a.mode == "gate":
        names = [ln.strip() for ln in HERMETIC.read_text().splitlines()] if HERMETIC.exists() else []
        names = [n for n in names if n and not n.startswith("#")]
        if not names:
            print("ci-public gate: hermetic.txt is empty or missing; refusing to report green", file=sys.stderr)
            return 1
        missing = [n for n in names if not suites.get(n, {}).get("run")]
        if missing:
            print("ci-public gate: hermetic.txt names suites with no run: row: %s" % ", ".join(missing), file=sys.stderr)
            return 1
    else:
        names = sorted(n for n, r in suites.items() if r.get("run"))
    names = [n for i, n in enumerate(sorted(names)) if i % a.shards == a.shard]
    # Prism 2275dc532cf3: a gate shard that selects nothing is never a pass. --shards larger than the gate set (or a
    # bad --shard) would otherwise run 0 suites and exit 0, so every shard must carry at least one hermetic suite.
    if a.mode == "gate" and not names:
        print("ci-public gate shard %d/%d: 0 hermetic suites selected; refusing to report green" % (a.shard, a.shards),
              file=sys.stderr)
        return 1
    env = dict(os.environ)
    bad = 0
    with open(a.out, "w") as fh:
        for n in names:
            print("::group::%s" % n, flush=True)
            r = run_suite(n, suites[n]["run"], a.timeout_s, env)
            print(r["tail"][-1500:])
            print("::endgroup::", flush=True)
            print("ci-public %s: %s rc=%d %.1fs %s" % (n, r["class"], r["rc"], r["secs"], r["marker"]), flush=True)
            fh.write(json.dumps(r) + "\n")
            fh.flush()   # a shard killed mid-run keeps every finished suite's row
            bad += r["rc"] != 0
    print("ci-public %s shard %d/%d: %d suites, %d red" % (a.mode, a.shard, a.shards, len(names), bad))
    return 1 if (a.mode == "gate" and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
