"""Was this domain blocked, and by what?

The `queries.outcome` column has always been written as the literal "query".
That is not a bug in the schema -- it is what the Pi-hole query log actually
contains. The log's `query[A] name from ip` lines are emitted for EVERY lookup,
blocked or not, and the line carries no verdict. The separate
`gravity blocked <name> is 0.0.0.0` lines do carry a verdict but no client IP,
so joining them back to a query would mean guessing which device asked.

So blocking cannot be recovered from the log stream. It is read from Pi-hole's
own gravity database instead, which is the authoritative list of what is
blocked. That is the trade the ingest comment already describes ("Blocking
status is read from Pi-hole's gravity instead") -- this module is that half,
implemented.

Why a subdomain walk and not a plain set lookup
----------------------------------------------
Pi-hole blocks a listed domain AND everything beneath it. A query for
`ads.doubleclick.net` is blocked because `doubleclick.net` is listed, so a
membership test on the full name alone reports "not blocked" for a domain that
was in fact blocked. Every ancestor suffix is therefore tested, shortest first,
and the match that fired is what gets reported -- knowing *which* listed domain
caught it is the part that makes the answer actionable.

The lookup is read-only and cached
----------------------------------
gravity.db holds ~300k rows on a stock install, so it is loaded once into a set
and reused, keyed on the file's mtime+size so an adlist update is picked up
without a restart. Opened with mode=ro: this is Pi-hole's live database and the
viewer must never take a write lock against FTL.
"""

import os
import sqlite3
import threading
import time

# Candidate locations for gravity.db, best first. Rootless podman bind-mounts the
# container's /etc/pihole into the user's home; a docker install usually leaves it
# only inside the container. Callers may override with the LANLOG_GRAVITY_DB
# environment variable.
CANDIDATES = (
    os.path.expanduser("~/.local/share/pihole/etc/gravity.db"),
    os.path.expanduser("~/podman/pihole/etc-pihole/gravity.db"),
    "/etc/pihole/gravity.db",
)

# How long a failed lookup is cached before retrying. Gravity can appear after
# lanlog starts (Pi-hole pulled later, mount created later), so a miss must not
# be permanent, but it also must not stat the filesystem on every query.
_MISS_TTL = 60.0

_lock = threading.Lock()
_cache = {
    "path": None,
    "stamp": None,     # (mtime, size) of the file the cache was built from
    "domains": frozenset(),
    "loaded": 0.0,
    "missing": None,   # path we looked for and did not find
}


def gravity_path():
    """Path to gravity.db, or None when it cannot be found."""
    env = os.environ.get("LANLOG_GRAVITY_DB")
    if env:
        return env if os.path.exists(env) else None
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def domains(force=False):
    """The set of blocked domains, as a frozenset. Empty when unavailable.

    Cached against the file's mtime and size, so a gravity update is picked up on
    the next call without the caller doing anything, and without re-reading
    300k rows on every repaint of a live dashboard.
    """
    path = gravity_path()
    with _lock:
        if path is None:
            # Remember the miss briefly so a dashboard repainting every second
            # does not re-probe every candidate path every second.
            if (_cache["missing"] is None
                    or time.time() - _cache["loaded"] > _MISS_TTL):
                _cache["missing"] = True
                _cache["loaded"] = time.time()
            return _cache["domains"]

        _cache["missing"] = None
        stamp = _stamp(path)
        if (not force and stamp is not None
                and stamp == _cache["stamp"]
                and _cache["domains"]):
            return _cache["domains"]

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT domain FROM gravity").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return _cache["domains"]

        # Pi-hole stores gravity lowercased, but a hand-added entry can differ in
        # case; normalise on load so a lookup never misses on case alone.
        loaded = frozenset(
            r[0].strip().lower().rstrip(".") for r in rows if r and r[0]
        )
        _cache["domains"] = loaded
        _cache["path"] = path
        _cache["stamp"] = stamp
        _cache["loaded"] = time.time()
        return loaded


def blocked_by(qname, _table=None):
    """The listed domain that blocks `qname`, or None if it is not blocked.

    Returns the MATCHING LISTED DOMAIN rather than a bool so a caller can say
    "blocked by doubleclick.net" instead of just "blocked" -- with ~300k entries
    in gravity, which listed domain caught it is the useful half of the answer.

    `localhost.localdomain` and friends are in gravity on a stock install, so a
    name that is itself listed wins over any ancestor, matching how dnsmasq
    evaluates the lists.
    """
    if not qname:
        return None
    table = domains() if _table is None else _table
    if not table:
        return None
    name = qname.strip().lower().rstrip(".")
    if name in table:
        return name
    # Walk up the labels: a.b.example.com -> b.example.com -> example.com.
    # Two labels are kept at minimum because a bare TLD is never a listed
    # domain in gravity and matching one would produce nonsense.
    labels = name.split(".")
    for i in range(1, max(1, len(labels) - 1)):
        parent = ".".join(labels[i:])
        if parent and parent in table:
            return parent
    return None


def is_blocked(qname, _table=None):
    """True if `qname` is blocked by Pi-hole's gravity."""
    return blocked_by(qname, _table) is not None


def status(qname, _table=None):
    """(verdict, matched_listed_domain) for one query name.

    verdict is "blocked", "allowed", or "unknown" when gravity.db is not
    readable. "unknown" is deliberately distinct from "allowed": reporting an
    unreadable blocklist as "allowed" would state as fact that nothing is being
    blocked, which is the one thing this function exists to tell you.
    """
    path = gravity_path()
    if path is None:
        return "unknown", None
    table = domains() if _table is None else _table
    if not table:
        return "unknown", None
    match = blocked_by(qname, table)
    return ("blocked", match) if match else ("allowed", None)
