#!/usr/bin/env python3
"""Plain-python tests for bot/sync_service.py (V2 sync-service).

SAFE: does NOT touch real git, network, or secrets. Checks:
  * import is side-effect-free (the module imports; main/run_loop/sync_once exist);
  * pure logic for detecting changed rule files (_paths_touch_rules);
  * run_loop with a fake sync_fn: pull count, fake-sleep between iterations,
    and that an exception inside an iteration does NOT crash the loop (fail-safe).
Run:  python3 scripts/test_sync_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import sync_service as ss  # noqa: E402

_FAILS = []


def check(cond, label):
    if cond:
        print(f"PASS: {label}")
    else:
        print(f"FAIL: {label}")
        _FAILS.append(label)


def main() -> int:
    # --- import surface ---
    check(callable(ss.main) and callable(ss.run_loop) and callable(ss.sync_once),
          "imports: main/run_loop/sync_once present")

    # --- pure rule-path detection (no git) ---
    check(ss._paths_touch_rules(["CLAUDE.md"]) is True, "rule-path: CLAUDE.md detected")
    check(ss._paths_touch_rules(["claude/skills/foo/SKILL.md"]) is True,
          "rule-path: claude/skills/* detected")
    check(ss._paths_touch_rules(["claude/hooks/guard.py"]) is True,
          "rule-path: claude/hooks/* detected")
    check(ss._paths_touch_rules(["bot/foo.py", "README.md", "docs/x.md"]) is False,
          "rule-path: ordinary code/docs NOT flagged")
    check(ss._paths_touch_rules(["claudeXskills/y"]) is False,
          "rule-path: lookalike prefix NOT flagged")
    check(ss._paths_touch_rules([]) is False, "rule-path: empty list -> False")

    # --- run_loop with fake sync_fn (no real git) ---
    slept = []
    fake_sleep = lambda s: slept.append(s)  # noqa: E731

    # 3 iterations, never pulled -> pulls=0, sleeps between iterations (2 sleeps).
    res = ss.run_loop(max_iterations=3, poll_interval=0.0, sleep=fake_sleep,
                      sync_fn=lambda cwd=None: {"pulled": False})
    check(res == {"iterations": 3, "pulls": 0}, "run_loop: 3 iters, 0 pulls")
    check(len(slept) == 2, "run_loop: slept between iterations only (N-1)")

    # Counts pulls when sync_fn reports pulled.
    seq = iter([
        {"pulled": True, "rules_changed": False},
        {"pulled": False},
        {"pulled": True, "rules_changed": True},
    ])
    res = ss.run_loop(max_iterations=3, poll_interval=0.0, sleep=lambda s: None,
                      sync_fn=lambda cwd=None: next(seq))
    check(res == {"iterations": 3, "pulls": 2}, "run_loop: counts 2 pulls of 3")

    # Exception inside an iteration must NOT crash the loop (fail-safe).
    def boom(cwd=None):
        raise RuntimeError("simulated git failure")

    res = ss.run_loop(max_iterations=2, poll_interval=0.0, sleep=lambda s: None,
                      sync_fn=boom)
    check(res == {"iterations": 2, "pulls": 0}, "run_loop: survives raising sync_fn")

    # --- sync_once branch logic via monkeypatched _git (no real git/network) ---
    import logging
    import tempfile

    def make_fake_git(*, branch="main", fetch_rc=0, pull_rc=0,
                      before="aaaaaaa", after="bbbbbbb", diff_out="", fetch_stderr=""):
        state = {"head": 0}

        def _fake(args, cwd=None, timeout=120.0):
            a = list(args)
            if a[:2] == ["rev-parse", "--abbrev-ref"]:
                return (0, branch, "")
            if a[:1] == ["rev-parse"]:               # rev-parse HEAD (called twice)
                state["head"] += 1
                return (0, before if state["head"] == 1 else after, "")
            if a[:1] == ["fetch"]:
                return (fetch_rc, "", fetch_stderr)
            if a[:2] == ["pull", "--ff-only"]:
                return (pull_rc, "", "")
            if a[:2] == ["diff", "--name-only"]:
                return (0, diff_out, "")
            return (0, "", "")
        return _fake

    orig_git = ss._git
    orig_sentinel = ss._RULES_SENTINEL
    tmp_sentinel = str(Path(tempfile.mkdtemp(prefix="aios_sync_test_")) / "rules-changed")
    try:
        ss._RULES_SENTINEL = tmp_sentinel

        ss._git = make_fake_git(branch="HEAD")
        r = ss.sync_once()
        check(r["error"] == "no-branch" and not r["pulled"],
              "sync_once: detached HEAD -> no-branch")

        ss._git = make_fake_git(fetch_rc=1)
        r = ss.sync_once()
        check(r["error"] == "fetch-failed" and not r["fetched"] and not r["pulled"],
              "sync_once: fetch failure -> fetch-failed")

        if Path(tmp_sentinel).exists():
            Path(tmp_sentinel).unlink()
        ss._git = make_fake_git(pull_rc=1)
        r = ss.sync_once()
        check(r["error"] == "pull-ff-only-skipped" and not r["pulled"],
              "sync_once: non-ff pull -> skipped (safe no-op)")
        check(not Path(tmp_sentinel).exists(),
              "sync_once: skipped pull writes NO rules sentinel")

        ss._git = make_fake_git(before="aaa", after="bbb", diff_out="CLAUDE.md")
        r = ss.sync_once()
        check(r["pulled"] and r["rules_changed"],
              "sync_once: ff pull touching a rule file -> rules_changed")
        check(Path(tmp_sentinel).exists(), "sync_once: rule change writes sentinel")

        if Path(tmp_sentinel).exists():
            Path(tmp_sentinel).unlink()
        ss._git = make_fake_git(before="aaa", after="ccc", diff_out="bot/handlers.py")
        r = ss.sync_once()
        check(r["pulled"] and not r["rules_changed"],
              "sync_once: ff pull of ordinary code -> no rule change")
        check(not Path(tmp_sentinel).exists(), "sync_once: ordinary pull writes NO sentinel")

        # SECURITY.md §0: a token in git stderr must NEVER reach the logs.
        TOKEN = "TELEGRAM-LOOKALIKE-1234567890ABCDEF"  # placeholder, not a real secret
        captured = []

        class _Cap(logging.Handler):
            def emit(self, rec):
                captured.append(self.format(rec))

        h = _Cap()
        ss.log.addHandler(h)
        ss.log.setLevel(logging.DEBUG)
        try:
            ss._git = make_fake_git(before="aaa", after="bbb", diff_out="bot/x.py",
                                    fetch_stderr="fatal: https://x:%s@github.com/r.git" % TOKEN)
            ss.run_loop(max_iterations=1, poll_interval=0.0, sleep=lambda s: None,
                        sync_fn=ss.sync_once)
        finally:
            ss.log.removeHandler(h)
        check(all(TOKEN not in line for line in captured),
              "sync_once/run_loop: token from git stderr never logged (SECURITY.md §0)")
    finally:
        ss._git = orig_git
        ss._RULES_SENTINEL = orig_sentinel

    print()
    if _FAILS:
        print(f"RESULT: {len(_FAILS)} FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
