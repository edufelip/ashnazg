//! Verified native runtime acquisition and launch composition.

use crate::{
    native_session::{NativeSession, NativeSessionLaunch},
    runtime_acquire::{RuntimeArtifacts, acquire_runtime},
};
use anyhow::{Context, anyhow, bail, ensure};
use std::{
    fs::{self, File},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Component, Path, PathBuf},
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

const RUNTIME_VERSION: &str = "0.3.2";
const ACQUISITION_TIMEOUT: Duration = Duration::from_secs(900);
const STARTUP_TIMEOUT: u16 = 300;

/// Validated paths and source identity for the current user's native runtime.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NativeBootstrapConfig {
    home: PathBuf,
    cache_root: PathBuf,
    state_dir: PathBuf,
    source_sha: String,
    version: String,
}

/// Derive private, source-scoped native paths for an existing user's home.
pub fn config_for_user(
    home: PathBuf,
    source_sha: Option<&str>,
) -> anyhow::Result<NativeBootstrapConfig> {
    let source_sha = source_sha.ok_or_else(|| anyhow!("runtime source SHA is unavailable"))?;
    ensure!(valid_sha(source_sha), "invalid runtime source SHA");
    ensure!(home.is_absolute(), "user home must be an absolute path");
    let metadata = fs::symlink_metadata(&home).context("could not inspect user home")?;
    ensure!(
        metadata.is_dir()
            && !metadata.file_type().is_symlink()
            && metadata.uid() == unsafe { libc::geteuid() },
        "user home must be an existing directory owned by current user"
    );
    Ok(NativeBootstrapConfig {
        cache_root: home.join("Library/Caches/ashnazg/runtime"),
        state_dir: home.join(format!(
            "Library/Application Support/ashnazg/native-mcp/{source_sha}"
        )),
        home,
        source_sha: source_sha.to_owned(),
        version: RUNTIME_VERSION.to_owned(),
    })
}

/// Acquire the catalog-pinned runtime and start one owned native session.
pub async fn start_native(
    config: NativeBootstrapConfig,
    cancel: CancellationToken,
) -> anyhow::Result<NativeSession> {
    ensure!(!cancel.is_cancelled(), "native runtime startup cancelled");
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        bail!("native runtime requires macOS/aarch64");
    }
    ensure!(valid_sha(&config.source_sha), "invalid runtime source SHA");
    ensure!(
        config.version == RUNTIME_VERSION,
        "unsupported native runtime version"
    );
    ensure!(
        config.home.is_absolute(),
        "user home must be an absolute path"
    );
    let home_metadata =
        fs::symlink_metadata(&config.home).context("could not inspect user home")?;
    ensure!(
        home_metadata.is_dir()
            && !home_metadata.file_type().is_symlink()
            && home_metadata.uid() == unsafe { libc::geteuid() },
        "user home must be an existing directory owned by current user"
    );
    ensure!(
        config.cache_root == config.home.join("Library/Caches/ashnazg/runtime"),
        "native runtime cache path does not match the private default"
    );
    ensure!(
        config.state_dir
            == config.home.join(format!(
                "Library/Application Support/ashnazg/native-mcp/{}",
                config.source_sha
            )),
        "native state path does not match the source-scoped default"
    );
    ensure_private_path(&config.home, &config.cache_root)?;
    ensure_private_path(&config.home, &config.state_dir)?;

    let worker_cancel = cancel.child_token();
    let acquisition_deadline = Instant::now() + ACQUISITION_TIMEOUT;
    let artifacts = acquire_runtime(
        config.cache_root.clone(),
        config.version.clone(),
        config.source_sha.clone(),
        acquisition_deadline,
        worker_cancel.clone(),
    )
    .await
    .context("native runtime acquisition failed")?;
    validate_artifact_identity(&artifacts, &config.version, &config.source_sha)?;
    ensure!(
        !worker_cancel.is_cancelled(),
        "native runtime startup cancelled"
    );

    let launch = launch_from_artifacts(&artifacts, &config.state_dir);
    NativeSession::start(
        launch,
        Instant::now() + Duration::from_secs(u64::from(STARTUP_TIMEOUT)),
        worker_cancel,
    )
    .await
    .context("native session startup failed")
}

fn launch_from_artifacts(artifacts: &RuntimeArtifacts, state_dir: &Path) -> NativeSessionLaunch {
    NativeSessionLaunch {
        helper: artifacts.helper.clone(),
        helper_sha256: artifacts.helper_sha256.clone(),
        assets: artifacts.assets.clone(),
        state_dir: state_dir.to_path_buf(),
        agent_apk: artifacts.agent_apk.clone(),
        agent_apk_sha256: artifacts.agent_apk_sha256.clone(),
        source_sha: artifacts.source_sha.clone(),
        adb_port: 15555,
        agent_port: 7777,
        install_port: 17778,
        startup_timeout_secs: STARTUP_TIMEOUT,
    }
}

