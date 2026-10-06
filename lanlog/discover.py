"""Discover devices on the LAN via the kernel neighbour table + a ping sweep."""

import ipaddress
import os
import re
import subprocess

OUI_FILES = (
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/share/arp-scan/ieee-oui.txt",
    "/usr/share/ieee-data/oui.txt",
    "/var/lib/ieee-data/oui.txt",
)
OUI_FILE = OUI_FILES[0]
NEIGH_RE = re.compile(
    r"^(?P<ip>\S+)\s+dev\s+(?P<dev>\S+)\s+lladdr\s+(?P<mac>[0-9a-f:]{17})",
    re.I,
)

_oui_cache = None


def oui_path():
    """First OUI database that exists on this machine, or None.

    Checked in order rather than assumed, because the nmap file is the one this
    was written against and it is NOT the only place an OUI list lives: a
    machine with arp-scan or ieee-data instead has an equally good list under a
    different name. Falling back to the nmap path unconditionally meant a
    friend's machine showed every vendor as unknown.
    """
    for path in OUI_FILES:
        if os.path.exists(path):
            return path
    return None


def load_oui(path=None):
    """Parse the OUI database: 'FC  FB  FB   Cisco...'. Keyed on 3 octets."""
    global _oui_cache
    if _oui_cache is not None:
        return _oui_cache
    table = {}
    path = path or oui_path()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                prefix = parts[0].replace(":", "").replace("-", "").upper()
                if len(prefix) != 6:
                    continue
                # Vendor is everything after the prefix -- splitting into 3 and
                # taking field [2] truncates multi-word names to their last word
                # ("AirTies Wireless Networks" -> "Wireless Networks").
                table[prefix] = parts[1].strip()
    _oui_cache = table
    return table


def vendor_for(mac, table=None):
    if not mac:
        return None
    table = table if table is not None else load_oui()
    return table.get(mac.replace(":", "").replace("-", "").upper()[:6])


def read_neigh():
    """Parse `ip neigh show`. Returns list of (ip, mac)."""
    out = subprocess.run(
        ["ip", "neigh", "show"], capture_output=True, text=True, check=False
    ).stdout
    found = []
    for line in out.splitlines():
        m = NEIGH_RE.match(line.strip())
        if not m:
            continue
        ip = m.group("ip")
        mac = m.group("mac").lower()
        # Locally-administered bit set == randomised MAC (private address on
        # modern phones). Worth flagging so it is not mistaken for a real OUIs.
        if int(mac[:2], 16) & 0b10:
            continue
        # IPv6 link-local (fe80::/10) and multicast are never useful as device
        # identities -- fe80:: entries are just the router seen from each stack.
        if ip.lower().startswith("fe80:") or ip.lower().startswith("ff"):
            continue
        found.append((ip, mac))
    return found


def sweep(cidr=None, timeout=0.4, parallel=64):
    """Ping-sweep a network so the neighbour table fills in.

    Uses unprivileged ICMP (datagram sockets) so no root is required. Devices
    that ignore ICMP still land in the ARP table, which is the common case on a
    LAN.

    `cidr` defaults to None and is resolved to THIS host's subnet at call time,
    not to a hardcoded 192.168.1.0/24. A fixed default silently swept the wrong
    network on anyone else's LAN -- the sweep ran, found nothing, and the
    dashboard looked empty rather than misconfigured.
    """
    if not cidr:
        from lanlog import config as lcfg

        cidr = lcfg.local_cidr() or ""
    if not cidr:
        return read_neigh()
    net = ipaddress.ip_network(cidr, strict=False)
    procs = []
    for host in net.hosts():
        procs.append(
            subprocess.Popen(
                ["ping", "-c", "1", "-W", str(int(timeout * 1.5) or 1), str(host)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        if len(procs) >= parallel:
            for p in procs:
                p.wait()
            procs = []
    for p in procs:
        p.wait()
    return read_neigh()


def resolve(ip, server=None):
    """Reverse-resolve via Pi-hole. Returns hostname or None.

    `server` defaults to the configured/detected Pi-hole address rather than a
    hardcoded LAN IP, so this works on a network that is not 192.168.1.0/24.

    Note: Pi-hole binds DNS to the LAN address under pasta, not 127.0.0.1, so
    the default here is the LAN IP rather than loopback.
    """
    if not server:
        from lanlog import config as lcfg

        server = lcfg.get("pihole_ip")
    if not server:
        return None
    try:
        out = subprocess.run(
            ["dig", "+short", "+time=1", "+tries=1", "-x", ip, "@" + server],
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return out.splitlines()[0].rstrip(".") if out else None
