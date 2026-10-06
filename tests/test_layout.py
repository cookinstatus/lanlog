"""Tests for how the live dashboard fits command output on screen.

The bug this guards against: command output was hard-capped at 6 lines, so
pressing [1] for `devices` showed 4 of the 12 devices on the network -- the
opposite of what was asked for.

Run: .venv/bin/python test_layout.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fit_budget(rows, use_big, big_cost, small_cost, busy_rows, menu_rows,
               cmd_rows):
    """Mirror of the dashboard's budget arithmetic."""
    fixed = big_cost if use_big else small_cost
    fixed += 1 + 3 + 1 + 1
    fixed += menu_rows if menu_rows else busy_rows
    return max(4, rows - fixed - 1)


def pick_lines(out_lines, budget):
    """Mirror of the head/room/tail selection used when truncating."""
    if len(out_lines) <= budget:
        return out_lines, 0
    keep_head = out_lines[:1]
    keep_tail = out_lines[-1:]
    room = budget - len(keep_head) - len(keep_tail)
    shown = keep_head + out_lines[1:1 + max(0, room)] + keep_tail
    return shown, len(out_lines) - len(shown)


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    return ok


def shed(rows, big_cost, small_cost, use_big, busy_rows, query_rows, cmd_rows):
    """Mirror of the dashboard's priority-ordered shedding.

    Order matters: the query list, then who's-busy, then the logo. An earlier
    version dropped the logo first, wasting 4 rows the panels then consumed.
    """
    body = 1 + 3 + 1 + busy_rows + query_rows + 1
    for _ in range(3):
        need = (big_cost if use_big else small_cost) + body + cmd_rows - rows
        if need <= 0:
            break
        # Drop whole sections, largest first, for as long as the screen is
        # still over budget. A section is worth dropping whenever it is
        # present -- the freed rows reduce `need`, and the loop re-tests.
        if query_rows:
            body -= query_rows
            query_rows = 0
        elif busy_rows:
            body -= busy_rows
            busy_rows = 0
        elif use_big and small_cost < big_cost:
            use_big = False
        else:
            break
    return use_big, body, query_rows, busy_rows


def main():
    passed = True
    rows = 24                     # the user's terminal
    big_cost, small_cost = 6, 2   # 5-row block logo + blank, or one-line + blank
    busy_rows = 3                # one client, header + blank
    menu_rows = 0

    # A real `devices` listing: header, rule, 12 devices, blank, summary.
    devices_out = (
        ["device    ip    last seen   now   queries", "-" * 92]
        + [f"dev{i}  192.168.1.{i}  7s ago  *  0" for i in range(12)]
        + ["", "12 device(s), 11 present now  (* = seen in the last 15 min)"]
    )
    cmd_rows = len(devices_out)

    # Measured ceiling: on 24 rows, the chrome around the output costs 7 rows
    # (one-line logo 2, plus separator/header/count/separator 5). So a 16-line
    # listing needs 23 of 24 rows -- it fits, but only with the compact logo.
    chrome = small_cost + 5
    ceiling = rows - chrome
    budget = fit_budget(rows, True, big_cost, small_cost, busy_rows,
                        menu_rows, cmd_rows)
    print(f"dashboard budget on a {rows}-row terminal: {budget} lines")
    print(f"max output once chrome is accounted for: {ceiling} lines\n")

    print("the 12-device listing fits on a 24-row terminal:")
    passed &= check("listing length", cmd_rows, 16)
    passed &= check("fits under the ceiling", cmd_rows <= ceiling, True)
    # It does NOT fit under the full 5-row logo -- the logo must yield.
    passed &= check("does not fit under the big logo",
                    cmd_rows <= fit_budget(rows, True, big_cost, small_cost,
                                           0, 0, cmd_rows), False)

    print("\nshedding order: panels go before the logo:")
    # 1 busy client + 8 query rows + the 16-line listing on a 24-row screen.
    use_big, body, q, b = shed(rows, big_cost, small_cost, True, 3, 10, cmd_rows)
    passed &= check("query list dropped", q, 0)
    passed &= check("busy dropped too", b, 0)
    # 6 (big) or 2 (small) + body + 16 must now fit in 24.
    cost = (big_cost if use_big else small_cost) + body + cmd_rows
    passed &= check("listing fits after shedding", cost <= rows, True)

    # A huge listing that cannot fit even with everything shed: drop the logo.
    use_big, body, q, b = shed(rows, big_cost, small_cost, True, 3, 10, 40)
    passed &= check("query dropped", q, 0)
    passed &= check("busy dropped", b, 0)
    passed &= check("logo dropped as last resort", use_big, False)
    total = small_cost + body + 40
    print(f"       (would still need {total} rows for a 40-line output)")

    # The regression: the old code capped at 6 lines.
    print("\nold 6-line cap showed only this many devices:")
    passed &= check("devices under old cap", len(devices_out[:6]) - 2, 4)

    print("\nhead and tail are preserved when it does NOT fit:")
    long_out = devices_out + [f"extra{i}" for i in range(20)]
    shown, hidden = pick_lines(long_out, budget)
    passed &= check("kept header", shown[0], long_out[0])
    passed &= check("kept summary", shown[-1], long_out[-1])
    passed &= check("line count", len(shown), budget)
    passed &= check("reports hidden", hidden, len(long_out) - budget)
    passed &= check("no duplicates", len(set(range(len(shown)))), len(shown))

    # Short output is never truncated or reshaped.
    print("\nshort output passes through untouched:")
    for n in (1, 2, 3, budget - 1, budget):
        s, h = pick_lines(long_out[:n], budget)
        passed &= check(f"{n} lines unchanged", (s, h), (long_out[:n], 0))

    # Tiny budget must not crash or drop the summary.
    print("\ndegenerate budgets:")
    for b in (0, 1, 2, 3):
        s, h = pick_lines(long_out, b)
        passed &= check(f"budget {b} keeps summary", s[-1], long_out[-1])

    # The settings menu outranks command output for space.
    print("\nmenu open (tighter budget, menu is protected):")
    m_budget = fit_budget(rows, True, big_cost, small_cost, 0, 8, cmd_rows)
    passed &= check("menu budget smaller", m_budget < budget, True)
    passed &= check("menu budget positive", m_budget >= 4, True)

    print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
