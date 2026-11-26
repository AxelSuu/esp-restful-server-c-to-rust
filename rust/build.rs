use std::{env, fmt::Write as _, fs, path::Path};

fn main() {
    linker_be_nice();
    // make sure linkall.x is the last linker script (otherwise might cause problems with flip-link)
    println!("cargo:rustc-link-arg=-Tlinkall.x");

    println!("cargo:rerun-if-env-changed=WIFI_SSID");
    println!("cargo:rerun-if-env-changed=WIFI_PASSWORD");
    println!("cargo:rerun-if-changed=Cargo.lock");
    println!(
        "cargo:rustc-env=ESP_HAL_VERSION={}",
        locked_version("esp-hal")
    );

    gen_assets();
}

/// Version of a dependency as resolved in Cargo.lock.
fn locked_version(name: &str) -> String {
    let lock = fs::read_to_string("Cargo.lock").unwrap_or_default();
    let needle = format!("name = \"{name}\"\nversion = \"");
    lock.find(&needle)
        .and_then(|i| lock[i + needle.len()..].split('"').next())
        .unwrap_or("unknown")
        .to_owned()
}

/// Embed the built frontend: one `(uri, content type, bytes)` row per file.
fn gen_assets() {
    let out = Path::new(&env::var("OUT_DIR").unwrap()).join("assets.rs");
    let mut rows = String::new();

    if env::var_os("CARGO_FEATURE_WEB").is_some() {
        let dist = fs::canonicalize("../front/web-demo/dist").unwrap_or_else(|_| {
            panic!("'front/web-demo/dist' doesn't exist. Run 'pnpm build' under 'front/web-demo'")
        });
        println!("cargo:rerun-if-changed={}", dist.display());
        let mut files = Vec::new();
        walk(&dist, &mut files);
        files.sort();
        for f in files {
            let uri = format!("/{}", f.strip_prefix(&dist).unwrap().display());
            writeln!(
                rows,
                "    ({uri:?}, {:?}, include_bytes!({:?})),",
                content_type(&uri),
                f.display().to_string()
            )
            .unwrap();
        }
    }

    fs::write(
        out,
        format!("pub static ASSETS: &[(&str, &str, &[u8])] = &[\n{rows}];\n"),
    )
    .unwrap();
}

fn walk(dir: &Path, files: &mut Vec<std::path::PathBuf>) {
    for e in fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        println!("cargo:rerun-if-changed={}", p.display());
        if p.is_dir() {
            walk(&p, files);
        } else {
            files.push(p);
        }
    }
}

/// Same mapping as `set_content_type_from_file` in the C example.
fn content_type(uri: &str) -> &'static str {
    let ext = uri.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "html" => "text/html",
        "js" => "application/javascript",
        "css" => "text/css",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "svg" => "text/xml",
        _ => "text/plain",
    }
}

fn linker_be_nice() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let kind = &args[1];
        let what = &args[2];

        match kind.as_str() {
            "undefined-symbol" => match what.as_str() {
                what if what.starts_with("_defmt_") => {
                    eprintln!();
                    eprintln!(
                        "💡 `defmt` not found - make sure `defmt.x` is added as a linker script and you have included `use defmt_rtt as _;`"
                    );
                    eprintln!();
                }
                "_stack_start" => {
                    eprintln!();
                    eprintln!("💡 Is the linker script `linkall.x` missing?");
                    eprintln!();
                }
                what if what.starts_with("esp_rtos_") => {
                    eprintln!();
                    eprintln!(
                        "💡 `esp-radio` has no scheduler enabled. Make sure you have initialized `esp-rtos` or provided an external scheduler."
                    );
                    eprintln!();
                }
                "free"
                | "malloc"
                | "calloc"
                | "get_free_internal_heap_size"
                | "malloc_internal"
                | "realloc_internal"
                | "calloc_internal"
                | "free_internal" => {
                    eprintln!();
                    eprintln!(
                        "💡 Did you forget the `esp-alloc` dependency or didn't enable the `compat` feature on it?"
                    );
                    eprintln!();
                }
                _ => (),
            },
            // we don't have anything helpful for "missing-lib" yet
            _ => {
                std::process::exit(1);
            }
        }

        std::process::exit(0);
    }
}
