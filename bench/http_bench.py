#!/usr/bin/env python3
"""On-device checks and latency for one build. Stdlib only; run from a machine on the same Wi-Fi.

  bench/http_bench.py check   --host 192.168.1.50 --build c
  bench/http_bench.py latency --host 192.168.1.50 --build c -n 1000
  bench/http_bench.py watch   --host 192.168.1.50 --build c     # reboot the AP while this runs
  bench/http_bench.py boot    --host 192.168.1.50 --build c     # needs pyserial and the USB port

Results go to results/http_<build>.json (each subcommand updates its own key).
"""

import argparse
import concurrent.futures as cf
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
    """One request on a fresh connection. Returns (status, headers, body, seconds).

    Seconds run to the last body byte. The socket is closed only after the server's FIN: closing
    first races the server's close, and smoltcp then holds the socket for a ~1 s retransmit.
    """
    h, _, p = host.partition(":")
    t0 = time.perf_counter()
    s = socket.create_connection((h, int(p or 80)), timeout=timeout)
    try:
        payload = body.encode() if isinstance(body, str) else (body or b"")
        head = f"{method} {path} HTTP/1.1\r\nHost: {h}\r\nConnection: close\r\n"
        if body is not None:
            head += f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        s.sendall(head.encode() + b"\r\n" + payload)
        buf, need, t_body = b"", None, None
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if need is None and b"\r\n\r\n" in buf:
                hdr, rest = buf.split(b"\r\n\r\n", 1)
                lines = hdr.decode("latin-1").split("\r\n")
                headers = dict(l.split(": ", 1) for l in lines[1:] if ": " in l)
                need = len(hdr) + 4 + int({k.lower(): v for k, v in headers.items()}
                                          .get("content-length", 1 << 62))
            if need is not None and t_body is None and len(buf) >= need:
                t_body = time.perf_counter()
        if need is None:
            raise ConnectionError("no response headers")
        t_body = t_body or time.perf_counter()
    finally:
        s.close()
    status = int(lines[0].split()[1])
    return status, headers, buf[len(hdr) + 4:], t_body - t0


def mdns_resolve(name, timeout=3.0):
    """Multicast A query for `name` from port 5353, as an OS resolver sends it; first answer or None.

    Replies to a query from a random port come back unicast, and those never arrived under
    WSL2 mirrored networking (probably the Hyper-V firewall). Multicast replies get through.
    """
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.settimeout(0.5)
    try:
        s.bind(("", 5353))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton("224.0.0.251") + socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.sendto(query, ("224.0.0.251", 5353))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(9000)
            except socket.timeout:
                continue
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
        return ip is not None, ip or "no answer (under WSL2 this needs networkingMode=mirrored)"

    def bad_json():
        st, _, body, _ = request(host, "POST", "/api/v1/light/brightness", BAD_JSON)
        return st == 400, f"{st} {body.decode()[:60]}"

    def alive():
        time.sleep(3)
        st, _, _, _ = request(host, "GET", "/api/v1/system/info")
        return st == 200, "serving again (a crash and reboot also passes: check the serial log)"

    for name, fn in [("GET / serves the frontend", index), ("GET system/info", info),
                     ("GET temp/raw", temp), ("POST light/brightness", light),
                     ("dashboard.local resolves", mdns), ("POST bad JSON answers 400", bad_json),
                     ("Serving again after bad JSON", alive)]:
        row(name, fn)
    return rows


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summary(secs):
    if not secs:
        return {"n": 0, "samples_ms": []}
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
        r = out[name]
        print(f"{name:40s} " + (f"p50 {r['p50']:7.1f} ms  p99 {r['p99']:7.1f} ms  max {r['max']:7.1f} ms"
                                if r["n"] else "no successful requests") + f"  errors {errors}")

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
    r = out["Full page load (6 parallel)"]
    print(f"Full page load: p50 {r.get('p50', float('nan')):.0f} ms, failed {failed}")
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


def up(host, port, timeout):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def watch(host, gateway, seconds, interval=0.5):
    """Poll device and router for `seconds`; per outage, how long the device lagged the router."""
    outages, dev_down, gw_down, gw_back = [], None, None, None
    print(f"Polling for {seconds:.0f} s; reboot the access point now.")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            ok = request(host, "GET", "/api/v1/system/info", timeout=2)[0] == 200
        except OSError:
            ok = False
        gw = up(gateway, 80, 1)
        now = time.monotonic()
        if not gw and gw_down is None:
            gw_down, gw_back = now, None
        elif gw and gw_down is not None and gw_back is None:
            gw_back = now
            print(f"router back after {gw_back - gw_down:.1f} s")
        if not ok and dev_down is None:
            dev_down = now
            print("device down")
        elif ok and dev_down is not None:
            o = {"device_down_s": now - dev_down,
                 "after_router_s": now - gw_back if gw_back else None}
            outages.append(o)
            print(f"device back after {o['device_down_s']:.1f} s"
                  + (f", {o['after_router_s']:.1f} s after the router" if gw_back else ""))
            dev_down, gw_down, gw_back = None, None, None
        time.sleep(interval)
    return {"outages": outages, "recovered": dev_down is None}


def boot(host, port, runs):
    """Reset over USB-Serial-JTAG, then time until the first HTTP 200, `runs` times."""
    import serial  # only this subcommand needs pyserial

    times = []
    for i in range(runs):
        with serial.Serial(port, 115200) as s:
            s.dtr, s.rts = False, True
            time.sleep(0.05)
            t0 = time.perf_counter()
            s.rts = False
        time.sleep(0.5)  # let the old server go away
        while time.perf_counter() - t0 < 30:
            try:
                if request(host, "GET", "/api/v1/system/info", timeout=0.5)[0] == 200:
                    times.append(time.perf_counter() - t0)
                    break
            except OSError:
                time.sleep(0.05)
        print(f"run {i + 1}: {times[-1]:.2f} s" if len(times) == i + 1 else f"run {i + 1}: timeout")
    return {"reset_to_http_s": times}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "latency", "watch", "boot"])
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--gateway", default="192.168.0.1", help="router, probed on TCP 80")
    ap.add_argument("--seconds", type=float, default=300, help="watch duration")
    ap.add_argument("--host", required=True)
    ap.add_argument("--build", required=True, help="c or rust")
    ap.add_argument("-n", type=int, default=1000)
    a = ap.parse_args()

    path = ROOT / f"results/http_{a.build}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[a.cmd] = {"check": lambda: check(a.host), "latency": lambda: latency(a.host, a.n),
                   "watch": lambda: watch(a.host, a.gateway, a.seconds),
                   "boot": lambda: boot(a.host, a.port, 10)}[a.cmd]()
    data.setdefault("meta", {})[a.cmd] = time.strftime("%Y-%m-%d %H:%M")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
