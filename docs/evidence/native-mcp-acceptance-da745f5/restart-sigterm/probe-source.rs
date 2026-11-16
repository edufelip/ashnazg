use std::{fs::File, io::{Read, Write}, net::{Ipv4Addr, SocketAddr}, path::{Path, PathBuf}, time::{Duration, Instant}};
use ashnazg::native_session::{NativeSession, NativeSessionLaunch};
use serde::Deserialize;
use tokio_util::sync::CancellationToken;

const HOST_SOURCE: &str = env!("ASHNAZG_PROOF_SOURCE_SHA");

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    host_source_sha: String,
    helper: PathBuf,
    helper_sha256: String,
    assets: PathBuf,
    state_dir: PathBuf,
    agent_apk: PathBuf,
    agent_apk_sha256: String,
    manifest_source_sha: String,
    adb_port: u16,
    agent_port: u16,
    install_port: u16,
    startup_timeout_secs: u16,
    http_port: u16,
    http_bearer: String,
    shutdown_marker: PathBuf,
}

fn emit(value: serde_json::Value) {
    println!("{value}");
    std::io::stdout().flush().unwrap();
}

async fn marker(path: &Path) {
    loop {
        if path.try_exists().unwrap_or(false) { return; }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

async fn run() -> Result<(), &'static str> {
    let path = std::env::args_os().nth(1).ok_or("configuration")?;
    let mut bytes = Vec::new();
    File::open(path).map_err(|_| "configuration")?.take(32 * 1024 + 1).read_to_end(&mut bytes).map_err(|_| "configuration")?;
    if bytes.len() > 32 * 1024 { return Err("configuration"); }
    let c: Config = serde_json::from_slice(&bytes).map_err(|_| "configuration")?;
    if c.host_source_sha != HOST_SOURCE { return Err("source_binding"); }
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).map_err(|_| "signal_setup")?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt()).map_err(|_| "signal_setup")?;
    let signal_notice = CancellationToken::new();
    let notice = signal_notice.clone();
    let signal_task = tokio::spawn(async move {
        tokio::select! { _ = term.recv() => {}, _ = interrupt.recv() => {} }
        notice.cancel();
    });
    let listener = tokio::net::TcpListener::bind(SocketAddr::from((Ipv4Addr::LOCALHOST,c.http_port))).await.map_err(|_| "http_bind")?;
    let http = listener.local_addr().map_err(|_| "http_bind")?;
    let parent = CancellationToken::new();
    let session = {
        let start = NativeSession::start(NativeSessionLaunch {
            helper: c.helper, helper_sha256: c.helper_sha256, assets: c.assets,
            state_dir: c.state_dir, agent_apk: c.agent_apk, agent_apk_sha256: c.agent_apk_sha256,
            source_sha: c.manifest_source_sha, adb_port: c.adb_port, agent_port: c.agent_port,
            install_port: c.install_port, startup_timeout_secs: c.startup_timeout_secs,
        }, Instant::now() + Duration::from_secs(u64::from(c.startup_timeout_secs)), parent.clone());
        tokio::pin!(start);
        tokio::select! {
            biased;
            _ = signal_notice.cancelled() => { parent.cancel(); start.await },
            value = &mut start => value,
        }.map_err(|_| "session_start")?
    };
    let pid = session.helper_pid();
    let id = session.session_id().to_owned();
    let adb = session.adb_addr();
    let serving_cancel = CancellationToken::new();
    let serving = ashnazg::mcp::serve_native_http(session, listener, c.http_bearer, serving_cancel.clone());
    tokio::pin!(serving);
    emit(serde_json::json!({"schema_version":1,"status":"ready","host_source_sha":HOST_SOURCE,
        "session_id":id,"helper_pid":pid,"adb_listen":adb.to_string(),"http_listen":http.to_string()}));
    let result = tokio::select! {
        value = &mut serving => value,
        _ = marker(&c.shutdown_marker) => { serving_cancel.cancel(); serving.await },
        _ = signal_notice.cancelled() => { serving_cancel.cancel(); serving.await },
    };
    signal_task.abort();
    let _ = signal_task.await;
    emit(serde_json::json!({"schema_version":1,"status":"stopped","host_source_sha":HOST_SOURCE,
        "session_id":id,"helper_pid":pid,"serving_and_cleanup_ok":result.is_ok(),
        "parent_cancelled":parent.is_cancelled(),"interrupted":signal_notice.is_cancelled()}));
    result.map_err(|_| "native_mcp")
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    match run().await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(phase) => { emit(serde_json::json!({"schema_version":1,"status":"failed","phase":phase})); std::process::ExitCode::from(2) }
    }
}
