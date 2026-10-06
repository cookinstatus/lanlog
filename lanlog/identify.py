"""Turn bare IP addresses into something a human recognises.

Three sources, tried in order of how readable the result is:

1. mDNS / Avahi -- what the device calls itself on the LAN ("Android.local").
   Fast and local, but only works for devices that advertise it.
2. The router's reverse DNS -- the DHCP hostname the router handed out
   ("myprinter.home.arpa"). Covers most consumer gear, which does not
   advertise mDNS.
3. A user alias file -- your own name for a device ("front door cam"). Always
   wins, because it is the only source that knows what you call it.

Router PTR is the workhorse. Pi-hole itself returns nothing for reverse
lookups unless dns.revServers is configured, so querying it directly is a dead
end -- it is not a bug in this tool.
"""

import os
import re
import subprocess
import time

from lanlog import config as lanconfig

ALIAS_FILE = os.environ.get(
    "LANLOG_ALIASES", os.path.expanduser("~/.config/lanlog/aliases")
)

# Router that hands out DHCP leases. Its reverse DNS knows the lease names.
# Resolved lazily: reading it at import time would freeze the detection result
# before `lanlog setup` had a chance to write the config file.
def _router():
    return lanconfig.get("router") or os.environ.get("LANLOG_ROUTER", "")

ROUTER = os.environ.get("LANLOG_ROUTER", "")

# ISP suffixes that carry no information: strip them, keep the useful part.
# Covers common consumer-router and mDNS naming so this is not AT&T-specific.
_NOISE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa",
                   ".localdomain", ".attlocal.net", ".att.net", ".fios")

# Router-generated names embed a MAC suffix (ATT_4991_f91139, amazon-d4682f3b8).
# These prefixes are more useful than the random tail.
_HINT_PREFIXES = {
    "vivintsmarthub": "Vivint smart hub",
    "amazon": "Amazon device",
    "android": "Android device",
    "iphone": "Apple iPhone",
    "ipad": "Apple iPad",
    "apple": "Apple device",
    "brwdc": "Brother printer",
    "hp": "HP device",
    "canon": "Canon device",
    "epson": "Epson device",
    "chromecast": "Chromecast",
    "firestick": "Amazon Fire TV",
    "roku": "Roku device",
    "playstation": "PlayStation",
    "xbox": "Xbox",
    "esp": "ESP/IoT device",
    "espressif": "ESP/IoT device",
    "shelly": "Shelly device",
    "tasmota": "Tasmota device",
    "sonos": "Sonos speaker",
    "nest": "Nest device",
    "ring": "Ring device",
    "roku": "Roku device",
    "printer": "Printer",
    "laptop": "Laptop",
    "desktop": "Desktop",
}


def load_aliases(path=ALIAS_FILE):
    """User-defined names. Format: <ip> <name>  (# comment). Wins over all."""
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


def _clean_hostname(name):
    """Strip ISP suffix and the router's random hex tail."""
    if not name:
        return None
    name = name.strip().rstrip(".")

    # Resolver chatter, not a hostname. `dig` writes its failure text to stdout
    # in some modes, and that string was being persisted as a device name --
    # it showed up in the device list as a literal
    # ";; communications error to 192.168.1.254#53: timed out". Anything
    # starting with ';' is a dig/dnsutils comment by convention, so reject the
    # whole class rather than matching this one message. The other resolvers'
    # wording is covered too, in case the router is swapped for one that
    # answers in a different dialect.
    if name.startswith(";"):
        return None
    low_all = name.lower()
    for marker in ("communications error", "no servers could be reached",
                   "connection timed out", "server failure"):
        if marker in low_all:
            return None

    low = name.lower()
    for suf in _NOISE_SUFFIXES:
        if low.endswith(suf):
            name = name[: -len(suf)]
            low = name.lower()
    if not name:
        return None
    return name


# Whether the "(Vendor)" hint is appended to a resolved name.
# The `show_kinds` setting was removed, so the hint is always shown: it is the
# only thing that makes a router-assigned name like ATT_4991_f91139 legible.
def _guess_kind(hostname):
    """Turn a DHCP name into a friendly hint, when the name is just an ID."""
    if not hostname:
        return None
    low = hostname.lower()
    for prefix, label in _HINT_PREFIXES.items():
        if prefix in low:
            return label
    return None


