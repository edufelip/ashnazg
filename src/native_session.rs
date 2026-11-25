//! Owns one native helper process from launch through cleanup.
//!
//! Helper output is treated as an untrusted protocol. It is bounded and parsed
//! into typed values; it is never included in public errors or status.

use anyhow::{Result, bail};
use serde::Deserialize;
use std::{
    fs,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    os::unix::io::AsRawFd,
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

mod launch;

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
    adb_addr: SocketAddr,
    guest: crate::GuestClient,
    install: Arc<crate::native_install::NativeInstallClient>,
    cancel: CancellationToken,
    exit: watch::Receiver<Option<SessionExit>>,
    worker: tokio::sync::Mutex<Option<tokio::task::JoinHandle<SessionExit>>>,
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
        if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            bail!("native sessions require macOS/aarch64");
        }
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
        let adb_addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), launch.adb_port);
        let worker_cancel = cancel.child_token();
        let mut drop_guard = StartupDrop(Some(worker_cancel.clone()));
        let (candidate_tx, candidate_rx) = mpsc::sync_channel(1);
        let (confirm_tx, confirm_rx) = mpsc::sync_channel(1);
        let (ack_tx, ack_rx) = mpsc::sync_channel(1);
        let (exit_tx, exit_rx) = watch::channel(None);
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
                bail!("native helper startup cancelled or timed out");
            }
            match candidate_rx.try_recv() {
                Ok(candidate) => break candidate,
                Err(mpsc::TryRecvError::Disconnected) => {
                    worker_cancel.cancel();
                    let _ = join_worker_handle(worker.take().unwrap()).await;
                    bail!("native helper stopped before readiness");
                }
                Err(mpsc::TryRecvError::Empty) => tokio::time::sleep(POLL).await,
            }
        };
        let credentials = candidate.credentials;
        let guest = credentials.guest_client();
        let ping_deadline = deadline.min(Instant::now() + Duration::from_secs(5));
        let mut termination = exit_rx.clone();
        let valid = if termination.borrow().is_some() || termination.has_changed().is_err() {
            false
        } else {
            tokio::select! {
                biased;
                _ = worker_cancel.cancelled() => false,
                _ = termination.changed() => false,
                ping = tokio::time::timeout_at(
                    tokio::time::Instant::from_std(ping_deadline),
                    guest.call("ping", serde_json::json!({})),
                ) => matches!(ping, Ok(Ok(ref v)) if v.get("apiLevel").and_then(serde_json::Value::as_u64).is_some_and(|n| n >= 34)),
            }
        };
        let install = match (valid && !cancel.is_cancelled() && Instant::now() < deadline)
            .then(|| credentials.install_client())
            .transpose()
        {
            Ok(Some(install)) => Arc::new(install),
            _ => {
                worker_cancel.cancel();
                let _ = join_worker_handle(worker.take().unwrap()).await;
                bail!("native helper authentication failed");
            }
        };
        let pid = credentials.helper_pid();
        let session_id = credentials.session_id().to_owned();
        if confirm_tx.send(true).is_err() {
            worker_cancel.cancel();
            let _ = join_worker_handle(worker.take().unwrap()).await;
            bail!("native helper readiness confirmation failed");
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
        if !ack
            || worker_cancel.is_cancelled()
            || Instant::now() >= deadline
            || exit_rx.borrow().is_some()
            || exit_rx.has_changed().is_err()
        {
            worker_cancel.cancel();
            let _ = join_worker_handle(worker.take().unwrap()).await;
            bail!("native helper readiness confirmation failed");
        }
        drop_guard.disarm();
        Ok(Self {
            pid,
            session_id,
            adb_addr,
            guest,
            install,
            cancel: worker_cancel,
            exit: exit_rx,
            worker: tokio::sync::Mutex::new(worker),
        })
    }

    pub fn helper_pid(&self) -> u32 {
        self.pid
    }
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    /// The host ADB relay confirmed by the owned helper's readiness receipt.
    /// This is session metadata; it does not perform a liveness check.
    pub fn adb_addr(&self) -> SocketAddr {
        self.adb_addr
    }
    pub fn guest_client(&self) -> Result<crate::GuestClient> {
        if self.exit.borrow().is_some() || self.exit.has_changed().is_err() {
            bail!("native helper has terminated");
        }
        Ok(self.guest.clone())
    }
    pub fn install_client(&self) -> Result<Arc<crate::native_install::NativeInstallClient>> {
        if self.exit.borrow().is_some() || self.exit.has_changed().is_err() {
            bail!("native helper has terminated");
        }
        Ok(self.install.clone())
    }
    pub async fn wait_terminated(&self) -> Result<SessionExit> {
        // Borrow the handle across await: cancellation drops only the mutex
        // guard, leaving the unique join handle available to the next waiter.
        let mut worker = self.worker.lock().await;
        if let Some(handle) = worker.as_mut() {
            let result = handle.await;
            worker.take();
            return result
                .map_err(|_| anyhow::anyhow!("native helper did not stop cleanly"))
                .and_then(result_for_exit);
        }
        self.exit
            .borrow()
            .clone()
            .ok_or_else(|| anyhow::anyhow!("native helper did not stop cleanly"))
            .and_then(result_for_exit)
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
    launch: &NativeSessionLaunch,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<launch::VerifiedLaunch> {
    launch::validate(launch, deadline, cancel)
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
    out_count: usize,
    err_count: usize,
    output_failed: bool,
    state_dir: PathBuf,
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
        }
        Ok(status)
    }
    fn drain_cleanup(&mut self) {
        let mut buf = [0u8; 4096];
        for stdout in [true, false] {
            let fd = if stdout {
                self.stdout.as_ref().map(AsRawFd::as_raw_fd)
            } else {
                self.stderr.as_ref().map(AsRawFd::as_raw_fd)
            };
            let Some(fd) = fd else {
                continue;
            };
            let n = unsafe { libc::read(fd, buf.as_mut_ptr().cast(), buf.len()) };
            let close = if n > 0 {
                let count = if stdout {
                    &mut self.out_count
                } else {
                    &mut self.err_count
                };
                *count += n as usize;
                // A ready helper has already emitted its sole stdout receipt.
                if stdout || *count > MAX_PIPE_BYTES {
                    self.output_failed = true;
                    true
                } else {
                    false
                }
            } else if n == 0 {
                true
            } else {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::Interrupted
                {
                    false
                } else {
                    self.output_failed = true;
                    true
                }
            };
            if close {
                if stdout {
                    self.stdout.take();
                } else {
                    self.stderr.take();
                }
            }
        }
    }
    fn stopped(&mut self, status: ExitStatus, reason: SessionReason, forced: bool) -> SessionExit {
        // A reaped child may leave buffered output. Inspect at most a lifetime
        // budget per stream, then close even if another process holds a writer.
        for _ in 0..(MAX_PIPE_BYTES / 4096 + 2) {
            self.drain_cleanup();
        }
        self.stdout.take();
        self.stderr.take();
        exit_status(
            status,
            if self.output_failed {
                SessionReason::PipeFailure
            } else {
                reason
            },
            forced,
            credential_absent(&self.state_dir),
        )
    }
    fn cleanup(&mut self, reason: SessionReason) -> SessionExit {
        // Reap a child that already exited before attempting any signal.
        match self.reap() {
            Ok(Some(status)) => return self.stopped(status, reason, false),
            Err(_) => return failed_exit(reason, true),
            Ok(None) => {}
        }
        if !self.reaped {
            let _ = unsafe { libc::kill(self.child.id() as i32, libc::SIGTERM) };
        }
        let grace = Instant::now() + cleanup_grace();
        loop {
            self.drain_cleanup();
            match self.reap() {
                Ok(Some(status)) => return self.stopped(status, reason, false),
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
                Ok(Some(status)) => return self.stopped(status, reason, true),
                Err(_) => return failed_exit(reason, true),
                _ => std::thread::sleep(POLL.min(Duration::from_millis(10))),
            }
        }
        failed_exit(reason, true)
    }
}
#[cfg(test)]
fn cleanup_grace() -> Duration {
    Duration::from_millis(500)
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
    let verified = if fixture {
        None
    } else {
        match validate_launch(&launch, deadline, &cancel).and_then(|verified| {
            verified.check_before_spawn(deadline, &cancel)?;
            Ok(verified)
        }) {
            Ok(verified) => Some(verified),
            Err(_) => {
                let exit = failed_exit(SessionReason::StartupFailure, false);
                let _ = exit_tx.send(Some(exit.clone()));
                return exit;
            }
        }
    };
    let state_dir = launch.state_dir.clone();
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
        out_count: 0,
        err_count: 0,
        output_failed: false,
        state_dir,
    };
    // Keep verified artifact and path descriptors alive across process creation.
    drop(verified);
    let setup = (|| -> Result<()> {
        let out = owner
            .child
            .stdout
            .take()
            .ok_or_else(|| anyhow::anyhow!("pipe"))?;
        nonblocking(out.as_raw_fd())?;
        owner.stdout = Some(out);
        let err = owner
            .child
            .stderr
            .take()
            .ok_or_else(|| anyhow::anyhow!("pipe"))?;
        nonblocking(err.as_raw_fd())?;
        owner.stderr = Some(err);
        Ok(())
    })();
    if setup.is_err() {
        owner.child.stdout.take();
        owner.child.stderr.take();
        let exit = owner.cleanup(SessionReason::StartupFailure);
        let _ = exit_tx.send(Some(exit.clone()));
        return exit;
    }
    let pid = owner.child.id();
    let mut line = Vec::new();
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
                owner.out_count += bytes.len();
                if owner.out_count > MAX_PIPE_BYTES {
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
                owner.err_count += bytes.len();
                if owner.err_count > MAX_PIPE_BYTES {
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
            if !matches!(owner.reap(), Ok(None)) {
                let exit = owner.cleanup(SessionReason::StartupFailure);
                let _ = exit_tx.send(Some(exit.clone()));
                return exit;
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
                Ok(credentials) => {
                    if !matches!(owner.reap(), Ok(None)) {
                        let exit = owner.cleanup(SessionReason::StartupFailure);
                        let _ = exit_tx.send(Some(exit.clone()));
                        return exit;
                    }
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
                    let live = matches!(owner.reap(), Ok(None));
                    let active = live && !cancel.is_cancelled() && Instant::now() < deadline;
                    confirmed = active;
                    if ack.send(active).is_err() || !active {
                        let exit = owner.cleanup(SessionReason::StartupFailure);
                        let _ = exit_tx.send(Some(exit.clone()));
                        return exit;
                    }
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
                let exit = owner.stopped(status, reason, false);
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
        || r.manifest_version.as_deref() != Some("0.3.2")
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
    c.arg("--assets")
        .arg(&l.assets)
        .arg("--state-dir")
        .arg(&l.state_dir)
        .arg("--adb-port")
        .arg(l.adb_port.to_string())
        .arg("--agent-port")
        .arg(l.agent_port.to_string())
        .arg("--install-port")
        .arg(l.install_port.to_string())
        .arg("--timeout")
        .arg(l.startup_timeout_secs.to_string())
        .arg("--agent-apk")
        .arg(&l.agent_apk)
        .arg("--agent-sha256")
        .arg(&l.agent_apk_sha256);
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
    use sha2::{Digest, Sha256};

    const READY: &str = r#"{"schema_version":1,"status":"agent_ready","architecture":"aarch64","cpu_count":4,"memory_bytes":8589934592,"adb_listen":"127.0.0.1:15555","android_serial":"emulator-5554","manifest_version":"0.3.2","manifest_source_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","kernel_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","rootfs_template_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","timings":{"total_startup_ms":1000,"vm_start_ms":500,"readiness_ms":500,"agent_startup_ms":250},"session_id":"6f6f1234-1234-4234-8234-123456789abc","agent_listen":"127.0.0.1:17777","install_listen":"127.0.0.1:17778","agent_apk_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"}"#;

    // Short-deadline OS process fixtures run in isolation from this suite's
    // other helper launches; the production owner has no shared fixture gate.
    static PROCESS_FIXTURE: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

    async fn process_fixture() -> tokio::sync::MutexGuard<'static, ()> {
        tokio::time::timeout(Duration::from_secs(30), PROCESS_FIXTURE.lock())
            .await
            .expect("process fixture gate timed out")
    }

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
        rpc_fixture_replies(api_level, 1).await
    }

    async fn rpc_fixture_replies(
        api_level: Option<u64>,
        count: usize,
    ) -> (u16, tokio::task::JoinHandle<()>) {
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
        let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = tokio::spawn(async move {
            let _ = tokio::time::timeout(Duration::from_secs(3), async {
                if let Ok((socket, _)) = listener.accept().await {
                    let (read, mut write) = socket.into_split();
                    let mut line = String::new();
                    let mut reader = BufReader::new(read);
                    for _ in 0..count {
                        line.clear();
                        reader.read_line(&mut line).await.unwrap();
                        let request: serde_json::Value = serde_json::from_str(&line).unwrap();
                        assert_eq!(request["token"], RPC_TOKEN);
                        assert_eq!(request["method"], "ping");
                        if let Some(api_level) = api_level {
                            let response = serde_json::json!({"jsonrpc":"2.0", "id":request["id"], "result":{"apiLevel":api_level}});
                            let _ = write.write_all(format!("{}\n", response).as_bytes()).await;
                        } else {
                            tokio::time::sleep(Duration::from_secs(3)).await;
                        }
                    }
                }
            }).await;
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
        let (setup, run) = if mode.starts_with("trap ") {
            mode.split_once('\n').unwrap_or((mode, ""))
        } else {
            ("", mode)
        };
        let script = format!(
            "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$@\" > '{}'\nenv | grep -E '^ASHNAZG_(TOKEN|AGENT_ADDR)=' > '{}/env.log' || true\nprintf '%s\\n' \"$$\" > '{}/pid'\ncat > '{}/.ashnazg-session-v1.json' <<CREDENTIAL\n{}\nCREDENTIAL\nchmod 600 '{}/.ashnazg-session-v1.json'\n{}\n{}\n{}\n",
            dir.join("argv.log").display(),
            dir.display(),
            state.display(),
            state.display(),
            credentials,
            state.display(),
            setup,
            ready_output,
            run
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
        let _fixture = process_fixture().await;
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
        let dir = tempfile::Builder::new()
            .tempdir_in(fs::canonicalize(std::env::temp_dir()).unwrap())
            .unwrap();
        fs::set_permissions(
            dir.path(),
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .unwrap();
        dir
    }

    #[tokio::test]
    async fn validated_launch_creates_fresh_state_and_reuses_authenticated_caller_stream() {
        let _fixture = process_fixture().await;
        use std::os::unix::fs::PermissionsExt;
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture_replies(Some(35), 2).await;
        let mut launch = fixture_launch(
            dir.path(),
            agent_port,
            free_port(),
            "trap 'rm -f \"__STATE__/.ashnazg-session-v1.json\"; exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let script = fs::read_to_string(&launch.helper)
            .unwrap()
            .replace(
                "set -eu\n",
                &format!("set -eu\nmkdir -m 700 '{}'\n", launch.state_dir.display()),
            )
            .replace("__STATE__", &launch.state_dir.display().to_string());
        fs::write(&launch.helper, &script).unwrap();
        launch.helper_sha256 = Sha256::digest(script.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        fs::create_dir(&launch.assets).unwrap();
        fs::set_permissions(&launch.assets, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(
            &launch.agent_apk,
            b"fixture bytes; Swift APK hashing is outside this fixture",
        )
        .unwrap();
        fs::set_permissions(&launch.agent_apk, fs::Permissions::from_mode(0o600)).unwrap();
        fs::remove_dir(&launch.state_dir).unwrap();
        validate_launch(
            &launch,
            Instant::now() + Duration::from_secs(2),
            &CancellationToken::new(),
        )
        .expect("fixture must satisfy production launch validation");
        let expected_args = vec![
            "--assets".to_owned(),
            launch.assets.display().to_string(),
            "--state-dir".to_owned(),
            launch.state_dir.display().to_string(),
            "--adb-port".to_owned(),
            launch.adb_port.to_string(),
            "--agent-port".to_owned(),
            launch.agent_port.to_string(),
            "--install-port".to_owned(),
            launch.install_port.to_string(),
            "--timeout".to_owned(),
            launch.startup_timeout_secs.to_string(),
            "--agent-apk".to_owned(),
            launch.agent_apk.display().to_string(),
            "--agent-sha256".to_owned(),
            launch.agent_apk_sha256.clone(),
        ];
        let expected_adb: SocketAddr = format!("127.0.0.1:{}", launch.adb_port).parse().unwrap();
        // Bypass only the public platform gate; run real production validation,
        // spawn, receipt/credential binding and the caller-runtime handshake.
        let session = NativeSession::start_impl(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
            false,
        )
        .await
        .unwrap();
        assert_eq!(session.adb_addr(), expected_adb);
        let second = session
            .guest_client()
            .unwrap()
            .call("ping", serde_json::json!({}))
            .await
            .unwrap();
        assert_eq!(second["apiLevel"], 35);
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("argv.log"))
                .unwrap()
                .lines()
                .collect::<Vec<_>>(),
            expected_args
        );
        tokio::time::timeout(Duration::from_secs(3), session.shutdown())
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn cancelled_termination_wait_does_not_lose_worker_join_ownership() {
        let _fixture = process_fixture().await;
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            free_port(),
            "trap 'rm -f \"__STATE__/.ashnazg-session-v1.json\"; exit 0' TERM\nwhile :; do sleep 0.05; done",
            "$$",
            "agent_ready",
        );
        let script = fs::read_to_string(&launch.helper)
            .unwrap()
            .replace("__STATE__", &launch.state_dir.display().to_string());
        fs::write(&launch.helper, script).unwrap();
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(30), session.wait_terminated())
                .await
                .is_err()
        );
        tokio::time::timeout(Duration::from_secs(3), session.shutdown())
            .await
            .expect("cancelled wait must leave the worker joinable")
            .unwrap();
    }

    #[tokio::test]
    async fn dropping_session_requests_cleanup_without_cancelling_parent() {
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let install_port = free_port();
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            install_port,
            "sleep 0.75; exit 17",
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
            tokio::time::timeout(Duration::from_secs(3), session.wait_terminated())
                .await
                .unwrap()
                .is_err()
        );
        assert!(session.guest_client().is_err());
        assert!(session.install_client().is_err());
    }

    #[tokio::test]
    async fn helper_exit_before_readiness_rejects_empty_stdout() {
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _ = server.await;
    }

    #[tokio::test]
    async fn leftover_credential_makes_requested_shutdown_fail_and_preserves_file() {
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
    async fn pipe_lifetime_budget_also_applies_during_graceful_cleanup() {
        let _fixture = process_fixture().await;
        let dir = canonical_test_dir();
        let (agent_port, server) = rpc_fixture().await;
        let mode = format!(
            "trap 'printf %s {} >&2; rm -f \"__STATE__/.ashnazg-session-v1.json\"; exit 0' TERM\nwhile :; do sleep 0.05; done",
            "x".repeat(MAX_PIPE_BYTES + 1)
        );
        let launch = fixture_launch(
            dir.path(),
            agent_port,
            free_port(),
            &mode,
            "$$",
            "agent_ready",
        );
        let script = fs::read_to_string(&launch.helper)
            .unwrap()
            .replace("__STATE__", &launch.state_dir.display().to_string());
        fs::write(&launch.helper, script).unwrap();
        let session = NativeSession::start_fixture(
            launch,
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        tokio::time::timeout(Duration::from_secs(3), server)
            .await
            .unwrap()
            .unwrap();
        session.cancel.cancel();
        assert!(
            tokio::time::timeout(Duration::from_secs(3), session.wait_terminated())
                .await
                .unwrap()
                .is_err()
        );
        assert_eq!(
            session.exit.borrow().as_ref().unwrap().reason,
            SessionReason::PipeFailure
        );
    }

    #[tokio::test]
    async fn malformed_duplicate_oversized_and_flooded_pipes_fail_without_detached_readers() {
        let _fixture = process_fixture().await;
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
            let _ = server.await;
        }
    }

    #[tokio::test]
    async fn invalid_receipt_startup_error_returns_after_helper_is_reaped() {
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _fixture = process_fixture().await;
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
        let _ = server.await;
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
    fn readiness_binds_the_host_adb_relay_separately_from_the_guest_serial() {
        let dir = canonical_test_dir();
        let launch = fixture_launch(dir.path(), 17777, 17778, "exit 0", "$$", "agent_ready");
        let ready = READY.replace("127.0.0.1:15555", "127.0.0.1:15556");
        assert!(parse_receipt(ready.as_bytes(), &launch).is_ok());
        for wrong in [
            "127.0.0.1:5555",
            "0.0.0.0:15556",
            "[::1]:15556",
            "emulator-5554",
        ] {
            let wrong = ready.replace("127.0.0.1:15556", wrong);
            assert!(parse_receipt(wrong.as_bytes(), &launch).is_err());
        }
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
