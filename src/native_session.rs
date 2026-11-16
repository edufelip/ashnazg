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
struct OwnerChannels {
    candidate: mpsc::SyncSender<StartupCandidate>,
    confirm: mpsc::Receiver<bool>,
    ack: mpsc::SyncSender<bool>,
    exit: watch::Sender<Option<SessionExit>>,
    fixture: bool,
}

pub struct NativeSession {
    pid: u32,
    session_id: String,
    guest: crate::GuestClient,
    install: Arc<crate::native_install::NativeInstallClient>,
    cancel: CancellationToken,
    exit: watch::Receiver<Option<SessionExit>>,
    worker: tokio::sync::Mutex<Option<tokio::task::JoinHandle<SessionExit>>>,
    joined: watch::Receiver<bool>,
    join_notify: watch::Sender<bool>,
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
        Self::start_impl(launch, deadline, cancel, false).await
    }

    #[cfg(test)]
    async fn start_fixture(
        launch: NativeSessionLaunch,
        deadline: Instant,
        cancel: CancellationToken,
    ) -> Result<Self> {
        Self::start_impl(launch, deadline, cancel, true).await
    }

    async fn start_impl(
        launch: NativeSessionLaunch,
        deadline: Instant,
        cancel: CancellationToken,
        fixture: bool,
    ) -> Result<Self> {
        let started = Instant::now();
        let deadline =
            deadline.min(started + Duration::from_secs(u64::from(launch.startup_timeout_secs)));
        let worker_cancel = cancel.child_token();
        let mut drop_guard = StartupDrop(Some(worker_cancel.clone()));
        let (candidate_tx, candidate_rx) = mpsc::sync_channel(1);
        let (confirm_tx, confirm_rx) = mpsc::sync_channel(1);
        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        let (exit_tx, exit_rx) = watch::channel(None);
        let (join_notify, joined) = watch::channel(false);
        let worker_cancelled = worker_cancel.clone();
        let worker = tokio::task::spawn_blocking(move || {
            run_owner(
                launch,
                deadline,
                worker_cancelled,
                OwnerChannels {
                    candidate: candidate_tx,
                    confirm: confirm_rx,
                    ack: ack_tx,
                    exit: exit_tx,
                    fixture,
                },
            )
        });
        let mut worker = Some(worker);
        let candidate = loop {
            if cancel.is_cancelled() || Instant::now() >= deadline {
                worker_cancel.cancel();
                let _ = join_worker_handle(worker.take().unwrap()).await;
                bail!("native helper startup failed");
            }
            match candidate_rx.try_recv() {
                Ok(candidate) => break candidate,
                Err(mpsc::TryRecvError::Disconnected) => {
                    worker_cancel.cancel();
                    let _ = join_worker_handle(worker.take().unwrap()).await;
                    bail!("native helper startup failed");
                }
                Err(mpsc::TryRecvError::Empty) => tokio::time::sleep(POLL).await,
            }
        };
        let credentials = candidate.credentials;
        let guest = credentials.guest_client();
        let ping_deadline = deadline.min(Instant::now() + Duration::from_secs(5));
        let ping = tokio::time::timeout_at(
            tokio::time::Instant::from_std(ping_deadline),
            guest.call("ping", serde_json::json!({})),
        )
        .await;
        let valid = matches!(ping, Ok(Ok(ref v)) if v.get("apiLevel").and_then(serde_json::Value::as_u64).is_some_and(|n| n >= 34));
        let install = match (valid && !cancel.is_cancelled() && Instant::now() < deadline)
            .then(|| credentials.install_client())
            .transpose()
        {
            Ok(Some(install)) => Arc::new(install),
            _ => {
                worker_cancel.cancel();
                let _ = join_worker_handle(worker.take().unwrap()).await;
                bail!("native helper startup failed");
            }
        };
        let pid = credentials.helper_pid();
        let session_id = credentials.session_id().to_owned();
        if confirm_tx.send(true).is_err() {
            worker_cancel.cancel();
            let _ = join_worker_handle(worker.take().unwrap()).await;
            bail!("native helper startup failed");
        }
        let ack = loop {
            if cancel.is_cancelled() || Instant::now() >= deadline {
                break false;
            }
            match ack_rx.try_recv() {
                Ok(value) => break value,
                Err(mpsc::TryRecvError::Disconnected) => break false,
                Err(mpsc::TryRecvError::Empty) => tokio::time::sleep(POLL).await,
            }
        };
        if !ack {
            worker_cancel.cancel();
            let _ = join_worker_handle(worker.take().unwrap()).await;
            bail!("native helper startup failed");
        }
        drop_guard.disarm();
        Ok(Self {
            pid,
            session_id,
            guest,
            install,
            cancel: worker_cancel,
            exit: exit_rx,
            worker: tokio::sync::Mutex::new(worker),
            joined,
            join_notify,
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
        let handle = self.worker.lock().await.take();
        if let Some(handle) = handle {
            let result = complete_worker(handle, self.exit.clone()).await;
            let _ = self.join_notify.send(true);
            result
        } else {
            let mut joined = self.joined.clone();
            while !*joined.borrow() {
                if joined.changed().await.is_err() {
                    bail!("native helper status unavailable");
                }
            }
            if let Some(value) = self.exit.borrow().clone() {
                result_for_exit(value)
            } else {
                bail!("native helper did not stop cleanly")
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

async fn join_worker_handle(handle: tokio::task::JoinHandle<SessionExit>) -> Result<SessionExit> {
    handle
        .await
        .map_err(|_| anyhow::anyhow!("native helper startup failed"))
}
async fn complete_worker(
    handle: tokio::task::JoinHandle<SessionExit>,
    exit: watch::Receiver<Option<SessionExit>>,
) -> Result<SessionExit> {
    match handle.await {
        Ok(value) => result_for_exit(value),
        Err(_) => {
            if let Some(value) = exit.borrow().clone() {
                result_for_exit(value)
            } else {
                bail!("native helper did not stop cleanly")
            }
        }
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

struct ProcessOwner {
    child: Child,
    stdout: Option<std::process::ChildStdout>,
    stderr: Option<std::process::ChildStderr>,
    reaped: bool,
}
impl Drop for ProcessOwner {
    fn drop(&mut self) {
        if self.reaped {
            return;
        }
        let _ = self.child.kill();
        let until = Instant::now() + Duration::from_secs(5);
        while Instant::now() < until {
            if self.child.try_wait().ok().flatten().is_some() {
                self.reaped = true;
                break;
            }
            std::thread::sleep(POLL);
        }
    }
}
impl ProcessOwner {
    fn reap(&mut self) -> Result<Option<ExitStatus>> {
        let status = self.child.try_wait()?;
        if status.is_some() {
            self.reaped = true;
            self.stdout.take();
            self.stderr.take();
        }
        Ok(status)
    }
    fn drain_cleanup(&mut self) {
        let mut buf = [0u8; 4096];
        if let Some(pipe) = self.stdout.as_ref() {
            let _ = unsafe { libc::read(pipe.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) };
        }
        if let Some(pipe) = self.stderr.as_ref() {
            let _ = unsafe { libc::read(pipe.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) };
        }
    }
    fn cleanup(&mut self, reason: SessionReason) -> SessionExit {
        if !self.reaped {
            let _ = unsafe { libc::kill(self.child.id() as i32, libc::SIGTERM) };
        }
        let grace = Instant::now() + cleanup_grace();
        loop {
            self.drain_cleanup();
            match self.reap() {
                Ok(Some(status)) => return exit_status(status, reason, false, false),
                Err(_) => return failed_exit(reason, true),
                _ => {}
            }
            if Instant::now() >= grace {
                break;
            }
            std::thread::sleep(POLL.min(Duration::from_millis(10)));
        }
        if !self.reaped {
            let _ = unsafe { libc::kill(self.child.id() as i32, libc::SIGKILL) };
        }
        let reap_by = Instant::now() + Duration::from_secs(5);
        while Instant::now() < reap_by {
            self.drain_cleanup();
            match self.reap() {
                Ok(Some(status)) => return exit_status(status, reason, true, false),
                Err(_) => return failed_exit(reason, true),
                _ => std::thread::sleep(POLL.min(Duration::from_millis(10))),
            }
        }
        failed_exit(reason, true)
    }
}
#[cfg(test)]
fn cleanup_grace() -> Duration {
    Duration::from_millis(150)
}
#[cfg(not(test))]
fn cleanup_grace() -> Duration {
    Duration::from_secs(220)
}
fn failed_exit(reason: SessionReason, forced: bool) -> SessionExit {
    SessionExit {
        code: None,
        signal: None,
        reason,
        forced,
        credential_absent: false,
    }
}

fn run_owner(
    launch: NativeSessionLaunch,
    deadline: Instant,
    cancel: CancellationToken,
    channels: OwnerChannels,
) -> SessionExit {
    let OwnerChannels {
        candidate,
        confirm,
        ack,
        exit: exit_tx,
        fixture,
    } = channels;
    if !fixture && validate_launch(&launch, deadline, &cancel).is_err() {
        let exit = failed_exit(SessionReason::StartupFailure, false);
        let _ = exit_tx.send(Some(exit.clone()));
        return exit;
    }
    let child = match spawn(&launch) {
        Ok(c) => c,
        Err(_) => {
            let exit = failed_exit(SessionReason::StartupFailure, false);
            let _ = exit_tx.send(Some(exit.clone()));
            return exit;
        }
    };
    // Ownership is established before pipe extraction or any other fallible operation.
    let mut owner = ProcessOwner {
        child,
        stdout: None,
        stderr: None,
        reaped: false,
    };
    let setup = (|| -> Result<()> {
        owner.stdout = owner.child.stdout.take();
        owner.stderr = owner.child.stderr.take();
        nonblocking(
            owner
                .stdout
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("pipe"))?
                .as_raw_fd(),
        )?;
        nonblocking(
            owner
                .stderr
                .as_ref()
                .ok_or_else(|| anyhow::anyhow!("pipe"))?
                .as_raw_fd(),
        )?;
        Ok(())
    })();
    if setup.is_err() {
        let exit = owner.cleanup(SessionReason::StartupFailure);
        let _ = exit_tx.send(Some(exit.clone()));
        return exit;
    }
    let pid = owner.child.id();
    let mut line = Vec::new();
    let (mut out_count, mut err_count) = (0usize, 0usize);
    let mut receipt: Option<Receipt> = None;
    let mut sent_candidate = false;
    let mut confirmed = false;
    let mut out_eof = false;
    loop {
        if cancel.is_cancelled() || (!confirmed && Instant::now() >= deadline) {
            let reason = if confirmed && cancel.is_cancelled() {
                SessionReason::Requested
            } else {
                SessionReason::StartupFailure
            };
            let exit = owner.cleanup(reason);
            let final_exit = if reason == SessionReason::Requested {
                SessionExit {
                    credential_absent: credential_absent(&launch.state_dir),
                    ..exit
                }
            } else {
                exit
            };
            let _ = exit_tx.send(Some(final_exit.clone()));
            return final_exit;
        }
        // Two fair bounded pumps; neither child can monopolize ownership of the worker.
        let mut pump_failed = false;
        for is_stdout in [true, false] {
            let stream: Option<&mut dyn std::os::fd::AsRawFd> = if is_stdout {
                owner
                    .stdout
                    .as_mut()
                    .map(|s| s as &mut dyn std::os::fd::AsRawFd)
            } else {
                owner
                    .stderr
                    .as_mut()
                    .map(|s| s as &mut dyn std::os::fd::AsRawFd)
            };
            let Some(stream) = stream else {
                continue;
            };
            let fd = stream.as_raw_fd();
            let mut buf = [0u8; 4096];
            let n = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
            if n == 0 {
                if is_stdout {
                    out_eof = true;
                }
                continue;
            }
            if n < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() != std::io::ErrorKind::WouldBlock
                    && error.kind() != std::io::ErrorKind::Interrupted
                {
                    pump_failed = true;
                    break;
                }
                continue;
            }
            let bytes = &buf[..n as usize];
            if is_stdout {
                out_count += bytes.len();
                if out_count > MAX_PIPE_BYTES {
                    pump_failed = true;
                    break;
                }
                for byte in bytes {
                    if *byte == b'\n' {
                        if line.len() + 1 > MAX_LINE_BYTES || receipt.is_some() {
                            pump_failed = true;
                            break;
                        }
                        match parse_receipt(&line, &launch) {
                            Ok(value) => receipt = Some(value),
                            Err(_) => {
                                pump_failed = true;
                                break;
                            }
                        }
                        line.clear();
                    } else {
                        line.push(*byte);
                        if line.len() >= MAX_LINE_BYTES {
                            pump_failed = true;
                            break;
                        }
                    }
                }
                if pump_failed {
                    break;
                }
            } else {
                err_count += bytes.len();
                if err_count > MAX_PIPE_BYTES {
                    pump_failed = true;
                    break;
                }
            }
        }
        if pump_failed {
            let exit = owner.cleanup(SessionReason::PipeFailure);
            let _ = exit_tx.send(Some(exit.clone()));
            return exit;
        }
        if out_eof && (!line.is_empty() || receipt.is_none() || !sent_candidate) {
            let exit = owner.cleanup(SessionReason::StartupFailure);
            let _ = exit_tx.send(Some(exit.clone()));
            return exit;
        }
        if let Some(r) = receipt.as_ref().filter(|_| !sent_candidate) {
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
                Ok(credentials) => {
                    if candidate.send(StartupCandidate { credentials }).is_err() {
                        let exit = owner.cleanup(SessionReason::StartupFailure);
                        let _ = exit_tx.send(Some(exit.clone()));
                        return exit;
                    }
                    sent_candidate = true;
                }
                Err(_) => {
                    let exit = owner.cleanup(SessionReason::StartupFailure);
                    let _ = exit_tx.send(Some(exit.clone()));
                    return exit;
                }
            }
        }
        if sent_candidate && !confirmed {
            match confirm.try_recv() {
                Ok(true) => {
                    let live = owner.reap().ok().flatten().is_none();
                    let active = live && !cancel.is_cancelled() && Instant::now() < deadline;
                    if ack.send(active).is_err() || !active {
                        let exit = owner.cleanup(SessionReason::StartupFailure);
                        let _ = exit_tx.send(Some(exit.clone()));
                        return exit;
                    }
                    confirmed = true;
                }
                Ok(false) | Err(mpsc::TryRecvError::Disconnected) => {
                    let exit = owner.cleanup(SessionReason::StartupFailure);
                    let _ = exit_tx.send(Some(exit.clone()));
                    return exit;
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }
        match owner.reap() {
            Ok(Some(status)) => {
                let reason = if confirmed {
                    SessionReason::UnexpectedExit
                } else {
                    SessionReason::StartupFailure
                };
                let exit = exit_status(status, reason, false, credential_absent(&launch.state_dir));
                let _ = exit_tx.send(Some(exit.clone()));
                return exit;
            }
            Err(_) => {
                let exit = owner.cleanup(SessionReason::PipeFailure);
                let _ = exit_tx.send(Some(exit.clone()));
                return exit;
            }
            _ => {}
        }
        std::thread::sleep(POLL);
    }
}

fn parse_receipt(line: &[u8], launch: &NativeSessionLaunch) -> Result<Receipt> {
    let r: Receipt = serde_json::from_slice(line)?;
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
        || !r.kernel_sha256.as_deref().is_some_and(|v| valid_hex(v, 64))
        || !r
            .rootfs_template_sha256
            .as_deref()
            .is_some_and(|v| valid_hex(v, 64))
        || r.manifest_version.as_deref() != Some("0.3.0")
        || r.manifest_source_sha.as_deref() != Some(&launch.source_sha)
        || r.agent_apk_sha256.as_deref() != Some(&launch.agent_apk_sha256)
        || r.adb_listen != format!("127.0.0.1:{}", launch.adb_port)
        || r.agent_listen.as_deref() != Some(&format!("127.0.0.1:{}", launch.agent_port))
        || r.install_listen.as_deref() != Some(&format!("127.0.0.1:{}", launch.install_port))
        || !r
            .session_id
            .as_deref()
            .is_some_and(crate::native_install::canonical_uuid)
    {
        bail!("invalid readiness receipt");
    }
    Ok(r)
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
    matches!(fs::symlink_metadata(state.join(".ashnazg-session-v1.json")), Err(ref e) if e.kind() == std::io::ErrorKind::NotFound)
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

#[cfg(test)]
mod tests {
    use super::*;

    const READY: &str = r#"{"schema_version":1,"status":"agent_ready","architecture":"aarch64","cpu_count":4,"memory_bytes":8589934592,"adb_listen":"127.0.0.1:15555","android_serial":"emulator-5554","manifest_version":"0.3.0","manifest_source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","kernel_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","rootfs_template_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","timings":{"total_startup_ms":1000,"vm_start_ms":500,"readiness_ms":500,"agent_startup_ms":250},"session_id":"6f6f1234-1234-4234-8234-123456789abc","agent_listen":"127.0.0.1:17777","install_listen":"127.0.0.1:17778","agent_apk_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#;

    const SESSION: &str = "6f6f1234-1234-4234-8234-123456789abc";
    const RPC_TOKEN: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    const INSTALL_TOKEN: &str = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

    fn free_port() -> u16 {
        std::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .unwrap()
            .local_addr()
            .unwrap()
            .port()
    }

    async fn rpc_fixture() -> (u16, tokio::task::JoinHandle<()>) {
        rpc_fixture_level(Some(34)).await
    }

    async fn rpc_fixture_level(api_level: Option<u64>) -> (u16, tokio::task::JoinHandle<()>) {
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            if let Ok((socket, _)) = listener.accept().await {
                let (read, mut write) = socket.into_split();
                let mut line = String::new();
                let mut reader = BufReader::new(read);
                if reader.read_line(&mut line).await.is_ok() {
                    let request: serde_json::Value = serde_json::from_str(&line).unwrap();
                    if let Some(api_level) = api_level {
                        let response = serde_json::json!({"jsonrpc":"2.0", "id":request["id"], "result":{"apiLevel":api_level}});
                        let _ = write.write_all(format!("{}\n", response).as_bytes()).await;
                    } else {
                        tokio::time::sleep(Duration::from_secs(3)).await;
                    }
                }
            }
        });
        (port, server)
    }

    fn fixture_launch(
        dir: &Path,
        agent_port: u16,
        install_port: u16,
        mode: &str,
        credential_pid: &str,
        receipt_status: &str,
    ) -> NativeSessionLaunch {
        let helper = dir.join("fake-helper.sh");
        let state = dir.join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let receipt = READY
            .replace("15555", "15556")
            .replace("17777", &agent_port.to_string())
            .replace("17778", &install_port.to_string())
            .replace("agent_ready", receipt_status);
        let credentials = format!(
            r#"{{"schema_version":1,"session_id":"{SESSION}","helper_pid":{credential_pid},"manifest_source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","agent_apk_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","rpc_token":"{RPC_TOKEN}","install_token":"{INSTALL_TOKEN}","agent_listen":"127.0.0.1:{agent_port}","install_listen":"127.0.0.1:{install_port}"}}"#
        );
        let ready_output = if receipt_status == "NO_OUTPUT" {
            String::new()
        } else {
            format!("printf '%s\\n' '{}'", receipt)
        };
        let script = format!(
            "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" > '{}'\nenv | grep -E '^ASHNAZG_(TOKEN|AGENT_ADDR)=' > '{}/env.log' || true\nprintf '%s\\n' \"$$\" > '{}/pid'\ncat > '{}/.ashnazg-session-v1.json' <<CREDENTIAL\n{}\nCREDENTIAL\nchmod 600 '{}/.ashnazg-session-v1.json'\n{}\n{}\n",
            dir.join("argv.log").display(),
            dir.display(),
            state.display(),
            state.display(),
            credentials,
            state.display(),
            ready_output,
            mode
        );
        fs::write(&helper, script).unwrap();
        fs::set_permissions(&helper, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        NativeSessionLaunch {
            helper,
            helper_sha256: String::new(),
            assets: dir.join("assets"),
            state_dir: state,
            agent_apk: dir.join("agent.apk"),
            agent_apk_sha256: "d".repeat(64),
            source_sha: "a".repeat(40),
            adb_port: 15556,
            agent_port,
            install_port,
            startup_timeout_secs: 15,
        }
    }

    #[tokio::test]
    async fn fixture_session_authenticates_and_stops_gracefully_past_startup_deadline() {
        let canonical_temp = fs::canonicalize(std::env::temp_dir()).unwrap();
        let dir = tempfile::Builder::new().tempdir_in(canonical_temp).unwrap();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        fs::write(dir.path().join("assets.marker"), b"caller asset").unwrap();
        fs::write(dir.path().join("agent.apk"), b"caller apk").unwrap();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap 'rm -f \"__STATE__/.ashnazg-session-v1.json\"; exit 0' TERM\nexport ASHNAZG_STATE='__STATE__'\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        // Keep the fixture script independent of inherited credential environment.
        let script = fs::read_to_string(&launch.helper)
            .unwrap()
            .replace("__STATE__", &launch.state_dir.display().to_string());
        fs::write(&launch.helper, script).unwrap();
        let parent_cancel = CancellationToken::new();
        let session_result = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_millis(900),
            parent_cancel.clone(),
        )
        .await;
        let session = session_result.unwrap();
        server.await.unwrap();
        tokio::time::sleep(Duration::from_millis(1100)).await;
        assert!(
            session.guest_client().is_ok(),
            "startup deadline must not expire an active session"
        );
        let exit = session.shutdown().await.unwrap();
        assert_eq!(exit.code, Some(0));
        assert!(!exit.forced);
        assert!(exit.credential_absent);
        assert!(!parent_cancel.is_cancelled());
        let args = fs::read_to_string(dir.path().join("argv.log")).unwrap();
        assert!(args.contains("--agent-sha256"));
        assert!(!args.contains(RPC_TOKEN));
        assert!(!args.contains(INSTALL_TOKEN));
        assert!(
            fs::read_to_string(dir.path().join("env.log"))
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            fs::read(dir.path().join("assets.marker")).unwrap(),
            b"caller asset"
        );
        assert_eq!(
            fs::read(dir.path().join("agent.apk")).unwrap(),
            b"caller apk"
        );
    }

    fn process_is_alive(pid: u32) -> bool {
        unsafe { libc::kill(pid as i32, 0) == 0 }
    }

    fn canonical_test_dir() -> tempfile::TempDir {
        tempfile::Builder::new()
            .tempdir_in(fs::canonicalize(std::env::temp_dir()).unwrap())
            .unwrap()
    }

    #[tokio::test]
    async fn dropping_session_requests_cleanup_without_cancelling_parent() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap 'exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let script = fs::read_to_string(&launch.helper)
            .unwrap()
            .replace("__STATE__", &launch.state_dir.display().to_string());
        fs::write(&launch.helper, script).unwrap();
        let state = launch.state_dir.clone();
        let parent = CancellationToken::new();
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            parent.clone(),
        )
        .await
        .unwrap();
        server.await.unwrap();
        let pid = fs::read_to_string(state.join("pid"))
            .unwrap()
            .trim()
            .parse::<u32>()
            .unwrap();
        drop(session);
        tokio::time::timeout(Duration::from_secs(3), async {
            while process_is_alive(pid) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert!(!parent.is_cancelled());
    }

    #[tokio::test]
    async fn dropping_startup_future_cancels_and_reaps_the_helper() {
        let dir = canonical_test_dir();
        let agent_port = free_port();
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "while :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let state = launch.state_dir.clone();
        let task = tokio::spawn(async move {
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(5),
                CancellationToken::new(),
            )
            .await
        });
        let pid_path = state.join("pid");
        tokio::time::timeout(Duration::from_secs(2), async {
            while !pid_path.exists() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        task.abort();
        let _ = task.await;
        let pid = fs::read_to_string(state.join("pid"))
            .unwrap()
            .trim()
            .parse::<u32>()
            .unwrap();
        tokio::time::timeout(Duration::from_secs(3), async {
            while process_is_alive(pid) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn helper_exit_after_receipt_never_returns_a_session() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "exit 17",
            "$$",
            "agent_ready",
        );
        assert!(
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn factories_refuse_clients_after_owned_helper_exits() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "sleep 0.20; exit 17",
            "$$",
            "agent_ready",
        );
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        server.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_secs(2), session.wait_terminated())
                .await
                .unwrap()
                .is_err()
        );
        assert!(session.guest_client().is_err());
        assert!(session.install_client().is_err());
    }

    #[tokio::test]
    async fn helper_exit_before_readiness_rejects_empty_stdout() {
        let dir = canonical_test_dir();
        let agent_port = free_port();
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "exit 0",
            "$$",
            "not_ready",
        );
        assert!(
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
    }

    #[tokio::test]
    async fn ping_requires_integer_api_level_at_least_34() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture_level(Some(33)).await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "while :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        assert!(
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn stalled_ping_is_bounded_by_remaining_startup_budget() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture_level(None).await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap 'exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let began = Instant::now();
        assert!(
            NativeSession::start_fixture(
                launch,
                began + Duration::from_millis(500),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
        assert!(began.elapsed() < Duration::from_secs(2));
        server.abort();
    }

    #[tokio::test]
    async fn leftover_credential_makes_requested_shutdown_fail_and_preserves_file() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap 'exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let credential = launch.state_dir.join(".ashnazg-session-v1.json");
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        server.await.unwrap();
        assert!(session.shutdown().await.is_err());
        assert!(
            credential.exists(),
            "supervisor must leave helper credentials untouched"
        );
    }

    #[tokio::test]
    async fn ignored_term_is_forced_and_reported_as_failure() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap '' TERM\nwhile :; do :; done",
            "$$",
            "agent_ready",
        );
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        server.await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_secs(3), session.shutdown())
                .await
                .unwrap()
                .is_err()
        );
    }

    #[tokio::test]
    async fn malformed_duplicate_oversized_and_flooded_pipes_fail_without_detached_readers() {
        for kind in ["malformed", "duplicate", "oversized", "stderr_flood"] {
            let dir = canonical_test_dir();
            let (agent_port, server) = rpc_fixture_level(None).await;
            let install_port = free_port();
            let receipt = READY
                .replace("15555", "15556")
                .replace("17777", &agent_port.to_string())
                .replace("17778", &install_port.to_string());
            let mode = match kind {
                "malformed" => "printf '%s\\n' 'not a receipt'; while :; do sleep 0.05; done".to_owned(),
                "duplicate" => format!("printf '%s\\n' '{}'; while :; do sleep 0.05; done", receipt),
                "oversized" => "i=0; while [ $i -lt 9000 ]; do printf x; i=$((i+1)); done; printf '\\n'; while :; do sleep 0.05; done".to_owned(),
                _ => "i=0; while [ $i -lt 70000 ]; do printf x >&2; i=$((i+1)); done; while :; do sleep 0.05; done".to_owned(),
            };
            let launch = fixture_launch(
                dir.path(),
                agent_port,
                install_port,
                &mode,
                "$$",
                "agent_ready",
            );
            let began = Instant::now();
            assert!(
                NativeSession::start_fixture(
                    launch,
                    began + Duration::from_millis(900),
                    CancellationToken::new()
                )
                .await
                .is_err(),
                "{kind} output must not activate a session"
            );
            assert!(began.elapsed() < Duration::from_secs(3));
            server.abort();
        }
    }

    #[tokio::test]
    async fn invalid_receipt_startup_error_returns_after_helper_is_reaped() {
        let dir = canonical_test_dir();
        let agent_port = free_port();
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "while :; do sleep 0.05; done",
            "$$",
            "not_ready",
        );
        let state = launch.state_dir.clone();
        assert!(
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
        let pid = fs::read_to_string(state.join("pid"))
            .unwrap()
            .trim()
            .parse::<u32>()
            .unwrap();
        assert!(
            !process_is_alive(pid),
            "startup error must join the owner after reap"
        );
    }

    #[tokio::test]
    async fn bad_credential_pid_cannot_signal_an_independently_owned_process() {
        let dir = canonical_test_dir();
        let mut sentinel = Command::new("/bin/sleep").arg("15").spawn().unwrap();
        let agent_port = free_port();
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "while :; do sleep 0.05; done",
            &sentinel.id().to_string(),
            "agent_ready",
        );
        assert!(
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new()
            )
            .await
            .is_err()
        );
        assert!(
            sentinel.try_wait().unwrap().is_none(),
            "untrusted credential PID must never be signaled"
        );
        sentinel.kill().unwrap();
        sentinel.wait().unwrap();
    }

    #[tokio::test]
    async fn startup_cancellation_joins_helper_and_does_not_cancel_parent_on_normal_stop() {
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "trap 'exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let state = launch.state_dir.clone();
        let parent = CancellationToken::new();
        let startup_parent = parent.clone();
        let task = tokio::spawn(async move {
            NativeSession::start_fixture(
                launch,
                Instant::now() + Duration::from_secs(4),
                startup_parent,
            )
            .await
        });
        let pid_path = state.join("pid");
        tokio::time::timeout(Duration::from_secs(2), async {
            while !pid_path.exists() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        parent.cancel();
        assert!(
            tokio::time::timeout(Duration::from_secs(3), task)
                .await
                .unwrap()
                .unwrap()
                .is_err()
        );
        let pid = fs::read_to_string(state.join("pid"))
            .unwrap()
            .trim()
            .parse::<u32>()
            .unwrap();
        assert!(!process_is_alive(pid));
        server.abort();
    }

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
    fn credential_absent_requires_verified_not_found() {
        let dir = tempfile::tempdir().unwrap();
        assert!(credential_absent(dir.path()));
        let credential = dir.path().join(".ashnazg-session-v1.json");
        fs::write(&credential, b"occupied").unwrap();
        assert!(!credential_absent(dir.path()));
    }

    #[test]
    fn lowercase_hex_validation_is_exact_and_bounded() {
        assert!(valid_hex(&"a1".repeat(32), 64));
        assert!(!valid_hex(&"A1".repeat(32), 64));
        assert!(!valid_hex(&"a1".repeat(31), 64));
        assert!(!valid_hex(&format!("{}g", "a".repeat(63)), 64));
    }
}
