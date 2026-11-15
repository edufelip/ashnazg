use std::{path::PathBuf, time::Duration};

use anyhow::{Context, bail};
use clap::{Parser, Subcommand};
use serde_json::Value;

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
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .with_writer(std::io::stderr)
        .init();
    match Cli::parse().command {
        None | Some(CommandLine::Mcp { http: None }) => ashnazg_stdio().await,
        Some(CommandLine::Mcp { http: Some(bind) }) => {
            let config = ashnazg::HostConfig::from_env()?;
            ashnazg::mcp::serve_http(config, &bind).await
        }
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
    }
}

async fn ashnazg_stdio() -> anyhow::Result<()> {
    let config = ashnazg::HostConfig::from_env()?;
    ashnazg::mcp::serve_stdio(config).await
}
