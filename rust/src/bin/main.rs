//! Port of `esp_rest_main.c`: Wi-Fi station, mDNS and the REST server.

#![no_std]
#![no_main]
#![deny(
    clippy::mem_forget,
    reason = "mem::forget is generally not safe to do with esp_hal types, especially those \
    holding buffers for the duration of a data transfer."
)]
#![deny(clippy::large_stack_frames)]

use embassy_executor::Spawner;
use embassy_net::{DhcpConfig, Runner, StackResources};
use embassy_time::{Duration, Timer};
use esp_backtrace as _;
use esp_hal::{clock::CpuClock, rng::Rng, timer::timg::TimerGroup};
use esp_radio::wifi::{
    AuthenticationMethodConfig, Config, Interface, WifiController, sta::StationConfig,
};
use log::{info, warn};
use restful_server::{
    HOSTNAME, mdns,
    web::{self, WEB_TASKS},
};
use static_cell::StaticCell;

extern crate alloc;

esp_bootloader_esp_idf::esp_app_desc!();

const SSID: &str = env!("WIFI_SSID");
const PASSWORD: &str = env!("WIFI_PASSWORD");

/// DHCP, mDNS and one TCP socket per web task.
const SOCKETS: usize = 2 + WEB_TASKS;

#[allow(
    clippy::large_stack_frames,
    reason = "it's not unusual to allocate larger buffers etc. in main"
)]
#[esp_rtos::main]
async fn main(spawner: Spawner) -> ! {
    esp_println::logger::init_logger_from_env();

    let peripherals = esp_hal::init(esp_hal::Config::default().with_cpu_clock(CpuClock::max()));

    esp_alloc::heap_allocator!(#[esp_hal::ram(reclaimed)] size: 64 * 1024);
    esp_alloc::heap_allocator!(size: 36 * 1024);

    let timg0 = TimerGroup::new(peripherals.TIMG0);
    esp_rtos::start(timg0.timer0, peripherals.FROM_CPU_INTR0);

    let controller = WifiController::new(peripherals.WIFI, Default::default())
        .expect("Failed to initialize Wi-Fi controller");

    let mut dhcp = DhcpConfig::default();
    dhcp.hostname = Some(HOSTNAME.try_into().unwrap());
    let rng = Rng::new();
    let seed = u64::from(rng.random()) << 32 | u64::from(rng.random());

    static RESOURCES: StaticCell<StackResources<SOCKETS>> = StaticCell::new();
    let (stack, runner) = embassy_net::new(
        Interface::station(),
        embassy_net::Config::dhcpv4(dhcp),
        RESOURCES.init(StackResources::new()),
        seed,
    );

    spawner.spawn(connection(controller).unwrap());
    spawner.spawn(net_task(runner).unwrap());

    stack.wait_config_up().await;
    if let Some(cfg) = stack.config_v4() {
        info!("Got IP: {}", cfg.address);
    }

    spawner.spawn(mdns::mdns_task(stack).unwrap());
    info!("Starting HTTP Server");
    for id in 0..WEB_TASKS {
        spawner.spawn(web::web_task(id, stack).unwrap());
    }

    loop {
        Timer::after(Duration::from_secs(60)).await;
    }
}

/// Connects and reconnects forever, so the device recovers after an access point reboot.
#[allow(clippy::large_stack_frames, reason = "runs once, as its own task")]
#[embassy_executor::task]
async fn connection(mut controller: WifiController<'static>) -> ! {
    let config = Config::Station(
        StationConfig::default()
            .with_ssid(SSID.try_into().unwrap())
            .with_authentication(AuthenticationMethodConfig::Wpa2Personal(
                PASSWORD.try_into().unwrap(),
            )),
    );
    controller.set_config(&config).unwrap();
    // ESP-IDF's default; esp-radio defaults to no power saving.
    #[cfg(feature = "ps-min")]
    controller
        .set_power_saving(esp_radio::wifi::PowerSaveMode::Minimum)
        .unwrap();

    loop {
        info!("Connecting to {SSID}...");
        match controller.connect_async().await {
            Ok(_) => {
                info!("Wi-Fi connected");
                let reason = controller.wait_for_disconnect_async().await;
                warn!("Wi-Fi disconnected: {reason:?}");
            }
            Err(e) => warn!("Wi-Fi connect failed: {e:?}"),
        }
        Timer::after(Duration::from_secs(2)).await;
    }
}

#[embassy_executor::task]
async fn net_task(mut runner: Runner<'static, Interface>) -> ! {
    runner.run().await
}
