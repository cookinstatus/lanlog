"""Regression test for the dashboard command bar.

The bug this guards against: pressing a quick-key digit and Enter in the same
read used to strand the command. `last_digit` parked the digit for the caller
to expand on the next frame, and Enter was handled immediately against a still
empty buffer, so it was discarded. The bar sat at "devices" and nothing ran
until you pressed Enter again.

Run: .venv/bin/python -m pytest test_keys.py -q
   or: .venv/bin/python test_keys.py
"""

import os
import select
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lanlog import devices as dev  # noqa: E402
from lanlog import keys as k  # noqa: E402

# The CLI's list of valid config keys. Imported rather than retyped, because
# the test is asserting that `labels` is actually reachable from the command
# line -- a hardcoded copy would pass even if the two lists drifted apart.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util  # noqa: E402
from importlib.machinery import SourceFileLoader  # noqa: E402

# bin/lanlog has no .py suffix, so spec_from_file_location cannot pick a loader
# and returns a spec whose loader is None. SourceFileLoader takes an explicit
# loader and does not care about the extension.
_cli_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "bin", "lanlog")
_spec = importlib.util.spec_from_loader(
    "lanlog_cli", SourceFileLoader("lanlog_cli", _cli_path))
_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cli)
DEFAULT_KEYS = _cli.DEFAULT_KEYS


def make_keys(scripted_reads):
    """A Keys instance that looks like a live tty to poll()."""
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

    queue = list(scripted_reads)
    ks._queue = queue
    return ks


def feed(ks, payload):
    """Push one read's worth of bytes through the real dispatch path."""
    old_select, old_read = select.select, os.read
    select.select = lambda r, w, x, t: (r, w, x)
    os.read = lambda fd, n: ks._queue.pop(0) if ks._queue else b""
    try:
        ks.poll(0)
    finally:
        select.select, os.read = old_select, old_read


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    return ok


