//! Owns one native helper process from launch through cleanup.
//!
//! Helper output is treated as an untrusted protocol. It is bounded and parsed
//! into typed values; it is never included in public errors or status.

use anyhow::{Result, bail};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::Read,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    os::unix::{fs::MetadataExt, io::AsRawFd},
    path::{Component, Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

#[cfg(test)]
mod launch;

const MAX_HELPER_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PIPE_BYTES: usize = 64 * 1024;
const MAX_LINE_BYTES: usize = 8 * 1024;
const POLL: Duration = Duration::from_millis(25);

pub struct NativeSessionLaunch {
    pub helper: PathBuf,
    pub helper_sha256: String,
    pub assets: PathBuf,
    pub state_dir: PathBuf,
    pub agent_apk: PathBuf,
    pub agent_apk_sha256: String,
    pub source_sha: String,
    pub adb_port: u16,
    pub agent_port: u16,
    pub install_port: u16,
    pub startup_timeout_secs: u16,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionReason {
    Requested,
    UnexpectedExit,
    PipeFailure,
    StartupFailure,
}

#[derive(Clone, Debug)]
pub struct SessionExit {
    pub code: Option<i32>,
    pub signal: Option<i32>,
    pub reason: SessionReason,
    pub forced: bool,
    pub credential_absent: bool,
}

struct StartupCandidate {
    credentials: crate::session_credentials::SessionCredentials,
}

pub struct NativeSession {
    pid: u32,
    session_id: String,
    guest: crate::GuestClient,
    install: Arc<crate::native_install::NativeInstallClient>,
    cancel: CancellationToken,
    exit: watch::Receiver<Option<SessionExit>>,
}

struct StartupDrop(Option<CancellationToken>);
impl StartupDrop {
    fn disarm(&mut self) {
        self.0 = None;
    }
}
impl Drop for StartupDrop {
    fn drop(&mut self) {
        if let Some(cancel) = self.0.take() {
            cancel.cancel();
        }
    }
}

impl NativeSession {
    pub async fn start(
        launch: NativeSessionLaunch,
        deadline: Instant,
        cancel: CancellationToken,
    ) -> Result<Self> {
        let entered = Instant::now();
        let budget = entered + Duration::from_secs(u64::from(launch.startup_timeout_secs));
        let deadline = deadline.min(budget);
        validate_launch(&launch, deadline, &cancel)?;
        let worker_cancel = cancel.child_token();
        let mut drop_guard = StartupDrop(Some(worker_cancel.clone()));
        let (candidate_tx, candidate_rx) = mpsc::sync_channel(1);
        let (confirm_tx, confirm_rx) = mpsc::sync_channel(1);
        let (exit_tx, exit_rx) = watch::channel(None);
        let worker_launch = launch;
        let worker_cancelled = worker_cancel.clone();
        let worker = tokio::task::spawn_blocking(move || {
            run_owner(
                worker_launch,
                deadline,
                worker_cancelled,
                candidate_tx,
                confirm_rx,
                exit_tx,
            )
        });
        let candidate = tokio::task::spawn_blocking(move || {
            candidate_rx.recv_timeout(deadline.saturating_duration_since(Instant::now()))
        })
        .await
        .map_err(|_| anyhow::anyhow!("native helper startup failed"))?
        .map_err(|_| anyhow::anyhow!("native helper startup failed"))?;
        if cancel.is_cancelled() || Instant::now() >= deadline {
            worker_cancel.cancel();
            let _ = worker.await;
            bail!("native helper startup failed");
        }
        let credentials = candidate.credentials;
        let guest = credentials.guest_client();
        let rpc_deadline = deadline.min(Instant::now() + Duration::from_secs(5));
        let ping = tokio::time::timeout_at(
            tokio::time::Instant::from_std(rpc_deadline),
            guest.call("ping", serde_json::json!({})),
        )
        .await;
        let valid = matches!(ping, Ok(Ok(ref v)) if v.get("apiLevel").and_then(serde_json::Value::as_u64).is_some_and(|n| n >= 34));
        if !valid || cancel.is_cancelled() || Instant::now() >= deadline {
            worker_cancel.cancel();
            let _ = worker.await;
            bail!("native helper startup failed");
        }
        let install = Arc::new(credentials.install_client()?);
        let pid = credentials.helper_pid();
        let session_id = credentials.session_id().to_owned();
        confirm_tx
            .send(true)
            .map_err(|_| anyhow::anyhow!("native helper startup failed"))?;
        // The owner thread lives independently; completion is observed through watch.
        drop(worker);
        drop_guard.disarm();
        Ok(Self {
            pid,
            session_id,
            guest,
            install,
            cancel: worker_cancel,
            exit: exit_rx,
        })
    }

    pub fn helper_pid(&self) -> u32 {
        self.pid
    }
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn guest_client(&self) -> Result<crate::GuestClient> {
        if self.exit.borrow().is_some() {
            bail!("native helper has terminated");
        }
        Ok(self.guest.clone())
    }
    pub fn install_client(&self) -> Result<Arc<crate::native_install::NativeInstallClient>> {
        if self.exit.borrow().is_some() {
            bail!("native helper has terminated");
        }
        Ok(self.install.clone())
    }
    pub async fn wait_terminated(&self) -> Result<SessionExit> {
        let mut exit = self.exit.clone();
        loop {
            if let Some(value) = exit.borrow().clone() {
                return result_for_exit(value);
            }
            if exit.changed().await.is_err() {
                bail!("native helper status unavailable");
            }
        }
    }
    pub async fn shutdown(self) -> Result<SessionExit> {
        self.cancel.cancel();
        self.wait_terminated().await
    }
}

impl Drop for NativeSession {
    fn drop(&mut self) {
        self.cancel.cancel();
    }
}

fn result_for_exit(exit: SessionExit) -> Result<SessionExit> {
    if exit.reason == SessionReason::Requested
        && exit.code == Some(0)
        && !exit.forced
        && exit.credential_absent
    {
        Ok(exit)
    } else {
        Err(anyhow::anyhow!("native helper did not stop cleanly"))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    schema_version: u8,
    status: String,
    architecture: String,
    manifest_version: Option<String>,
    manifest_source_sha: Option<String>,
    session_id: Option<String>,
    agent_listen: Option<String>,
    install_listen: Option<String>,
    agent_apk_sha256: Option<String>,
    timings: Timings,
    cpu_count: u32,
    memory_bytes: u64,
    adb_listen: String,
    android_serial: Option<String>,
    kernel_sha256: Option<String>,
    rootfs_template_sha256: Option<String>,
    error: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Timings {
    total_startup_ms: u64,
    vm_start_ms: Option<u64>,
    readiness_ms: Option<u64>,
    agent_startup_ms: Option<u64>,
}

fn valid_hex(text: &str, len: usize) -> bool {
    text.len() == len
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn validate_launch(
    l: &NativeSessionLaunch,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<()> {
    if !cfg!(all(target_os = "macos", target_arch = "aarch64")) && !cfg!(test) {
        bail!("native sessions require macOS/aarch64");
    }
    if cancel.is_cancelled()
        || Instant::now() >= deadline
        || !(15..=1800).contains(&l.startup_timeout_secs)
        || !valid_hex(&l.source_sha, 40)
        || !valid_hex(&l.helper_sha256, 64)
        || !valid_hex(&l.agent_apk_sha256, 64)
        || [l.adb_port, l.agent_port, l.install_port]
            .iter()
            .any(|p| !(1024..=u16::MAX).contains(p))
        || l.adb_port == l.agent_port
        || l.adb_port == l.install_port
        || l.agent_port == l.install_port
    {
        bail!("invalid native session launch");
    }
    for path in [&l.helper, &l.assets, &l.state_dir, &l.agent_apk] {
        validate_path(path)?;
    }
    let state_parent = l
        .state_dir
        .parent()
        .ok_or_else(|| anyhow::anyhow!("invalid native session launch"))?;
    private_dir(state_parent)?;
    if l.state_dir.exists() {
        private_dir(&l.state_dir)?;
    }
    let state_credential = l.state_dir.join(".ashnazg-session-v1.json");
    if state_credential.exists() || fs::symlink_metadata(&state_credential).is_ok() {
        bail!("native session state is occupied");
    }
    private_dir(&l.assets)?;
    private_dir(
        l.agent_apk
            .parent()
            .ok_or_else(|| anyhow::anyhow!("invalid native session launch"))?,
    )?;
    validate_helper(&l.helper, &l.helper_sha256, deadline, cancel)?;
    let apk = fs::symlink_metadata(&l.agent_apk)?;
    if !apk.file_type().is_file() || apk.len() == 0 || apk.len() > 128 * 1024 * 1024 {
        bail!("invalid agent artifact");
    }
    Ok(())
}
fn validate_path(path: &Path) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
        || path.as_os_str().as_encoded_bytes().len() > 4096
    {
        bail!("invalid native session path");
    }
    let canonical = fs::canonicalize(path)?;
    if canonical != path {
        bail!("native session path is not canonical");
    }
    Ok(())
}
fn private_dir(path: &Path) -> Result<()> {
    let m = fs::symlink_metadata(path)?;
    let uid = unsafe { libc::geteuid() };
    if !m.is_dir() || m.uid() != uid || m.mode() & 0o7777 != 0o700 {
        bail!("native session directory is not private");
    }
    Ok(())
}
fn validate_helper(
    path: &Path,
    expected: &str,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<()> {
    let f = File::open(path)?;
    let before = f.metadata()?;
    let uid = unsafe { libc::geteuid() };
    if !before.is_file()
        || before.uid() != uid
        || before.len() > MAX_HELPER_BYTES
        || before.mode() & 0o100 == 0
        || before.mode() & 0o022 != 0
    {
        bail!("invalid helper artifact");
    }
    let mut reader = f;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    loop {
        if cancel.is_cancelled() || Instant::now() >= deadline {
            bail!("native session startup failed");
        }
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let after = reader.metadata()?;
    if before.dev() != after.dev()
        || before.ino() != after.ino()
        || before.len() != after.len()
        || hasher
            .finalize()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
            != expected
    {
        bail!("helper artifact verification failed");
    }
    Ok(())
}

#[cfg(unix)]
fn nonblocking(fd: i32) -> Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        bail!("native helper pipe setup failed");
    }
    Ok(())
}

fn run_owner(
    launch: NativeSessionLaunch,
    deadline: Instant,
    cancel: CancellationToken,
    candidate: mpsc::SyncSender<StartupCandidate>,
    confirm: mpsc::Receiver<bool>,
    exit_tx: watch::Sender<Option<SessionExit>>,
) {
    let mut child = match spawn(&launch) {
        Ok(c) => c,
        Err(_) => {
            let _ = exit_tx.send(Some(SessionExit {
                code: None,
                signal: None,
                reason: SessionReason::StartupFailure,
                forced: false,
                credential_absent: false,
            }));
            return;
        }
    };
    let pid = child.id();
    let setup = (|| -> Result<_> {
        let out = child.stdout.take().ok_or_else(|| anyhow::anyhow!("pipe"))?;
        let err = child.stderr.take().ok_or_else(|| anyhow::anyhow!("pipe"))?;
        nonblocking(out.as_raw_fd())?;
        nonblocking(err.as_raw_fd())?;
        Ok((out, err))
    })();
    let Ok((stdout, stderr)) = setup else {
        finish_child(&mut child, &cancel, SessionReason::StartupFailure);
        return;
    };
    let mut line = Vec::new();
    let (mut out_count, mut err_count) = (0usize, 0usize);
    let mut receipt: Option<Receipt> = None;
    let mut creds = None;
    let mut confirmed = false;
    loop {
        if cancel.is_cancelled() || Instant::now() >= deadline {
            let e = finish_child(
                &mut child,
                &cancel,
                if confirmed {
                    SessionReason::Requested
                } else {
                    SessionReason::StartupFailure
                },
            );
            let _ = exit_tx.send(Some(e));
            return;
        }
        let mut buf = [0u8; 4096];
        for (fd, is_out) in [(stdout.as_raw_fd(), true), (stderr.as_raw_fd(), false)] {
            let read_result = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
            if read_result > 0 {
                let n = read_result as usize;
                if is_out {
                    out_count += n;
                    if out_count > MAX_PIPE_BYTES {
                        let e = finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                        let _ = exit_tx.send(Some(e));
                        return;
                    }
                    for b in &buf[..n] {
                        if *b == b'\n' {
                            if line.len() > MAX_LINE_BYTES {
                                let e =
                                    finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                                let _ = exit_tx.send(Some(e));
                                return;
                            }
                            if receipt.is_some() {
                                let e =
                                    finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                                let _ = exit_tx.send(Some(e));
                                return;
                            }
                            match serde_json::from_slice::<Receipt>(&line) {
                                Ok(r) => {
                                    if r.status != "agent_ready"
                                        || r.schema_version != 1
                                        || r.architecture != "aarch64"
                                        || r.cpu_count == 0
                                        || r.memory_bytes == 0
                                        || r.timings.total_startup_ms == 0
                                        || r.timings.vm_start_ms.is_some_and(|v| v > 1_800_000)
                                        || r.timings.readiness_ms.is_some_and(|v| v > 1_800_000)
                                        || r.timings.agent_startup_ms.is_some_and(|v| v > 1_800_000)
                                        || r.error.is_some()
                                        || r.android_serial.as_deref().is_none_or(str::is_empty)
                                        || !r
                                            .kernel_sha256
                                            .as_deref()
                                            .is_some_and(|v| valid_hex(v, 64))
                                        || !r
                                            .rootfs_template_sha256
                                            .as_deref()
                                            .is_some_and(|v| valid_hex(v, 64))
                                        || r.manifest_version.as_deref() != Some("0.3.0")
                                        || r.manifest_source_sha.as_deref()
                                            != Some(&launch.source_sha)
                                        || r.agent_apk_sha256.as_deref()
                                            != Some(&launch.agent_apk_sha256)
                                        || r.adb_listen != format!("127.0.0.1:{}", launch.adb_port)
                                        || r.agent_listen.as_deref()
                                            != Some(&format!("127.0.0.1:{}", launch.agent_port))
                                        || r.install_listen.as_deref()
                                            != Some(&format!("127.0.0.1:{}", launch.install_port))
                                        || !r
                                            .session_id
                                            .as_deref()
                                            .is_some_and(crate::native_install::canonical_uuid)
                                    {
                                        let e = finish_child(
                                            &mut child,
                                            &cancel,
                                            SessionReason::StartupFailure,
                                        );
                                        let _ = exit_tx.send(Some(e));
                                        return;
                                    }
                                    receipt = Some(r);
                                }
                                Err(_) => {
                                    let e = finish_child(
                                        &mut child,
                                        &cancel,
                                        SessionReason::StartupFailure,
                                    );
                                    let _ = exit_tx.send(Some(e));
                                    return;
                                }
                            }
                            line.clear();
                        } else {
                            line.push(*b);
                            if line.len() > MAX_LINE_BYTES {
                                let e =
                                    finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                                let _ = exit_tx.send(Some(e));
                                return;
                            }
                        }
                    }
                } else {
                    err_count += n;
                    if err_count > MAX_PIPE_BYTES {
                        let e = finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                        let _ = exit_tx.send(Some(e));
                        return;
                    }
                }
            } else if read_result < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() != std::io::ErrorKind::WouldBlock
                    && error.kind() != std::io::ErrorKind::Interrupted
                {
                    let e = finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                    let _ = exit_tx.send(Some(e));
                    return;
                }
            }
        }
        if let Some(r) = receipt.as_ref().filter(|_| creds.is_none()) {
            if child.try_wait().ok().flatten().is_some() {
                let e = finish_child(&mut child, &cancel, SessionReason::UnexpectedExit);
                let _ = exit_tx.send(Some(e));
                return;
            }
            let expected = crate::session_credentials::SessionExpectations {
                session_id: r.session_id.clone().unwrap(),
                helper_pid: pid,
                manifest_source_sha: launch.source_sha.clone(),
                agent_apk_sha256: launch.agent_apk_sha256.clone(),
                agent_addr: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), launch.agent_port),
                install_addr: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), launch.install_port),
            };
            match crate::session_credentials::read_private_credentials(
                &launch.state_dir,
                &expected,
                deadline,
                &cancel,
            ) {
                Ok(c) => {
                    if candidate.send(StartupCandidate { credentials: c }).is_err() {
                        let e = finish_child(&mut child, &cancel, SessionReason::StartupFailure);
                        let _ = exit_tx.send(Some(e));
                        return;
                    }
                    creds = Some(());
                }
                Err(_) => {
                    let e = finish_child(&mut child, &cancel, SessionReason::StartupFailure);
                    let _ = exit_tx.send(Some(e));
                    return;
                }
            }
        }
        if creds.is_some() && !confirmed {
            match confirm.recv_timeout(POLL) {
                Ok(true) => confirmed = true,
                _ => {
                    if cancel.is_cancelled() {
                        continue;
                    }
                }
            }
        }
        if confirmed {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let e = exit_status(
                        status,
                        SessionReason::UnexpectedExit,
                        false,
                        credential_absent(&launch.state_dir),
                    );
                    let _ = exit_tx.send(Some(e));
                    return;
                }
                Err(_) => {
                    let e = finish_child(&mut child, &cancel, SessionReason::PipeFailure);
                    let _ = exit_tx.send(Some(e));
                    return;
                }
                _ => {}
            }
        }
        std::thread::sleep(POLL);
    }
}
fn spawn(l: &NativeSessionLaunch) -> Result<Child> {
    let mut c = Command::new(&l.helper);
    c.args([
        "--assets",
        l.assets.to_str().ok_or_else(|| anyhow::anyhow!("path"))?,
        "--state-dir",
        l.state_dir
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("path"))?,
        "--adb-port",
        &l.adb_port.to_string(),
        "--agent-port",
        &l.agent_port.to_string(),
        "--install-port",
        &l.install_port.to_string(),
        "--timeout",
        &l.startup_timeout_secs.to_string(),
        "--agent-apk",
        l.agent_apk
            .to_str()
            .ok_or_else(|| anyhow::anyhow!("path"))?,
        "--agent-sha256",
        &l.agent_apk_sha256,
    ]);
    c.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("ASHNAZG_TOKEN")
        .env_remove("ASHNAZG_AGENT_ADDR");
    c.spawn().map_err(Into::into)
}
fn credential_absent(state: &Path) -> bool {
    fs::symlink_metadata(state.join(".ashnazg-session-v1.json")).is_err()
}
fn exit_status(
    status: ExitStatus,
    reason: SessionReason,
    forced: bool,
    absent: bool,
) -> SessionExit {
    use std::os::unix::process::ExitStatusExt;
    SessionExit {
        code: status.code(),
        signal: status.signal(),
        reason,
        forced,
        credential_absent: absent,
    }
}
fn finish_child(
    child: &mut Child,
    _cancel: &CancellationToken,
    reason: SessionReason,
) -> SessionExit {
    let _ = child.kill();
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(s)) => return exit_status(s, reason, true, false),
            Err(_) => {
                return SessionExit {
                    code: None,
                    signal: None,
                    reason,
                    forced: true,
                    credential_absent: false,
                };
            }
            _ => {}
        }
        if start.elapsed() > Duration::from_secs(5) {
            return SessionExit {
                code: None,
                signal: None,
                reason,
                forced: true,
                credential_absent: false,
            };
        }
        std::thread::sleep(POLL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const READY: &str = r#"{"schema_version":1,"status":"agent_ready","architecture":"aarch64","cpu_count":4,"memory_bytes":8589934592,"adb_listen":"127.0.0.1:15555","android_serial":"emulator-5554","manifest_version":"0.3.0","manifest_source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","kernel_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","rootfs_template_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","timings":{"total_startup_ms":1000,"vm_start_ms":500,"readiness_ms":500,"agent_startup_ms":250},"session_id":"6f6f1234-1234-4234-8234-123456789abc","agent_listen":"127.0.0.1:17777","install_listen":"127.0.0.1:17778","agent_apk_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#;

    #[test]
    fn runtime_receipt_fixture_parses_exact_swift_schema() {
        let receipt: Receipt = serde_json::from_str(READY).unwrap();
        assert_eq!(receipt.status, "agent_ready");
        assert_eq!(
            receipt.session_id.as_deref(),
            Some("6f6f1234-1234-4234-8234-123456789abc")
        );
        assert!(
            serde_json::from_str::<Receipt>(&READY.replacen(
                "\"schema_version\":1",
                "\"schema_version\":1,\"schema_version\":1",
                1
            ))
            .is_err()
        );
        assert!(
            serde_json::from_str::<Receipt>(&READY.replace(
                "\"status\":\"agent_ready\"",
                "\"status\":\"agent_ready\",\"secret\":\"sentinel\""
            ))
            .is_err()
        );
    }

    #[test]
    fn lowercase_hex_validation_is_exact_and_bounded() {
        assert!(valid_hex(&"a1".repeat(32), 64));
        assert!(!valid_hex(&"A1".repeat(32), 64));
        assert!(!valid_hex(&"a1".repeat(31), 64));
        assert!(!valid_hex(&format!("{}g", "a".repeat(63)), 64));
    }
}
