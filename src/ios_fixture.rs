//! Live iOS fixture bridge for the ashnazg MCP server (macOS only).
//!
//! One [`FixtureSession`] owns a disposable simulator, a private copy of the
//! `platform/ios` fixture sources, and a single `xcodebuild test` process
//! whose bridge test serves live `ui_snapshot`/`ui_act`/`ui_screenshot`
//! operations over a loopback TCP connection guarded by a per-run capability
//! token. The session exists only behind the explicit `ashnazg mcp
//! --ios-fixture` entry point; the default Android server never touches this
//! module. Every owned resource is tracked from the moment it is created:
//! simulator deletion is awaited and verified before a successful exit, and a
//! failed open or close preserves the work directory as evidence.

use anyhow::{Context, bail};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use futures_util::{SinkExt as _, StreamExt as _};
use serde_json::{Map, Value, json};
use std::{
    env, fs,
    io::Read as _,
    path::{Path, PathBuf},
    process::Stdio,
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};
use tokio::{
    net::TcpListener,
    process::{Child, Command},
    sync::Mutex,
    time::timeout,
};
use tokio_util::{
    codec::{FramedRead, FramedWrite, LinesCodec},
    sync::CancellationToken,
};

/// Covers the cold first run: simulator boot, fixture build, and install.
pub const OPEN_TIMEOUT: Duration = Duration::from_secs(900);
/// One live bridge operation must answer well within this deadline.
pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(180);
/// The bridge test must finish and exit xcodebuild within this grace.
pub const CLOSE_TIMEOUT: Duration = Duration::from_secs(300);
/// Bounded wait for `simctl bootstatus -b` on the owned device.
const BOOT_TIMEOUT: Duration = Duration::from_secs(240);
/// Bounded wait for installing the pinned staged copy on the owned device.
const INSTALL_TIMEOUT: Duration = Duration::from_secs(300);
/// One bridge response line (base64 PNG of an app screenshot fits easily).
pub const LINE_LIMIT: usize = 16 * 1024 * 1024;
const SCREENSHOT_LIMIT: usize = 8 * 1024 * 1024;
const FIXTURE_BUNDLE_ID: &str = "dev.ashnazg.ios.fixture";

/// Errors split so the MCP layer can distinguish a rejected client request
/// (invalid params) from a host-side session failure (internal error).
#[derive(Debug)]
pub enum BridgeError {
    Rejected(String),
    Host(anyhow::Error),
}

impl std::fmt::Display for BridgeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BridgeError::Rejected(message) => {
                write!(f, "iOS fixture rejected the request: {message}")
            }
            BridgeError::Host(error) => write!(f, "iOS fixture session failed: {error}"),
        }
    }
}

impl From<anyhow::Error> for BridgeError {
    fn from(error: anyhow::Error) -> Self {
        BridgeError::Host(error)
    }
}

/// Simulator and work directory owned by this invocation. Created as soon as
/// the simulator exists so every later error path can destroy it.
struct Owned {
    simulator: String,
    work_dir: PathBuf,
    simulator_deleted: AtomicBool,
}

