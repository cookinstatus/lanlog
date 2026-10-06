"""Tests for the log-line parsers: query lines and reply/answer lines.

Two things are guarded here:

  1. The answer parser must ignore non-addresses. Reply lines also carry
     `<CNAME>`, `NODATA`, `NODATA-IPv6` and `NXDOMAIN`; storing those in the
     answers table would make "what does this name resolve to" return something
     that is not an address, which is worse than returning nothing.

  2. The answer parser must not swallow query lines, and vice versa. Both read
     the same stream, so a pattern that is too loose would double-count.

Run: python3 test_ingest.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lanlog import ingest  # noqa: E402


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}")
    return ok


def main():
    passed = True

    print("query lines:")
    q = ingest.classify(
        "Oct  6 00:53:29 dnsmasq[48]: query[A] www.youtube.com from 192.168.1.166")
    passed &= check("parsed", q is not None, True)
    passed &= check("client", q[1], "192.168.1.166")
    passed &= check("qname", q[2], "www.youtube.com")
    passed &= check("qtype", q[3], "A")
    passed &= check("a reply line is not a query",
                    ingest.classify(
                        "Oct  6 00:53:29 dnsmasq[48]: reply x.com is 1.2.3.4"),
                    None)

    print("\nanswer lines:")
    a = ingest.classify_answer(
        "Oct  6 00:53:29 dnsmasq[48]: reply www.youtube.com is 142.251.157.4")
    passed &= check("reply parsed", a[1:], ("www.youtube.com", "142.251.157.4",
                                            "A", "reply"))
    a = ingest.classify_answer(
        "Oct  6 00:53:29 dnsmasq[48]: cached www.youtube.com is 142.251.157.4")
    passed &= check("cached kind", a[4], "cached")
    a = ingest.classify_answer(
        "Oct  6 00:53:29 dnsmasq[48]: cached-stale www.youtube.com is 1.2.3.4")
    passed &= check("cached-stale kind", a[4], "cached-stale")
    a = ingest.classify_answer(
        "Oct  6 00:53:29 dnsmasq[48]: reply v6.example.com is 2606:4700::1111")
    passed &= check("AAAA inferred from the address", a[3], "AAAA")

    print("\nnon-addresses are not stored as answers:")
    for line, label in [
        ("Oct  6 00:47:02 dnsmasq[48]: cached-stale live.github.com is NODATA-IPv6",
         "NODATA-IPv6"),
        ("Oct  6 00:47:02 dnsmasq[48]: cached-stale live.github.com is NODATA",
         "NODATA"),
        ("Oct  6 00:47:02 dnsmasq[48]: cached-stale x.com is NXDOMAIN", "NXDOMAIN"),
        ("Oct  6 00:47:02 dnsmasq[48]: reply cdn.example.com is <CNAME>", "<CNAME>"),
    ]:
        passed &= check(f"{label} skipped", ingest.classify_answer(line), None)

    print("\nother line kinds are ignored by both:")
    for line in [
        "Oct  6 00:53:29 dnsmasq[48]: forwarded www.youtube.com to 8.8.8.8",
        "Oct  6 00:53:29 dnsmasq[48]: gravity blocked ads.example.com is 0.0.0.0",
        "Oct  6 00:53:29 dnsmasq[48]: config www.youtube.com is NODATA",
    ]:
        passed &= check(f"ignored: {line.split(': ', 1)[1][:34]}",
                        (ingest.classify(line), ingest.classify_answer(line)),
                        (None, None))

    print("\nPTR answers are dropped (they are not browsing):")
    passed &= check("PTR answer skipped",
                    ingest.classify_answer(
                        "Oct  6 00:53:29 dnsmasq[48]: reply 2.1.254.169.in-addr.arpa"
                        " is 192.168.1.151"),
                    None)

    print("\n" + ("ALL PASS" if passed else "FAILURES PRESENT"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
