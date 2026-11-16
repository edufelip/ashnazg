pub mod apk_manifest;
mod axml_package;
mod guest;
mod host;
pub mod mcp;
pub mod native_install;
pub mod native_session;
pub mod runtime_acquire;
pub mod runtime_cache;
pub mod runtime_catalog;
pub mod runtime_download;
pub mod runtime_extract;
pub mod session_credentials;
#[cfg(unix)]
pub mod stdio_bridge;

pub use runtime_acquire::{RuntimeArtifacts, acquire_runtime};

pub use guest::{GuestClient, GuestError};
pub use host::{HostConfig, app_install, validate_loopback_addr, validate_ui_act};
