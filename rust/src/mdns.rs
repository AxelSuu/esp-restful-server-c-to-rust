//! mDNS responder: `dashboard.local` plus the `_http._tcp` service, as in `initialise_mdns()`.

use core::net::{Ipv4Addr, Ipv6Addr};

use edge_mdns::{
    HostAnswersMdnsHandler,
    buf::VecBufAccess,
    domain::base::Ttl,
    host::{Host, Service, ServiceAnswers},
    io::{self, IPV4_DEFAULT_SOCKET},
};
use edge_nal::UdpSplit;
use edge_nal_embassy::{Udp, UdpBuffers};
use embassy_futures::select::select;
use embassy_net::Stack;
use embassy_sync::{blocking_mutex::raw::NoopRawMutex, signal::Signal};
use esp_hal::rng::Rng;
use log::{info, warn};

use crate::HOSTNAME;

static SERVICE: Service = Service {
    name: "ESP32-WebServer",
    priority: 1,
    weight: 5,
    service: "_http",
    protocol: "_tcp",
    port: 80,
    service_subtypes: &[],
    txt_kvs: &[("chip", esp_hal::chip!()), ("path", "/")],
};

#[embassy_executor::task]
pub async fn mdns_task(stack: Stack<'static>) -> ! {
    let buffers = UdpBuffers::<1, 1500, 1500, 2>::new();
    let udp = Udp::new(stack, &buffers);
    let (recv_buf, send_buf) = (
        VecBufAccess::<NoopRawMutex, 1500>::new(),
        VecBufAccess::<NoopRawMutex, 1500>::new(),
    );

    loop {
        stack.wait_config_up().await;
        let Some(cfg) = stack.config_v4() else {
            continue;
        };
        let host = Host {
            hostname: HOSTNAME,
            ipv4: cfg.address.address(),
            ipv6: Ipv6Addr::UNSPECIFIED,
            ttl: Ttl::from_secs(60),
        };
        info!("mDNS: {}.local -> {}", HOSTNAME, host.ipv4);

        let mut socket =
            match io::bind(&udp, IPV4_DEFAULT_SOCKET, Some(Ipv4Addr::UNSPECIFIED), None).await {
                Ok(s) => s,
                Err(e) => {
                    warn!("mDNS bind failed: {e:?}");
                    stack.wait_config_down().await;
                    continue;
                }
            };
        let (recv, send) = socket.split();
        let signal = Signal::<NoopRawMutex, ()>::new();
        let mdns = io::Mdns::new(
            Some(Ipv4Addr::UNSPECIFIED),
            None,
            recv,
            send,
            &recv_buf,
            &send_buf,
            Rng::new(),
            &signal,
        );
        let answers = ServiceAnswers::new(&host, &SERVICE);

        // Re-announce with the new address after the link comes back.
        select(
            mdns.run(HostAnswersMdnsHandler::new(answers)),
            stack.wait_config_down(),
        )
        .await;
        warn!("mDNS: link down");
    }
}