def router_ptr(ip, router=None, timeout=2):
    """Reverse-resolve via the router, which knows DHCP lease names.

    `router` defaults to the detected gateway. Resolved per call rather than
    bound as a default argument, so a config file written after import (by
    `lanlog setup`) is still picked up.
    """
    router = router or _router()
    if not router:
        return None
    try:
        out = subprocess.run(
            ["dig", "+short", f"+time={timeout}", "+tries=1", "-x", ip,
             f"@{router}"],
            capture_output=True, text=True, check=False, timeout=timeout + 2,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if not out:
        return None
    return _clean_hostname(out.splitlines()[0])


def mdns_ptr(ip, timeout=0.4):
    """Resolve via mDNS. Needs the device to advertise it.

    Timeout is deliberately short (0.4s, not the previous 3s): a device that
    DOES answer replies in about 12ms, so anything slower is a miss. At 3s a
    single non-advertising device stalled the whole dashboard for 3 seconds.
    """
    try:
        out = subprocess.run(
            ["avahi-resolve", "-a", ip, "-4"],
            capture_output=True, text=True, check=False, timeout=timeout,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if not out:
        return None
    # Output is "<ip>\t<hostname>"
    parts = out.split("\t")
    name = parts[-1] if len(parts) > 1 else parts[0]
    return _clean_hostname(name)


def describe(ip, mac=None, vendor=None, aliases=None, use_mdns=True):
    """Return (display_name, source) for an IP.

    Priority: user alias > mDNS > router PTR > vendor > raw IP.

    Results are memoised on disk (see _name_cache) because a name lookup costs
    12ms-3s and the dashboard calls this on every repaint. Negative results are
    cached too: a device that does not advertise mDNS makes avahi-resolve block
    for its FULL timeout, and that was the dashboard's entire frame delay.
    """
    cached = _cache_get(ip)
    if cached is not None:
        return cached

    # The machine running the logger. Its traffic is folded to this one label at
    # ingest (see ingest.attribute_host), so it is named here rather than
    # resolved: there is no DHCP lease or mDNS record for a container's gateway.
    if ip == "localhost":
        return "this machine", "self"

    aliases = load_aliases() if aliases is None else aliases
    if ip in aliases:
        result = (aliases[ip], "alias")
        _cache_put(ip, *result)
        return result

    md = mdns_ptr(ip) if use_mdns else None
    if md:
        kind = _guess_kind(md)
        result = (f"{md} ({kind})" if kind else md), "mdns"
        _cache_put(ip, *result)
        return result

    ptr = router_ptr(ip)
    if ptr:
        kind = _guess_kind(ptr)
        result = (f"{ptr} ({kind})" if kind else ptr), "router"
        _cache_put(ip, *result)
        return result

    if vendor:
        result = f"{ip} ({vendor})", "vendor"
    else:
        result = ip, "ip"
    _cache_put(ip, *result)
    return result


# -- persistent name cache -------------------------------------------------
#
# Stored as <ip>\t<name>\t<source>\t<expiry_epoch>, one per line. Negatives
# are cached too, with a SHORTER ttl, because a miss is the expensive case:
# avahi-resolve blocks for its full timeout on a device that never answers.
#
# TTLs: positive results are stable for hours, misses expire quickly so a
# device that starts advertising mDNS is picked up without a restart.
CACHE_FILE = lanconfig.CACHE_FILE
POS_TTL = 6 * 3600
NEG_TTL = 300

# In-memory index, loaded once per process. Re-reading and re-parsing the file
# for every describe() call would be O(devices) per frame -- the exact cost
# this cache exists to remove.
_cache = {}          # ip -> (name, source, expiry)
_cache_loaded = False


def _load_cache():
    global _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    now = time.time()
    try:
        with open(CACHE_FILE) as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 4:
                    continue
                try:
                    expiry = float(parts[3])
                except ValueError:
                    continue
                if expiry >= now:
                    # Later rows win: a re-resolution overwrites an earlier miss.
                    # Resolver chatter is rejected here as well as in
                    # _clean_hostname, because the cache is a separate trust
                    # boundary: rows written by an older build, or while the
                    # router was misconfigured, are read back without passing
                    # through the cleaner. That is how
                    # ";; communications error to 192.168.1.254#53: timed out"
                    # kept resurfacing as a device name after the cleaner itself
                    # was fixed. Never serve a name that is not a name.
                    if _clean_hostname(parts[1]) is None:
                        continue
                    _cache[parts[0]] = (parts[1], parts[2], expiry)
    except OSError:
        pass


def _cache_get(ip):
    _load_cache()
    hit = _cache.get(ip)
    if not hit:
        return None
    name, source, expiry = hit
    if expiry < time.time():
        _cache.pop(ip, None)
        return None
    return name, source


def _cache_put(ip, name, source):
    if not name or name == ip:
        return
    _load_cache()
    ttl = NEG_TTL if source == "ip" else POS_TTL
    expiry = time.time() + ttl
    _cache[ip] = (name, source, expiry)
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "a") as fh:
            fh.write(f"{ip}\t{name}\t{source}\t{expiry}\n")
    except OSError:
        pass


def clear_cache():
    """Drop the name cache. Use after a rename or an IP change."""
    _cache.clear()
    global _cache_loaded
    _cache_loaded = True
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass


def describe_all(conn, use_mdns=True):
    """Resolve every device in the DB. Returns {ip: (name, source)}."""
    aliases = load_aliases()
    out = {}
    rows = conn.execute("SELECT ip, mac, vendor FROM devices ORDER BY ip")
    for row in rows:
        # Vendor is only a fallback, so skip the DNS round-trips when we
        # already have a real alias.
        ip = row["ip"]
        if ip in aliases:
            out[ip] = (aliases[ip], "alias")
            continue
        out[ip] = describe(ip, row["mac"], row["vendor"], aliases, use_mdns)
    return out


def template_alias_file():
    return """\
# lanlog device names. Format: <ip> <name>
# These win over everything else the tool can infer.

# 192.168.1.20   front door cam
# 192.168.1.21   firestick
# 192.168.1.22   my phone
"""