impl Owned {
    /// Shuts the simulator down and deletes it, then verifies the device is
    /// gone; required before any successful session exit.
    async fn destroy(&self) -> anyhow::Result<()> {
        if self.simulator_deleted.load(Ordering::Relaxed) {
            return Ok(());
        }
        let _ = Command::new("xcrun")
            .args(["simctl", "shutdown", &self.simulator])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await;
        for attempt in 0..2 {
            let deleted = Command::new("xcrun")
                .args(["simctl", "delete", &self.simulator])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .await
                .context("failed to run simctl delete")?;
            if deleted.success() && !Self::device_exists(&self.simulator).await {
                self.simulator_deleted.store(true, Ordering::Relaxed);
                return Ok(());
            }
            if attempt == 0 {
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
        bail!("simulator {} was not deleted", self.simulator);
    }

    async fn device_exists(simulator: &str) -> bool {
        let Ok(output) = Command::new("xcrun")
            .args(["simctl", "list", "-j", "devices"])
            .output()
            .await
        else {
            return true;
        };
        let Ok(listing) = serde_json::from_slice::<Value>(&output.stdout) else {
            return true;
        };
        let text = serde_json::to_string(&listing).unwrap_or_default();
        text.contains(simulator)
    }

    fn evidence(&self) -> String {
        self.work_dir.display().to_string()
    }

    fn remove_evidence(&self) {
        let _ = fs::remove_dir_all(&self.work_dir);
    }
}

/// Which live bridge one `FixtureSession` owns: the classic in-tree iOS
/// fixture, or an external app pinned by a manifest. Both modes share the
/// owned simulator, bridge transport, token handshake, sequencing,
/// cancellation and teardown; only the launch differs.
pub enum SessionLaunch {
    Fixture,
    App(crate::ios_app::AppManifest),
}

/// Everything the shared transport needs that differs per launch mode.
struct LaunchPlan {
    name: String,
    work_dir: PathBuf,
    scheme: &'static str,
    only_testing: &'static str,
    result_bundle: &'static str,
    install: Option<PathBuf>,
    extra_envs: Vec<(String, String)>,
    greeting_app: String,
    platform: &'static str,
    session_tag: &'static str,
    descriptor_extra: Value,
    device_type: String,
    runtime: String,
}

pub struct FixtureSession {
    owned: Owned,
    child: Mutex<Child>,
    io: Mutex<Option<BridgeIo>>,
    launch: SessionLaunch,
}

struct BridgeIo {
    reader: FramedRead<tokio::net::tcp::OwnedReadHalf, LinesCodec>,
    writer: FramedWrite<tokio::net::tcp::OwnedWriteHalf, LinesCodec>,
    next_id: u64,
    sequence: u64,
}

impl FixtureSession {
    /// Creates the disposable simulator, generates the bridge project in a
    /// private work directory, starts the bridge test, and completes the
    /// capability-token handshake. On any failure the simulator is deleted
    /// before the error returns, and the work directory is preserved as
    /// evidence with its path in the error chain. The app launch adds its
    /// manifest, digest, bundle, platform and deep-link validation before
    /// the simulator exists and installs the pinned staged copy on the
    /// owned device.
    pub async fn open(launch: SessionLaunch) -> anyhow::Result<Self> {
        Self::require_tool("xcodebuild", &["-version"]).await?;
        Self::require_tool("xcrun", &["simctl", "help"]).await?;
        Self::require_tool("xcodegen", &["--version"]).await?;

        let plan = match &launch {
            SessionLaunch::Fixture => Self::fixture_plan().await?,
            SessionLaunch::App(manifest) => Self::app_plan(manifest).await?,
        };

        // The capability token exists before the UDID is allocated, so a
        // generation failure cannot leave an owned device behind.
        let token = capability_token()?;

        let name = plan.name.clone();
        let create = Command::new("xcrun")
            .args(["simctl", "create", &name, &plan.device_type, &plan.runtime])
            .output()
            .await
            .context("failed to run simctl create")?;
        if !create.status.success() {
            bail!(
                "simctl create failed with {}: {}",
                create.status,
                String::from_utf8_lossy(&create.stderr)
            );
        }
        let simulator = String::from_utf8_lossy(&create.stdout).trim().to_owned();
        if simulator.is_empty() {
            bail!("simctl create did not return a device UDID");
        }
        let owned = Owned {
            simulator: simulator.clone(),
            work_dir: plan.work_dir.clone(),
            simulator_deleted: AtomicBool::new(false),
        };

        if let Some(staged) = plan.install.as_deref() {
            let installed = Self::boot_and_install(&owned, staged).await;
            if let Err(error) = installed {
                let cleanup = owned.destroy().await;
                let message = format!(
                    "evidence preserved at {}; {}",
                    owned.evidence(),
                    log_tail(&owned.work_dir)
                );
                return match cleanup {
                    Err(cleanup) => Err(error.context(format!("{message}; {cleanup}"))),
                    Ok(()) => Err(error.context(message)),
                };
            }
        }

        let connected = Self::connect(&owned, &plan, &token).await;
        let (child, io) = match connected {
            Ok(connected) => connected,
            Err(error) => {
                let cleanup = owned.destroy().await;
                let message = format!(
                    "evidence preserved at {}; {}",
                    owned.evidence(),
                    log_tail(&owned.work_dir)
                );
                if let Err(cleanup) = cleanup {
                    return Err(error.context(format!("{message}; {cleanup}")));
                }
                return Err(error.context(message));
            }
        };
        Ok(Self {
            owned,
            child: Mutex::new(child),
            io: Mutex::new(Some(io)),
            launch,
        })
    }

    /// True when this session drives an external manifest-pinned app.
    pub(crate) fn is_app_launch(&self) -> bool {
        matches!(self.launch, SessionLaunch::App(_))
    }

    /// The platform label carried by every snapshot and action result.
    pub(crate) fn platform(&self) -> &'static str {
        match self.launch {
            SessionLaunch::App(_) => "ios-app",
            SessionLaunch::Fixture => "ios-fixture",
        }
    }

    /// The exact deep-link allowlist the host-side routing checks against.
    pub(crate) fn deep_links(&self) -> &[String] {
        match &self.launch {
            SessionLaunch::App(manifest) => &manifest.deep_links,
            SessionLaunch::Fixture => &[],
        }
    }

    /// One live snapshot against the running session app.
    pub async fn snapshot(&self, cancel: &CancellationToken) -> Result<Value, BridgeError> {
        let (response, sequence) = self.exchange(&json!({"op": "ui_snapshot"}), cancel).await?;
        let nodes = response
            .get("nodes")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("bridge snapshot omitted nodes"))?;
        Ok(json!({"snapshotId": sequence, "nodes": nodes, "platform": self.platform()}))
    }

    /// One live tap or type action; the selector must come from a prior
    /// snapshot. The bridge only answers after the fixture state settled,
    /// matching the Android action result shape `{"settled": ..., "snapshot":
    /// ...}`.
    pub async fn act(
        &self,
        command: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value, BridgeError> {
        let action = command
            .get("action")
            .and_then(Value::as_str)
            .unwrap_or("act");
        let (response, sequence) = self.exchange(command, cancel).await?;
        let nodes = response
            .get("nodes")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("bridge {action} response omitted nodes"))?;
        Ok(json!({
            "settled": true,
            "snapshot": {"snapshotId": sequence, "nodes": nodes, "platform": self.platform()},
        }))
    }

