"""ASCII banner for the lanlog dashboard.

The logo is a "Standard"-style slant figure for LANLOG, which reads far better
at terminal size than block letters -- block figures are dense and were hard to
read at a glance.

The logo is 76 columns and 11 rows, which fills the width budget on its own,
so there is no room for bursts beside it. Fireworks therefore sit in a 3-row
band ABOVE the logo, drawn at varying horizontal offsets so they twinkle
between refreshes without ever changing the total width.

Width is fixed at 76 so the dashboard never reflows.
"""

import random
import sys

BOLD = "\033[1m"
RST = "\033[0m"

# Bold "Shadow"-style block letters.
#
# Chosen over the earlier 11-row slant figure for two reasons: it is 5 rows
# instead of 15 including the fireworks band, and the solid block forms stay
# legible at a glance, where the thin slashes were easy to misread. Only the
# five solid letterform rows are kept -- the dithered rows underneath in the
# original art are a drop shadow that reads as noise at this size.
LOGO = [
    " ██▓    ▄▄▄       ███▄    █  ██▓     ▒█████    ▄████",
    "▓██▒   ▒████▄     ██ ▀█   █ ▓██▒    ▒██▒  ██▒ ██▒ ▀█▒",
    "▒██░   ▒██  ▀█▄  ▓██  ▀█ ██▒▒██░    ▒██░  ██▒▒██░▄▄▄░",
    "▒██░   ░██▄▄▄▄██ ▓██▒  ▐▌██▒▒██░    ▒██   ██░░▓█  ██▓",
    "░██████▒▓█   ▓██▒▒██░   ▓██░░██████▒░ ████▓▒░░▒▓███▀▒",
]

# Width of the logo itself. The banner no longer pads to a fixed column, since
# there is no fireworks band beside it -- this is used only for the height
# budget and for centring the settings menu.
LOGO_W = max(len(r) for r in LOGO)

# Column the fireworks band is drawn to if it is ever re-enabled. Unused while
# BAND_H is 0.
BANNER_W = 76

BAND_H = 0

# The fireworks band is gone. The block logo fills the space the band and the
# slant rows used to take, so the dashboard is 10 rows shorter than before with
# nothing lost. MOTIFS/SCENES are kept out of the render path entirely rather
# than deleted, so restoring them is a one-line change if the logo ever
# changes shape again.
MOTIFS = [
    [" .*. ", "*.*.*", " * * "],   # radial
    [" \\|/ ", " /|\\ ", "  *  "],   # fountain
    [" o.o ", " \\|/ ", "  *  "],   # ring
]

SCENES = [
    (0, (4, 34, 60)),
    (1, (20, 50, 10)),
    (2, (2, 30, 68)),
    (0, (14, 44, 26)),
    (1, (8, 40, 58)),
]

COLORS = ["\033[33m", "\033[36m", "\033[32m", "\033[35m", "\033[31m"]


def _c(txt, code):
    return f"{code}{txt}{RST}" if sys.stdout.isatty() else txt


def _band(scene):
    """Fireworks band. Disabled: BAND_H is 0, so this returns no rows.

    Kept so the band can be switched back on by setting BAND_H and restoring
    BANNER_W without touching the callers.
    """
    if not BAND_H:
        return []
    motif_idx, offsets = SCENES[scene % len(SCENES)]
    motif = MOTIFS[motif_idx]
    width = max(LOGO_W, BANNER_W)
    rows = []
    for r in range(BAND_H):
        row = [" "] * width
        for off in offsets:
            for c, ch in enumerate(motif[r]):
                pos = off + c
                if 0 <= pos < width and ch != " ":
                    row[pos] = ch
        rows.append("".join(row).rstrip())
    return rows


def compact(text=None):
    """Collapse the logo to a single bold line for very short terminals."""
    return "  \\/\\/\\  L A N L O G  /\\/\\/"


def banner(rng=None):
    """Return the LANLOG logo, with the fireworks band above it if enabled."""
    rng = rng or random
    col = rng.choice(COLORS)

    lines = [_c(row, col) for row in _band(rng.randrange(len(SCENES)))]
    if lines:
        lines.append("")                  # spacer between fireworks and logo
    lines.extend(_c(row, BOLD) for row in LOGO)
    return "\n".join(lines)


def banner_lines(rng=None):
    """Banner as a list of lines."""
    return banner(rng).splitlines()


HELP = [
    "devices  inventory      name set <ip> \"nick\"   mac <mac-or-ip>",
    "domains  top queries    name list                toggle labels",
    "client <ip>              config [key val]        Ctrl-C to quit",
    "shared   multi-device   third-party",
]


def help_panel():
    return "\n".join(f"  {_c(h, chr(90))}" for h in HELP)
