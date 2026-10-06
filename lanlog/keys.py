"""Raw-mode keyboard input for the live dashboard.

The dashboard repaints every 2 seconds with a clear-screen sequence, so
anything the user types gets wiped. Two problems follow:

  1. Typing a command in the shell is impossible while it runs.
  2. On an 80x24 terminal the 14-row banner alone does not fit, so the display
     scrolls on every repaint.

This module handles keystrokes in cbreak mode and exposes a small command bar,
so commands are typed INTO the dashboard rather than in the shell.
"""

import os
import select
import sys
import termios
import time
import tty

# Digit keys -> command. Press the digit, type the rest, Enter runs it.
QUICK = {
    "1": "devices",
    "3": "shared",
    "4": "third-party",
    "5": "client ",
    "6": "name list",
    "7": "config",          # opens the settings menu
    "0": "toggle",          # cycle device labels: names -> ip -> both
}

# [2] does NOT run a command directly -- it opens this submenu. "domains" and
# "top blocked domains" are two answers to the same question and the whole point
# of the feature is choosing between them, so the digit picks the question and
# the submenu picks the report. A submenu costs one extra keystroke and saves
# remembering which of two similar command names to type.
DOMAIN_MENU = [
    ("1", "domains", "top domains by query count"),
    ("2", "blocked", "only what Pi-hole BLOCKED"),
]
DOMAIN_MENU_BY_KEY = {k: cmd for k, cmd, _h in DOMAIN_MENU}

BAR_HINT = (
    "  [1]devices [2]domains [3]shared [4]3rd-party [5]client "
    "[6]names [7]settings [0]labels   Esc quit"
)

# Key [7] opens this menu instead of seeding a "config" command. Typing
# "config query_lines 20" by hand was too easy to get wrong -- a dropped
# leading space turns it into "configquery_lines", which is not a command.
CONFIG_MENU = [
    ("1", "query_lines", "rows of recent queries (1-25)"),
    ("2", "refresh", "repaint interval in seconds (1-30)"),
    ("3", "hide_idle", "hide devices with no queries  yes/no"),
    ("4", "only", "comma-separated IPs, or blank for all"),
    # names / ip / both. [0] in the command bar cycles this instead, which is
    # the faster route when you only want to see whether names are resolving.
    ("5", "labels", "device column: names / ip / both"),
]
MENU_BY_KEY = {k: name for k, name, _h in CONFIG_MENU}