fn validate_artifact_identity(
    artifacts: &RuntimeArtifacts,
    version: &str,
    source_sha: &str,
) -> anyhow::Result<()> {
    ensure!(
        artifacts.version == version,
        "acquired runtime version mismatch"
    );
    ensure!(
        artifacts.source_sha == source_sha,
        "acquired runtime source mismatch"
    );
    Ok(())
}

fn valid_sha(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Create missing app-owned directories and validate each existing component without following links.
fn ensure_private_path(home: &Path, path: &Path) -> anyhow::Result<()> {
    ensure!(
        home.is_absolute() && path.is_absolute() && path.starts_with(home),
        "private directory path must be under the validated home"
    );
    let relative = path.strip_prefix(home)?;
    ensure!(
        relative
            .components()
            .all(|part| matches!(part, Component::Normal(_))),
        "private directory path contains traversal components"
    );
    let mut directory =
        open_absolute_dir(home).context("could not open validated home without following links")?;
    let home_metadata = directory.metadata()?;
    ensure!(
        home_metadata.uid() == unsafe { libc::geteuid() },
        "user home must be owned by current user"
    );
    let mut private_scope = false;
    for component in relative.components() {
        let Component::Normal(name) = component else {
            bail!("invalid private directory component")
        };
        if name == "ashnazg" {
            private_scope = true;
        }
        let bytes = name.as_bytes();
        match open_dir_at(directory.as_raw_fd(), bytes) {
            Ok(next) => {
                let metadata = next.metadata()?;
                if private_scope {
                    ensure!(
                        metadata.uid() == unsafe { libc::geteuid() },
                        "private path component is not owned by current user"
                    );
                    ensure!(
                        metadata.mode() & 0o7777 == 0o700,
                        "app directory must be owned mode 0700"
                    );
                } else {
                    ensure!(
                        metadata.mode() & 0o022 == 0 || metadata.mode() & 0o1000 != 0,
                        "private path parent is writable by other users"
                    );
                }
                directory = next;
            }
            Err(error) if error.raw_os_error() == Some(libc::ENOENT) => {
                let name =
                    std::ffi::CString::new(bytes).context("invalid private directory name")?;
                if unsafe { libc::mkdirat(directory.as_raw_fd(), name.as_ptr(), 0o700) } != 0 {
                    let create_error = std::io::Error::last_os_error();
                    if create_error.raw_os_error() != Some(libc::EEXIST) {
                        return Err(create_error).context("could not create private app directory");
                    }
                }
                let next = open_dir_at(directory.as_raw_fd(), bytes)
                    .context("could not open created directory without following links")?;
                let metadata = next.metadata()?;
                if private_scope {
                    ensure!(
                        metadata.uid() == unsafe { libc::geteuid() },
                        "private path component is not owned by current user"
                    );
                    ensure!(
                        metadata.mode() & 0o7777 == 0o700,
                        "app directory must be owned mode 0700"
                    );
                } else {
                    ensure!(
                        metadata.mode() & 0o022 == 0 || metadata.mode() & 0o1000 != 0,
                        "private path parent is writable by other users"
                    );
                }
                directory = next;
            }
            Err(error) => {
                return Err(error)
                    .context("could not open private path component without following links");
            }
        }
    }
    Ok(())
}

fn open_absolute_dir(path: &Path) -> anyhow::Result<File> {
    ensure!(path.is_absolute(), "directory path must be absolute");
    let mut directory = open_dir_at(libc::AT_FDCWD, b"/")?;
    for component in path.components().skip(1) {
        let Component::Normal(name) = component else {
            bail!("invalid absolute directory component")
        };
        let next = open_dir_at(directory.as_raw_fd(), name.as_bytes())?;
        let metadata = next.metadata()?;
        ensure!(
            metadata.mode() & 0o022 == 0 || metadata.mode() & 0o1000 != 0,
            "home path parent is writable by other users"
        );
        directory = next;
    }
    Ok(directory)
}

fn open_dir_at(parent: i32, name: &[u8]) -> std::io::Result<File> {
    let name = std::ffi::CString::new(name)
        .map_err(|_| std::io::Error::from(std::io::ErrorKind::InvalidInput))?;
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;

    fn private_tempdir() -> tempfile::TempDir {
        tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap()
    }

    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[tokio::test]
    async fn already_cancelled_bootstrap_does_not_create_private_directories() {
        let home = private_tempdir();
        let config =
            config_for_user(home.path().canonicalize().unwrap(), Some(&"a".repeat(40))).unwrap();
        let library = config.home.join("Library");
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert!(start_native(config, cancel).await.is_err());
        assert!(
            !library.exists(),
            "a cancelled initializer must not start path setup"
        );
    }

    #[test]
    fn config_rejects_missing_source_stamp() {
        let home = private_tempdir();
        let error = config_for_user(home.path().to_path_buf(), None).unwrap_err();
        assert!(error.to_string().contains("source SHA"));
    }

    #[test]
    fn config_rejects_malformed_source_stamp() {
        let home = private_tempdir();
        for source in ["", "ABCDEF0123456789abcdef0123456789abcdef01", "1234"] {
            assert!(config_for_user(home.path().to_path_buf(), Some(source)).is_err());
        }
    }

    #[test]
    fn config_requires_existing_absolute_home() {
        let relative = PathBuf::from("relative-home");
        assert!(config_for_user(relative, Some(&"a".repeat(40))).is_err());
        let missing = std::env::temp_dir().join(format!("ashnazg-missing-{}", std::process::id()));
        assert!(config_for_user(missing, Some(&"a".repeat(40))).is_err());
    }

    #[test]
    fn config_uses_source_scoped_private_paths() {
        let home = private_tempdir();
        let source = "0123456789abcdef0123456789abcdef01234567";
        let config = config_for_user(home.path().to_path_buf(), Some(source)).unwrap();
        assert_eq!(
            config.cache_root,
            home.path().join("Library/Caches/ashnazg/runtime")
        );
        assert_eq!(
            config.state_dir,
            home.path().join(format!(
                "Library/Application Support/ashnazg/native-mcp/{source}"
            ))
        );
        assert_eq!(config.version, "0.3.2");
        assert!(!config.cache_root.exists());
        assert!(!config.state_dir.exists());
    }

    #[test]
    fn private_directory_setup_refuses_symlink_without_touching_target() {
        let parent = private_tempdir();
        let home = parent.path().canonicalize().unwrap();
        let target = home.join("caller-data");
        fs::create_dir(&target).unwrap();
        fs::write(target.join("keep"), b"unchanged").unwrap();
        let app_dir = home.join("ashnazg");
        fs::create_dir(&app_dir).unwrap();
        fs::set_permissions(&app_dir, fs::Permissions::from_mode(0o700)).unwrap();
        let link = app_dir.join("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        assert!(ensure_private_path(&home, &link.join("child")).is_err());
        assert_eq!(fs::read(target.join("keep")).unwrap(), b"unchanged");
        assert!(!target.join("child").exists());
    }

    #[test]
    fn private_directory_setup_creates_only_private_app_tree() {
        use std::os::unix::fs::PermissionsExt;
        let temporary_home = private_tempdir();
        let home = temporary_home.path().canonicalize().unwrap();
        let path = home.join("Library/Caches/ashnazg/runtime");
        ensure_private_path(&home, &path).unwrap();
        for directory in [
            home.join("Library"),
            home.join("Library/Caches"),
            home.join("Library/Caches/ashnazg"),
            path.clone(),
        ] {
            assert_eq!(
                fs::metadata(directory).unwrap().permissions().mode() & 0o777,
                0o700
            );
        }
    }

    #[test]
    fn private_directory_setup_does_not_repair_existing_mode() {
        use std::os::unix::fs::PermissionsExt;
        let parent = private_tempdir();
        let home = parent.path().canonicalize().unwrap();
        let app_dir = home.join("ashnazg");
        fs::create_dir(&app_dir).unwrap();
        fs::set_permissions(&app_dir, fs::Permissions::from_mode(0o700)).unwrap();
        let existing = app_dir.join("caller-dir");
        fs::create_dir(&existing).unwrap();
        fs::set_permissions(&existing, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(ensure_private_path(&home, &existing).is_err());
        assert_eq!(
            fs::metadata(existing).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    fn artifacts(version: &str, source_sha: &str) -> RuntimeArtifacts {
        RuntimeArtifacts {
            version: version.to_owned(),
            source_sha: source_sha.to_owned(),
            catalog_sha256: "c".repeat(64),
            helper: PathBuf::from("helper"),
            helper_sha256: "a".repeat(64),
            agent_apk: PathBuf::from("agent.apk"),
            agent_apk_sha256: "b".repeat(64),
            fixture_apk: PathBuf::from("fixture.apk"),
            assets: PathBuf::from("assets"),
            cache_hit: false,
        }
    }

    #[test]
    fn acquired_artifact_identity_must_match_config() {
        let source = "0123456789abcdef0123456789abcdef01234567";
        let result = artifacts("0.2.0", source);
        assert!(validate_artifact_identity(&result, "0.3.2", source).is_err());
        let result = artifacts("0.3.2", "a123456789abcdef0123456789abcdef01234567");
        assert!(validate_artifact_identity(&result, "0.3.2", source).is_err());
    }

    #[test]
    fn native_launch_uses_catalog_hashes_and_fixed_identity() {
        let source = "0123456789abcdef0123456789abcdef01234567";
        let artifacts = artifacts("0.3.2", source);
        let launch = launch_from_artifacts(&artifacts, Path::new("state"));
        assert_eq!(launch.helper_sha256, artifacts.helper_sha256);
        assert_eq!(launch.agent_apk_sha256, artifacts.agent_apk_sha256);
        assert_eq!(launch.source_sha, source);
        assert_eq!(
            (launch.adb_port, launch.agent_port, launch.install_port),
            (15555, 7777, 17778)
        );
        assert_eq!(launch.startup_timeout_secs, 300);
    }
}
