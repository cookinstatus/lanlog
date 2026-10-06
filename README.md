# lanlog

See what devices on your LAN are talking to, right now.

One project: a background logger, a live terminal dashboard / CLI, and a system
tray viewer. All three import the same `lanlog` package, so a name, a setting or
a report means exactly the same thing everywhere.

Reads Pi-hole's query log (streamed via `podman exec`, so no permission or
firewall changes needed) plus a periodic ARP sweep for device identity.
Everything lands in a local SQLite database at `./lanlog.db`.

## Install

    ./install.sh              # launchers, icon, autostart
    ./install.sh --gui-deps   # offer to install GTK/PyGObject for the tray

Then start the logger so data accumulates without a dashboard open:

    lanlog start
    journalctl --user -u lan-logger -f

## Use

    lanlog                      live dashboard (Ctrl-C to stop)
    lanlog record               record in the background, no dashboard
    lanlog devices              device inventory
    lanlog domains              most-queried domains
    lanlog blocked              only what Pi-hole blocked
    lanlog client 192.168.1.74  what one device looked up
    lanlog shared               domains hit by more than one device
    lanlog third-party          cross-device trackers
    lanlog reset                wipe the database
    lanlog purge-reverse        delete stored PTR/reverse-lookup rows

    lanlog setup                detect this network, write the config
    lanlog start / stop / status  control the background service
    lanlog config [key val]     show/change display settings
    lanlog toggle               cycle device labels: names -> ip -> both
    lanlog name set <ip> "nick" give a device a nickname
    lanlog mac <mac-or-ip>      look up what a MAC / OUI prefix belongs to
    lanlog orbic probe          dump the hotspot's DNS log over SSH

The tray: `lanlog-tray` (or `lanlog-tray --window` for a normal window). It
starts at login from `~/.config/autostart/lanlog-tray.desktop`.

## Layout

    bin/lanlog            CLI + live dashboard entry point
    bin/lanlog-tray       tray entry point (system python)
    lan-logger.py         the recording loops (log tail + sweep + orbic)
    lanlog/               the shared package
      db.py               schema, migrations, connection
      ingest.py           Pi-hole log tail -> rows
      orbic.py            hotspot DNS log over SSH -> rows
      discover.py         ARP/ping sweep, OUI vendor lookup
      identify.py         IP -> name (alias > mDNS > router PTR)
      blocking.py         is a domain blocked, and by which listed domain
      devices.py          nicknames + view settings (~/.config/lanlog/)
      report.py           the reports, shared by CLI, dashboard and tray
      keys.py, banner.py  dashboard input and logo
      config.py           network auto-detection (`lanlog setup`)
    tests/                regression tests (run: python3 tests/test_*.py)
    install.sh

Config lives in `~/.config/lanlog/` (`config`, `aliases`). The database and
name cache are in the project dir and `~/.cache/lanlog/`.

## Naming devices

Names come from three sources, best first:

1. `~/.config/lanlog/aliases` -- your own names, one `<ip> <name>` per line.
   These always win.
2. mDNS/Avahi, if the device advertises it.
3. The router's reverse DNS (the default gateway) -- the DHCP hostname. This is
   what names most consumer gear, since those devices do not advertise mDNS.

Pi-hole itself returns nothing for reverse lookups unless `dns.revServers` is
configured, so naming deliberately queries the router rather than Pi-hole.

## Reverse lookups are excluded

PTR queries are dropped at ingest. They are not browsing, and this machine
generated most of them: the discovery loop used to run `dig -x` on every host,
each query went out through Pi-hole and came back logged, so the dashboard was
mostly the tool measuring itself. `lanlog purge-reverse` clears rows stored
before the filter existed.

## Orbic hotspot (DagShell DNS sniffer)

lanlog can pull DNS query logs from the Orbic over SSH and merge them into the
same database, tagged `source='orbic'`, so you get one timeline across both your
wired LAN and devices connected to the hotspot.

The service has `ORBIC_HOST=192.168.1.1` set. When the hotspot is in range, run
this FIRST to confirm the log format:

    lanlog orbic probe          # shows raw lines + which ones parse
    lanlog orbic sources        # list /var/log/*.log on the device
    lanlog orbic status         # what has been recorded so far

`probe` prints `[OK]`/`[MISS]` per line; if everything is MISS, add a regex to
`_PATTERNS` in `lanlog/orbic.py` using the real output as the example. If the
hotspot is out of range the poll loop logs and retries -- it never crashes.

## Notes on this setup

- **Pi-hole v6 has no dnstap.** Upstream dropped it; the FTL binary has zero
  references. Log-tailing is the only option, not a fallback.
- **Under rootless podman with pasta, Pi-hole binds DNS to the host's LAN
  address, not 127.0.0.1.** Point any client at that address; `dig @127.0.0.1`
  gives "connection refused".
- **`169.254.1.2` in the log is pasta's NAT address** for host-local traffic,
  not a real device. It is dropped at ingest.
- **reply/cached log lines are ignored** because they carry no client IP.
  Attributing them to a device would mean guessing.
- **A host that resolves through Pi-hole itself will have its own traffic
  recorded as `169.254.1.2`** (pasta) or `127.0.0.1` and dropped, so those
  queries will not appear. Point the host's resolver elsewhere, or expect to
  see only the *other* devices' traffic in the dashboard.
