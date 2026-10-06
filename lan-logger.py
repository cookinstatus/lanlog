#!/usr/bin/env python3
"""lan-logger: record which device on the LAN is talking to which domain.

Runs two loops:
  1. tail the Pi-hole query log -> per-client DNS query rows
  2. periodic ping sweep + neighbour-table read -> per-device MAC/vendor rows
"""

import argparse
import os
import signal
import sqlite3
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from lanlog import config as lcfg  # noqa: E402
from lanlog import db, discover, ingest  # noqa: E402

_running = True

# The router answers for both its IPv4 and its global IPv6. Skipping by ".254"
# alone missed the v6 half, so match the gateway MAC too.
GATEWAY_MAC = "0c:7f:b2:c0:55:68"
GATEWAYS = {"192.168.1.254", "2600:1700:4c50:6be0::1"}


def _apply_ingest_settings():
    """Push the resolved Pi-hole host into ingest before the first line.

    ingest decides at import time whether a link-local source is this machine's
    own traffic or an unmappable local link, and it reads LANLOG_PIHOLE_HOST for
    that. The value can come from the config file as well as the environment, so
    it is written into the environment here, before any query is classified.
    """
    host = (os.environ.get("LANLOG_PIHOLE_HOST")
            or lcfg.get("pihole_host") or "")
    os.environ["LANLOG_PIHOLE_HOST"] = host
    ingest.PIHOLE_HOST = host.strip().lower()
    ingest.IN_CONTAINER = ingest.PIHOLE_HOST in ("", "localhost",
                                                 "127.0.0.1", "::1")


def _stop(*_):
    global _running
    _running = False


def query_loop(conn, verbose):
    """Stream Pi-hole query lines into the DB. Exits if the stream dies."""
    seen = 0
    for line in ingest.stream():
        if not _running:
            break
        rec = ingest.classify(line)
        if rec is None:
            continue
        db.record_query(conn, *rec)
        seen += 1
        # No commit needed: the connection is in autocommit mode (see db.connect).
        if verbose:
            print(f"{rec[1]:>15}  {rec[3]:<6} {rec[2]}", flush=True)
    print(f"ingest stream ended after {seen} queries", file=sys.stderr)
    return seen


def discovery_loop(conn_factory, cidr, interval, verbose):
    # SQLite connections are bound to the creating thread, so this loop opens
    # its own rather than sharing the ingest connection.
    conn = conn_factory()
    table = discover.load_oui()
    while _running:
        found = discover.sweep(cidr)
        for ip, mac in found:
            if ip in GATEWAYS or ip.endswith(".1") and mac == GATEWAY_MAC:
                continue
            try:
                # No reverse lookup here. Doing dig -x on every host made this
                # tool measure itself: each PTR query went out through Pi-hole
                # and came back as a logged query, so the dashboard was mostly
                # the logger's own noise. Naming is resolved on read instead,
                # straight from the router (lanlog/identify.py), which does not
                # touch Pi-hole.
                db.touch_device(conn, ip, mac=mac,
                                vendor=discover.vendor_for(mac, table))
            except sqlite3.OperationalError as exc:
                # A thread that dies here silently stops discovery forever.
                # Log it, drop the connection, and let the next sweep retry.
                print(f"[discovery] {ip}: {exc}", file=sys.stderr)
                try:
                    conn.close()
                except Exception:
                    pass
                conn = conn_factory()
        if verbose and found:
            print(f"[discovery] {len(found)} hosts on {cidr}", flush=True)
        for _ in range(int(interval)):
            if not _running:
                return
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cidr", default=None,
                    help="subnet to sweep (default: this host's subnet)")
    ap.add_argument("--sweep-interval", type=int, default=300,
                    help="seconds between ping sweeps (0 = discovery off)")
    ap.add_argument("--container", default="pihole")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Must run before the first log line is classified.
    _apply_ingest_settings()

    conn = db.connect()
    discover.load_oui()

    threads = []
    if args.sweep_interval > 0:
        t = threading.Thread(
            target=discovery_loop,
            args=(db.connect, args.cidr, args.sweep_interval, args.verbose),
            daemon=True,
        )
        t.start()
        threads.append(t)

    n = query_loop(conn, args.verbose)
    print(f"ingest stream ended after {n} queries", file=sys.stderr)


if __name__ == "__main__":
    main()
