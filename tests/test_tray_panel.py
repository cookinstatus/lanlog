"""Test the tray's menu -> action dispatch, without a display or a panel.

What this guards: the DbusMenu Event handler is reached over D-Bus, so a bad
attribute or a wrong argument shape only shows up when a host delivers a real
click. On this machine the first version of "click a query row" raised
AttributeError inside the D-Bus dispatch and the menu simply highlighted and
closed. That failure is invisible to any test that does not actually call
Event, so this calls it directly.

The menu is now ONLY the query feed -- no report rows, no quit -- so these
assert that shape rather than the old one. Getting this wrong in the other
direction is the bug that was reported: a menu of navigation items standing
between a single click and the data.

Runs headless: the app object is built with __new__ and the widgets it touches
are recorded, not realised.

Run: /usr/bin/python3 test_tray_menu.py
"""

import os
import sys
import sqlite3
import inspect
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The tray imports Gtk and dbus at module import time. Both exist here, but
# Gtk must not try to open a display, and the D-Bus classes are never used
# because the object under test is built with __new__.
os.environ.setdefault("GDK_BACKEND", "x11")
try:
    import importlib.util
    from importlib.machinery import SourceFileLoader

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "lanlog-tray")
    # The tray has no .py suffix, so spec_from_file_location cannot pick a
    # loader for it and returns a spec whose loader is None -- which then fails
    # with "'NoneType' object has no attribute 'loader'". SourceFileLoader takes
    # an explicit loader and does not care about the extension.
    loader = SourceFileLoader("lanlog_tray", path)
    spec = importlib.util.spec_from_loader("lanlog_tray", loader)
    tray = importlib.util.module_from_spec(spec)
    loader.exec_module(tray)
except Exception as exc:  # noqa: BLE001
    print(f"could not import the tray: {exc!r}")
    sys.exit(2)

passed = True


def check(label, got, want):
    global passed
    ok = got == want
    passed = passed and ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}"
          + ("" if ok else f" (wanted {want!r})"))


class FakeEntry:
    def __init__(self):
        self.text = ""

    def set_text(self, v):
        self.text = v

    def get_text(self):
        return self.text


class FakeRow:
    def __init__(self):
        self.visible = None

    def set_visible(self, v):
        self.visible = v


class FakeWin:
    def __init__(self):
        self.current = "devices"
        self.client_entry = FakeEntry()
        self.client_row = FakeRow()
        self.shown = False
        self.loaded = 0
        self.presented = 0

    def show_all(self):
        self.shown = True

    def present(self):
        self.presented += 1

    def _load(self, *_a):
        self.loaded += 1


class FakeNotebook:
    def __init__(self):
        self.page = 0

    def get_current_page(self):
        return self.page

    def get_n_pages(self):
        return len(tray.TABS)

    def set_current_page(self, n):
        self.page = n


class FakePopup:
    """Records show_at calls instead of mapping a real window."""

    def __init__(self):
        self.hidden = 0
        self.current = "latest"
        self.shown_at = []
        # The popup is a Gtk.Stack plus a row of ToggleButtons now, not a
        # Gtk.Notebook, so the fake tracks the visible child name. Anything
        # reading .notebook here would be testing a widget the tray no longer
        # builds.

    def hide(self):
        self.hidden += 1

    def show_at(self, view, at=None):
        # The real TrayPopup.load() sets self.current, which is what on_scroll
        # reads. A fake that recorded the call without updating current would
        # make every flick resolve from the same starting point and the wrap
        # test would fail for a reason that is not a tray bug.
        self.shown_at.append(view)
        if view in dict(tray.TABS):
            self.current = view

    def get_visible(self):
        return False

    def _tab_index(self, view):
        for i, (k, _l) in enumerate(tray.TABS):
            if k == view:
                return i
        return 0


class FakeItem:
    def unregister(self):
        pass


class FakeApp:
    def __init__(self):
        self.win = FakeWin()
        self.popup = FakePopup()
        self.item = FakeItem()
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1

    # Bound from the real class so the test exercises the shipped signatures
    # rather than copies that can drift from them.
    show_window = tray.LanlogApp.show_window
    on_scroll = tray.LanlogApp.on_scroll


