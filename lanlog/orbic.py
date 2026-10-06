"""Ingest DNS queries captured by DagShell's sniffer on the Orbic hotspot.

I could not test this against a real device (none was on the network), so the
parser is deliberately tolerant: it tries several common log shapes, and
`probe()` dumps raw lines so the real format can be confirmed and a new regex
added without guessing.

The hotspot is reached over SSH (root). Its DNS sniffer log is pulled on an
interval and appended to the same SQLite database as the Pi-hole feed, tagged
with source='orbic' so the two never get confused.
"""

import os
import re
import subprocess
import time

# Candidate line shapes, tried in order. Day/month ordering is ambiguous, so
# both are accepted and disambiguated by which one parses as a real date.
_PATTERNS = [
    # "2026-09-29 11:03:06 client 192.168.1.74 query example.com"
    re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\D+?"
        r"(?:client|src|from)?\s*(?P<client>\d{1,3}(?:\.\d{1,3}){3})\D+"
        r"(?P<verb>query|lookup|resolve[d]?)\s+(?P<qname>\S+?)(?:\s|$)",
        re.I,
    ),
    # "Sep 29 11:03:06 192.168.1.74 example.com"
    re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+"
        r"(?P<client>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<qname>[a-z0-9][\w.-]+)",
    ),
    # "192.168.1.74 example.com"  (no timestamp at all)
    re.compile(
        r"^(?P<client>\d{1,3}(?:\.\d{1,3}){3})\s+(?P<qname>[a-z0-9][\w.-]+)"
    ),
]

QTYPE_RE = re.compile(r"(?P<qname>\S+?)\s+(?P<qtype>TYPE\d+|A|AAAA|PTR|HTTPS?|SVCB)\b")


def parse_line(line, default_ts=None):
    """Return (ts, client, qname, qtype, outcome) or None."""
    line = line.rstrip("\n")
    if not line.strip():
        return None

    for i, pat in enumerate(_PATTERNS):
        m = pat.match(line)
        if not m:
            continue
        g = m.groupdict()
        client = g.get("client")

        # A leading IPv6 address is not a hotspot client; skip rather than
        # record garbage.
        if not client:
            return None

        qname = g.get("qname")
        qtype = g.get("qtype") or "A"

        qt = QTYPE_RE.search(line)
        if qt:
            qname = qt.group("qname")
            qtype = qt.group("qtype").lstrip("TYPE") or "A"

        if not qname or "." not in qname:
            return None

        ts = default_ts
        raw_ts = g.get("ts")
        if raw_ts:
            if i == 0:
                try:
                    ts = time.mktime(time.strptime(raw_ts[:19], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    ts = default_ts
            else:
                try:
                    p = time.strptime(raw_ts, "%b %d %H:%M:%S")
                    ts = time.mktime(
                        (time.localtime().tm_year, p.tm_mon, p.tm_mday,
                         p.tm_hour, p.tm_min, p.tm_sec, 0, 1, -1)
                    )
                except ValueError:
                    ts = default_ts

        return (ts or time.time(), client, qname.lower().rstrip("."),
                str(qtype).upper(), "orbic-query")
    return None


# --- remote transport -------------------------------------------------------

DEFAULT_LOG = "/var/log/orbic_dns.log"
# Plausible locations; the real one is whichever exists on the device.
CANDIDATE_LOGS = [
    DEFAULT_LOG,
    "/var/log/dagshell_dns.log",
    "/var/log/dnsmasq.log",
    "/tmp/dns_queries.log",
]


def ssh(host, cmd, user="root", timeout=10):
    """Run a command on the hotspot. Returns (rc, stdout)."""
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
         f"{user}@{host}", cmd],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    return proc.returncode, proc.stdout


def find_log(host, user="root"):
    """Return the first candidate log path that exists on the device."""
    for path in CANDIDATE_LOGS:
        rc, out = ssh(host, f"test -f {path} && echo yes", user=user)
        if rc == 0 and "yes" in out:
            return path
    rc, out = ssh(host,
                  "ls -1 /var/log/*.log 2>/dev/null | head -20", user=user)
    return None


def probe(host, log=None, user="root", lines=15):
    """Dump raw lines from the hotspot log so the format can be confirmed.

    Run this FIRST when the Orbic is in range. If nothing parses, add a new
    regex to _PATTERNS using the real output as the example.
    """
    path = log or find_log(host, user)
    if not path:
        return None, []
    rc, out = ssh(host, f"tail -n {lines} {path}", user=user)
    return path, out.splitlines()


def pull(host, log=None, user="root", state_path=None):
    """Fetch new lines since last pull. Returns list of raw lines."""
    path = log or find_log(host, user)
    if not path:
        return []

    state_path = state_path or os.path.expanduser("~/.cache/lanlog/orbic.offset")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)

    try:
        with open(state_path) as fh:
            seen = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0

    # Tail from N lines back so nothing is missed if the service was down,
    # then re-filter on the client side by line offset.
    rc, out = ssh(host, f"tail -n +{seen + 1} {path} 2>/dev/null", user=user)
    if rc != 0:
        return []
    lines = out.splitlines()
    if lines:
        try:
            with open(state_path, "w") as fh:
                fh.write(str(seen + len(lines)))
        except OSError:
            pass
    return lines