    /// One live screenshot; returns the PNG bytes, the state sequence id the
    /// image was captured in, and the fixture counter the bridge observed
    /// at capture time when the session has one, for correlation with the
    /// post-tap snapshot. An external app session reports no counter.
    pub async fn screenshot(
        &self,
        cancel: &CancellationToken,
    ) -> Result<(Vec<u8>, u64, Option<String>), BridgeError> {
        let (response, sequence) = self
            .exchange(&json!({"op": "ui_screenshot"}), cancel)
            .await?;
        let encoded = response
            .get("png")
            .and_then(Value::as_str)
            .ok_or_else(|| anyhow::anyhow!("bridge screenshot omitted png data"))?;
        let png = STANDARD
            .decode(encoded)
            .map_err(|_| anyhow::anyhow!("bridge screenshot returned invalid base64"))?;
        if png.len() > SCREENSHOT_LIMIT {
            return Err(
                anyhow::anyhow!("bridge screenshot exceeds {SCREENSHOT_LIMIT} bytes").into(),
            );
        }
        if png_dimensions(&png).is_none() {
            return Err(anyhow::anyhow!("bridge screenshot is not a valid PNG").into());
        }
        let counter = response
            .get("counter")
            .and_then(Value::as_str)
            .map(str::to_owned);
        Ok((png, sequence, counter))
    }

    /// Closes the bridge, waits for the owned child to exit, and awaits and
    /// verifies deletion of the owned simulator. The work directory is kept
    /// whenever anything failed, with its path in the error chain.
    pub async fn close(&self) -> anyhow::Result<()> {
        if let Some(mut io) = self.io.lock().await.take() {
            let _ = io.writer.send(json!({"op": "close"}).to_string()).await;
        }
        let mut child = self.child.lock().await;
        let status = match timeout(CLOSE_TIMEOUT, child.wait()).await {
            Ok(status) => status.context("failed to wait for xcodebuild")?,
            Err(_) => {
                child.kill().await.context("failed to kill xcodebuild")?;
                child.wait().await.context("failed to reap xcodebuild")?
            }
        };
        if !status.success() {
            self.owned.destroy().await?;
            bail!(
                "xcodebuild failed with {status}; evidence preserved at {}; {}",
                self.owned.evidence(),
                log_tail(&self.owned.work_dir)
            );
        }
        self.owned.destroy().await?;
        self.owned.remove_evidence();
        Ok(())
    }

    /// Serializes one bridge exchange. Every ambiguous outcome (timeout,
    /// cancellation, malformed or mismatched response) poisons the session so
    /// a delayed reply can never be consumed by the next call.
    async fn exchange(
        &self,
        command: &Value,
        cancel: &CancellationToken,
    ) -> Result<(Value, u64), BridgeError> {
        let mut guard = self.io.lock().await;
        let Some(io) = guard.as_mut() else {
            return Err(BridgeError::Host(anyhow::anyhow!(
                "bridge session is closed"
            )));
        };
        let outcome = Self::exchange_on(io, command, cancel).await;
        match outcome {
            Ok(result) => Ok(result),
            Err(Poisoned::Rejected(message)) => Err(BridgeError::Rejected(message)),
            Err(Poisoned::Host(error)) => {
                *guard = None;
                Err(BridgeError::Host(error))
            }
        }
    }

    async fn exchange_on(
        io: &mut BridgeIo,
        command: &Value,
        cancel: &CancellationToken,
    ) -> Result<(Value, u64), Poisoned> {
        let request_id = io.next_id;
        io.next_id += 1;
        let mut envelope = command.as_object().cloned().unwrap_or_else(Map::new);
        envelope.insert("id".into(), json!(request_id));
        let line = serde_json::to_string(&envelope).map_err(|e| Poisoned::Host(e.into()))?;
        io.writer
            .send(line)
            .await
            .map_err(|e| Poisoned::Host(e.into()))?;
        let line = tokio::select! {
            biased;
            _ = cancel.cancelled() => {
                return Err(Poisoned::Host(anyhow::anyhow!("bridge request was cancelled")));
            }
            answer = timeout(COMMAND_TIMEOUT, io.reader.next()) => match answer {
                Ok(Some(Ok(line))) => line,
                Ok(None) => {
                    return Err(Poisoned::Host(anyhow::anyhow!(
                        "bridge closed before answering"
                    )));
                }
                Ok(Some(Err(error))) => {
                    return Err(Poisoned::Host(anyhow::anyhow!(
                        "bridge response exceeded the line limit: {error}"
                    )));
                }
                Err(_) => {
                    return Err(Poisoned::Host(anyhow::anyhow!(
                        "bridge response timed out after {COMMAND_TIMEOUT:?}; {}",
                        log_tail_hint()
                    )));
                }
            }
        };
        let response: Value = serde_json::from_str(&line)
            .map_err(|_| anyhow::anyhow!("bridge response was not a JSON line"))?;
        if response.get("id").and_then(Value::as_u64) != Some(request_id) {
            return Err(Poisoned::Host(anyhow::anyhow!(
                "bridge response id did not match the request"
            )));
        }
        if response.get("ok") == Some(&Value::Bool(true)) {
            io.sequence += 1;
            return Ok((response, io.sequence));
        }
        let message = response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("bridge rejected the request")
            .to_owned();
        Err(Poisoned::Rejected(message))
    }

