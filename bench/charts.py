# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib>=3.9"]
# ///
"""Draw every figure from results/*.json into results/figures/. Run: uv run bench/charts.py"""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
FIG = RES / "figures"

BUILDS = {"c": ("C / ESP-IDF 6.0", "#2a78d6"), "rust": ("Rust / esp-hal 1.2", "#eb6834")}
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK,
    "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": True, "axes.grid.axis": "x", "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True, "ytick.left": False, "axes.titleweight": "bold",
    "axes.titlesize": 12, "axes.titlelocation": "left",
})


def load(name):
    try:
        return json.loads((RES / name).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save(fig, name, note=None):
    if note:
        fig.canvas.draw()
        r = fig.canvas.get_renderer()
        inv = fig.transFigure.inverted()
        y0 = min(inv.transform(ax.get_tightbbox(r))[0][1] for ax in fig.axes)
        fig.text(0.01, y0 - 0.03, note, fontsize=8, color=INK2, va="top", wrap=True)
    FIG.mkdir(parents=True, exist_ok=True)
    for ext in ("svg", "png"):
        fig.savefig(FIG / f"{name}.{ext}", dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("wrote", FIG / f"{name}.svg")


def legend_top(ax):
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, borderaxespad=0.2)
    ax.set_title(ax.get_title(loc="left"), loc="left", pad=26)


def legend_fig(fig, ax, title):
    """Title and legend above a row of panels, panel titles level below them."""
    fig.suptitle(title, x=0.01, y=1.22, ha="left", fontweight="bold")
    fig.legend(*ax.get_legend_handles_labels(), loc="upper left", bbox_to_anchor=(0.01, 1.13),
               ncol=2, frameon=False)


def paired_bars(ax, labels, values, fmt, unit="", notes=None):
    """Horizontal bars, C above Rust in each row, value labelled at the bar end."""
    h = 0.36
    ys = range(len(labels))
    top = max(v for vals in values.values() for v in vals if v is not None)
    for k, (build, vals) in enumerate(values.items()):
        name, color = BUILDS[build]
        for y, v in zip(ys, vals):
            yy = y - h / 2 - 0.02 + k * (h + 0.04)
            if v is None:
                ax.text(0, yy, "  n/a", va="center", fontsize=8, color=INK2)
                continue
            ax.barh(yy, v, height=h, color=color, label=name if y == 0 else None)
            note = notes[build][y] if notes else ""
            ax.text(v + top * 0.01, yy, fmt(v) + unit + note, va="center", fontsize=8.5, color=INK)
    ax.set_yticks(list(ys), labels)
    pad = 0.9 if len(labels) == 1 else 0.5
    ax.set_ylim(len(labels) - 1 + pad, -pad)
    ax.set_xlim(0, top * (1.45 if notes else 1.18))


def flash_by_origin(s):
    cats = ["Total app image", "Wi-Fi, PHY, crypto", "TCP/IP", "OS, HAL, runtime",
            "HTTP, JSON, mDNS", "Storage", "App", "Unattributed"]
    vals = {}
    for b in BUILDS:
        o = s[b]["origin"]
        total = s[b]["image"] if b == "c" else s[b]["image_noweb"]
        vals[b] = [total / 1024] + [o.get(c, 0) / 1024 for c in cats[1:]]
    fig, ax = plt.subplots(figsize=(8, 5.2))
    paired_bars(ax, cats, vals, lambda v: f"{v:,.0f}", " KiB")
    ax.set_title("Application image in flash, by origin (ESP32-C6, size-optimised)")
    ax.set_xlabel("KiB in the app image (frontend excluded)")
    legend_top(ax)
    save(fig, "flash_by_origin",
         "Frontend (925 KiB) excluded: C keeps it in a 2 MiB LittleFS partition, Rust embeds it in the "
         f"app (full Rust image {s['rust']['image'] / 1024:,.0f} KiB). Unattributed = merged string "
         "literals, anonymous constants and alignment padding.")


def ram_static(s):
    kinds = ["IRAM code", "data", "bss", "task futures", "heap arena"]
    vals = {b: [s[b]["ram"].get(k) / 1024 if k in s[b]["ram"] else None for k in kinds]
            for b in BUILDS}
    labels = ["Code in RAM (IRAM)", ".data", ".bss (other)", "Task futures", "Heap arena (reserved)"]
    fig, ax = plt.subplots(figsize=(8, 3.8))
    paired_bars(ax, labels, vals, lambda v: f"{v:,.1f}", " KiB")
    ax.set_title("Static RAM reserved at link time (ESP32-C6, 512 KiB SRAM)")
    ax.set_xlabel("KiB")
    legend_top(ax)
    save(fig, "ram_static",
         "C allocates task stacks, socket buffers and its heap at run time, so they do not appear "
         "here; Rust reserves its async task state and heap arenas statically. Compare run-time "
         "peak use, not these bars alone.")


def timings(data):
    """{build: {mean, min, max, cpu}} in seconds from a hyperfine export."""
    out = {}
    for r in data["results"]:
        w = r["summary"]["time_wall_clock"]
        out[r["name"]] = {"mean": w["mean"], "min": w["min"], "max": w["max"],
                          "cpu": r["summary"]["time_cpu"]["mean"]}
    return out


def build_times(clean, incr):
    fig, axes = plt.subplots(1, 2, figsize=(9, 2.3))
    for ax, data, title in ((axes[0], clean, "Clean build"), (axes[1], incr, "One-file change")):
        res = timings(data)
        vals = {b: [res[b]["mean"]] for b in BUILDS if b in res}
        notes = {b: [f"  ({res[b]['min']:.1f} to {res[b]['max']:.1f})"] for b in vals}
        paired_bars(ax, [""], vals, lambda v: f"{v:.1f}", " s", notes)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("seconds, mean (min to max)")
        ax.set_yticks([])
    legend_fig(fig, axes[0], "Build time, release profile, warm toolchain, 20-thread laptop")
    save(fig, "build_times",
         "Rust release uses fat LTO and one codegen unit, so a one-file change re-optimises the "
         "whole program. C clean build includes all IDF components; Rust clean includes core and alloc.")


def source(s):
    fig, axes = plt.subplots(1, 2, figsize=(9, 2.1))
    loc = {"c": [sum(s["c"]["source"].get(k, 0) for k in ("C",))],
           "rust": [s["rust"]["source"].get("Rust", 0)]}
    paired_bars(axes[0], [""], loc, lambda v: f"{v:,.0f}")
    axes[0].set_title("Application source (code lines)", fontsize=11)
    pk = {b: [s[b]["packages"]] for b in BUILDS}
    paired_bars(axes[1], [""], pk, lambda v: f"{v:,.0f}")
    axes[1].set_title("Packages in the build", fontsize=11)
    for ax in axes:
        ax.set_yticks([])
    legend_fig(fig, axes[0], "Source and dependencies")
    save(fig, "source",
         f"Code lines by tokei, comments and blanks excluded; each build includes about 20 lines of "
         f"opt-in heap endpoint. Rust src/ only; its build.rs adds "
         f"{s['rust']['build_script']} lines (asset embedding, template linker hints). "
         "Packages: IDF components vs crates, so they show scale, not like for like.")


def latency(runs):
    names = [n for n in next(iter(runs.values()))["latency"] if n != "heap"]
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(names) + 1.4))
    h = 0.36
    for k, (b, data) in enumerate(runs.items()):
        name, color = BUILDS[b]
        for y, n in enumerate(names):
            r = data["latency"].get(n)
            if not r or not r["n"]:
                continue
            yy = y - h / 2 + k * h
            ax.plot([r["p50"], r["p99"]], [yy, yy], color=color, lw=2)
            ax.plot(r["p50"], yy, "o", color=color, ms=7, label=name if y == 0 else None)
            ax.plot(r["max"], yy, "x", color=color, ms=6)
    ax.set_yticks(range(len(names)), names)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel("ms per request, log scale (dot p50, line to p99, cross max)")
    ax.set_title("HTTP latency from a laptop on the same Wi-Fi")
    legend_top(ax)
    save(fig, "latency", "Fresh TCP connection per request, sequential; page load fetches all "
                         "frontend files over six parallel connections.")


