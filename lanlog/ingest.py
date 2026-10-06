"""Tail the Pi-hole query log and turn it into rows.

Pi-hole v6 dropped dnstap support from FTL, so the query log is the feed. It is
mode 0640 owned by the container's pihole user, which is unreadable from the
host, so we stream it through `podman exec` rather than changing any
permissions.
"""

import re
import subprocess
import time

# "Sep 28 01:25:18 dnsmasq[48]: query[A] example.com from 192.168.1.151"
QUERY_RE = re.compile(
    r"^(?P<ts>\w{3}\s+\d+\s+\d+:\d+:\d+)\s+dnsmasq\[\d+\]:\s+"
    r"query\[(?P<qtype>\w+)\]\s+(?P<qname>\S+)\s+from\s+(?P<client>\S+)"
)
# Domains that only ever appear in reverse lookups.
REVERSE_SUFFIXES = ("in-addr.arpa", "ip6.arpa")

# Clients in 169.254.0.0/16 are link-local. On this host Pi-hole runs under
# rootless podman, where pasta presents a per-container gateway at 169.254.1.2
# and every forwarded query appears to originate there. Recording it as a device
# is worse than useless: it was 93% of all queries (61,929 of 66,625), which
# buried the handful of real LAN clients in the dashboard.
#
# Matched as a prefix on the string, not parsed as an int, so IPv6 link-local
# (fe80::/10) is handled by the same test below.
LINK_LOCAL_PREFIXES = ("169.254.", "fe80:")


def _is_link_local(client: str) -> bool:
    return client.startswith(LINK_LOCAL_PREFIXES)

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
    client = m.group("client")

    # Podman's pasta gateway masquerading as a client. Dropped at ingest rather
    # than display time, for the same reason PTR lookups are: the counts have to
    # be honest, and a container plumbing artifact is not a device on the LAN.
    if _is_link_local(client):
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