    async fn connect(
        owned: &Owned,
        plan: &LaunchPlan,
        token: &str,
    ) -> anyhow::Result<(Child, BridgeIo)> {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .context("failed to bind the loopback bridge listener")?;
        let port = listener
            .local_addr()
            .context("bridge listener has no local address")?
            .port();

        let arguments = vec![
            "test".to_owned(),
            "-project".to_owned(),
            "AshnazgSimulatorProbe.xcodeproj".to_owned(),
            "-scheme".to_owned(),
            plan.scheme.to_owned(),
            "-destination".to_owned(),
            format!("platform=iOS Simulator,id={}", owned.simulator),
            format!("-only-testing:{}", plan.only_testing),
            "-parallel-testing-enabled".to_owned(),
            "NO".to_owned(),
            "-resultBundlePath".to_owned(),
            owned
                .work_dir
                .join(plan.result_bundle)
                .display()
                .to_string(),
        ];
        let command = format!("xcodebuild {}", arguments.join(" "));
        let log = fs::File::create(owned.work_dir.join("xcodebuild.log")).with_context(|| {
            format!(
                "failed to create {}",
                owned.work_dir.join("xcodebuild.log").display()
            )
        })?;
        let mut child = Command::new("xcodebuild")
            .args(&arguments)
            .env("TEST_RUNNER_ASHNAZG_PROBE_BRIDGE", port.to_string())
            .env("TEST_RUNNER_ASHNAZG_PROBE_TOKEN", token)
            // External-mode selection keys are cleared first so a fixture
            // launch can never inherit them from this process's ambient
            // environment; an app launch re-applies its own values below.
            .env_remove("TEST_RUNNER_ASHNAZG_APP_BUNDLE_ID")
            .env_remove("TEST_RUNNER_ASHNAZG_APP_INITIAL_URL")
            .env_remove("TEST_RUNNER_ASHNAZG_APP_READY_SELECTOR")
            .envs(plan.extra_envs.iter().map(|(key, value)| (key, value)))
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .current_dir(&owned.work_dir)
            .kill_on_drop(true)
            .spawn()
            .context("failed to start xcodebuild")?;

        let mut descriptor = json!({
            "simulator": {
                "udid": owned.simulator,
                "name": plan.name,
                "deviceType": plan.device_type,
                "runtime": plan.runtime,
            },
            "bridgePort": port,
            "workDir": owned.evidence(),
            "pid": child.id().unwrap_or_default(),
            "command": command,
        });
        if let (Some(object), Some(extra)) = (
            descriptor.as_object_mut(),
            plan.descriptor_extra.as_object(),
        ) {
            for (key, value) in extra {
                object.insert(key.clone(), value.clone());
            }
        }
        tracing::info!("{} {descriptor}", plan.session_tag);

        let (stream, _) = tokio::select! {
            accepted = listener.accept() => {
                accepted.context("failed to accept the bridge connection")?
            }
            status = child.wait() => {
                bail!(
                    "xcodebuild exited before the bridge test connected: {status:?}; {}",
                    log_tail(&owned.work_dir)
                )
            }
            _ = tokio::time::sleep(OPEN_TIMEOUT) => {
                bail!("bridge connection timed out after {OPEN_TIMEOUT:?}; {}", log_tail(&owned.work_dir))
            }
        };

        let (read_half, write_half) = stream.into_split();
        let mut reader = FramedRead::new(read_half, LinesCodec::new_with_max_length(LINE_LIMIT));
        let writer = FramedWrite::new(write_half, LinesCodec::new());
        let greeting_line = timeout(OPEN_TIMEOUT, reader.next())
            .await
            .map_err(|_| {
                anyhow::anyhow!("bridge greeting timed out; {}", log_tail(&owned.work_dir))
            })?
            .transpose()
            .map_err(|error| anyhow::anyhow!("bridge greeting exceeded the line limit: {error}"))?
            .context("bridge closed before the greeting")?;
        // Never include the greeting contents in an error: it carries the
        // per-run capability token.
        let greeting_ok = (|| {
            let greeting: Value = serde_json::from_str(&greeting_line).ok()?;
            (greeting.get("ready") == Some(&Value::Bool(true))).then_some(greeting)
        })()
        .filter(|greeting| {
            greeting.get("token").and_then(Value::as_str) == Some(token)
                && greeting.get("app").and_then(Value::as_str) == Some(plan.greeting_app.as_str())
        });
        if greeting_ok.is_none() {
            bail!("bridge greeting failed the capability handshake");
        }

        Ok((
            child,
            BridgeIo {
                reader,
                writer,
                next_id: 1,
                sequence: 0,
            },
        ))
    }