def build_menu(app):
    m = tray.DbusMenu.__new__(tray.DbusMenu)
    m.app = app
    m._ids = {}
    m._rev = 0
    m._rebuild()
    return m


# A real database with a couple of rows, so the menu has query entries to
# assert on and the test does not depend on whatever the live logger recorded,
# or on Pi-hole being installed.
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
_real = os.environ.get("LANLOG_DB")
os.environ["LANLOG_DB"] = tmp.name
con = sqlite3.connect(tmp.name)
# The real schema, column for column. An earlier version of this fixture used a
# guessed schema, and identify.describe_all() raised OperationalError on the
# missing `vendor` column -- which _rebuild() swallows, so every menu row
# silently degraded to the "no queries recorded yet" info line. The test then
# failed for a reason that looked like a tray bug and was not one.
con.executescript("""
    CREATE TABLE queries (id INTEGER PRIMARY KEY, ts REAL, client TEXT,
                          qname TEXT, qtype TEXT, outcome TEXT, detail TEXT,
                          source TEXT);
    CREATE TABLE devices (ip TEXT, mac TEXT, vendor TEXT, hostname TEXT,
                          first_seen REAL, last_seen REAL);
""")
con.executemany(
    "INSERT INTO queries (ts, client, qname, qtype, outcome, source) "
    "VALUES (?,?,?,?,?,?)",
    [(1000.0, "192.168.1.10", "a.example.com", "A", "query", "pihole"),
     (1001.0, "192.168.1.11", "b.example.com", "HTTPS", "query", "pihole")])
con.execute("INSERT INTO devices VALUES ('192.168.1.10', NULL, 'Acme', NULL, "
            "900, 1000)")
con.commit()
con.close()

