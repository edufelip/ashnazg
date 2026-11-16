#![cfg(unix)]

use std::process::Stdio;
use std::time::Duration;

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use anyhow::{Context, ensure};
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use tokio::process::Child;
use tokio::process::Command;

fn cli() -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_ashnazg"));
    // No HOME means even a stamped test binary must fail before acquisition.
    // These actual-CLI fixtures never download assets or launch a VM.
    command
        .env_clear()
        .arg("mcp")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    command
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
async fn terminate(mut child: Child) -> anyhow::Result<std::process::ExitStatus> {
    if child.try_wait()?.is_none() {
        let pid = child.id().context("owned CLI process missing")?;
        // SAFETY: this is the fixture's still-owned child, not a supplied PID.
        ensure!(
            unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) } == 0,
            "cannot terminate owned CLI"
        );
    }
    match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
        Ok(status) => Ok(status?),
        Err(_) => {
            child.start_kill()?;
            child.wait().await?;
            anyhow::bail!("CLI required forced stop instead of joined signal cleanup")
        }
    }
}

#[tokio::test]
async fn explicit_invalid_token_does_not_fall_back_to_native() {
    use std::os::unix::ffi::OsStringExt;
    for value in [
        std::ffi::OsString::new(),
        std::ffi::OsString::from_vec(vec![0xff]),
    ] {
        let output = tokio::time::timeout(
            Duration::from_secs(5),
            cli().env("ASHNAZG_TOKEN", value).output(),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(error.contains("ASHNAZG_TOKEN"));
        assert!(!error.contains("native runtime initialization"));
    }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
async fn reply(
    reader: &mut BufReader<tokio::process::ChildStdout>,
    id: u64,
) -> anyhow::Result<serde_json::Value> {
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let mut line = String::new();
            ensure!(
                reader.read_line(&mut line).await? > 0,
                "CLI closed before MCP reply"
            );
            let value: serde_json::Value = serde_json::from_str(&line)?;
            if value["id"] == id {
                return Ok(value);
            }
        }
    })
    .await
    .context("MCP handshake waited for runtime startup")?
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn native_cli_serves_protocol_before_unavailable_runtime_and_joins_sigterm() {
    let mut child = cli().spawn().unwrap();
    let outcome: anyhow::Result<()> = async {
        let mut input = child.stdin.take().context("CLI stdin missing")?;
        let mut output = BufReader::new(child.stdout.take().context("CLI stdout missing")?);
        input.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"cli-fixture\",\"version\":\"1\"}}}\n").await?;
        ensure!(reply(&mut output, 1).await?["result"].is_object(), "initialize failed");
        input.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}\n").await?;
        ensure!(reply(&mut output, 2).await?["result"]["tools"].as_array().is_some_and(|tools| tools.len() == 5), "tool contract missing while runtime unavailable");
        input.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"ui_snapshot\",\"arguments\":{}}}\n").await?;
        let failure = reply(&mut output, 3).await?;
        ensure!(failure["error"].is_object() || failure["result"]["isError"] == true, "runtime unavailable must not return a successful tool result");
        // Keep the client input open across signal cleanup; EOF is not what
        // unblocks inherited stdin in this fixture.
        let status = terminate_signal(&mut child).await?;
        ensure!(status.code() == Some(1), "unavailable runtime must exit with its startup failure after joined signal cleanup: {status}");
        drop(input);
        Ok(())
    }.await;
    let cleanup = terminate(child).await;
    outcome.unwrap();
    cleanup.unwrap();
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
async fn terminate_signal(child: &mut Child) -> anyhow::Result<std::process::ExitStatus> {
    let pid = child.id().context("CLI exited before signal cleanup")?;
    ensure!(
        unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) } == 0,
        "owned signal failed"
    );
    tokio::time::timeout(Duration::from_secs(5), child.wait())
        .await
        .context("native CLI signal cleanup timed out")?
        .context("cannot reap CLI")
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn native_cli_sigterm_cancels_a_test_owned_terminal_without_enter() {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let (mut master, mut slave) = (-1, -1);
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0
    );
    let master = unsafe { OwnedFd::from_raw_fd(master) };
    let slave = unsafe { OwnedFd::from_raw_fd(slave) };
    for fd in [master.as_raw_fd(), slave.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) },
            0
        );
    }
    let mut child = cli()
        .stdin(std::fs::File::from(slave.try_clone().unwrap()))
        .stdout(std::fs::File::from(slave))
        .spawn()
        .unwrap();
    let outcome: anyhow::Result<()> = async {
        let mut errors = BufReader::new(child.stderr.take().context("CLI stderr missing")?);
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let mut line = String::new();
                ensure!(
                    errors.read_line(&mut line).await? > 0,
                    "CLI closed before starting its initializer"
                );
                if line.contains("native runtime initialization failed") {
                    return Ok::<(), anyhow::Error>(());
                }
            }
        })
        .await
        .context("native initializer did not start while terminal input was idle")??;
        let status = terminate_signal(&mut child).await?;
        ensure!(
            status.code() == Some(1),
            "unavailable runtime must preserve its failure after terminal cleanup: {status}"
        );
        Ok(())
    }
    .await;
    let cleanup = terminate(child).await;
    drop(master);
    outcome.unwrap();
    cleanup.unwrap();
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tokio::test]
async fn native_http_rejects_listener_or_bearer_before_initialization() {
    for (bind, token) in [
        ("0.0.0.0:0", "test-bearer"),
        ("127.0.0.1:0", ""),
        ("127.0.0.1:0", "bad\nheader"),
    ] {
        let result = tokio::time::timeout(
            Duration::from_secs(5),
            cli()
                .args(["--http", bind])
                .env("ASHNAZG_HTTP_TOKEN", token)
                .output(),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!result.status.success());
        assert!(result.stdout.is_empty());
        assert!(
            !String::from_utf8_lossy(&result.stderr)
                .contains("native runtime initialization failed")
        );
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let result = tokio::time::timeout(
        Duration::from_secs(5),
        cli()
            .args(["--http", &listener.local_addr().unwrap().to_string()])
            .env("ASHNAZG_HTTP_TOKEN", "test-bearer")
            .output(),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(!result.status.success());
    assert!(
        !String::from_utf8_lossy(&result.stderr).contains("native runtime initialization failed")
    );
}
