"""Reporting: what is on the LAN and what it is talking to."""

import re
import sqlite3
import time
from collections import Counter, defaultdict

from lanlog import db, identify


def _label(row, names=None, mode="names"):
    """Device label for a row, per the `labels` display setting.

    names -> the readable name, falling back to the IP when nothing resolves it
    ip    -> the raw address, always
    both  -> "name (ip)", or just the IP when there is no name to pair with it

    The fallback in `names` mode is deliberate and is the reason `ip` mode
    exists: a resolver failure produces a screen of bare IPs that is
    pixel-identical to a healthy one, so there is no way to notice from the
    display that names have stopped working. `ip` is the control.

    The vendor fallback the resolver produces is shaped "1.2.3.4 (Vendor)", so
    appending the address again in `both` mode yields "1.2.3.4 (Vendor)
    (1.2.3.4)". When the name already leads with the address it is passed
    through untouched, because the mode's job is to show the address, not to
    repeat it.
    """
    ip = row["ip"]
    name = names.get(ip, (ip, "ip"))[0] if names else ip
    if mode == "ip":
        return ip
    if mode == "both":
        if name == ip or name.startswith(f"{ip} "):
            return name
        return f"{name} ({ip})"
    return name


def _fit(text, width):
    """Truncate `text` to `width` without cutting off a trailing "(ip)".

    Plain truncation drops the address from the end of a long label, which is
    the one half of `both` mode that exists to be seen -- a row reading
    "Android_6af7fe4ca3174eda862804ee1471f213 (Android device) (192" is worse
    than useless. So the "(...)" group at the end is kept intact and the name
    in front of it absorbs the cut.
    """
    if len(text) <= width:
        return text
    # "(...)" at the tail, if there is one.
    if text.endswith(")") and " (" in text:
        tail_start = text.rfind(" (")
        if tail_start != -1:
            tail = text[tail_start:]
            room = width - len(tail)
            if room >= 8:
                return text[:room].rstrip() + tail
    return text[:max(1, width - 1)]


