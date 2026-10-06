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

from lanlog import db, discover, ingest, orbic  # noqa: E402

_running = True

# The router answers for both its IPv4 and its global IPv6. Skipping by ".254"
# alone missed the v6 half, so match the gateway MAC too.
GATEWAY_MAC = "0c:7f:b2:c0:55:68"
GATEWAYS = {"192.168.1.254", "2600:1700:4c50:6be0::1"}


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


def orbic_loop(conn_factory, host, interval, log=None, verbose=False):
    """Poll the Orbic's DNS sniffer log over SSH and record what it finds.

    Disabled unless --orbic-host is given, since the hotspot is not always
    reachable (it is mobile).
    """
    conn = conn_factory()
    while _running:
        try:
            lines = orbic.pull(host, log=log)
            got = 0
            for line in lines:
                rec = orbic.parse_line(line)
                if rec is None:
                    continue
                ts, client, qname, qtype, outcome = rec
                db.record_query(conn, ts, client, qname, qtype, outcome,
                                source="orbic")
                got += 1
            if verbose and got:
                print(f"[orbic] {got} queries from {host}", flush=True)
        except Exception as exc:  # noqa: BLE001 - hotspot may vanish mid-poll
            print(f"[orbic] {host}: {exc}", file=sys.stderr)

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
    ap.add_argument("--orbic-host", default=os.environ.get("ORBIC_HOST"),
                    help="hotspot IP to pull DNS logs from")
    ap.add_argument("--orbic-log", default=None,
                    help="log path on the hotspot (auto-detected if omitted)")
    ap.add_argument("--orbic-interval", type=int, default=60)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

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

    if args.orbic_host:
        t = threading.Thread(
            target=orbic_loop,
            args=(db.connect, args.orbic_host, args.orbic_interval,
                  args.orbic_log, args.verbose),
            daemon=True,
        )
        t.start()
        threads.append(t)

    n = query_loop(conn, args.verbose)
    print(f"ingest stream ended after {n} queries", file=sys.stderr)


if __name__ == "__main__":
    main()
