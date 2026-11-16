mod guest;
mod host;
pub mod mcp;
pub mod native_install;
pub mod runtime_acquire;
pub mod runtime_cache;
pub mod runtime_catalog;
pub mod runtime_download;
pub mod runtime_extract;

pub use runtime_acquire::{RuntimeArtifacts, acquire_runtime};

pub use guest::{GuestClient, GuestError};
pub use host::{HostConfig, app_install, validate_loopback_addr, validate_ui_act};
