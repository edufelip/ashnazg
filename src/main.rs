use std::{path::PathBuf, time::Duration};

use anyhow::{Context, bail};
use clap::{Parser, Subcommand};
use futures_util::FutureExt;
use serde_json::Value;
use tokio_util::sync::CancellationToken;

mod microvm;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum McpBackend {
    External,
    Native,
}

fn select_mcp_backend(token_present: bool, os: &str, arch: &str) -> anyhow::Result<McpBackend> {
    if token_present {
        return Ok(McpBackend::External);
    }
    if os == "macos" && arch == "aarch64" {
        return Ok(McpBackend::Native);
    }
    bail!("MCP requires ASHNAZG_TOKEN for external Android; native startup requires macOS/aarch64")
}

#[derive(Parser)]
#[command(
    name = "ashnazg",
    version,
    about = "Host control CLI and MCP server for Ashnazg Android agent"
)]
struct Cli {
    #[command(subcommand)]
    command: Option<CommandLine>,
}

#[derive(Subcommand)]
enum CommandLine {
    /// Run the MCP server. Defaults to stdio; --http starts authenticated Streamable HTTP.
    Mcp {
        #[arg(long, value_name = "LOOPBACK:PORT")]
        http: Option<String>,
        /// Serve stdio MCP for the live iOS fixture session (macOS only).
        #[arg(long)]
        ios_fixture: bool,
    },
    /// Perform one guest JSON-RPC call.
    Rpc {
        method: String,
        #[arg(default_value = "{}")]
        params: String,
    },
    /// Install an APK through adb on the explicit ANDROID_SERIAL device.
    Install {
        apk: PathBuf,
        #[arg(long)]
        launch: Option<String>,
    },
    /// Start the native macOS Apple Silicon Linux VM and loopback ADB relay.
    Up(microvm::UpArgs),
}

fn main() -> anyhow::Result<()> {
    #[cfg(unix)]
    match ashnazg::stdio_bridge::hidden_child_fd(std::env::args_os()) {
        Ok(Some(fd)) => {
            if ashnazg::stdio_bridge::run_hidden_child(fd).is_err() {
                std::process::exit(1);
            }
            return Ok(());
        }
        Err(_) => std::process::exit(1),
        Ok(None) => {}
    }
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(run())
}

async fn run() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .with_writer(std::io::stderr)
        .init();
    match Cli::parse().command {
        None => serve_mcp(None, false).await,
        Some(CommandLine::Mcp { http, ios_fixture }) => serve_mcp(http, ios_fixture).await,
        Some(CommandLine::Rpc { method, params }) => {
            let params: Value =
                serde_json::from_str(&params).context("params must be valid JSON")?;
            if !params.is_object() {
                bail!("RPC params must be a JSON object");
            }
            let config = ashnazg::HostConfig::from_env()?;
            let result =
                tokio::time::timeout(Duration::from_secs(35), config.guest.call(&method, params))
                    .await
                    .context("RPC timed out")??;
            println!("{}", serde_json::to_string(&result)?);
            Ok(())
        }
        Some(CommandLine::Install { apk, launch }) => {
            let adb = std::env::var("ASHNAZG_ADB").unwrap_or_else(|_| "adb".into());
            let result = ashnazg::app_install(&adb, &apk, launch.is_some(), launch.as_deref())?;
            println!("{}", serde_json::to_string(&result)?);
            Ok(())
        }
        Some(CommandLine::Up(args)) => match microvm::run(args) {
            Ok(status) if status.success() => Ok(()),
            Ok(status) => microvm::finish(status),
            Err(error) => Err(error),
        },
    }
}

async fn serve_mcp(http: Option<String>, ios_fixture: bool) -> anyhow::Result<()> {
    if ios_fixture {
        if http.is_some() {
            bail!("--ios-fixture serves stdio only");
        }
        #[cfg(target_os = "macos")]
        {
            let session = std::sync::Arc::new(ashnazg::ios_fixture::FixtureSession::open().await?);
            let result = ashnazg::mcp::serve_stdio_ios(std::sync::Arc::clone(&session)).await;
            session.close().await?;
            result
        }
        #[cfg(not(target_os = "macos"))]
        {
            bail!("--ios-fixture requires macOS with Xcode and an iOS Simulator runtime");
        }
    } else {
        let backend = select_mcp_backend(
            std::env::var_os("ASHNAZG_TOKEN").is_some(),
            std::env::consts::OS,
            std::env::consts::ARCH,
        )?;
        match backend {
            McpBackend::External => {
                let config = ashnazg::HostConfig::from_env()?;
                match http {
                    Some(bind) => ashnazg::mcp::serve_http(config, &bind).await,
                    None => ashnazg::mcp::serve_stdio(config).await,
                }
            }
            McpBackend::Native => match http {
                Some(bind) => serve_native_http(&bind).await,
                None => serve_native_stdio().await,
            },
        }
    }
}

async fn initialize_native(
    cancel: CancellationToken,
) -> anyhow::Result<ashnazg::native_session::NativeSession> {
    let result = async {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .context("native runtime requires an existing HOME directory")?;
        let config = ashnazg::native_bootstrap::config_for_user(
            home,
            ashnazg::build_identity::runtime_source_sha(),
        )?;
        ashnazg::native_bootstrap::start_native(config, cancel).await
    }
    .await;
    if let Err(error) = &result {
        tracing::error!(error = %error, "native runtime initialization failed");
    }
    result
}

