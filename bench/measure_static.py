#!/usr/bin/env python3
"""Static metrics for both builds: image size by origin, static RAM, source and dependency counts.

Needs both builds done first (scripts/build_c.sh, scripts/build_rust.sh, and the Rust build with
--no-default-features --target-dir target/noweb). Writes results/static.json.
"""

import glob
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C_BUILD = ROOT / "c/build"
RUST_TARGET = ROOT / "rust/target"
ELF_REL = "riscv32imac-unknown-none-elf/release/restful_server"
TOOLS = Path.home() / ".espressif/tools"
IDF_PY = TOOLS / "python/v6.0/venv/bin/python"
DIST = ROOT / "front/web-demo/dist"


def tool(name):
    return glob.glob(str(TOOLS / f"riscv32-esp-elf/*/riscv32-esp-elf/bin/riscv32-esp-elf-{name}"))[0]


def run(*cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


# Origin categories, in stacking order.
CATS = ["Wi-Fi, PHY, crypto", "TCP/IP", "HTTP, JSON, mDNS", "Storage", "OS, HAL, runtime", "App"]

C_ARCHIVES = {
    "Wi-Fi, PHY, crypto": r"net80211|pp|phy|coexist|core|mesh|espnow|regulatory|btbb|wpa_supplicant"
    r"|esp_wifi|esp_phy|esp_coex|mbed-\w+|tfpsacrypto",
    "TCP/IP": r"lwip|esp_netif",
    "HTTP, JSON, mDNS": r"esp_http_server|http_parser|espressif__cjson|espressif__mdns",
    "Storage": r"joltwallet__littlefs|vfs|nvs_flash|esp_partition",
    "App": r"main|protocol_examples_common",
}

RUST_CRATES = {
    "Wi-Fi, PHY, crypto": r"esp_radio\w*|esp_phy|esp_wifi_sys|esp_radio_rtos_driver",
    "TCP/IP": r"smoltcp|embassy_net\w*|edge_nal\w*",
    "HTTP, JSON, mDNS": r"picoserve\w*|serde\w*|ryu|edge_mdns|domain|octseq",
    "App": r"restful_server",
}
RUST_BLOBS = r"net80211|pp|phy|coexist|core|mesh|espnow|regulatory|btbb|wpa_supplicant|printf"


def classify(name, table):
    for cat, pat in table.items():
        if re.fullmatch(pat, name):
            return cat
    return "OS, HAL, runtime"


def c_origin():
    """Flash-resident bytes per archive from esp_idf_size: flash code/rodata plus IRAM text and data."""
    out = run(str(IDF_PY), "-m", "esp_idf_size", "--archives", "--format", "json2",
              str(C_BUILD / "restful_server.map"))
    by_cat, by_lib = defaultdict(int), {}
    for path, a in json.loads(out).items():
        size = 0
        for mem in a["memory_types"].values():
            for sec, s in mem["sections"].items():
                if not sec.endswith(".bss") and "noinit" not in sec:
                    size += s["size"]
        lib = re.sub(r"^lib|\.a$", "", a["abbrev_name"])
        by_lib[lib] = size
        by_cat[classify(lib, C_ARCHIVES)] += size

    # The linker merges every object's string literals into the first `.str1` input it sees,
    # so move those bytes out of that archive and into "Unattributed", like Rust's anonymous rodata.
    for lib, size in list(by_lib.items()):
        if size < 16 * 1024:
            continue
        det = json.loads(run(str(IDF_PY), "-m", "esp_idf_size", "--archive-details", f"lib{lib}.a",
                             "--format", "json2", str(C_BUILD / "restful_server.map")))
        merged = sum(v["size"] for k, v in det.items() if ".str1." in k)
        by_lib[lib] -= merged
        by_lib["merged string literals"] = by_lib.get("merged string literals", 0) + merged
        by_cat[classify(lib, C_ARCHIVES)] -= merged
        by_cat["Unattributed"] = by_cat.get("Unattributed", 0) + merged
    return dict(by_cat), by_lib


def elf_sections(elf):
    """(name, type, flags, size, addr) for every section header."""
    rows = []
    for line in run(tool("readelf"), "-S", "--wide", elf).splitlines():
        m = re.match(r"\s*\[\s*\d+\]\s+(\S+)\s+(\S+)\s+([0-9a-f]+)\s+[0-9a-f]+\s+([0-9a-f]+)\s+\S+\s+(\S*)", line)
        if m:
            name, typ, addr, size, flags = m.groups()
            rows.append((name, typ, flags, int(size, 16), int(addr, 16)))
    return rows


def rust_crate(demangled):
    m = re.search(r"([A-Za-z_]\w*)\[[0-9a-f]+\]", demangled)
    return m.group(1) if m else None


def rust_origin(elf):
    """Split image-resident output sections by input: blob archives by name, Rust code by crate."""
    resident = {n for n, t, f, s, _ in elf_sections(elf) if "A" in f and t == "PROGBITS" and s}
    inputs = []  # (out section, size, source, input section)
    out_sizes, out = {}, None
    for line in open(elf + ".map", errors="replace"):
        m = re.match(r"\s*[0-9a-f]+\s+[0-9a-f]+\s+([0-9a-f]+)\s+\d+ (\s*)(\S.*)$", line)
        if not m:
            continue
        size, indent, text = int(m.group(1), 16), len(m.group(2)), m.group(3)
        if indent == 0:
            out = text.strip()
            out_sizes[out] = size
        elif indent == 8 and out in resident:
            src, _, sec = text.partition(":(")
            inputs.append((out, size, src, sec.rstrip(")")))

    mangled = sorted({sec.split(".", 2)[2] for _, _, src, sec in inputs
                      if sec.count(".") >= 2 and sec.split(".", 2)[2].startswith(("_R", "_ZN"))})
    demangled = dict(zip(mangled, run_input([tool("c++filt"), "-s", "rust"], "\n".join(mangled))
                         .splitlines())) if mangled else {}

    by_cat, by_crate = defaultdict(int), defaultdict(int)
    for _, size, src, sec in inputs:
        lib = re.search(r"lib(\w+)\.a\(", src)
        if lib:
            name = lib.group(1)
            cat = "Wi-Fi, PHY, crypto" if re.fullmatch(RUST_BLOBS, name) else "OS, HAL, runtime"
            name = f"lib{name}.a"
        else:
            sym = sec.split(".", 2)[2] if sec.count(".") >= 2 else ""
            full = demangled.get(sym, "")
            # Task wrappers are generic over the app's futures: count them as app code.
            if full.startswith("<embassy_executor[") and "restful_server[" in full:
                full = "restful_server[0]"
            name = rust_crate(full) or (
                "esp_radio" if sym.startswith("__esp_radio") else "unattributed constants")
            cat = classify(name, RUST_CRATES) if name != "unattributed constants" else "Unattributed"
        by_cat[cat] += size
        by_crate[name] += size
    padding = sum(out_sizes[o] for o in resident if o in out_sizes) - sum(i[1] for i in inputs)
    by_cat["Unattributed"] += padding
    by_crate["section padding"] += padding
    return dict(by_cat), dict(by_crate)


def run_input(cmd, data):
    return subprocess.run(cmd, input=data, check=True, capture_output=True, text=True).stdout


def ram(elf):
    """Static RAM by kind, from section headers. Rust arenas and reserved regions are split out."""
    kinds = defaultdict(int)
    for name, typ, flags, size, _ in elf_sections(elf):
        if "A" not in flags or not size:
            continue
        if name in (".stack",):
            kinds["unused (main stack)"] += size
        elif name == ".dram2_uninit":
            kinds["heap arena"] += size
        elif name.startswith((".flash", ".rodata", ".text", ".flash_rodata_dummy")) or "flash" in name:
            continue
        elif "X" in flags:
            kinds["IRAM code"] += size
        elif typ == "NOBITS":
            kinds["bss"] += size
        elif "W" in flags:
            kinds["data"] += size
    return dict(kinds)


def big_bss_symbols(elf, patterns):
    """Sum of .bss symbol sizes matching each regex (demangled)."""
    lo, size = next((a, s) for n, _, _, s, a in elf_sections(elf) if n == ".bss")
    sums = defaultdict(int)
    for line in run(tool("nm"), "-S", "-C", elf).splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) == 4 and parts[2] in "bB" and lo <= int(parts[0], 16) < lo + size:
            for key, pat in patterns.items():
                if re.search(pat, parts[3]):
                    sums[key] += int(parts[1], 16)
    return dict(sums)