try:
    print("menu shape -- queries only, no navigation:")
    app = FakeApp()
    m = build_menu(app)
    kinds = [e[1] for e in m._ids.values()]
    labels = [e[0] for e in m._ids.values()]
    check("every row is a query", set(kinds), {"query"})
    check("no report rows", [k for k in kinds if k == "view"], [])
    check("no quit row", [k for k in kinds if k == "quit"], [])
    check("no separator", [k for k in kinds if k == "separator"], [])
    check("row count matches the database", len(kinds), 2)
    check("labels carry a mark", all(l[:2] in ("· ", "■ ") for l in labels),
          True)
    check("labels carry the device",
          any("192.168.1.10" in l for l in labels), True)
    check("every row is clickable",
          all(bool(m._props_for(i)["enabled"]) for i in m._ids), True)

    print("\nclicking a query opens the popup on Devices, NOT a new window:")
    app = FakeApp()
    m = build_menu(app)
    qid = next(i for i, e in m._ids.items() if e[1] == "query")
    m.Event(qid, "clicked", None, 0)
    check("popup shown at devices", app.popup.shown_at, ["devices"])
    check("no new window", app.win.shown, False)
    check("quit not called", app.quit_calls, 0)

    print("\nscrolling switches tabs, not reports:")
    app = FakeApp()
    app.on_scroll(1, 0)
    check("forward -> devices", app.popup.shown_at, ["devices"])
    app.on_scroll(1, 0)
    check("again -> wraps to the queries tab", app.popup.shown_at,
          ["devices", "latest"])
    app.on_scroll(-1, 0)
    check("back -> wraps to devices", app.popup.shown_at[-1], "devices")
    app.popup.shown_at.clear()
    app.on_scroll(1, 1)
    check("horizontal ignored", app.popup.shown_at, [])

    print("\nthe popup has exactly two tabs, queries first:")
    check("tab keys", [k for k, _l in tray.TABS], ["latest", "devices"])
    check("both tabs are real views",
          all(k in dict(tray.VIEWS) for k, _l in tray.TABS), True)

    print("\nempty database still produces a usable menu:")
    empty = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    empty.close()
    ec = sqlite3.connect(empty.name)
    ec.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, ts REAL, "
               "client TEXT, qname TEXT, qtype TEXT, outcome TEXT, "
               "detail TEXT, source TEXT)")
    ec.execute("CREATE TABLE devices (ip TEXT, mac TEXT, vendor TEXT, "
               "hostname TEXT, first_seen REAL, last_seen REAL)")
    ec.commit()
    ec.close()
    os.environ["LANLOG_DB"] = empty.name
    app = FakeApp()
    m = build_menu(app)
    kinds = [e[1] for e in m._ids.values()]
    check("info row appears", "info" in kinds, True)
    check("info row is not clickable",
          bool(m._props_for(next(i for i, e in m._ids.items()
                                 if e[1] == "info"))["enabled"]), False)
    os.unlink(empty.name)

    print("\nLanlogApp still has the methods the menu and panel call:")
    check("quit exists", hasattr(tray.LanlogApp, "quit"), True)
    check("show_window takes client_ip", "client_ip" in
          tray.LanlogApp.show_window.__code__.co_varnames, True)
    check("on_scroll exists", hasattr(tray.LanlogApp, "on_scroll"), True)
    check("popup load takes keep_scroll", "keep_scroll" in
          tray.TrayPopup.load.__code__.co_varnames, True)

    print("\nThe popup is a button tab bar over a Gtk.Stack, not a notebook:")
    # A Gtk.Notebook draws its own tab strip from the theme and no CSS selector
    # reliably reaches it -- every candidate was tried and the strip came out in
    # the default light theme on a dark window, allocated and mapped but
    # invisible. Ordinary ToggleButtons are styled by selectors that do match.
    # A class object never has the instance attribute, so this checks the
    # instance built in __init__ via the source, not hasattr on the class.
    check("builds a stack",
          "self.stack = Gtk.Stack()" in
          inspect.getsource(tray.TrayPopup.__init__), True)
    check("builds toggle buttons for tabs",
          "Gtk.ToggleButton" in inspect.getsource(tray.TrayPopup.__init__),
          True)
    check("_on_tab_switched is gone", hasattr(tray.TrayPopup,
                                              "_on_tab_switched"), False)
    check("has _select_tab", hasattr(tray.TrayPopup, "_select_tab"), True)
    check("has _on_tab_toggled", hasattr(tray.TrayPopup, "_on_tab_toggled"),
          True)
    # Two tabs, Queries first: a single click must land on the live feed.
    check("TABS is queries then devices", tray.TABS,
          [("latest", "Queries"), ("devices", "Devices")])
    # The guards that stop the toggle handler re-entering itself. Without them
    # clicking Devices left Queries looking selected, because the handler set
    # the stack child without clearing the other button.
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bin", "lanlog-tray")).read()
    check("tab buttons are ToggleButtons", "Gtk.ToggleButton" in src, True)
    check("tab bar has its own style class", "lanlog-tabbar" in src, True)
    check("checked state is styled", "button.lanlog-tab:checked" in src, True)

    print("\nThe tabbed popup is reachable by a single press (middle click):")
    check("SecondaryActivate exists",
          hasattr(tray.StatusNotifierItem, "SecondaryActivate"), True)
    # Both routes must go through the app, which owns the popup. A helper on
    # the SNI item itself raised AttributeError on every click, live, because
    # self.popup lives on LanlogApp.
    check("Activate calls the app", "self.app.on_activate" in inspect.getsource(
        tray.StatusNotifierItem.Activate), True)
    check("SecondaryActivate calls the app",
          "self.app.on_activate" in inspect.getsource(
              tray.StatusNotifierItem.SecondaryActivate), True)
    check("no open_popup helper left on the item",
          hasattr(tray.StatusNotifierItem, "open_popup"), False)
    # The left button cannot be used: the GNOME appindicator extension decides
    # between the menu and Activate() itself, and a single left press only ever
    # reaches menu.toggle(). SecondaryActivate is the one single-press route
    # that is left, so it must open the same tabbed popup as Activate.
    check("SecondaryActivate opens the popup, not the window",
          "show_window" not in inspect.getsource(
              tray.StatusNotifierItem.SecondaryActivate), True)

finally:
    if _real is None:
        os.environ.pop("LANLOG_DB", None)
    else:
        os.environ["LANLOG_DB"] = _real
    os.unlink(tmp.name)

print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
sys.exit(0 if passed else 1)
