#!/usr/bin/env python3
"""On-device checks and latency for one build. Stdlib only; run from a machine on the same Wi-Fi.

  bench/http_bench.py check   --host 192.168.1.50 --build c
  bench/http_bench.py latency --host 192.168.1.50 --build c -n 1000
  bench/http_bench.py watch   --host 192.168.1.50 --build c     # reboot the AP while this runs

Results go to results/http_<build>.json (each subcommand updates its own key).
"""

import argparse
import concurrent.futures as cf
import http.client
import json
import socket
import statistics
import struct
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "front/web-demo/dist"
BAD_JSON = b'{"red": 1, "green": '


def request(host, method, path, body=None, timeout=10):
    """One request on a fresh connection. Returns (status, headers, body, seconds)."""
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, timeout=timeout)
    headers = {"Connection": "close"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data, time.perf_counter() - t0


def mdns_resolve(name, timeout=3.0):
    """Send one mDNS A query for `name` and return the first answer's address, or None."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(timeout)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    try:
        s.sendto(query, ("224.0.0.251", 5353))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data, _ = s.recvfrom(1500)
            for rname, rtype, rdata in dns_records(data):
                if rtype == 1 and rname.lower() == name.lower() and len(rdata) == 4:
                    return socket.inet_ntoa(rdata)
    except (OSError, IndexError, struct.error):
        pass
    finally:
        s.close()
    return None


def dns_name(msg, i):
    """Decode a possibly compressed name at offset i. Returns (name, offset after it)."""
    labels, end = [], None
    while msg[i]:
        if msg[i] & 0xC0 == 0xC0:
            end = end or i + 2
            i = struct.unpack(">H", msg[i:i + 2])[0] & 0x3FFF
            continue
        labels.append(msg[i + 1:i + 1 + msg[i]].decode(errors="replace"))
        i += 1 + msg[i]
    return ".".join(labels), end or i + 1


def dns_records(msg):
    """(name, type, rdata) for every answer, authority and additional record."""
    _, _, qd, an, ns, ar = struct.unpack(">HHHHHH", msg[:12])
    i = 12
    for _ in range(qd):
        i = dns_name(msg, i)[1] + 4
    for _ in range(an + ns + ar):
        name, i = dns_name(msg, i)
        rtype, _, _, rdlen = struct.unpack(">HHIH", msg[i:i + 10])
        yield name, rtype, msg[i + 10:i + 10 + rdlen]
        i += 10 + rdlen


def check(host):
    """The MVP parity checklist. Bad JSON runs last: it may crash a build."""
    rows = {}

    def row(name, fn):
        try:
            ok, note = fn()
        except Exception as e:  # a failed row is a result
            ok, note = False, f"{type(e).__name__}: {e}"
        rows[name] = {"pass": ok, "note": note}
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

    def index():
        st, h, body, _ = request(host, "GET", "/")
        return st == 200 and b"<div id=\"app\"" in body, f"{st}, {len(body)} B, {h.get('Content-Type')}"

    def info():
        st, _, body, _ = request(host, "GET", "/api/v1/system/info")
        j = json.loads(body)
        return st == 200 and {"chip", "cores"} <= j.keys(), body.decode().strip()

    def temp():
        st, _, body, _ = request(host, "GET", "/api/v1/temp/raw")
        return st == 200 and 0 <= json.loads(body)["raw"] < 20, body.decode().strip()

    def light():
        st, _, body, _ = request(host, "POST", "/api/v1/light/brightness",
                                 json.dumps({"red": 160, "green": 160, "blue": 48}).encode())
        return st == 200, f"{st} {body.decode()[:60]}"

    def mdns():
        ip = mdns_resolve("dashboard.local")
        return ip is not None, ip or "no answer (multicast may not reach this host, e.g. WSL2 NAT)"

    def bad_json():
        st, _, body, _ = request(host, "POST", "/api/v1/light/brightness", BAD_JSON)
        return st == 400, f"{st} {body.decode()[:60]}"

    def alive():
        time.sleep(3)
        st, _, _, _ = request(host, "GET", "/api/v1/system/info")
        return st == 200, "still serving after bad JSON"

    for name, fn in [("GET / serves the frontend", index), ("GET system/info", info),
                     ("GET temp/raw", temp), ("POST light/brightness", light),
                     ("dashboard.local resolves", mdns), ("POST bad JSON answers 400", bad_json),
                     ("Survives bad JSON", alive)]:
        row(name, fn)
    return rows


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summary(secs):
    ms = [s * 1000 for s in secs]
    return {"n": len(ms), "p50": pct(ms, 50), "p90": pct(ms, 90), "p99": pct(ms, 99),
            "max": max(ms), "mean": statistics.fmean(ms), "samples_ms": ms}


def latency(host, n):
    """Sequential requests, one connection each, per endpoint; then full page loads, 6 in parallel."""
    largest = max((f for f in DIST.rglob("*") if f.is_file()), key=lambda f: f.stat().st_size)
    endpoints = {
        "GET system/info": ("GET", "/api/v1/system/info", None),
        "GET temp/raw": ("GET", "/api/v1/temp/raw", None),
        "POST light/brightness": ("POST", "/api/v1/light/brightness", b'{"red":1,"green":2,"blue":3}'),
        "GET index.html": ("GET", "/index.html", None),
        f"GET {largest.name} ({largest.stat().st_size // 1024} KB)":
            ("GET", "/" + str(largest.relative_to(DIST)), None),
    }
    out = {}
    heap_before = heap(host)
    for name, (method, path, body) in endpoints.items():
        reps = n if "KB)" not in name else max(20, n // 20)
        secs, errors = [], 0
        for _ in range(reps):
            try:
                st, _, _, s = request(host, method, path, body)
                if st == 200:
                    secs.append(s)
                else:
                    errors += 1
            except OSError:
                errors += 1
        out[name] = {**summary(secs), "errors": errors}
        print(f"{name:40s} p50 {out[name]['p50']:7.1f} ms  p99 {out[name]['p99']:7.1f} ms  "
              f"max {out[name]['max']:7.1f} ms  errors {errors}")

    files = ["/" + str(f.relative_to(DIST)) for f in DIST.rglob("*") if f.is_file()]
    loads, failed = [], 0
    for _ in range(max(5, n // 50)):
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(6) as pool:  # browsers open about six connections per host
            results = list(pool.map(lambda p: _try(host, p), files))
        if all(results):
            loads.append(time.perf_counter() - t0)
        else:
            failed += 1
    out["Full page load (6 parallel)"] = {**summary(loads), "errors": failed}
    print(f"Full page load: p50 {out['Full page load (6 parallel)']['p50']:.0f} ms, failed {failed}")
    if heap_before:
        out["heap"] = {"before": heap_before, "after": heap(host)}
        print("heap", out["heap"])
    return out


def heap(host):
    """{total, used, peak} from /api/v1/heap, or None if the build lacks the endpoint."""
    try:
        st, _, body, _ = request(host, "GET", "/api/v1/heap")
        return json.loads(body) if st == 200 else None
    except (OSError, ValueError):
        return None


def _try(host, path):
    try:
        return request(host, "GET", path, timeout=20)[0] == 200
    except OSError:
        return False


def watch(host, interval=0.5):
    """Poll until interrupted; report each outage and the time to recover."""
    outages, down_since = [], None
    print("Polling; reboot the access point now. Ctrl-C to stop.")
    try:
        while True:
            try:
                ok = request(host, "GET", "/api/v1/system/info", timeout=2)[0] == 200
            except OSError:
                ok = False
            now = time.monotonic()
            if not ok and down_since is None:
                down_since = now
                print("down")
            elif ok and down_since is not None:
                outages.append(now - down_since)
                print(f"back after {outages[-1]:.1f} s")
                down_since = None
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return {"outages_s": outages, "recovered": down_since is None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "latency", "watch"])
    ap.add_argument("--host", required=True)
    ap.add_argument("--build", required=True, help="c or rust")
    ap.add_argument("-n", type=int, default=1000)
    a = ap.parse_args()

    path = ROOT / f"results/http_{a.build}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[a.cmd] = {"check": lambda: check(a.host), "latency": lambda: latency(a.host, a.n),
                   "watch": lambda: watch(a.host)}[a.cmd]()
    data.setdefault("meta", {})[a.cmd] = time.strftime("%Y-%m-%d %H:%M")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
