# lanlog

See what devices on your LAN are talking to, right now.

One project: a background logger, a live terminal dashboard / CLI, and a system
tray viewer. All three import the same `lanlog` package, so a name, a setting or
a report means exactly the same thing everywhere.

Reads Pi-hole's query log (streamed via `podman exec`, so no permission or
firewall changes needed) plus a periodic ARP sweep for device identity.
Everything lands in a local SQLite database at `./lanlog.db`.

## Requirements

- Linux, Python 3.9+ — standard library only, nothing to `pip install`.
- Pi-hole running in a container (podman or docker) on this machine. The tool
  reads Pi-hole's query log by streaming it through `podman exec`, so it needs
  no extra permissions and no changes to Pi-hole.
- `ip`, `ping` and `dig` for device discovery and name resolution
  (iproute2, iputils, and bind-utils/dnsutils or equivalent).
- For the tray only: GTK3 + PyGObject and an appindicator library, plus a
  system tray host. `./install.sh --gui-deps` prints the package names.

## Install

    git clone https://github.com/cookinstatus/lanlog
    cd lanlog
    ./install.sh              # launchers, icon, autostart
    ./install.sh --gui-deps   # also offer to install GTK/PyGObject for the tray

Then start the logger so data accumulates without a dashboard open:

    lanlog start
    journalctl --user -u lan-logger -f

If Pi-hole is not on this machine, see `lanlog setup` and the `pihole_host`
setting in `~/.config/lanlog/config`.

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

The tray: `lanlog-tray` (or `lanlog-tray --window` for a normal window). It
starts at login from `~/.config/autostart/lanlog-tray.desktop`.

## Layout

    bin/lanlog            CLI + live dashboard entry point
    bin/lanlog-tray       tray entry point (system python)
    lan-logger.py         the recording loops (log tail + device sweep)
    lanlog/               the shared package
      db.py               schema, migrations, connection
      ingest.py           Pi-hole log tail -> rows, host attribution
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

## This machine's own queries

A host that resolves through Pi-hole has its own traffic logged from the
container's plumbing address — `169.254.1.2` under rootless podman (pasta), or
`127.0.0.1` for anything sent to loopback. Neither is a real device, but the
traffic is real and it belongs to this machine, so lanlog folds it into a single
**`this machine`** row rather than dropping it. The same folding applies to a
query that arrives attributed to one of this host's own LAN addresses, so the
machine is one row whichever path its traffic took.

This only applies when Pi-hole runs on this machine. Set `pihole_host` in
`~/.config/lanlog/config` when Pi-hole is elsewhere, and a `169.254.x` client is
then treated as an unmappable link-local on that side and dropped, because it
cannot be matched to anything on this LAN.

## Notes on this setup

- **Pi-hole v6 has no dnstap.** Upstream dropped it; the FTL binary has zero
  references. Log-tailing is the only option, not a fallback.
- **Under rootless podman with pasta, Pi-hole binds DNS to the host's LAN
  address, not 127.0.0.1.** Point any client at that address; `dig @127.0.0.1`
  gives "connection refused".
- **reply/cached log lines are ignored** because they carry no client IP.
  Attributing them to a device would mean guessing.
- **A device that ignores ICMP still appears** — the ping sweep exists to fill
  the ARP table, and the neighbour table is what is actually read.