    /// The classic in-tree fixture launch: newest runtime and iPhone type,
    /// no install step, the fixture bridge test, and the fixture bundle id
    /// in the capability handshake.
    async fn fixture_plan() -> anyhow::Result<LaunchPlan> {
        let source = Self::fixture_source()?;
        let runtime = Self::newest_runtime().await?;
        let device_type = Self::newest_iphone_type().await?;
        let work_dir = Self::fresh_work_dir("ashnazg-ios-fixture")?;
        Self::copy_bridge_sources(&source, &work_dir)?;
        Self::generate_project(&work_dir).await?;
        Ok(LaunchPlan {
            name: format!("ashnazg-mcp-fixture-{}", hex_suffix(4)),
            work_dir,
            scheme: "AshnazgSimulatorProbe",
            only_testing: "FixtureUITests/McpBridgeTests",
            result_bundle: "Probe.xcresult",
            install: None,
            extra_envs: Vec::new(),
            greeting_app: FIXTURE_BUNDLE_ID.to_owned(),
            platform: "ios-fixture",
            session_tag: "ios-fixture-session",
            descriptor_extra: json!({"fixtureApp": FIXTURE_BUNDLE_ID}),
            device_type,
            runtime,
        })
    }

    /// The external app launch from a validated manifest. Every wrong
    /// bundle, platform, digest, path and URI failure happens here, before
    /// the simulator exists; the pinned staged copy is what gets installed
    /// on the owned device.
    async fn app_plan(manifest: &crate::ios_app::AppManifest) -> anyhow::Result<LaunchPlan> {
        use crate::ios_app::{AppManifest, ReadySelector, stage_tree_pinned};

        manifest.validate()?;

        let AppManifest {
            app_path,
            app_sha256,
            bundle_id,
            runtime,
            device_type,
            initial_url,
            ready_selector,
            deep_links,
        } = manifest;
        let source = Self::fixture_source()?;
        Self::preflight_launch_pair(runtime, device_type).await?;

        let app_name = app_path
            .file_name()
            .with_context(|| format!("appPath {} has no bundle name", app_path.display()))?
            .to_owned();
        let app_stat = fs::metadata(app_path)
            .with_context(|| format!("failed to stat the app at {}", app_path.display()))?;
        if !app_stat.is_dir() {
            bail!(
                "appPath {} is not an app bundle directory",
                app_path.display()
            );
        }

        let work_dir = Self::fresh_work_dir("ashnazg-ios-app")?;
        Self::copy_bridge_sources(&source, &work_dir)?;
        Self::generate_project(&work_dir)
            .await
            .with_context(|| format!("evidence preserved at {}", work_dir.display()))?;

        // The pinned staging copy is what gets installed: its digest is
        // computed and compared before any simulator is created.
        let staged = work_dir.join("Staged").join(&app_name);
        fs::create_dir_all(staged.parent().expect("the staged parent exists"))
            .with_context(|| format!("failed to create {}", staged.display()))?;
        let digest = stage_tree_pinned(app_path, &staged)
            .with_context(|| format!("evidence preserved at {}", work_dir.display()))?;
        if digest != *app_sha256 {
            bail!(
                "staged app digest {digest} does not match the manifest appSha256; evidence preserved at {}",
                work_dir.display()
            );
        }

        let plist = read_plist(&staged.join("Info.plist"))
            .with_context(|| format!("evidence preserved at {}", work_dir.display()))?;
        let staged_bundle_id = plist
            .get("CFBundleIdentifier")
            .and_then(Value::as_str)
            .with_context(|| {
                format!(
                    "staged app has no CFBundleIdentifier; evidence preserved at {}",
                    work_dir.display()
                )
            })?;
        if staged_bundle_id != bundle_id {
            bail!(
                "staged app bundle id {staged_bundle_id} does not match the manifest bundleId {bundle_id}; evidence preserved at {}",
                work_dir.display()
            );
        }
        let executable = plist
            .get("CFBundleExecutable")
            .and_then(Value::as_str)
            .with_context(|| {
                format!(
                    "staged app has no CFBundleExecutable; evidence preserved at {}",
                    work_dir.display()
                )
            })?
            .to_owned();
        // One plain basename only: a manifest-controlled plist value must
        // never steer validation outside the hashed bundle root.
        if executable.is_empty()
            || executable == "."
            || executable == ".."
            || executable.contains(['/', '\\'])
            || Path::new(&executable).components().count() != 1
        {
            bail!(
                "staged app CFBundleExecutable must be one plain bundle-root basename; evidence preserved at {}",
                work_dir.display()
            );
        }
        let staged_executable = staged.join(&executable);
        if !staged_executable.is_file() {
            bail!(
                "staged app executable {} is missing; evidence preserved at {}",
                staged_executable.display(),
                work_dir.display()
            );
        }
        Self::validate_simulator_platform(&staged_executable).await?;
        Self::validate_deep_link_schemes(&plist, deep_links, &work_dir)?;

        let mut extra_envs = Vec::new();
        if let Some(initial_url) = initial_url.as_deref() {
            extra_envs.push((
                "TEST_RUNNER_ASHNAZG_APP_INITIAL_URL".to_owned(),
                initial_url.to_owned(),
            ));
        }
        let selector = match ready_selector {
            ReadySelector {
                test_id: Some(test_id),
                text: None,
            } => {
                json!({"testId": test_id})
            }
            ReadySelector {
                test_id: None,
                text: Some(text),
            } => json!({"text": text}),
            _ => bail!("readySelector must be exactly one nonempty testId or text"),
        };
        extra_envs.push((
            "TEST_RUNNER_ASHNAZG_APP_READY_SELECTOR".to_owned(),
            selector.to_string(),
        ));
        extra_envs.push((
            "TEST_RUNNER_ASHNAZG_APP_BUNDLE_ID".to_owned(),
            bundle_id.clone(),
        ));

        Ok(LaunchPlan {
            name: format!("ashnazg-mcp-app-{}", hex_suffix(4)),
            work_dir,
            scheme: "AshnazgSimulatorProbe",
            only_testing: "FixtureUITests/McpBridgeTests",
            result_bundle: "AppProbe.xcresult",
            install: Some(staged),
            extra_envs,
            greeting_app: bundle_id.clone(),
            platform: "ios-app",
            session_tag: "ios-app-session",
            descriptor_extra: json!({"bundleId": bundle_id, "appSha256": digest}),
            device_type: device_type.clone(),
            runtime: runtime.clone(),
        })
    }