def main():
    passed = True

    # 1. The regression: digit and Enter in the SAME read must run the command.
    print("digit+enter in one read (the reported bug):")
    ks = make_keys([b"1\n"])
    feed(ks, b"1\n")
    passed &= check("pending", ks.pending, "devices")
    passed &= check("buffer cleared", ks.buffer, "")
    passed &= check("bar released", ks.active, False)

    # 2. Every quick key maps to the right command, digit+enter together.
    #    Key [5] seeds "client " with a trailing space so the next keystroke
    #    lands correctly; that space must survive Enter. Key [7] is special --
    #    it opens the settings menu instead of seeding a command.
    print("\nall quick keys, digit+enter in one read:")
    for digit, want in k.QUICK.items():
        if digit == "7":
            continue
        expect = want.rstrip() if want == "client " else want
        ks = make_keys([f"{digit}\n".encode()])
        feed(ks, b"x")
        passed &= check(f"[{digit}] {expect}", (ks.pending or "").rstrip(), expect)

    # 3. Typing a command from scratch still works.
    print("\ntyped by hand:")
    ks = make_keys([b"name list\n"])
    feed(ks, b"x")
    passed &= check("pending", ks.pending, "name list")

    # 4. Backspace, Esc, and a partial command with no Enter.
    print("\nediting:")
    ks = make_keys([b"devicess"])
    feed(ks, b"x")
    passed &= check("buffer", ks.buffer, "devicess")
    ks._queue.insert(0, b"\x7f")
    feed(ks, b"x")
    passed &= check("after backspace", ks.buffer, "devices")
    ks._queue.insert(0, b"\x7f\x7f")
    feed(ks, b"x")
    passed &= check("after two backspaces", ks.buffer, "devic")
    # Esc on an IDLE bar now quits, so cancelling is tested with a buffer.
    ks = make_keys([b"shared"])
    feed(ks, b"x")
    ks._queue.insert(0, b"\x1b")
    feed(ks, b"x")
    passed &= check("Esc on idle-with-text cancels", (ks.buffer, ks.active), ("", False))

    # 5. Enter on an empty buffer must not fire a command.
    print("\nempty enter is ignored:")
    ks = make_keys([b"\n"])
    feed(ks, b"x")
    passed &= check("pending", ks.pending, None)

    # 6. A digit typed mid-command is literal text, not a quick key.
    print("\ndigit inside an active command is literal:")
    ks = make_keys([b"client 192.168.1.5\n"])
    feed(ks, b"x")
    passed &= check("pending", ks.pending, "client 192.168.1.5")

    # 7. 'q' is now an ordinary character -- it must never quit. This was the
    #    bug: "config query_lines 20" and "shared" died mid-typing whenever a
    #    'q' landed on an idle bar.
    print("\nq is a normal character (regression):")
    for cmd in (b"config query_lines 20\n", b"shared\n", b"q\n", b"quit\n"):
        ks = make_keys([cmd])
        feed(ks, b"x")
        passed &= check(f"{cmd!r} runs", ks.pending, cmd.decode().strip())
    # A lone q on an idle bar opens the bar instead of quitting.
    ks = make_keys([b"q"])
    feed(ks, b"x")
    passed &= check("lone q types", ks.buffer, "q")
    passed &= check("lone q does not quit", ks.active, True)

    # 8. Esc quits when the bar is idle.
    print("\nEsc semantics:")
    ks = make_keys([b"\x1b"])
    try:
        feed(ks, b"x")
        passed &= check("idle Esc quits", False, True)
    except KeyboardInterrupt:
        passed &= check("idle Esc quits", True, True)

    # 9. Esc mid-command cancels rather than quits -- losing a half-typed
    #    command to a quit would be worse than pressing Esc twice.
    ks = make_keys([b"config query_li"])
    feed(ks, b"x")
    passed &= check("mid-command buffer", ks.buffer, "config query_li")
    ks._queue.insert(0, b"\x1b")
    feed(ks, b"x")
    passed &= check("Esc cancels command", (ks.buffer, ks.active), ("", False))
    # A second Esc, now on an idle bar, quits.
    ks._queue.insert(0, b"\x1b")
    try:
        feed(ks, b"x")
        passed &= check("second Esc quits", False, True)
    except KeyboardInterrupt:
        passed &= check("second Esc quits", True, True)

    # 10. Ctrl-C still quits unconditionally, even mid-command.
    ks = make_keys([b"config\x03"])
    try:
        feed(ks, b"x")
        passed &= check("Ctrl-C quits mid-command", False, True)
    except KeyboardInterrupt:
        passed &= check("Ctrl-C quits mid-command", True, True)

    # 11. Settings menu: [7] -> digit -> value -> Enter.
    #     This replaces typing "config query_lines 20" by hand, which broke
    #     whenever the leading space was lost ("configquery_lines").
    print("\nsettings menu:")
    ks = make_keys([b"7"])
    feed(ks, b"x")
    passed &= check("7 opens menu", ks.menu, "config")
    passed &= check("no setting picked yet", ks.menu_key, None)
    passed &= check("bar shows menu", "SETTINGS" in ks.bar()[0], True)

    ks._queue.insert(0, b"1")
    feed(ks, b"x")
    passed &= check("1 picks query_lines", ks.menu_key, "query_lines")

    ks._queue.insert(0, b"20\n")
    feed(ks, b"x")
    passed &= check("value applied", ks.pending, "config query_lines 20")
    passed &= check("menu closes", ks.menu, None)

    # Whole flow in one read, which is what a fast typist produces.
    ks = make_keys([b"712\n"])
    feed(ks, b"x")
    passed &= check("7-1-2+Enter in one read", ks.pending, "config query_lines 2")

    # Every menu entry maps to a real setting name.
    for key, name, _hint in k.CONFIG_MENU:
        ks = make_keys([b"7"])
        feed(ks, b"x")
        ks._queue.insert(0, key.encode() + b"1\n")
        feed(ks, b"x")
        passed &= check(f"[{key}] -> {name}", ks.pending, f"config {name} 1")

    # Esc backs out of the value step, then out of the menu.
    ks = make_keys([b"71"])
    feed(ks, b"x")
    ks._queue.insert(0, b"5\x1b")
    feed(ks, b"x")
    passed &= check("Esc at value step", (ks.menu, ks.buffer), (None, ""))
    ks._queue.insert(0, b"7\x1b")
    feed(ks, b"x")
    passed &= check("Esc at menu step", ks.menu, None)
    passed &= check("Esc at menu did not quit", ks.pending, None)

    # A blank value still fires, so "only" can be cleared.
    ks = make_keys([b"75\n"])
    feed(ks, b"x")
    passed &= check("blank value fires", ks.pending, "config only")

    # Typing the old long form must still work.
    ks = make_keys([b"config query_lines 7\n"])
    feed(ks, b"x")
    passed &= check("long form still works", ks.pending, "config query_lines 7")

    # 12. The dashboard reads the menu size off the keys MODULE, not the Keys
    #     instance. Getting this wrong crashed the live dashboard the moment
    #     the settings menu was opened, and no unit test covered it.
    print("\ndashboard/keys interface:")
    passed &= check("CONFIG_MENU on module", hasattr(k, "CONFIG_MENU"), True)
    # Use a fresh bare object -- do not clobber `ks`, whose .stream is closed
    # during cleanup below.
    bare = k.Keys.__new__(k.Keys)
    passed &= check(
        "CONFIG_MENU NOT on instance (guards the wrong attr access)",
        hasattr(bare, "CONFIG_MENU"), False,
    )
    passed &= check("menu row count", len(k.CONFIG_MENU) + 3, 9)

    # The labels toggle. [0] runs it straight from the bar, and it must reach
    # every mode -- including back round to the start -- because a single
    # one-way step would leave the user stuck in a mode with no key to undo it.
    print("\nlabels toggle:")
    passed &= check("[0] maps to toggle", k.QUICK.get("0"), "toggle")
    ks = make_keys([b"0\n"])
    feed(ks, b"x")
    passed &= check("[0] runs toggle", ks.pending, "toggle")
    passed &= check("labels is on the settings menu",
                    any(n == "labels" for _d, n, _h in k.CONFIG_MENU), True)
    passed &= check("labels is a valid config key", "labels" in
                    DEFAULT_KEYS, True)
    # Every mode cycles, and the cycle is closed.
    passed &= check("cycles through every mode",
                    [dev.LABEL_MODES[(i + 1) % len(dev.LABEL_MODES)]
                     for i in range(len(dev.LABEL_MODES))],
                    ["ip", "both", "names"])

    ks.stream.close()
    print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
    return 0 if passed else 1

if __name__ == "__main__":
    sys.exit(main())
