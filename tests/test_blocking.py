"""Tests for the blocked-domain feature: the [2] submenu and the query mark.

Two things are guarded here, both of which fail silently rather than loudly:

  1. The [2] submenu state machine. If digit 2 regressed to seeding the command
     bar, the submenu would be unreachable; if Esc inside it quit the dashboard,
     opening it by accident would kill the session.

  2. blocking.blocked_by's ancestor walk. A plain set membership test reports
     "not blocked" for `ads.doubleclick.net` when `doubleclick.net` is listed,
     which is the wrong answer for every subdomain of every listed domain -- and
     wrong in the direction that looks like good news.

Run: .venv/bin/python test_blocking.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lanlog import blocking, keys as k  # noqa: E402


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    return ok


def feed(ks, data):
    """Push bytes through the real Keys.poll dispatch path."""
    import select

    ks._queue = [data]
    old_select, old_read = select.select, os.read
    select.select = lambda r, w, x, t: (r, w, x)
    os.read = lambda fd, n: ks._queue.pop(0) if ks._queue else b""
    try:
        ks.poll(0)
    finally:
        select.select, os.read = old_select, old_read


def make_keys():
    ks = k.Keys.__new__(k.Keys)
    ks.tty = True
    ks._saved = None
    ks.buffer = ""
    ks.active = False
    ks.pending = None
    ks.last_digit = None
    ks.menu = None
    ks.menu_key = None
    ks.stream = open(os.devnull)
    ks.fd = 0
    return ks


def main():
    passed = True

    print("[2] submenu:")
    ks = make_keys()
    feed(ks, b"2")
    passed &= check("2 opens the submenu", ks.menu, "domains")
    passed &= check("nothing queued yet", ks.pending, None)
    passed &= check("bar not focused", ks.active, False)
    passed &= check("2 not in QUICK", "2" in k.QUICK, False)

    feed(ks, b"2")
    passed &= check("then 2 -> blocked", ks.pending, "blocked")
    passed &= check("submenu closed", ks.menu, None)

    ks = make_keys()
    feed(ks, b"2")
    feed(ks, b"1")
    passed &= check("then 1 -> domains", ks.pending, "domains")

    ks = make_keys()
    feed(ks, b"2")
    feed(ks, b"\x1b")
    passed &= check("Esc backs out", ks.menu, None)
    passed &= check("Esc does NOT queue a report", ks.pending, None)

    ks = make_keys()
    feed(ks, b"2")
    feed(ks, b"x")
    passed &= check("a letter is ignored in the submenu", ks.menu, "domains")
    passed &= check("and queues nothing", ks.pending, None)

    print("\nother quick keys are unaffected:")
    for digit, want in (("1", "devices"), ("3", "shared"), ("4", "third-party")):
        ks = make_keys()
        feed(ks, f"{digit}\n".encode())
        passed &= check(f"{digit} still runs {want}", ks.pending, want)

    ks = make_keys()
    feed(ks, b"7")
    passed &= check("7 still opens settings", ks.menu, "config")
    ks = make_keys()
    feed(ks, b"7")
    feed(ks, b"1")
    feed(ks, b"2")
    feed(ks, b"0")
    feed(ks, b"\n")
    passed &= check("settings menu still completes",
                    ks.pending, "config query_lines 20")

    print("\nsubdomain matching:")
    table = frozenset({"doubleclick.net", "www.googletagmanager.com",
                       "example.co.uk"})
    passed &= check("exact match", blocking.blocked_by("doubleclick.net", table),
                    "doubleclick.net")
    passed &= check("subdomain reports its listed parent",
                    blocking.blocked_by("ads.doubleclick.net", table),
                    "doubleclick.net")
    passed &= check("deep subdomain",
                    blocking.blocked_by("a.b.c.doubleclick.net", table),
                    "doubleclick.net")
    passed &= check("multi-label suffix",
                    blocking.blocked_by("ads.example.co.uk", table),
                    "example.co.uk")
    passed &= check("unrelated is not blocked",
                    blocking.blocked_by("github.com", table), None)
    passed &= check("case is ignored",
                    blocking.blocked_by("ADS.DoubleClick.NET", table),
                    "doubleclick.net")
    passed &= check("trailing dot is ignored",
                    blocking.blocked_by("doubleclick.net.", table),
                    "doubleclick.net")
    passed &= check("empty name", blocking.blocked_by("", table), None)
    # A bare TLD must never match: gravity has no TLDs, and matching one would
    # report unrelated domains as blocked.
    passed &= check("TLD-only is not blocked", blocking.blocked_by("com", table),
                    None)

    print("\nunreadable blocklist reports unknown, not allowed:")
    saved = os.environ.get("LANLOG_GRAVITY_DB")
    os.environ["LANLOG_GRAVITY_DB"] = "/nonexistent/gravity.db"
    try:
        # "unknown" is the whole point: claiming "allowed" when the blocklist
        # cannot be read asserts that nothing is blocked.
        passed &= check("status is unknown", blocking.status("doubleclick.net"),
                        ("unknown", None))
        passed &= check("domains() is empty", blocking.domains(), frozenset())
    finally:
        if saved is None:
            os.environ.pop("LANLOG_GRAVITY_DB", None)
        else:
            os.environ["LANLOG_GRAVITY_DB"] = saved

    ks.stream.close()
    print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