    fn copy_bridge_sources(source: &Path, work_dir: &Path) -> anyhow::Result<()> {
        copy_tree(&source.join("Fixture"), &work_dir.join("Fixture"))?;
        copy_tree(&source.join("UITests"), &work_dir.join("UITests"))?;
        fs::copy(source.join("project.yml"), work_dir.join("project.yml"))?;
        Ok(())
    }

    async fn generate_project(work_dir: &Path) -> anyhow::Result<()> {
        let generate = Command::new("xcodegen")
            .arg("generate")
            .current_dir(work_dir)
            .output()
            .await
            .context("failed to run xcodegen")?;
        if !generate.status.success() {
            bail!(
                "xcodegen failed with {}: {}",
                generate.status,
                String::from_utf8_lossy(&generate.stderr)
            );
        }
        Ok(())
    }

    /// Boots the owned device and installs the pinned staged copy on it;
    /// both waits are bounded.
    async fn boot_and_install(owned: &Owned, staged: &Path) -> anyhow::Result<()> {
        let boot = Command::new("xcrun")
            .args(["simctl", "boot", &owned.simulator])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await
            .context("failed to run simctl boot")?;
        if !boot.success() {
            bail!("simctl boot failed with {boot}");
        }
        let booted = timeout(
            BOOT_TIMEOUT,
            Command::new("xcrun")
                .args(["simctl", "bootstatus", &owned.simulator, "-b"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .kill_on_drop(true)
                .status(),
        )
        .await
        .context("simctl bootstatus timed out")??;
        if !booted.success() {
            bail!("simulator {} did not finish booting", owned.simulator);
        }
        let install = timeout(
            INSTALL_TIMEOUT,
            Command::new("xcrun")
                .args(["simctl", "install", &owned.simulator])
                .arg(staged)
                .output(),
        )
        .await
        .context("simctl install timed out")?
        .context("failed to run simctl install")?;
        if !install.status.success() {
            bail!(
                "simctl install failed with {}: {}",
                install.status,
                String::from_utf8_lossy(&install.stderr)
            );
        }
        Ok(())
    }

    /// Qualifies the manifest's explicit runtime/deviceType pair against
    /// the host, mirroring the hosted probe evidence: the named runtime must
    /// be available and its own `supportedDeviceTypes` must list the named
    /// device type. An absent or empty list is never treated as verified
    /// compatibility, and separate presence of either identifier alone is
    /// insufficient.
    async fn preflight_launch_pair(runtime: &str, device_type: &str) -> anyhow::Result<()> {
        let output = Command::new("xcrun")
            .args(["simctl", "list", "-j", "runtimes"])
            .output()
            .await
            .context("failed to list simulator runtimes")?;
        let runtimes: Value = serde_json::from_slice(&output.stdout)
            .context("simctl runtimes output was not JSON")?;
        let selected = runtimes
            .get("runtimes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .find(|candidate| candidate.get("identifier").and_then(Value::as_str) == Some(runtime));
        let Some(selected) = selected else {
            bail!("manifest runtime {runtime} is not an installed simulator runtime");
        };
        if selected.get("isAvailable") != Some(&Value::Bool(true)) {
            bail!("manifest runtime {runtime} is not available on this host");
        }
        let supported = selected
            .get("supportedDeviceTypes")
            .and_then(Value::as_array);
        let Some(supported) = supported else {
            bail!(
                "manifest runtime {runtime} did not report supportedDeviceTypes; the device pair is not verified compatible"
            );
        };
        let listed = supported
            .iter()
            .any(|entry| entry.get("identifier").and_then(Value::as_str) == Some(device_type));
        if !listed {
            bail!(
                "manifest deviceType {device_type} is not in runtime {runtime}'s supportedDeviceTypes; the pair is not verified compatible"
            );
        }
        Ok(())
    }

    /// Apple-tool platform validation on the staged executable, the same
    /// recipe the Meer proof uses: `lipo -archs` must contain the host's
    /// native slice (a universal simulator build is fine), and
    /// `xcrun vtool -arch <native>` must report the iOSSimulator platform
    /// for that slice. Device and wrong-slice builds fail before the
    /// simulator exists.
    async fn validate_simulator_platform(staged_executable: &Path) -> anyhow::Result<()> {
        let native = match std::env::consts::ARCH {
            "aarch64" => "arm64",
            "x86_64" => "x86_64",
            other => bail!("unsupported host architecture {other} for the iOS app session"),
        };
        let lipo = Command::new("lipo")
            .arg("-archs")
            .arg(staged_executable)
            .output()
            .await
            .context("failed to run lipo")?;
        if !lipo.status.success() {
            bail!(
                "lipo -archs failed with {}: {}",
                lipo.status,
                String::from_utf8_lossy(&lipo.stderr)
            );
        }
        let archs = String::from_utf8_lossy(&lipo.stdout);
        if !archs.split_whitespace().any(|arch| arch == native) {
            bail!(
                "staged executable has no {native} slice (lipo -archs: {})",
                archs.trim()
            );
        }
        let vtool = Command::new("xcrun")
            .args(["vtool", "-arch", native, "-show-build"])
            .arg(staged_executable)
            .output()
            .await
            .context("failed to run vtool")?;
        if !vtool.status.success() {
            bail!(
                "xcrun vtool failed with {}: {}",
                vtool.status,
                String::from_utf8_lossy(&vtool.stderr)
            );
        }
        // The platform line is compared case-insensitively on its exact
        // value, with the numeric iOSSimulator platform accepted; a loose
        // substring match would let "iOS" or unrelated output pass.
        let build = String::from_utf8_lossy(&vtool.stdout);
        let platform = build
            .lines()
            .find_map(|line| {
                let lowered = line.trim().to_ascii_lowercase();
                let rest = lowered.strip_prefix("platform")?;
                rest.trim_start_matches([':', ' ', '\t'])
                    .split_whitespace()
                    .next()
                    .map(str::to_owned)
            })
            .unwrap_or_default();
        if platform != "iossimulator" && platform != "7" {
            bail!(
                "staged executable {native} slice was not built for the iOS Simulator platform; vtool reported platform {platform:?}"
            );
        }
        Ok(())
    }

    /// Every manifest deep link must be an exact allowlist entry whose
    /// scheme the staged app actually declares in CFBundleURLTypes.
    fn validate_deep_link_schemes(
        plist: &Value,
        deep_links: &[String],
        work_dir: &Path,
    ) -> anyhow::Result<()> {
        let declared: Vec<String> = plist
            .get("CFBundleURLTypes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|entry| entry.get("CFBundleURLSchemes"))
            .filter_map(Value::as_array)
            .flatten()
            .filter_map(Value::as_str)
            .map(str::to_lowercase)
            .collect();
        for deep_link in deep_links {
            let scheme = deep_link
                .split_once(':')
                .map(|(scheme, _)| scheme)
                .unwrap_or_default()
                .to_lowercase();
            if !declared.iter().any(|declared| declared == &scheme) {
                bail!(
                    "deep link {deep_link} scheme {scheme} is not declared by the staged app's CFBundleURLTypes; evidence preserved at {}",
                    work_dir.display()
                );
            }
        }
        Ok(())
    }

    async fn require_tool(program: &str, args: &[&str]) -> anyhow::Result<()> {
        let probe = Command::new(program)
            .args(args)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .await
            .with_context(|| format!("{program} is required for the iOS fixture bridge"))?;
        if !probe.success() {
            bail!("{program} is required for the iOS fixture bridge");
        }
        Ok(())
    }

    fn fixture_source() -> anyhow::Result<PathBuf> {
        let source = env::var_os("ASHNAZG_IOS_FIXTURE_SOURCE")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("platform/ios"));
        if !source.join("project.yml").is_file() {
            bail!(
                "iOS fixture sources are missing at {}; set ASHNAZG_IOS_FIXTURE_SOURCE",
                source.display()
            );
        }
        Ok(source)
    }

    async fn newest_runtime() -> anyhow::Result<String> {
        let output = Command::new("xcrun")
            .args(["simctl", "list", "-j", "runtimes"])
            .output()
            .await
            .context("failed to list simulator runtimes")?;
        let runtimes: Value = serde_json::from_slice(&output.stdout)
            .context("simctl runtimes output was not JSON")?;
        let mut best: Option<(Vec<u64>, String)> = None;
        for runtime in runtimes
            .get("runtimes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if runtime.get("platform").and_then(Value::as_str) != Some("iOS") {
                continue;
            }
            if runtime.get("isAvailable") != Some(&Value::Bool(true)) {
                continue;
            }
            let version = runtime
                .get("version")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .split('.')
                .map(|part| part.parse::<u64>().unwrap_or(0))
                .collect::<Vec<_>>();
            let identifier = runtime
                .get("identifier")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            if identifier.is_empty() {
                continue;
            }
            if best
                .as_ref()
                .is_none_or(|(existing, _)| version > *existing)
            {
                best = Some((version, identifier));
            }
        }
        best.map(|(_, identifier)| identifier)
            .context("no available iOS Simulator runtime was found")
    }

    async fn newest_iphone_type() -> anyhow::Result<String> {
        let output = Command::new("xcrun")
            .args(["simctl", "list", "-j", "devicetypes"])
            .output()
            .await
            .context("failed to list simulator device types")?;
        let types: Value = serde_json::from_slice(&output.stdout)
            .context("simctl devicetypes output was not JSON")?;
        let mut best: Option<(u64, String)> = None;
        for kind in types
            .get("devicetypes")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let identifier = kind
                .get("identifier")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let Some(rest) =
                identifier.strip_prefix("com.apple.CoreSimulator.SimDeviceType.iPhone-")
            else {
                continue;
            };
            let generation = rest
                .split('-')
                .next()
                .unwrap_or_default()
                .parse::<u64>()
                .unwrap_or(0);
            if best
                .as_ref()
                .is_none_or(|(existing, _)| generation > *existing)
            {
                best = Some((generation, identifier.to_owned()));
            }
        }
        best.map(|(_, identifier)| identifier)
            .context("no iPhone simulator device type was found")
    }

    fn fresh_work_dir(prefix: &str) -> anyhow::Result<PathBuf> {
        let work_dir = env::temp_dir().join(format!("{prefix}-{}", hex_suffix(8)));
        fs::create_dir(&work_dir)
            .with_context(|| format!("failed to create {}", work_dir.display()))?;
        Ok(work_dir)
    }
}

/// Outcome of one bridge exchange; `Host` poisons the session.
enum Poisoned {
    Rejected(String),
    Host(anyhow::Error),
}

impl From<anyhow::Error> for Poisoned {
    fn from(error: anyhow::Error) -> Self {
        Poisoned::Host(error)
    }
}

impl Drop for FixtureSession {
    fn drop(&mut self) {
        // A failed or abandoned session can leave diagnostics in the work directory.
        if *self.owned.simulator_deleted.get_mut() {
            return;
        }
        let simulator = self.owned.simulator.clone();
        std::thread::spawn(move || {
            for arguments in [["shutdown", &simulator], ["delete", &simulator]] {
                let _ = std::process::Command::new("xcrun")
                    .arg("simctl")
                    .args(arguments)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
        });
    }
}

/// PNG dimensions from the IHDR chunk, used for screenshot metadata.
pub fn png_dimensions(png: &[u8]) -> Option<(u32, u32)> {
    if png.len() < 24 || &png[..8] != b"\x89PNG\r\n\x1a\n" || &png[12..16] != b"IHDR" {
        return None;
    }
    let width = u32::from_be_bytes(png[16..20].try_into().ok()?);
    let height = u32::from_be_bytes(png[20..24].try_into().ok()?);
    (width > 0 && height > 0).then_some((width, height))
}

fn log_tail(work_dir: &Path) -> String {
    fs::read(work_dir.join("xcodebuild.log"))
        .map(|bytes| {
            let start = bytes.len().saturating_sub(2048);
            String::from_utf8_lossy(&bytes[start..]).to_string()
        })
        .unwrap_or_else(|_| "no xcodebuild log available".to_owned())
}

fn log_tail_hint() -> &'static str {
    "check the session evidence directory"
}

fn copy_tree(source: &Path, target: &Path) -> anyhow::Result<()> {
    fs::create_dir_all(target).with_context(|| format!("failed to create {}", target.display()))?;
    for entry in
        fs::read_dir(source).with_context(|| format!("failed to read {}", source.display()))?
    {
        let entry = entry.with_context(|| format!("failed to read {}", source.display()))?;
        let path = entry.path();
        if path.is_dir() {
            copy_tree(&path, &target.join(entry.file_name()))?;
        } else {
            fs::copy(&path, target.join(entry.file_name()))
                .with_context(|| format!("failed to copy {}", path.display()))?;
        }
    }
    Ok(())
}

fn hex_suffix(bytes: usize) -> String {
    let mut buffer = vec![0u8; bytes];
    fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut buffer))
        .expect("reading /dev/urandom must not fail on macOS");
    buffer.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn capability_token() -> anyhow::Result<String> {
    let token = hex_suffix(32);
    if token.len() != 64 {
        bail!("capability token generation failed");
    }
    Ok(token)
}

/// Reads one staged plist as JSON through the real `plutil` executable.
/// A nonzero status or an unparseable document is an explicit error, so a
/// malformed staged `Info.plist` fails before any simulator exists.
fn read_plist(path: &Path) -> anyhow::Result<Value> {
    let output = std::process::Command::new("/usr/bin/plutil")
        .args(["-convert", "json", "-o", "-"])
        .arg(path)
        .output()
        .context("failed to run plutil on the staged Info.plist")?;
    if !output.status.success() {
        bail!(
            "plutil failed with {} on {}: {}",
            output.status,
            path.display(),
            String::from_utf8_lossy(&output.stderr)
        );
    }
    serde_json::from_slice(&output.stdout)
        .with_context(|| format!("plutil did not emit JSON for {}", path.display()))
}
