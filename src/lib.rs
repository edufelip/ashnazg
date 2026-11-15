mod guest;
mod host;
pub mod mcp;

pub use guest::{GuestClient, GuestError};
pub use host::{HostConfig, app_install, validate_loopback_addr, validate_ui_act};