def kib(b):
    return f"{b / 1024:,.0f}"


def report(s, clean, incr, runs):
    """results/README.md: the tables behind the figures, regenerated with them."""
    c, r = s["c"], s["rust"]
    lines = [
        "# Results",
        "",
        "Generated by `bench/charts.py` from the JSON files in this folder. ESP32-C6, ESP-IDF 6.0 "
        f"vs esp-hal (Rust: `{r['toolchain']}`).",
        "",
        "## Flash",
        "",
        "![Flash by origin](figures/flash_by_origin.svg)",
        "",
        "| KiB | C / IDF | Rust / esp-hal |",
        "|---|---:|---:|",
        f"| App image, frontend excluded | {kib(c['image'])} | {kib(r['image_noweb'])} |",
        f"| Frontend ({kib(s['web_bytes'])} KiB of files) | LittleFS partition, 2,048 reserved "
        f"| in the app image |",
        f"| Flash written by a full flash (app + frontend image) | {kib(c['image'] + 2 * 1024 * 1024)} "
        f"| {kib(r['image'])} |",
        "",
        "## Static RAM",
        "",
        "![Static RAM](figures/ram_static.svg)",
        "",
        "## Source and dependencies",
        "",
        "![Source](figures/source.svg)",
        "",
        f"Rust application code has {r['unsafe']} `unsafe` blocks.",
        "",
    ]
    if clean and incr:
        rc, ri = timings(clean), timings(incr)
        lines += [
            "## Build time",
            "",
            "![Build time](figures/build_times.svg)",
            "",
            "| seconds, mean (min to max) | C / IDF | Rust / esp-hal |",
            "|---|---:|---:|",
        ] + [
            f"| {label} | " + " | ".join(
                f"{d[b]['mean']:.1f} ({d[b]['min']:.1f} to {d[b]['max']:.1f})" for b in BUILDS) + " |"
            for label, d in (("Clean", rc), ("One-file change", ri))
        ] + [
            "| Clean, CPU time (all threads) | " + " | ".join(f"{rc[b]['cpu']:.0f}" for b in BUILDS) + " |",
            "",
        ]
    if runs:
        lines += ["## On the device", "", "![Latency](figures/latency.svg)", ""]
        checks = {b: d.get("check", {}) for b, d in runs.items()}
        names = list(next(iter(checks.values())))
        lines += ["| Check | " + " | ".join(BUILDS[b][0] for b in checks) + " |",
                  "|---|" + "---|" * len(checks)]
        for n in names:
            lines.append(f"| {n} | " + " | ".join(
                ("pass" if checks[b].get(n, {}).get("pass") else "**fail**") for b in checks) + " |")
        lines.append("")
    else:
        lines += ["## On the device", "", "Not measured yet: needs a board. See the top-level README.", ""]
    (RES / "README.md").write_text("\n".join(lines))
    print("wrote", RES / "README.md")


def main():
    s = load("static.json")
    if s:
        flash_by_origin(s)
        ram_static(s)
        source(s)
    clean, incr = load("build_clean.json"), load("build_incremental.json")
    if clean and incr:
        build_times(clean, incr)
    runs = {b: load(f"http_{b}.json") for b in BUILDS}
    if any(r and "latency" in r for r in runs.values()):
        latency({b: r for b, r in runs.items() if r and "latency" in r})
    if s:
        report(s, clean, incr, {b: r for b, r in runs.items() if r})


if __name__ == "__main__":
    main()