def _strip_kind(name):
    """Drop the trailing '(Vendor)' hint the name resolver appends."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name)


def _label_mode(cfg=None):
    """Active labels mode, defensively. A bad config must not kill a report."""
    try:
        from lanlog import devices as devcfg

        return devcfg.label_mode(cfg)
    except Exception:  # noqa: BLE001
        return "names"


def _fmt_age(ts):
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_clock(ts):
    """Wall-clock time for a row, e.g. '22:39:12'.

    Rows used to carry only a relative age, which answers "how long ago" but not
    "when" -- and "when" is what you need to line a query up against something
    else that happened. The date is added when the row is not from today, so a
    stale database does not look like it is from this evening.
    """
    lt = time.localtime(ts)
    now = time.localtime()
    if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("%H:%M:%S", lt)
    return time.strftime("%b %d %H:%M", lt)


def devices(conn, window=None):
    sql = "SELECT * FROM devices"
    args = ()
    if window:
        sql += " WHERE last_seen >= ?"
        args = (time.time() - window,)
    sql += " ORDER BY ip"
    return [dict(r) for r in conn.execute(sql, args)]


def _present_ips(conn):
    cutoff = time.time() - PRESENT_WINDOW
    return {r["ip"] for r in conn.execute(
        "SELECT ip FROM devices WHERE last_seen >= ?", (cutoff,))}


# A device counts as present if the most recent sweep heard from it. Three
# sweep intervals of slack (sweeps run every 300s), so one missed sweep does
# not flip a device to absent.
PRESENT_WINDOW = 900


def show_devices(conn, window=None):
    from lanlog import devices as devcfg

    cfg = devcfg.load_config()
    rows = devices(conn, window)
    if not rows:
        print("no devices recorded yet")
        return

    # Display toggles from `lanlog config`.
    if cfg.get("hide_idle", "no").lower() in ("yes", "true", "1"):
        counts = Counter(r["client"] for r in conn.execute(
            "SELECT client, COUNT(*) c FROM queries GROUP BY client"))
        rows = [r for r in rows if counts.get(r["ip"], 0) > 0]
    only = [s.strip() for s in (cfg.get("only") or "").split(",") if s.strip()]
    if only:
        rows = [r for r in rows if r["ip"] in only]
    if not rows:
        print("no devices match the current display settings")
        return

    names = identify.describe_all(conn)
    mode = _label_mode(cfg)

    # Mark which devices answered the most recent sweep. "last seen" alone is
    # ambiguous -- every device gets a last_seen from the sweep that found it,
    # so a device that has been powered off for a week still looks recently
    # seen if the DB was written shortly after it dropped.
    online = _present_ips(conn)
    # Present devices first. The request was "show what is on the network", and
    # alphabetically-sorted rows bury the live ones among stale entries.
    rows.sort(key=lambda r: (r["ip"] not in online, r["ip"]))
    labels = [_label(r, names, mode) for r in rows]
    # The columns adapt to the mode rather than sitting at fixed widths.
    #
    # In `ip` mode the separate ip column would just repeat the device column,
    # so it is dropped and the address moves into the first column where it can
    # use the full width. In `both` mode the address is already inside the label,
    # so that column is dropped instead. Showing the same 15 characters twice is
    # how a table becomes unreadable, and in `both` mode the old fixed 44 also
    # truncated the address straight off the end of every row -- cutting off the
    # one half of that mode that exists to be seen.
    dev_w = 44 if mode == "ip" else max([24] + [len(x) for x in labels])
    # Cap against the terminal, not just a constant: on an 80-column screen a
    # 62-wide device column plus the ip/last-seen/now columns overflows and the
    # rows wrap. The cap leaves room for the fixed columns.
    width = _term_width()
    fixed_w = 2 + 15 + 2 + 11 + 2 + 5 + 7 if mode == "names" else 2 + 11 + 2 + 5 + 7
    dev_w = min(dev_w, 62, max(20, width - fixed_w))
    show_ip = mode == "names"

    if show_ip:
        print(f"{'device':<{dev_w}} {'ip':<15} {'last seen':<11} {'now':<5} queries")
        print("-" * (dev_w + 2 + 15 + 2 + 11 + 2 + 5 + 7))
    else:
        print(f"{'device':<{dev_w}} {'last seen':<11} {'now':<5} queries")
        print("-" * (dev_w + 2 + 11 + 2 + 5 + 7))

    counts = Counter(
        r["client"] for r in conn.execute(
            "SELECT client, COUNT(*) c FROM queries GROUP BY client"
        )
    )
    for r, name in zip(rows, labels):
        # Sorted present-first: the user asked to see what is on the network.
        mark = "*" if r["ip"] in online else " "
        if show_ip:
            print(f"{_fit(name, dev_w):<{dev_w}} {r['ip']:<15} "
                  f"{_fmt_age(r['last_seen']):<11}"
                  f" {mark:<5} {counts.get(r['ip'], 0)}")
        else:
            print(f"{_fit(name, dev_w):<{dev_w}} {_fmt_age(r['last_seen']):<11}"
                  f" {mark:<5} {counts.get(r['ip'], 0)}")
    n_on = sum(1 for r in rows if r["ip"] in online)
    print(f"\n{len(rows)} device(s), {n_on} present now  (* = seen in the "
          f"last {PRESENT_WINDOW // 60} min)")


def _block_table():
    """Gravity as a set, loaded once. Empty set when Pi-hole is not readable."""
    from lanlog import blocking

    return blocking.domains()


def _verdict_header(with_ips=False):
    """Column header and separator for a report that shows blocking.

    The "blocked by" column is only printed when gravity is actually readable.
    Without it the column would be blank on every row, which reads as "nothing
    was blocked" -- the exact false claim this feature exists to avoid.
    """
    from lanlog import blocking

    ips_head = f" {'address':<44}" if with_ips else ""
    ips_sep = 46 if with_ips else 0
    if not _block_table():
        return (f"{'domain':<50} {'queries':>8} {'devices':>8}{ips_head}",
                "-" * (70 + ips_sep), False)
    return (f"{'domain':<50} {'queries':>8} {'devices':>8}{ips_head}  blocked by",
            "-" * (80 + ips_sep), True)


def _term_width(default=100):
    """Usable terminal width. Honours $COLUMNS, so piping and tests are stable."""
    try:
        import shutil

        return shutil.get_terminal_size((default, 24)).columns
    except Exception:  # noqa: BLE001 - a width is never worth failing a report
        return default


def _shape_addresses(ips, width):
    """Fit a name's addresses into `width`, dropping addresses until they fit.

    A raw join can be 200 characters (youtube resolves to 16 addresses), and
    padding does not truncate -- so the row silently overflowed its column and
    the terminal wrapped it. Reducing the count until the string fits keeps the
    column honest: what is shown is real, and the `(+N more)` says what is not.
    """
    if not ips:
        return "-"
    for n in range(len(ips), 0, -1):
        shown = ", ".join(ips[:n])
        cand = shown if n == len(ips) else f"{shown} (+{len(ips) - n} more)"
        if len(cand) <= width:
            return cand
    return ips[0][:max(1, width)]


def _answers_for(conn, qnames, max_shown=3, width=None):
    """{qname: "ip1, ip2, ip3 (+13 more)"} for the names given, best-effort.

    Addresses come from the `answers` table (the log's reply/cached lines). A
    name with no recorded answer is simply absent from the result -- showing
    nothing is honest, and the alternative (guessing, or leaving a stale address
    in place) is not.

    With `width`, the string is shaped to fit that many columns exactly (see
    _shape_addresses); without it, `max_shown` addresses are shown plus a count.

    Tolerant of a database without the table: a viewer pointed at an older
    lanlog.db, or any foreign database, must still render its reports rather
    than dying on a missing column.
    """
    if not qnames:
        return {}
    out = {}
    # Chunked so a long list cannot exceed SQLite's bound-variable limit.
    names = list(qnames)
    for i in range(0, len(names), 400):
        chunk = names[i:i + 400]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT qname, ip FROM answers WHERE qname IN ({marks}) "
                f"ORDER BY qname, last_seen DESC", chunk
            )
        except sqlite3.Error:
            return {}
        for qname, ip in rows:
            out.setdefault(qname, []).append(ip)
    shaped = {}
    for qname, ips in out.items():
        if width:
            shaped[qname] = _shape_addresses(ips, width)
        elif len(ips) > max_shown:
            shaped[qname] = (f"{', '.join(ips[:max_shown])} "
                             f"(+{len(ips) - max_shown} more)")
        else:
            shaped[qname] = ", ".join(ips)
    return shaped


def show_top_domains(conn, limit=25, window=None, show_ips=True):
    """Busiest domains, each with the addresses it resolved to and its verdict.

    The address sits immediately after the domain rather than at the end of the
    line: appended last, it landed past the queries and devices columns and read
    as belonging to nothing. The whole table is sized to the terminal, because a
    140-character row on an 80-column screen wraps and turns the report into
    noise -- which is what the address column looked like when it was added.
    """
    from lanlog import blocking

    sql = "SELECT qname, COUNT(*) c, COUNT(DISTINCT client) n FROM queries"
    args = ()
    if window:
        sql += " WHERE ts >= ?"
        args = (time.time() - window,)
    sql += " GROUP BY qname ORDER BY c DESC LIMIT ?"
    rows = list(conn.execute(sql, args + (limit,)))
    if not rows:
        print("no queries recorded yet")
        return

    width = _term_width()
    table = _block_table()
    show_block = bool(table)
    # Derive the columns from the real suffix lengths rather than fixed numbers.
    # The header ends in "  blocked by" (12 chars) while a compact row ends in
    # "  BLK" (5), and a fixed address width overflowed at any terminal narrower
    # than the sum -- which is the wrapping this is meant to prevent.
    blk_full = "  blocked by" if show_block else ""
    blk_short = "  BLK" if show_block else ""
    # non-compact: domain + addr + ' queries' + ' devices' + blk_full
    over_full = 1 + 1 + 8 + 1 + 8 + len(blk_full)
    # compact: domain + addr + ' queries' + blk_short
    over_compact = 1 + 1 + 8 + len(blk_short)
    compact = (width - 44 - over_full) < 18
    dom_w = 30 if compact else 44
    overhead = over_compact if compact else over_full
    addr_w = max(0, width - dom_w - overhead)
    if addr_w < 14:
        # Still tight: give the domain less room so the address survives, since
        # the address is the reason this column exists.
        dom_w = max(18, width - overhead - 14)
        addr_w = max(0, width - dom_w - overhead)
    show_ips = show_ips and addr_w >= 10
    n_ips = 1 if compact else 3

    head = f"{'domain':<{dom_w}}"
    if show_ips:
        head += f" {'address':<{addr_w}}"
    head += f" {'queries':>7}"
    if not compact:
        head += f" {'devices':>7}"
    if show_block:
        head += "  blk" if compact else "  blocked by"
    print(head)
    print("-" * min(width, len(head)))

    ips = _answers_for(conn, [r[0] for r in rows], width=addr_w) \
        if show_ips else {}
    for qname, c, n in rows:
        line = f"{qname[:dom_w]:<{dom_w}}"
        if show_ips:
            # Never truncated: a half-shown address is worse than none.
            line += f" {ips.get(qname, '-'):<{addr_w}}"
        line += f" {c:>7}"
        if not compact:
            line += f" {n:>7}"
        if show_block:
            match = blocking.blocked_by(qname, table)
            # A colour alone is unreadable to anyone whose terminal does not
            # render it, so the mark is a word first and colour second.
            if match:
                line += ("  " + _c("BLK", "1;31")) if compact else (
                    "  " + _c("BLOCKED", "1;31") + " by " + match[:28])
            else:
                line += "  " + _c("ok" if compact else "ok", "32")
        print(line)


def latest_rows(conn, limit=8, window=None):
    """The most recent queries, newest first, each with its blocking verdict and
    the addresses the name resolved to.

    Structured rather than pre-formatted because two callers need this and they
    want different things: show_latest() renders it as text for the tray popup,
    and the tray's D-Bus menu turns each row into a menu entry. Returning
    rendered text would force the menu to strip it back apart.

    Each row is a dict so a caller adding a field later does not silently shift
    everyone else's tuple unpacking.
    """
    from lanlog import blocking

    table = _block_table()
    if window:
        sql = ("SELECT ts, client, qtype, qname FROM queries WHERE ts >= ? "
               "ORDER BY ts DESC LIMIT ?")
        args = (time.time() - window, limit)
    else:
        sql = ("SELECT ts, client, qtype, qname FROM queries "
               "ORDER BY ts DESC LIMIT ?")
        args = (limit,)

    rows = list(conn.execute(sql, args))
    # One query for every name on screen, not one per row: the live dashboard
    # repaints every second and a per-row lookup would repeat the same set walk.
    ips = _answers_for(conn, [r[3] for r in rows])

    out = []
    for ts, client, qtype, qname in rows:
        match = blocking.blocked_by(qname, table) if table else None
        out.append({
            "ts": ts,
            "clock": _fmt_clock(ts),
            "client": client,
            "qtype": qtype,
            "qname": qname,
            "address": ips.get(qname, ""),
            "blocked": match is not None,
            "match": match,
        })
    return out


def show_latest(conn, limit=8, window=None):
    """The live feed of recent queries, marked blocked or allowed.

    This is the same list the terminal dashboard shows as LATEST QUERIES, and it
    is the default tray view because the common question is "what is happening
    right now", not a ranking over the whole database.
    """
    from lanlog import devices as devcfg

    rows = latest_rows(conn, limit, window)
    if not rows:
        print("no queries recorded yet")
        return

    cfg = devcfg.load_config()
    names = identify.describe_all(conn)
    mode = _label_mode()
    show_block = bool(_block_table())
    # The device column is sized to the mode rather than fixed. "both" mode
    # produces "name (192.168.1.163)", which is 26 characters at the very
    # least, so the old hardcoded 24 truncated the address off the end of every
    # row -- which defeats the point of a mode whose job is to show you both.
    # Width is computed from the rows actually on screen so nothing is cut.
    dev_w = max([24] + [len(_label({"ip": r["client"]}, names, mode))
                        for r in rows])
    dev_w = min(dev_w, 44)
    # The mark column is only printed when the blocklist is readable. A column
    # of blanks would read as "nothing was blocked", which is the one claim this
    # must never make on no evidence.
    if show_block:
        print(f"{'time':<9} {'device':<{dev_w}} {'type':<6} {'':<8} domain")
        print("-" * (9 + dev_w + 6 + 8 + 6 + 44))
    else:
        print(f"{'time':<9} {'device':<{dev_w}} {'type':<6} domain")
        print("-" * (9 + dev_w + 6 + 6 + 44))

    n_blocked = 0
    for r in rows:
        label = _label({"ip": r["client"]}, names, mode)
        line = f"{_fmt_clock(r['ts']):<9} {_fit(label, dev_w):<{dev_w}} {r['qtype']:<6}"
        if show_block:
            # Pad the PLAIN text to the column width and colour it afterwards.
            # Padding the wrapped string instead counts the escape bytes as
            # visible characters, so every colourised row would pad to a
            # different width and the domain column would come out ragged --
            # and only on a real terminal, where colour is on.
            plain = "BLOCKED" if r["blocked"] else "ok"
            mark = _c(plain, "1;31" if r["blocked"] else "32")
            line += f" {mark:<8}{' ' * (8 - len(plain))}"
            n_blocked += 1 if r["blocked"] else 0
        line += f" {r['qname'][:44]}"
        if r.get("address"):
            line += f"  {_c(r['address'], '2')}"
        print(line)

    if show_block:
        tail = (f"{n_blocked} of {len(rows)} blocked"
                if n_blocked else f"none of {len(rows)} blocked")
        print(f"\n{tail}   (newest first)")
    else:
        print("\n(newest first; blocking unknown -- Pi-hole not readable)")


def show_blocked_domains(conn, limit=25, window=None):
    """Only the domains Pi-hole actually blocked, busiest first.

    Answers the question the overall top-domains list cannot: not "what is
    talking to the internet" but "what tried to get through and was stopped".
    A domain that is listed in gravity is counted once per query row, the same
    way the overall list counts, so the two are comparable.
    """
    from lanlog import blocking

    table = _block_table()
    if not table:
        print("cannot read Pi-hole's gravity database, so nothing can be")
        print("classified as blocked. Checked:")
        from lanlog import blocking as b
        for path in b.CANDIDATES:
            print(f"  {path}")
        print("set LANLOG_GRAVITY_DB to gravity.db if Pi-hole lives elsewhere.")
        return

    sql = ("SELECT qname, COUNT(*) c, COUNT(DISTINCT client) n, "
           "MAX(ts) last FROM queries")
    args = ()
    if window:
        sql += " WHERE ts >= ?"
        args = (time.time() - window,)
    sql += " GROUP BY qname ORDER BY c DESC"

    rows = []
    for qname, c, n, last in conn.execute(sql, args):
        match = blocking.blocked_by(qname, table)
        if match:
            rows.append((qname, c, n, match, last))

    if not rows:
        scope = f"in the last {window // 3600}h" if window else "so far"
        print(f"nothing blocked {scope} -- every query so far was allowed")
        return
    rows.sort(key=lambda r: -r[1])
    rows = rows[:limit]
    n_blocked_rows = sum(r[1] for r in rows)
    # Sized to the terminal; the fixed 88-column table wrapped on an 80-column
    # screen.
    width = _term_width()
    dom_w = 30 if width < 104 else 44
    by_w = 24 if width < 104 else 30
    print(f"{'domain':<{dom_w}} {'queries':>7} {'devices':>7}  blocked by")
    print("-" * min(width, dom_w + 30 + by_w))
    for qname, c, n, match, last in rows:
        print(f"{qname[:dom_w]:<{dom_w}} {c:>7} {n:>7}  {match[:by_w]}")
    print(f"\n{len(rows)} blocked domain(s), {n_blocked_rows} queries stopped"
          f"   (showing top {limit})")


def _c(text, code):
    """Colour helper, local so report.py stays importable with no dependencies."""
    return f"\033[{code}m{text}\033[0m"


def show_resolve(conn, name=None, limit=25, window=None):
    """What a name resolved to, or the most recently resolved names.

    Reads the `answers` table, which is built from the log's reply/cached lines.
    Those lines carry no client, so this report is deliberately name-keyed: it
    answers "where does this actually point", not "who looked it up" (that is
    `lanlog client`, off the query rows).
    """
    if name:
        like = name.strip().lower().rstrip(".")
        rows = list(conn.execute(
            "SELECT qname, ip, qtype, kind, first_seen, last_seen, hits "
            "FROM answers WHERE qname = ? OR qname LIKE ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (like, f"%.{like}", limit),
        ))
    else:
        sql = ("SELECT qname, ip, qtype, kind, first_seen, last_seen, hits "
               "FROM answers")
        args = ()
        if window:
            sql += " WHERE last_seen >= ?"
            args = (time.time() - window,)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        rows = list(conn.execute(sql, args + (limit,)))

    if not rows:
        print(f"no resolved addresses recorded for {name!r}" if name
              else "no resolved addresses recorded yet")
        print("(answers come from the log's reply/cached lines; if this is "
              "empty,")
        print(" the logger may have been started before this feature existed)")
        return

    # Sized to the terminal. In compact mode the `type` and `hits` columns go:
    # the address is the reason to run this command, so it keeps the room.
    width = _term_width()
    compact = width < 104
    dom_w = 28 if compact else 42
    addr_w = 34 if compact else 44
    if compact:
        head = f"{'domain':<{dom_w}} {'address':<{addr_w}} last"
        print(head)
        print("-" * min(width, len(head)))
        for qname, ip, _qtype, _kind, _first, last, _hits in rows:
            print(f"{qname[:dom_w]:<{dom_w}} {ip[:addr_w]:<{addr_w}} "
                  f"{_fmt_clock(last)}")
    else:
        head = (f"{'domain':<{dom_w}} {'address':<{addr_w}} {'type':<5} "
                f"{'last':<9} hits")
        print(head)
        print("-" * min(width, len(head)))
        for qname, ip, qtype, _kind, _first, last, hits in rows:
            print(f"{qname[:dom_w]:<{dom_w}} {ip[:addr_w]:<{addr_w}} "
                  f"{str(qtype or ''):<5} {_fmt_clock(last):<9} {hits}")
    n_names = len({r[0] for r in rows})
    print(f"\n{len(rows)} address(es) across {n_names} name(s)")


def show_client(conn, ip, limit=40):
    rows = list(conn.execute(
        "SELECT qname, qtype, COUNT(*) c, MAX(ts) last FROM queries WHERE client=? "
        "GROUP BY qname, qtype ORDER BY c DESC LIMIT ?",
        (ip, limit),
    ))
    if not rows:
        print(f"no queries recorded for {ip}")
        return
    names = identify.describe_all(conn)
    mode = _label_mode()
    name = _label({"ip": ip}, names, mode)
    total = sum(r[2] for r in rows)
    print(f"{name}  ({ip}): {total} queries, top {len(rows)}")
    # Sized to the terminal: the fixed widths here produced a 128-column row,
    # which wrapped on an 80-column screen and made the table unreadable.
    width = _term_width()
    compact = width < 104
    dom_w = 30 if compact else 44
    addr_w = 26 if compact else 40
    qcol = 6 if compact else 8
    head = f"{'domain':<{dom_w}} {'type':<6} {'queries':>{qcol}}  {'last':<9} address"
    print(head)
    print("-" * min(width, len(head)))
    ips = _answers_for(conn, [r[0] for r in rows], width=addr_w)
    for qname, qtype, c, last in rows:
        print(f"{qname[:dom_w]:<{dom_w}} {qtype:<6} {c:>{qcol}}  "
              f"{_fmt_clock(last):<9} {ips.get(qname, '-')}")


def show_shared(conn, limit=30):
    """Domains requested by more than one device -- shared infrastructure,
    or a tracker fanning out across devices."""
    rows = list(conn.execute(
        "SELECT qname, COUNT(DISTINCT client) n, COUNT(*) c, MAX(ts) last "
        "FROM queries GROUP BY qname HAVING n > 1 ORDER BY n DESC, c DESC LIMIT ?",
        (limit,),
    ))
    if not rows:
        print("no multi-device domains yet")
        return
    width = _term_width()
    dom_w = 40 if width < 104 else 46
    print(f"{'domain':<{dom_w}} {'devices':>7} {'queries':>7}  last")
    print("-" * min(width, dom_w + 26))
    for qname, n, c, last in rows:
        print(f"{qname[:dom_w]:<{dom_w}} {n:>7} {c:>7}  {_fmt_clock(last)}")


def show_third_party(conn, window=86400):
    """Group queries by registrable-ish domain, ignoring the big CDNs."""
    noise = {
        "google", "gstatic", "googleapis", "cloudflare", "akamaitech", "akamai",
        "amazonaws", "microsoft", "msftncsi", "apple", "mozilla", "ubuntu",
        "fedora", "verizon", "att", "samsung", "lg", "tizen", "dns.google",
    }
    # Reverse-DNS lookups are infrastructure noise, not third-party tracking:
    # grouping on them produced in-addr.arpa as a "shared third-party domain".
    skip_suffixes = ("in-addr.arpa", "ip6.arpa")
    buckets = defaultdict(set)
    last_seen = {}
    for qname, client, ts in conn.execute(
        "SELECT qname, client, ts FROM queries WHERE ts >= ?",
        (time.time() - window,)
    ):
        if qname.endswith(skip_suffixes):
            continue
        labels = qname.split(".")
        for i in range(len(labels) - 1):
            cand = ".".join(labels[i:])
            if labels[i] not in noise and len(cand.split(".")) >= 2:
                buckets[cand].add(client)
                if ts > last_seen.get(cand, 0):
                    last_seen[cand] = ts
    rows = [(d, len(v)) for d, v in buckets.items() if len(v) >= 2]
    rows.sort(key=lambda r: -r[1])
    if not rows:
        print("no cross-device third-party domains in the last 24h")
        return
    print(f"{'domain':<46} {'devices':>7}  last")
    print("-" * 66)
    for d, n in rows[:40]:
        print(f"{d[:46]:<46} {n:>7}  {_fmt_clock(last_seen.get(d, 0))}")


def main():
    import argparse

    ap = argparse.ArgumentParser(prog="lanlog-report")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("devices"); p.add_argument("--hours", type=float)
    p = sub.add_parser("domains"); p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("blocked")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--hours", type=float)
    sub.add_parser("shared").add_argument("--limit", type=int, default=30)
    sub.add_parser("third-party")

    c = sub.add_parser("client"); c.add_argument("ip"); c.add_argument("--limit", type=int, default=40)

    args = ap.parse_args()
    conn = db.connect()
    window = args.hours * 3600 if getattr(args, "hours", None) else None

    if args.cmd == "devices":
        show_devices(conn, window)
    elif args.cmd == "domains":
        show_top_domains(conn, args.limit, window)
    elif args.cmd == "blocked":
        bw = args.hours * 3600 if args.hours else None
        show_blocked_domains(conn, args.limit, bw)
    elif args.cmd == "shared":
        show_shared(conn, args.limit)
    elif args.cmd == "third-party":
        show_third_party(conn)
    elif args.cmd == "client":
        show_client(conn, args.ip, args.limit)


if __name__ == "__main__":
    main()
