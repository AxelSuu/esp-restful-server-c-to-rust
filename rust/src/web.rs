//! HTTP server: the port of `rest_server.c`.

use embassy_net::Stack;
use esp_hal::rng::Rng;
use log::{info, warn};
use picoserve::{
    ResponseSent,
    extract::Json,
    io::Read,
    request::{Path, Request},
    response::{self, Content, IntoResponse, Response, ResponseWriter, StatusCode},
    routing::{PathRouter, PathRouterService, get, post},
};
use serde::{Deserialize, Serialize};

include!(concat!(env!("OUT_DIR"), "/assets.rs"));

/// Concurrent HTTP connections; the C server allows 7 sockets but serves them from one task.
pub const WEB_TASKS: usize = 4;

#[derive(Serialize)]
struct SystemInfo {
    chip: &'static str,
    /// Key kept so the unchanged frontend can display it.
    idf_version: &'static str,
    cores: usize,
}

#[derive(Serialize)]
struct Temperature {
    raw: u32,
}

#[derive(Deserialize)]
struct Brightness {
    red: u8,
    green: u8,
    blue: u8,
}

pub fn router() -> picoserve::Router<impl PathRouter> {
    let app = picoserve::Router::from_service(StaticFiles)
        .route(
            "/api/v1/system/info",
            get(|| async {
                response::Json(SystemInfo {
                    chip: esp_hal::chip!(),
                    idf_version: concat!("esp-hal v", env!("ESP_HAL_VERSION")),
                    cores: esp_hal::system::Cpu::COUNT,
                })
            }),
        )
        .route(
            "/api/v1/temp/raw",
            // Simulated, as in the C example.
            get(|| async {
                response::Json(Temperature {
                    raw: Rng::new().random() % 20,
                })
            }),
        )
        .route(
            "/api/v1/light/brightness",
            post(|Json(b): Json<Brightness>| async move {
                info!(
                    "Light control: red = {}, green = {}, blue = {}",
                    b.red, b.green, b.blue
                );
                "Post control value successfully"
            }),
        );

    // Measurement only: off by default so the size numbers stay those of the port.
    #[cfg(feature = "heap-stats")]
    let app = app.route(
        "/api/v1/heap",
        get(|| async { response::Json(heap_stats()) }),
    );
    app
}

#[cfg(feature = "heap-stats")]
#[derive(Serialize)]
struct HeapStats {
    total: usize,
    used: usize,
    peak: usize,
}

#[cfg(feature = "heap-stats")]
fn heap_stats() -> HeapStats {
    let s = esp_alloc::HEAP.stats();
    HeapStats {
        total: s.size,
        used: s.current_usage,
        peak: s.max_usage,
    }
}

/// Serves the embedded frontend; the C build reads the same files from LittleFS.
struct StaticFiles;

struct Asset(&'static str, &'static [u8]);

impl Content for Asset {
    fn content_type(&self) -> &'static str {
        self.0
    }

    fn content_length(&self) -> usize {
        self.1.len()
    }

    async fn write_content<W: picoserve::io::Write>(self, mut w: W) -> Result<(), W::Error> {
        w.write_all(self.1).await
    }
}

impl PathRouterService for StaticFiles {
    async fn call_path_router_service<R: Read, W: ResponseWriter<Error = R::Error>>(
        &self,
        _state: &(),
        _params: (),
        path: Path<'_>,
        request: Request<'_, R>,
        response_writer: W,
    ) -> Result<ResponseSent, W::Error> {
        let uri = match path.encoded() {
            p if p.ends_with('/') => "/index.html",
            p => p,
        };
        let asset = ASSETS.iter().find(|(u, ..)| *u == uri);
        let connection = request.body_connection.finalize().await?;
        match asset {
            Some(&(_, ty, body)) if request.parts.method() == "GET" => {
                let sent = Response::ok(Asset(ty, body))
                    .write_to(connection, response_writer)
                    .await;
                info!("File sending complete");
                sent
            }
            _ => {
                warn!("Failed to open file : {uri}");
                (StatusCode::NOT_FOUND, "Not found")
                    .write_to(connection, response_writer)
                    .await
            }
        }
    }
}

static CONFIG: picoserve::Config = picoserve::Config::new(picoserve::Timeouts {
    start_read_request: embassy_time::Duration::from_secs(5),
    persistent_start_read_request: embassy_time::Duration::from_secs(5),
    read_request: embassy_time::Duration::from_secs(3),
    write: embassy_time::Duration::from_secs(3),
})
.keep_connection_alive();

#[embassy_executor::task(pool_size = WEB_TASKS)]
pub async fn web_task(id: usize, stack: Stack<'static>) -> ! {
    let app = router();
    let mut tcp_rx = [0; 1024];
    let mut tcp_tx = [0; 4096];
    let mut http = [0; 2048];

    picoserve::Server::new(&app, &CONFIG, &mut http)
        .listen_and_serve(id, stack, 80, &mut tcp_rx, &mut tcp_tx)
        .await
        .into_never()
}
