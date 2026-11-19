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
}

impl Owned {
    /// Shuts the simulator down and deletes it, then verifies the device is
    /// gone; required before any successful session exit.
    async fn destroy(&self) -> anyhow::Result<()> {
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

pub struct FixtureSession {
    owned: Owned,
    child: Mutex<Child>,
    io: Mutex<Option<BridgeIo>>,
}

struct BridgeIo {
    reader: FramedRead<tokio::net::tcp::OwnedReadHalf, LinesCodec>,
    writer: FramedWrite<tokio::net::tcp::OwnedWriteHalf, LinesCodec>,
    next_id: u64,
    sequence: u64,
}

impl FixtureSession {
    /// Creates the disposable simulator, generates the fixture project in a
    /// private work directory, starts the bridge test, and completes the
    /// capability-token handshake. On any failure the simulator is deleted
    /// before the error returns, and the work directory is preserved as
    /// evidence with its path in the error chain.
    pub async fn open() -> anyhow::Result<Self> {
        Self::require_tool("xcodebuild", &["-version"]).await?;
        Self::require_tool("xcrun", &["simctl", "help"]).await?;
        Self::require_tool("xcodegen", &["--version"]).await?;

        let source = Self::fixture_source()?;
        let runtime = Self::newest_runtime().await?;
        let device_type = Self::newest_iphone_type().await?;
        let work_dir = Self::fresh_work_dir()?;
        copy_tree(&source.join("Fixture"), &work_dir.join("Fixture"))?;
        copy_tree(&source.join("UITests"), &work_dir.join("UITests"))?;
        fs::copy(source.join("project.yml"), work_dir.join("project.yml"))?;

        let generate = Command::new("xcodegen")
            .arg("generate")
            .current_dir(&work_dir)
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

        let name = format!("ashnazg-mcp-fixture-{}", hex_suffix(4));
        let create = Command::new("xcrun")
            .args(["simctl", "create", &name, &device_type, &runtime])
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
            work_dir: work_dir.clone(),
        };

        let token = capability_token()?;
        let connected = Self::connect(&owned, &token, &name, &device_type, &runtime).await;
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
        })
    }

    /// One live snapshot against the running fixture app.
    pub async fn snapshot(&self, cancel: &CancellationToken) -> Result<Value, BridgeError> {
        let (response, sequence) = self.exchange(&json!({"op": "ui_snapshot"}), cancel).await?;
        let nodes = response
            .get("nodes")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("bridge snapshot omitted nodes"))?;
        Ok(json!({"snapshotId": sequence, "nodes": nodes, "platform": "ios-fixture"}))
    }

    /// One live tap; the selector must come from a prior snapshot. The bridge
    /// only answers after the fixture state settled, matching the Android
    /// action result shape `{"settled": ..., "snapshot": ...}`.
    pub async fn act(
        &self,
        command: &Value,
        cancel: &CancellationToken,
    ) -> Result<Value, BridgeError> {
        let (response, sequence) = self.exchange(command, cancel).await?;
        let nodes = response
            .get("nodes")
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("bridge tap response omitted nodes"))?;
        Ok(json!({
            "settled": true,
            "snapshot": {"snapshotId": sequence, "nodes": nodes, "platform": "ios-fixture"},
        }))
    }

    /// One live screenshot; returns the PNG bytes, the state sequence id the
    /// image was captured in, and the fixture counter the bridge observed at
    /// capture time, for correlation with the post-tap snapshot.
    pub async fn screenshot(
        &self,
        cancel: &CancellationToken,
    ) -> Result<(Vec<u8>, u64, String), BridgeError> {
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
            .unwrap_or_default()
            .to_owned();
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
        token: &str,
        name: &str,
        device_type: &str,
        runtime: &str,
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
            "AshnazgSimulatorProbe".to_owned(),
            "-destination".to_owned(),
            format!("platform=iOS Simulator,id={}", owned.simulator),
            "-only-testing:FixtureUITests/McpBridgeTests".to_owned(),
            "-parallel-testing-enabled".to_owned(),
            "NO".to_owned(),
            "-resultBundlePath".to_owned(),
            owned.work_dir.join("Probe.xcresult").display().to_string(),
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
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .current_dir(&owned.work_dir)
            .kill_on_drop(true)
            .spawn()
            .context("failed to start xcodebuild")?;

        let descriptor = json!({
            "simulator": {
                "udid": owned.simulator,
                "name": name,
                "deviceType": device_type,
                "runtime": runtime,
            },
            "bridgePort": port,
            "workDir": owned.evidence(),
            "pid": child.id().unwrap_or_default(),
            "command": command,
            "fixtureApp": FIXTURE_BUNDLE_ID,
        });
        tracing::info!("ios-fixture-session {descriptor}");

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
                && greeting.get("app").and_then(Value::as_str) == Some(FIXTURE_BUNDLE_ID)
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

    fn fresh_work_dir() -> anyhow::Result<PathBuf> {
        let work_dir = env::temp_dir().join(format!("ashnazg-ios-fixture-{}", hex_suffix(8)));
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
        // The deterministic close() path awaited verified deletion; this only
        // covers sessions abandoned without close().
        let simulator = self.owned.simulator.clone();
        let work_dir = self.owned.work_dir.clone();
        std::thread::spawn(move || {
            for arguments in [["shutdown", &simulator], ["delete", &simulator]] {
                let _ = std::process::Command::new("xcrun")
                    .arg("simctl")
                    .args(arguments)
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            let _ = fs::remove_dir_all(&work_dir);
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
