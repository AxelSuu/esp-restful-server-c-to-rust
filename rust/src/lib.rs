#![no_std]

pub mod mdns;
pub mod web;

/// mDNS host name, `CONFIG_EXAMPLE_MDNS_HOST_NAME` in the C build.
pub const HOSTNAME: &str = "dashboard";
