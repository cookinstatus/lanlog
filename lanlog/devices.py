"""Device naming, MAC lookup, and view preferences.

Backed by two plain-text files so everything is editable by hand:

  ~/.config/lanlog/aliases    <ip> <nickname>
  ~/.config/lanlog/config     key = value view settings
"""

import os
import re
import subprocess

CONFIG_FILE = os.path.expanduser("~/.config/lanlog/config")
ALIAS_FILE = os.path.expanduser("~/.config/lanlog/aliases")

OUI_FILE = "/usr/share/nmap/nmap-mac-prefixes"

# Local (nmap) OUI database is incomplete and stale for some vendors. Fall back
# to an online lookup, which is broader but sends the OUI prefix to a third
# party. Off unless the user asks for it.
API_URL = "https://api.macvendors.com/{}"

MAC_CLEAN = re.compile(r"[^0-9a-fA-F]")


# --------------------------------------------------------------------------
# view config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # Show the vendor/hint suffix next to a device name.
    "show_kinds": "yes",
    # Hide devices that have never been seen talking to anything.
    "hide_idle": "no",
    # Include these comma-separated prefixes in `lanlog devices`.
    "only": "",
    # What goes in the device column: readable names, raw IPs, or both.
    # See LABEL_MODES below for why all three exist.
    "labels": "names",
    # How many rows of recent queries the live dashboard shows. Clamped to
    # 1-25: the dashboard shares an 80x24 terminal with the rest of the view,
    # so a large value pushes the other panels off screen.
    "query_lines": "8",
    # Live dashboard repaint interval in seconds. 1-30.
    "refresh": "1",
}

# Valid values for the non-numeric settings, checked in set_config so a typo is
# reported to whoever typed it rather than silently behaving as the default.
#
# `labels` is three-valued rather than a yes/no because a name-only view has a
# real failure mode: when the resolver cannot name a device, every row degrades
# to a bare IP and the display looks identical to a working one. `ip` is the
# honest baseline to compare against, and `both` is what you want when you need
# to map a name you can read back to the address you can paste into a command.
LABEL_MODES = ("names", "ip", "both")

CHOICES = {
    "show_kinds": ("yes", "no"),
    "hide_idle": ("yes", "no"),
    "labels": LABEL_MODES,
}

# Bounds for the numeric settings, so `lanlog config query_lines 500` cannot
# silently produce an unreadable dashboard.
INT_BOUNDS = {
    "query_lines": (1, 25),
    "refresh": (1, 30),
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip().lower()
                    if k in cfg:
                        cfg[k] = v.strip()
    except OSError:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write("# lanlog view settings. Managed by: lanlog config <key> <value>\n")
        for key, val in cfg.items():
            fh.write(f"{key} = {val}\n")
    return CONFIG_FILE


def set_config(key, value):
    cfg = load_config()
    if key not in DEFAULT_CONFIG:
        raise KeyError(key)
    # A bad value must be reported to the user who typed it instead of being
    # silently ignored later. Numeric settings are bounded; the rest are
    # checked against a fixed set of choices.
    if key in INT_BOUNDS:
        lo, hi = INT_BOUNDS[key]
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number {lo}-{hi}, got {value!r}")
        if not lo <= n <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}, got {n}")
        value = str(n)
    elif key in CHOICES:
        allowed = CHOICES[key]
        # Accept the y/n and 0/1 spellings the yes/no settings have always
        # tolerated, so `labels` does not become the first setting that rejects
        # a value the rest of the tool accepts.
        text = str(value).strip().lower()
        if text in ("y", "yes", "true", "1", "on"):
            text = "yes"
        elif text in ("n", "no", "false", "0", "off"):
            text = "no"
        if text not in allowed:
            raise ValueError(f"{key} must be one of {'/'.join(allowed)}, "
                             f"got {value!r}")
        value = text
    cfg[key] = value
    save_config(cfg)
    return cfg


def label_mode(cfg=None):
    """The active labels mode, normalised. Unknown values fall back to names."""
    cfg = load_config() if cfg is None else cfg
    mode = str(cfg.get("labels", "names")).strip().lower()
    return mode if mode in LABEL_MODES else "names"


def toggle_label_mode(cfg=None):
    """Advance labels to the next mode and persist it. Returns the new mode.

    This is the whole feature behind `lanlog toggle`: a name-only view that has
    silently degraded to bare IPs is indistinguishable from a working one, so
    the cheapest way to tell them apart is to flip to a mode where the
    difference is visible. Cycling covers all three modes from one action.
    """
    cfg = load_config() if cfg is None else cfg
    cur = label_mode(cfg)
    nxt = LABEL_MODES[(LABEL_MODES.index(cur) + 1) % len(LABEL_MODES)]
    set_config("labels", nxt)
    return nxt


