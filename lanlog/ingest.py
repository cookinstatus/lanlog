"""Tail the Pi-hole query log and turn it into rows.

Pi-hole v6 dropped dnstap support from FTL, so the query log is the feed. It is
mode 0640 owned by the container's pihole user, which is unreadable from the
host, so we stream it through `podman exec` rather than changing any
permissions.
"""

import ipaddress
import os
import re
import subprocess
import time

# "Sep 28 01:25:18 dnsmasq[48]: query[A] example.com from 192.168.1.50"
QUERY_RE = re.compile(
    r"^(?P<ts>\w{3}\s+\d+\s+\d+:\d+:\d+)\s+dnsmasq\[\d+\]:\s+"
    r"query\[(?P<qtype>\w+)\]\s+(?P<qname>\S+)\s+from\s+(?P<client>\S+)"
)
# Domains that only ever appear in reverse lookups.
REVERSE_SUFFIXES = ("in-addr.arpa", "ip6.arpa")

# Where Pi-hole runs. This is what decides whether a query that appears to come
# from the container's own plumbing address actually came from this machine.
PIHOLE_HOST = os.environ.get("LANLOG_PIHOLE_HOST", "").strip().lower()
IN_CONTAINER = PIHOLE_HOST in ("", "localhost", "127.0.0.1", "::1")

# The host's own addresses, so a query that arrives attributed to one of them is
# recognised as "this machine" rather than as an unknown device.
_LOCAL_ADDRS = None


def _local_addrs():
    """This host's own IPv4 addresses, as a set. Cached after the first call."""
    global _LOCAL_ADDRS
    if _LOCAL_ADDRS is not None:
        return _LOCAL_ADDRS
    addrs = {"127.0.0.1", "::1"}
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                addrs.add(parts[parts.index("inet") + 1].split("/")[0])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    _LOCAL_ADDRS = addrs
    return addrs


def _is_pasta_local(client: str) -> bool:
    """True when a link-local source is really THIS machine's own traffic.

    Pi-hole under rootless podman (pasta) presents a per-container gateway at
    169.254.1.2, and every query this host sends through it is logged from
    there. That is only true when Pi-hole runs on this machine; when it runs
    elsewhere a 169.254 source is an unmappable link-local on that side.
    """
    return IN_CONTAINER and client.startswith(("169.254.", "fe80:"))


def attribute_host(client):
    """Map a query's source address to a stable identity for THIS host.

    This machine's own queries arrive as 169.254.1.2 (pasta) or 127.0.0.1
    (loopback), neither of which is a real device -- but the traffic is real and
    it belongs to this machine. It is folded into one 'localhost' client rather
    than dropped, so the machine running the logger shows up in its own
    dashboard instead of its traffic vanishing. A query already attributed to
    one of this host's LAN addresses is folded in too, so the machine is a
    single row whichever path its traffic took.

    Returns the address unchanged for any other device.
    """
    if not client:
        return client
    if client in ("127.0.0.1", "::1", "localhost") or _is_pasta_local(client):
        return "localhost"
    # Compare as strings: _local_addrs() holds strings, and an ip_address()
    # object never equals its own text form, so an object test silently missed
    # every match.
    if client in _local_addrs():
        return "localhost"
    return client

# reply/cached lines carry the name but NOT the client IP, so they cannot be
# attributed to a device. They are deliberately ignored: joining them back to a
# query would mean guessing, and a wrong client attribution is worse than no
# reply data at all. Blocking status is read from Pi-hole's gravity instead.


def parse_ts(ts_str, now=None):
    """Pi-hole logs in local time with no year, e.g. 'Sep 28 01:25:18'.

    Build a local-time struct and let time.mktime do the conversion. Do NOT use
    calendar.timegm here: that treats the input as UTC, and subtracting an
    offset afterwards double-shifts the result (this produced 10h-off rows).
    """
    now = now if now is not None else time.localtime()
    try:
        parsed = time.strptime(ts_str, "%b %d %H:%M:%S")
    except ValueError:
        return None

    def build(year):
        return time.mktime((
            year, parsed.tm_mon, parsed.tm_mday,
            parsed.tm_hour, parsed.tm_min, parsed.tm_sec,
            0, 1, -1,
        ))

    stamp = build(now.tm_year)
    # Log rotated across New Year's: a December line parsed as this year would
    # land in the future, so back it up a year.
    if stamp > time.time() + 86400:
        stamp = build(now.tm_year - 1)
    return stamp


def stream(container="pihole", log="/var/log/pihole/pihole.log"):
    """Yield (line, raw_timestamp) forever, following log rotation.

    `tail -F` handles rotation for us; we just pump its stdout.
    """
    proc = subprocess.Popen(
        ["podman", "exec", "-i", container, "tail", "-n", "200", "-F", log],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    finally:
        proc.terminate()


def classify(line):
    """Return (ts, client, qname, qtype, outcome, detail) or None."""
    m = QUERY_RE.match(line)
    if not m:
        return None
    qtype = m.group("qtype").upper()
    qname = m.group("qname").lower()
    client = attribute_host(m.group("client"))
    # Still link-local after attribution: Pi-hole is on another machine, so this
    # is an unmappable local link there and not a device on this LAN.
    if client.startswith(("169.254.", "fe80:")):
        return None

    # Reverse-DNS lookups are not browsing. They are mostly produced by this
    # machine and by mDNS/avahi, and they used to dominate the dashboard with
    # the logger measuring its own name lookups. Kept out of the query stream
    # entirely rather than filtered at display time, so the counts are honest.
    if qtype == "PTR" or qname.endswith(REVERSE_SUFFIXES):
        return None

    ts = parse_ts(m.group("ts"))
    if ts is None:
        return None
    return (ts, client, qname, qtype, "query", None)
