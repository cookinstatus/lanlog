"""Tests for the `labels` display setting: names / ip / both.

Why this exists. The reported symptom was "the readable names are gone, it is
all IPs again". The cause was not cosmetic -- `identify.py` had the router
hardcoded to 192.168.1.1 while the real gateway was 192.168.1.254, and `dig`'s
error text (";; communications error to ...") was being persisted into the name
cache as if it were a hostname. Everything downstream then reported a screen of
bare IPs, which is exactly what a healthy name view degrades into, so there was
no way to tell the two apart from the display.

These cover both halves: the resolver must never produce a name that is not a
name, and the three label modes must render what they claim.

Run: .venv/bin/python test_labels.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lanlog import report  # noqa: E402
from lanlog import devices as dev  # noqa: E402
from lanlog import identify  # noqa: E402

passed = True


def check(label, got, want):
    global passed
    ok = got == want
    passed = passed and ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}"
          + ("" if ok else f" (wanted {want!r})"))


# The three label modes, over one device that resolved and one that did not.
NAMES = {
    "192.168.1.86": ("VivintSmarthub-B2A7B6 (Vivint smart hub)", "router"),
    "192.168.1.163": ("Android_6af7fe4ca3174eda862804ee1471f213 (Android device)",
                      "mdns"),
    "192.168.1.141": ("192.168.1.141 (Reliance Communications)", "vendor"),
}
UNRESOLVED = "192.168.1.200"


print("the resolver never returns resolver chatter as a name:")
for junk in (";; communications error to 192.168.1.254#53: timed out",
             ";; no servers could be reached",
             ";; connection timed out; no servers could be reached",
             "server failure"):
    check(f"rejects {junk[:34]!r}", identify._clean_hostname(junk), None)

print("\na good hostname still survives the cleaner:")
check("strips the ISP suffix",
      identify._clean_hostname("09AA01AC421807Z0.attlocal.net."),
      "09AA01AC421807Z0")
check("keeps a plain name", identify._clean_hostname("fedora"), "fedora")
check("empty stays empty", identify._clean_hostname(""), None)

print("\nthe router is detected, not hardcoded:")
from lanlog import config as _lcfg  # noqa: E402

check("_router() returns something", bool(identify._router()), True)
check("default_gateway parses a route",
      _lcfg.default_gateway().count(".") == 3
      or _lcfg.default_gateway() == "", True)
# The specific regression: the old constant was 192.168.1.1, which answers no
# DNS, while the working gateway is 192.168.1.254.
check("not the dead 192.168.1.1", identify._router() != "192.168.1.1", True)

print("\nlabel modes, for a device that resolved:")
row = {"ip": "192.168.1.86"}
check("names", report._label(row, NAMES, "names"),
      "VivintSmarthub-B2A7B6 (Vivint smart hub)")
check("ip", report._label(row, NAMES, "ip"), "192.168.1.86")
check("both", report._label(row, NAMES, "both"),
      "VivintSmarthub-B2A7B6 (Vivint smart hub) (192.168.1.86)")

print("\nlabel modes, for a device that did NOT resolve:")
row = {"ip": UNRESOLVED}
# This is the reported failure, rendered. It must be legible as an address in
# every mode rather than blank or something invented.
check("names falls back to the IP", report._label(row, None, "names"), UNRESOLVED)
check("ip", report._label(row, None, "ip"), UNRESOLVED)
check("both has no name to pair, so just the IP",
      report._label(row, None, "both"), UNRESOLVED)

print("\nthe vendor fallback does not repeat its own address:")
# identify() renders a vendor-only device as "1.2.3.4 (Vendor)". Appending the
# address again produced "1.2.3.4 (Vendor) (1.2.3.4)".
row = {"ip": "192.168.1.141"}
check("both passes an address-leading name through",
      report._label(row, NAMES, "both"),
      "192.168.1.141 (Reliance Communications)")

print("\ntruncation keeps the address in 'both' mode:")
# Plain [:n] truncation cut the address off the end, which is the one half of
# this mode that exists to be seen.
long_label = ("Android_6af7fe4ca3174eda862804ee1471f213 (Android device) "
              "(192.168.1.163)")
fitted = report._fit(long_label, 44)
check("fits the width", len(fitted) <= 44, True)
check("address survives", fitted.endswith("(192.168.1.163)"), True)
check("short labels untouched", report._fit("192.168.1.86", 44), "192.168.1.86")
check("a name with no tail group still truncates",
      len(report._fit("a" * 60, 20)) <= 20, True)

print("\nconfig validation:")
check("labels accepts each mode",
      [dev.label_mode({"labels": m}) for m in dev.LABEL_MODES],
      list(dev.LABEL_MODES))
check("unknown mode falls back to names",
      dev.label_mode({"labels": "banana"}), "names")
for good in ("names", "ip", "both", "IP", " Both "):
    check(f"{good!r} normalises to a valid mode",
          good.strip().lower() in dev.LABEL_MODES, True)

# The write path is what really validates, and it writes the real config file,
# so it is pointed at a throwaway one for this block and restored afterwards.
# Without this the test would rewrite the user's live display settings.
_real_cfg = dev.CONFIG_FILE
_tmp_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".test-labels-config")
dev.CONFIG_FILE = _tmp_cfg
try:
    # A typo must raise rather than silently behaving as the default: the whole
    # point of the setting is that you can trust what the display is showing.
    try:
        dev.set_config("labels", "banana")
        check("set_config rejects a typo", "no error", "ValueError")
    except ValueError:
        check("set_config rejects a typo", "ValueError", "ValueError")
    except KeyError:
        check("set_config rejects a typo", "KeyError", "ValueError")

    # Each real mode round-trips through the file.
    for mode in dev.LABEL_MODES:
        dev.set_config("labels", mode)
        check(f"{mode} round-trips", dev.label_mode(dev.load_config()), mode)
    # Case and stray whitespace are normalised rather than rejected, because the
    # rest of the tool has always tolerated those spellings for yes/no.
    dev.set_config("labels", " IP ")
    check("' IP ' normalises on write", dev.label_mode(dev.load_config()), "ip")
    # And the cycle is closed: three toggles from any start returns home.
    for start in dev.LABEL_MODES:
        dev.set_config("labels", start)
        for _ in range(len(dev.LABEL_MODES)):
            dev.toggle_label_mode()
        check(f"toggle returns to {start}", dev.label_mode(dev.load_config()),
              start)
    # The existing yes/no settings must keep accepting the spellings they always
    # took, or `labels` becomes the first setting that rejects them.
    dev.set_config("show_kinds", "1")
    check("show_kinds still takes 1", dev.load_config()["show_kinds"], "yes")
    dev.set_config("show_kinds", "off")
    check("show_kinds still takes off", dev.load_config()["show_kinds"], "no")
finally:
    dev.CONFIG_FILE = _real_cfg
    if os.path.exists(_tmp_cfg):
        os.unlink(_tmp_cfg)

print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
sys.exit(0 if passed else 1)