class Keys:
    """cbreak-mode keystroke reader. Always restores the terminal on exit."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.fd = self.stream.fileno()
        self.tty = os.isatty(self.fd)
        self._saved = None
        self.buffer = ""          # command being typed
        self.active = False       # bar has focus
        self.pending = None       # completed command awaiting execution
        self.last_digit = None    # quick-key digit awaiting expansion
        self.menu = None          # "config" while the settings menu is open
        self.menu_key = None      # setting chosen from the menu, awaiting value

    # -- terminal state -------------------------------------------------
    def __enter__(self):
        if self.tty:
            self._saved = termios.tcgetattr(self.fd)
            # cbreak still leaves ECHO on, so every keystroke the dashboard
            # consumes gets echoed back at the cursor -- keystrokes appeared
            # inline in the repainted frame. Disable it; the dashboard renders
            # its own command bar instead.
            mode = termios.tcgetattr(self.fd)
            mode[3] &= ~termios.ECHO
            termios.tcsetattr(self.fd, termios.TCSANOW, mode)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.tty and self._saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
        return False

    # -- input ----------------------------------------------------------
    def poll(self, timeout=0.25):
        """Read any pending keystrokes. Returns True if state changed.

        Never blocks longer than `timeout`, so the refresh loop keeps running.
        """
        if not self.tty:
            time.sleep(timeout)
            return False
        r, _, _ = select.select([self.stream], [], [], timeout)
        if not r:
            return False
        try:
            data = os.read(self.fd, 64).decode(errors="replace")
        except OSError:
            return False

        for ch in data:
            if ch == "\x03":                      # Ctrl-C
                raise KeyboardInterrupt

            # -- domain submenu state machine -------------------------
            # [2] opens this; a digit picks the report and runs it straight
            # away. No value to type, so unlike the settings menu there is no
            # second step -- one keypress in, one report out.
            if self.menu == "domains":
                if ch in DOMAIN_MENU_BY_KEY:
                    self.menu = None
                    self.pending = DOMAIN_MENU_BY_KEY[ch]
                elif ch == "\x1b":
                    # Esc backs out to the dashboard. It must NOT quit: the
                    # same reasoning as the settings menu.
                    self.menu = None
                continue

            # -- settings menu state machine ---------------------------
            # [7] opens the menu, a digit picks the setting, the value is
            # typed, Enter applies. Keeping this out of the free-text buffer
            # means the user never has to spell a setting name correctly.
            if self.menu == "config" and self.menu_key is None:
                if ch in MENU_BY_KEY:
                    self.menu_key = MENU_BY_KEY[ch]
                    self.active = True
                elif ch == "\x1b":
                    # Esc closes the menu and returns to the dashboard. It must
                    # NOT quit here -- quitting straight out of a menu you just
                    # opened by accident is the wrong outcome.
                    self.menu = None
                # Enter with no pick does nothing.
                continue

            if self.menu == "config" and self.menu_key is not None:
                if ch in ("\n", "\r"):
                    value = self.buffer.strip()
                    key = self.menu_key
                    self.buffer = ""
                    self.active = False
                    self.menu = None
                    self.menu_key = None
                    if value:
                        # Blank clears string settings like "only", which the
                        # user reaches by pressing Enter on purpose.
                        self.pending = f"config {key} {value}"
                    else:
                        self.pending = f"config {key}"
                elif ch == "\x1b":
                    self.buffer = ""
                    self.active = False
                    self.menu = None
                    self.menu_key = None
                elif ch in ("\x7f", "\b"):
                    self.buffer = self.buffer[:-1]
                elif ch.isprintable():
                    self.buffer += ch
                continue

            if ch in ("\n", "\r"):
                # lstrip, NOT strip: quick key [5] seeds the buffer with a
                # trailing space ("client ") so the next keystroke lands
                # correctly. strip() removed it and the result ran as
                # "client192.168.1.5" -- an invalid command that silently
                # reported as unavailable. Trailing space in the rest of a
                # command is harmless because the parser splits on whitespace.
                cmd = self.buffer.lstrip()
                self.buffer = ""
                self.active = False
                if cmd:
                    self.pending = cmd
                continue
            if ch == "\x1b":                      # Esc
                if self.buffer:
                    # Mid-command: Esc cancels what you are typing. Losing a
                    # half-typed command to a quit would be worse than having
                    # to press Esc twice.
                    self.buffer = ""
                    self.active = False
                else:
                    # Bar is idle: Esc is the quit key.
                    #
                    # 'q' used to be the quit key and it was wrong: it quit the
                    # whole dashboard whenever a 'q' landed alone on an idle
                    # bar, which killed commands like "config query_lines 20"
                    # and "shared". A quit key that collides with ordinary
                    # command text is not worth having.
                    raise KeyboardInterrupt
                continue
            if ch in ("\x7f", "\b"):              # Backspace
                self.buffer = self.buffer[:-1]
                continue
            if not self.active and ch == "2":
                # Opens the domains submenu rather than running "domains".
                # "2" is deliberately NOT in QUICK: it must not seed the bar,
                # or the submenu would be unreachable.
                self.menu = "domains"
                self.menu_key = None
                self.buffer = ""
                self.active = False
                continue
            if not self.active and ch == "7":
                # Opens the settings menu rather than seeding "config".
                self.menu = "config"
                self.menu_key = None
                self.buffer = ""
                self.active = False
                continue
            if not self.active and ch in QUICK:
                # Quick key: seed the command immediately.
                #
                # This used to defer to the caller via last_digit, but that
                # broke when a digit and Enter arrived in the SAME read: the
                # digit was parked, then Enter was handled against a still-empty
                # buffer and discarded. Fast typists (and key repeat) hit this
                # constantly, leaving the bar stuck showing "devices" with
                # nothing running. Expanding inline keeps the two in order.
                self.active = True
                self.buffer = QUICK.get(ch, "")
                continue
            if ch.isprintable():
                if not self.active:
                    # First printable key opens the bar and starts a command.
                    self.active = True
                self.buffer += ch
        return True

    # -- rendering ------------------------------------------------------
    def bar(self):
        """The bottom command bar. Returns (text, is_active).

        Three states, in priority order: a value being typed for a chosen
        setting, the menu itself, then the normal hint line.
        """
        if self.menu == "domains":
            return "  DOMAINS  " + "  ".join(
                f"[{k}]{n}" for k, n, _h in DOMAIN_MENU
            ) + "   Esc back", False
        if self.menu == "config":
            if self.menu_key:
                return f"  {self.menu_key} = {self.buffer}\u2588", True
            return "  SETTINGS  " + "  ".join(
                f"[{k}]{n}" for k, n, _h in CONFIG_MENU
            ) + "   Esc back", False
        if self.active or self.buffer:
            return f"  > {self.buffer}\u2588", True
        return f"{BAR_HINT}", False

    def menu_lines(self, current=None):
        """The settings menu, one entry per line, for the panel above the bar.

        `current` is the loaded config dict so the live values are visible.
        """
        out = ["  SETTINGS -- press a number, then type the value, Enter applies"]
        for k, name, hint in CONFIG_MENU:
            val = (current or {}).get(name, "")
            out.append(f"   [{k}] {name:<12} {val:<10} {hint}")
        out.append("        Esc back to the dashboard")
        return out

    def domain_menu_lines(self):
        """The [2] domains submenu, one entry per line.

        Unlike the settings menu there is no live value column: neither entry
        takes an argument, so a value column would be empty on every row.
        """
        out = ["  DOMAINS -- pick a report"]
        for k, name, hint in DOMAIN_MENU:
            out.append(f"   [{k}] {name:<12} {hint}")
        out.append("        Esc back to the dashboard")
        return out

    def expand(self, digit):
        """Seed the bar with a quick-command prefix."""
        self.active = True
        self.buffer = QUICK.get(digit, "")


def fits(banner_lines, body_lines, extra=0):
    """True if the whole dashboard fits the terminal height."""
    try:
        rows = os.get_terminal_size().lines
    except OSError:
        return True
    return len(banner_lines) + len(body_lines) + extra <= rows
