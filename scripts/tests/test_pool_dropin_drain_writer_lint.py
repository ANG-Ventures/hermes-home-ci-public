"""pool_dropin_drain_writer_lint (t_92988772): the tree is clean, and the lint actually catches each writer shape."""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import pool_dropin_drain_writer_lint as lint  # noqa: E402


def test_this_tree_has_no_dropin_drain_writer():
    r = subprocess.run([sys.executable, str(SCRIPTS / "pool_dropin_drain_writer_lint.py"), str(REPO)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 writer(s)" in r.stdout


WRITERS = [
    # the 2026-10-15 shape, as a skill / agent would type it
    "printf '[Service]\\nEnvironment=POOL_SIZE=0\\n' | sudo tee /etc/systemd/system/ci-runner-pool@$p.service.d/zz-drain.conf",
    'echo "Environment=POOL_SIZE=0" > "$d/zz-drain.conf"',
    'Path(d, "zz-drain.conf").write_text("[Service]\\n" + body)',
    'atomic_put(host, f"{d}/zz-drain.conf", body)',
    'with open(f"/etc/systemd/system/ci-runner-pool@{i}.service.d/zz-drain.conf", "w") as fh:',
    'sudo cp /tmp/x.conf /etc/systemd/system/ci-runner-pool@p0.service.d/zz-drain.conf',
    'BODY = "[Service]\\nEnvironment=POOL_SIZE=0\\n"',
]
READERS = [
    'legacy = f"{d}/zz-drain.conf"',
    'rc, out, err = hrun(host, f"if test -f {shlex.quote(legacy)}; then echo present; else echo absent; fi", 30)',
    '# a drop-in with Environment=POOL_SIZE=0 does nothing to a running pool',
    'if base == "zz-drain.conf" and k.strip() == "POOL_SIZE" and v == "0":',
    'Environment=POOL_SIZE=8',
    'Environment=POOL_SIZE=05',
    'atomic_put(host, drain_file, "0\\n", mode="644")',              # the ONE mechanism: a drain FILE
    'printf "Environment=POOL_SIZE=%s\\n" "$SIZE" | sudo tee "$D/20-size.conf"',  # enroll sizing, not a drain
]


@pytest.mark.parametrize("line", WRITERS)
def test_each_writer_shape_is_caught(line):
    assert lint.is_writer(line), line


@pytest.mark.parametrize("line", READERS)
def test_reads_and_legacy_detection_are_not_writers(line):
    assert not lint.is_writer(line), line


def test_mutation_a_planted_writer_turns_the_tree_red(tmp_path):
    """Mutation arm: the same scan over a tree with ONE planted writer goes red and names it."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "quiesce.sh").write_text(
        "#!/bin/bash\nfor p in $(pools); do\n"
        "  echo 'Environment=POOL_SIZE=0' | sudo tee /etc/systemd/system/ci-runner-pool@$p.service.d/zz-drain.conf\n"
        "done\n")
    (tmp_path / "scripts" / "tests").mkdir()
    (tmp_path / "scripts" / "tests" / "fixture.sh").write_text("#!/bin/bash\necho Environment=POOL_SIZE=0 > x\n")
    (tmp_path / "skills-shared" / "devops" / "s").mkdir(parents=True)
    (tmp_path / "skills-shared" / "devops" / "s" / "SKILL.md").write_text("Environment=POOL_SIZE=0\n")  # prose
    assert lint.main([str(tmp_path)]) == 1
    hits = lint.scan(tmp_path)
    assert [h.split(":")[0] for h in hits] == ["scripts/quiesce.sh"], hits