def get_int(cfg, key, fallback=None):
    """Read a bounded int setting, falling back when the file holds junk."""
    lo, hi = INT_BOUNDS.get(key, (None, None))
    try:
        n = int(str(cfg.get(key, "")).strip())
    except (TypeError, ValueError):
        n = fallback if fallback is not None else int(DEFAULT_CONFIG[key])
    if lo is not None:
        n = max(lo, min(hi, n))
    return n


# --------------------------------------------------------------------------
# nicknames
# --------------------------------------------------------------------------

def set_nickname(ip, name):
    """Write a nickname for an IP into the aliases file, preserving comments."""
    lines = []
    try:
        with open(ALIAS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        pass

    entry = f"{ip} {name}"
    replaced = False
    out = []
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if stripped and stripped.split()[0] == ip:
            out.append(entry)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if not out or (out and out[-1].strip()):
            out.append("")
        out.append(entry)

    os.makedirs(os.path.dirname(ALIAS_FILE), exist_ok=True)
    with open(ALIAS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    return entry


def list_nicknames():
    return load_aliases()


def remove_nickname(ip):
    try:
        with open(ALIAS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return False
    out, removed = [], False
    for line in lines:
        stripped = line.split("#", 1)[0].strip()
        if stripped and stripped.split()[0] == ip:
            removed = True
            continue
        out.append(line)
    with open(ALIAS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    return removed


def load_aliases(path=ALIAS_FILE):
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    out[parts[0].strip()] = parts[1].strip()
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------
# MAC lookup
# --------------------------------------------------------------------------

def normalize_mac(text):
    """Accept aa:bb:cc:dd:ee:ff, aa-bb-..., aabbccddeeff, or a partial prefix.

    Returns (prefix_upper, formatted_full_or_None). The prefix is uppercased
    because the OUI files store it that way; comparing a lowercase prefix
    against them silently matches nothing.
    """
    hexdigits = MAC_CLEAN.sub("", text).lower()
    if len(hexdigits) < 6:
        return None, None
    prefix = hexdigits[:6].upper()
    full = hexdigits[:12] if len(hexdigits) >= 12 else None
    formatted = ":".join(prefix[i:i + 2] for i in range(0, 6, 2))
    return prefix, (":".join(full[i:i + 2] for i in range(0, 12, 2))
                    if full else None)


def local_vendor(prefix, oui_file=None):
    """Look a 6-digit prefix up in the local OUI database.

    Accepts both layouts: nmap's 'FC  FB  FB   Cisco...' and the IEEE/arp-scan
    'FC-FB-FB   (hex)  Cisco...' / 'FC:FB:FB   (base 16)'. The file is picked
    per machine (see discover.oui_path); a missing one is a miss, not an error.
    """
    from lanlog import discover

    oui_file = oui_file or discover.oui_path()
    if not oui_file:
        return None
    try:
        with open(oui_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # nmap:  "FC  FB  FB   Vendor"
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                key = parts[0].replace(":", "").replace("-", "").upper()
                if key == prefix:
                    return parts[1].strip()
                # IEEE/arp-scan: "FC-FB-FB   (hex)   Vendor"
                if key[:6] == prefix and "(" in parts[1]:
                    return parts[1].split(")", 1)[-1].strip()
    except OSError:
        pass
    return None


def online_vendor(mac):
    """Fallback lookup. Sends the OUI to a third-party API."""
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "6", API_URL.format(mac)],
            capture_output=True, text=True, check=False, timeout=8,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if not out or out.startswith("{") or "error" in out.lower():
        return None
    return out


def lookup_mac(text, use_online=True):
    """Return a dict describing what a MAC (or OUI prefix) belongs to."""
    prefix, full = normalize_mac(text)
    if not prefix:
        return {"error": f"'{text}' does not contain enough hex digits "
                         f"(need at least 6, e.g. a0:2d:13)"}

    result = {
        "input": text, "prefix": prefix, "mac": full,
        "local": local_vendor(prefix), "online": None, "source": "local",
    }
    if result["local"] is None:
        result["local"] = "(not in local OUI database)"
    if use_online:
        result["online"] = online_vendor(full or prefix)
        if result["online"]:
            result["source"] = "online"
    return result


def mac_for_ip(ip):
    """Find a device's MAC from the kernel neighbour table."""
    try:
        out = subprocess.run(["ip", "neigh", "show", ip],
                             capture_output=True, text=True,
                             check=False).stdout
    except (FileNotFoundError, OSError):
        return None
    m = re.search(r"lladdr\s+([0-9a-fA-F:]{17})", out)
    return m.group(1).lower() if m else None