def tokei(paths):
    out = json.loads(run("tokei", "--output", "json", *map(str, paths)))
    return {lang: v["code"] for lang, v in out.items() if lang != "Total" and v["code"]}


def main():
    run(tool("readelf"), "--version")
    os.makedirs(ROOT / "results", exist_ok=True)

    rust_elf = str(RUST_TARGET / ELF_REL)
    noweb_elf = str(RUST_TARGET / "noweb" / ELF_REL)
    images = {}
    for key, elf in (("rust", rust_elf), ("rust_noweb", noweb_elf)):
        img = ROOT / f"rust/target/{key}.bin"
        run("espflash", "save-image", "--chip", "esp32c6", elf, str(img))
        images[key] = img.stat().st_size

    c_cat, c_lib = c_origin()
    c_image = (C_BUILD / "restful_server.bin").stat().st_size
    c_cat["Unattributed"] = c_cat.get("Unattributed", 0) + c_image - sum(c_cat.values())
    r_cat, r_crate = rust_origin(noweb_elf)
    r_cat["Unattributed"] += images["rust_noweb"] - sum(r_cat.values())

    rust_bss = big_bss_symbols(rust_elf, {
        "heap arena": r"::HEAP$",
        "task futures": r"::POOL$",
    })
    rust_ram = ram(rust_elf)
    rust_ram["heap arena"] = rust_ram.get("heap arena", 0) + rust_bss.get("heap arena", 0)
    rust_ram["task futures"] = rust_bss.get("task futures", 0)
    rust_ram["bss"] -= rust_bss.get("heap arena", 0) + rust_bss.get("task futures", 0)

    web = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    crates = run("cargo", "tree", "-e", "normal,build", "--prefix", "none", "--target",
                 "riscv32imac-unknown-none-elf", cwd=ROOT / "rust",
                 env={**os.environ, "WIFI_SSID": "x", "WIFI_PASSWORD": "x"})
    crates = {re.sub(r" \(.*$", "", l).strip() for l in crates.splitlines() if l.strip()}
    proj = json.load(open(C_BUILD / "project_description.json"))

    result = {
        "c": {
            "image": c_image,
            "web_storage": "LittleFS partition, 2 MiB",
            "origin": c_cat,
            "origin_detail": c_lib,
            "ram": ram(str(C_BUILD / "restful_server.elf")),
            "source": tokei([ROOT / "c/main"]),
            "packages": len(proj["build_components"]),
            "toolchain": run(proj["c_compiler"], "--version").splitlines()[0],
        },
        "rust": {
            "image": images["rust"],
            "image_noweb": images["rust_noweb"],
            "web_storage": "embedded in app image",
            "origin": r_cat,
            "origin_detail": r_crate,
            "ram": rust_ram,
            "source": tokei([ROOT / "rust/src"]),
            "build_script": tokei([ROOT / "rust/build.rs"]).get("Rust", 0),
            "packages": len(crates),
            "unsafe": int(run("sh", "-c", f"grep -rw unsafe {ROOT}/rust/src | wc -l")),
            "toolchain": run("rustc", "--version", cwd=ROOT / "rust").strip(),
        },
        "web_bytes": web,
    }
    json.dump(result, open(ROOT / "results/static.json", "w"), indent=2)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if "detail" not in kk}
                      if isinstance(v, dict) else v for k, v in result.items()}, indent=2))


if __name__ == "__main__":
    main()
