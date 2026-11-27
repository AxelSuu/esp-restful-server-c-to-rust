# restful_server: C on ESP-IDF vs Rust on esp-hal

Espressif's `restful_server` example, ported from C on ESP-IDF 6.0 to bare-metal Rust on esp-hal,
then measured side by side on an ESP32-C6. Same Vue frontend, same REST API, same mDNS name.

| | C build (`c/`) | Rust build (`rust/`) |
|---|---|---|
| SDK | ESP-IDF 6.0, GCC 15.2 | esp-hal 1.2.3, esp-radio 1.0.0-beta.1, esp-rtos 0.4, rustc 1.99 stable |
| Concurrency | FreeRTOS, `esp_http_server` task | Embassy async tasks |
| Network | lwIP | embassy-net on smoltcp |
| HTTP, JSON | `esp_http_server`, cJSON | picoserve, serde-json-core |
| mDNS | `espressif/mdns` (+ NetBIOS) | edge-mdns |
| Frontend | LittleFS partition | embedded in the app with `include_bytes!` |
| Optimisation | `-Os` | `opt-level = "s"`, fat LTO |

esp-hal 1.3 is not released yet (27 Nov 2026). `rust/Cargo.toml` pins `~1.2.2`; bump to `~1.3`
when it ships and re-run the measurements.

## Layout

```
c/          ESP-IDF project (the original example, moved here)
rust/       esp-hal port: src/bin/main.rs (Wi-Fi, tasks), src/web.rs (HTTP), src/mdns.rs
front/      Vue frontend shared by both builds
scripts/    reproducible builds for both
bench/      measurement and chart scripts
results/    measured data (JSON) and figures
```

## Build

Both builds read Wi-Fi credentials from `WIFI_SSID` and `WIFI_PASSWORD`, or from a gitignored
`wifi.env` at the repo root holding those two assignments. The network must be 2.4 GHz and allow
WPA2: the Rust build cannot join a WPA3-only network.

```sh
(cd front/web-demo && pnpm install && pnpm build)   # once, both builds embed dist/

scripts/build_c.sh                    # c/build/
scripts/build_c.sh flash monitor      # any idf.py arguments
scripts/build_rust.sh                 # rust/target/riscv32imac-unknown-none-elf/release/
(cd rust && cargo run --release)      # flash and monitor with espflash
```

Toolchain: ESP-IDF 6.0 via EIM; Rust stable with `rustup target add riscv32imac-unknown-none-elf`
and `cargo install espflash`.

## Measure

```sh
python3 bench/measure_static.py       # size by origin, static RAM, source, packages
bench/build_times.sh                  # hyperfine, clean and one-file change
bench/http_bench.py check   --host <ip> --build c       # parity checklist
bench/http_bench.py latency --host <ip> --build c -n 1000
bench/http_bench.py watch   --host <ip> --build c       # reboot the AP during this
uv run bench/charts.py                # results/figures/*.svg
```

`measure_static.py` needs the Rust build without the frontend as well:
`cargo build --release --no-default-features --target-dir target/noweb` in `rust/`.

For heap numbers, flash builds with the opt-in `/api/v1/heap` endpoint:
`HEAP_STATS=1 scripts/build_c.sh flash` and `scripts/build_rust.sh --features heap-stats`.
`http_bench.py latency` then records heap use before and after the load.

Run `http_bench.py` from a machine on the same Wi-Fi. Under WSL2 the mDNS check needs
`networkingMode=mirrored` in `%UserProfile%\.wslconfig`: NAT mode never delivers multicast.
The check queries from port 5353 so that answers come back by multicast; unicast answers to a
random port never arrived in testing (probably the Hyper-V firewall). No firewall change is needed.

## Behavioural differences

| | C | Rust |
|---|---|---|
| POST with invalid JSON | `cJSON_Parse` returns NULL and the handler dereferences it (`rest_server.c:126`): Load access fault, reboot, serving again after ~5.6 s | 400 Bad Request |
| Colour value above 255 | accepted (`valueint`) | 400, fields are `u8` |
| Unknown file | 500 | 404 |
| JSON formatting | pretty-printed (`cJSON_Print`) | compact |
| `idf_version` field | IDF version | `esp-hal v1.2.3` (key kept for the frontend) |
| NetBIOS name | yes | no |
| Wi-Fi security | up to WPA3 | up to WPA2: esp-radio beta.1 has no WPA3 station mode |
| Concurrent HTTP connections | 7 sockets, one server task, lwIP accept backlog | 8 sockets, one task each, no backlog: more connections are refused |
| Client closes before the server's FIN | no visible effect | the socket stays busy ~1 s (a retransmit), so bursts of such clients get refused; the bench waits for the server's FIN |

## Results

See [results/README.md](results/README.md).