struct McpSignals {
    cancellation: CancellationToken,
    stop: CancellationToken,
    task: Option<tokio::task::JoinHandle<()>>,
}

impl McpSignals {
    fn start() -> anyhow::Result<Self> {
        use tokio::signal::unix::{SignalKind, signal};
        // Register before spawning any transport/runtime child, including
        // before the signal task's first poll.
        let mut interrupt = signal(SignalKind::interrupt())?;
        let mut terminate = signal(SignalKind::terminate())?;
        let cancellation = CancellationToken::new();
        let stop = CancellationToken::new();
        let worker_stop = stop.clone();
        let worker_cancel = cancellation.clone();
        let task = tokio::spawn(async move {
            tokio::select! {
                biased;
                _ = worker_stop.cancelled() => {}
                _ = interrupt.recv() => worker_cancel.cancel(),
                _ = terminate.recv() => worker_cancel.cancel(),
            }
        });
        Ok(Self {
            cancellation,
            stop,
            task: Some(task),
        })
    }

    async fn finish(mut self) -> anyhow::Result<()> {
        self.stop.cancel();
        if let Some(task) = self.task.take() {
            task.await.context("MCP signal owner failed")?;
        }
        Ok(())
    }
}

impl Drop for McpSignals {
    fn drop(&mut self) {
        self.stop.cancel();
    }
}

async fn serve_native_stdio() -> anyhow::Result<()> {
    use std::os::fd::AsFd;
    let executable = std::env::current_exe()?;
    let signals = McpSignals::start()?;
    let bridge = ashnazg::stdio_bridge::OwnedStdioBridge::spawn(
        &executable,
        std::io::stdin().as_fd(),
        std::io::stdout().as_fd(),
        signals.cancellation.clone(),
    );
    let mut bridge = match bridge {
        Ok(bridge) => bridge,
        Err(_) => {
            signals.finish().await?;
            bail!("native MCP stdio descriptors or bridge startup unavailable");
        }
    };
    // Keep the transport owner outside the serving unwind boundary, then join
    // it even when protocol construction or polling fails.
    let serving = std::panic::AssertUnwindSafe(async {
        let (read, write) = bridge.take_io()?;
        ashnazg::mcp::serve_initializing_native_io(
            initialize_native,
            read,
            write,
            signals.cancellation.clone(),
        )
        .await
    })
    .catch_unwind()
    .await
    .unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")));
    let transport = bridge.shutdown().await.map(|_| ());
    let signal_cleanup = signals.finish().await;
    finish_native(serving, transport, signal_cleanup)
}

async fn serve_native_http(bind: &str) -> anyhow::Result<()> {
    let address = ashnazg::validate_loopback_addr(bind)?;
    let bearer = std::env::var("ASHNAZG_HTTP_TOKEN")
        .context("ASHNAZG_HTTP_TOKEN is required for HTTP MCP")?;
    if bearer.is_empty()
        || bearer.len() > 4096
        || axum::http::HeaderValue::from_str(&bearer).is_err()
    {
        bail!("ASHNAZG_HTTP_TOKEN must be a valid nonempty bounded header value");
    }
    // Binding and bearer validation precede the initializer, so a bad listener
    // cannot acquire artifacts or start a VM.
    let listener = tokio::net::TcpListener::bind(address).await?;
    let bound_address = listener.local_addr()?;
    let signals = McpSignals::start()?;
    tracing::info!(address = %bound_address, endpoint = "/mcp", "native MCP Streamable HTTP listening");
    let serving = std::panic::AssertUnwindSafe(ashnazg::mcp::serve_initializing_native_http(
        initialize_native,
        listener,
        bearer,
        signals.cancellation.clone(),
    ))
    .catch_unwind()
    .await
    .unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")));
    let signal_cleanup = signals.finish().await;
    finish_native(serving, Ok(()), signal_cleanup)
}

fn finish_native(
    serving: anyhow::Result<()>,
    transport: anyhow::Result<()>,
    signals: anyhow::Result<()>,
) -> anyhow::Result<()> {
    let mut failures = Vec::new();
    if serving.is_err() {
        failures.push("native MCP serving failed");
    }
    if transport.is_err() {
        failures.push("stdio bridge cleanup failed");
    }
    if signals.is_err() {
        failures.push("MCP signal owner cleanup failed");
    }
    if failures.is_empty() {
        Ok(())
    } else {
        bail!(failures.join("; "))
    }
}

#[cfg(test)]
mod backend_tests {
    use super::*;

    #[test]
    fn explicit_token_presence_selects_external_on_every_platform() {
        for (os, arch) in [
            ("macos", "aarch64"),
            ("linux", "x86_64"),
            ("windows", "x86_64"),
        ] {
            assert_eq!(
                select_mcp_backend(true, os, arch).unwrap(),
                McpBackend::External
            );
        }
    }

    #[test]
    fn absent_token_selects_native_only_on_apple_silicon_macos() {
        assert_eq!(
            select_mcp_backend(false, "macos", "aarch64").unwrap(),
            McpBackend::Native
        );
        for (os, arch) in [
            ("macos", "x86_64"),
            ("linux", "aarch64"),
            ("linux", "x86_64"),
        ] {
            assert!(select_mcp_backend(false, os, arch).is_err());
        }
    }
}
