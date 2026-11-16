//! Bounded installation over a caller-owned helper session's loopback relay.
//!
//! The session owner must validate the private credentials and bind them to its
//! spawned helper. This client validates wire shape, not helper process identity,
//! and never discovers credentials or runs host ADB.

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::FileExt;
    use std::time::{Duration, Instant};
    use tokio_util::sync::CancellationToken;

    fn fixture_connection(listener: &TcpListener) -> TcpStream {
        listener.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        let stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        Instant::now() < deadline,
                        "fixture never received a connection"
                    );
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(error) => panic!("fixture accept failed: {error}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
    }

    #[test]
    fn final_eof_after_deadline_is_not_success() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let mut client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        client.set_nonblocking(true).unwrap();
        let (mut peer, _) = listener.accept().unwrap();
        peer.write_all(b"{\"schema_version\":1,\"status\":\"installed\"}\n")
            .unwrap();
        let deadline = Instant::now() + Duration::from_millis(75);
        let server = std::thread::spawn(move || {
            std::thread::sleep(
                (deadline + Duration::from_millis(10)).saturating_duration_since(Instant::now()),
            );
            drop(peer);
        });
        let result = read_final_frame(
            &mut client,
            deadline,
            deadline,
            &deadline,
            &CancellationToken::new(),
        );
        server.join().unwrap();
        assert!(
            result.is_err(),
            "EOF arriving after the absolute deadline was accepted"
        );
    }

    #[tokio::test]
    async fn installs_only_after_ack_and_requires_matching_reply_and_eof() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let session = "123e4567-e89b-12d3-a456-426614174000".to_owned();
        let token = "a".repeat(64);
        let server_session = session.clone();
        let server_token = token.clone();
        let apk = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(apk.path(), b"fixture apk").unwrap();
        let path = apk.path().to_path_buf();
        let server = std::thread::spawn(move || {
            let mut stream = fixture_connection(&listener);
            let mut header = Vec::new();
            loop {
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                header.push(byte[0]);
                if byte[0] == b'\n' {
                    break;
                }
                assert!(header.len() < 4096);
            }
            let parsed: serde_json::Value =
                serde_json::from_slice(&header[..header.len() - 1]).unwrap();
            assert_eq!(parsed["op"], "install");
            assert_eq!(parsed["session_id"], server_session);
            assert_eq!(parsed["install_token"], server_token);
            stream
                .set_read_timeout(Some(Duration::from_millis(40)))
                .unwrap();
            let mut before_ack = [0u8; 1];
            assert!(
                matches!(stream.read(&mut before_ack), Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock || e.kind() == std::io::ErrorKind::TimedOut)
            );
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            for byte in b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n" {
                stream.write_all(&[*byte]).unwrap();
            }
            let mut body = Vec::new();
            stream.read_to_end(&mut body).unwrap();
            assert_eq!(body, b"fixture apk");
            let reply = format!(
                "{{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"{}\",\"launched\":false}}\n",
                server_session
            );
            for byte in reply.as_bytes() {
                stream.write_all(&[*byte]).unwrap();
            }
        });
        let client = NativeInstallClient::new(endpoint, session, token).unwrap();
        let result = client
            .install(
                path,
                None,
                Instant::now() + Duration::from_secs(5),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        assert!(!result.launched);
        assert_eq!(result.package, None);
        server.join().unwrap();
    }

    #[test]
    fn rejects_invalid_inputs_before_opening_a_connection() {
        let endpoint = "127.0.0.1:1".parse().unwrap();
        assert!(
            NativeInstallClient::new(
                "0.0.0.0:1".parse().unwrap(),
                "123e4567-e89b-12d3-a456-426614174000".into(),
                "a".repeat(64)
            )
            .is_err()
        );
        assert!(
            NativeInstallClient::new(
                endpoint,
                "123E4567-E89B-12D3-A456-426614174000".into(),
                "a".repeat(64)
            )
            .is_err()
        );
        assert!(
            NativeInstallClient::new(
                endpoint,
                "123e4567-e89b-12d3-a456-426614174000".into(),
                "A".repeat(64)
            )
            .is_err()
        );
        assert!(valid_package("com.good_name.app2"));
        assert!(!valid_package("com..app"));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn refuses_symlink_fifo_and_empty_apks_without_connecting() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let empty = dir.path().join("empty");
        File::create(&empty).unwrap();
        let oversized = dir.path().join("oversized");
        File::create(&oversized)
            .unwrap()
            .set_len(MAX_APK_SIZE + 1)
            .unwrap();
        let target = dir.path().join("target");
        std::fs::write(&target, b"apk").unwrap();
        let link = dir.path().join("link");
        std::os::unix::fs::symlink(&target, &link).unwrap();
        let fifo = dir.path().join("fifo");
        assert_eq!(
            unsafe {
                libc::mkfifo(
                    std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes())
                        .unwrap()
                        .as_ptr(),
                    0o600,
                )
            },
            0
        );
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        for path in [empty, link, fifo, oversized] {
            let result = client
                .install(
                    path,
                    None,
                    Instant::now() + Duration::from_secs(1),
                    CancellationToken::new(),
                )
                .await;
            assert!(result.is_err());
        }
        assert!(
            matches!(listener.accept(), Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock)
        );
    }

    #[tokio::test]
    async fn error_reply_never_reflects_guest_text_or_token() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let secret = "sentinel-secret-".to_owned() + &"b".repeat(64);
        let token = "c".repeat(64);
        let token_for_server = token.clone();
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            let mut line = Vec::new();
            while !line.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                line.push(b[0]);
            }
            assert!(String::from_utf8_lossy(&line).contains(&token_for_server));
            s.write_all(
                format!("{{\"schema_version\":1,\"status\":\"error\",\"error\":\"{secret}\"}}\n")
                    .as_bytes(),
            )
            .unwrap();
            let mut rejected_body = Vec::new();
            s.read_to_end(&mut rejected_body).unwrap();
            assert!(rejected_body.is_empty());
        });
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            token.clone(),
        )
        .unwrap();
        let apk = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(apk.path(), b"apk").unwrap();
        let result = client
            .install(
                apk.path().to_path_buf(),
                None,
                Instant::now() + Duration::from_secs(2),
                CancellationToken::new(),
            )
            .await;
        let error = result.unwrap_err().to_string();
        assert!(!error.contains("sentinel-secret"));
        assert!(!error.contains(&token));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn replacing_apk_path_does_not_switch_the_opened_file() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (acked_tx, acked_rx) = std::sync::mpsc::sync_channel(1);
        let (continue_tx, continue_rx) = std::sync::mpsc::sync_channel(1);
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            let mut line = Vec::new();
            while !line.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                line.push(b[0]);
            }
            acked_tx.send(()).unwrap();
            continue_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            s.write_all(b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n")
                .unwrap();
            let mut body = Vec::new();
            s.read_to_end(&mut body).unwrap();
            assert_eq!(body, b"original apk bytes");
            s.write_all(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"123e4567-e89b-12d3-a456-426614174000\",\"launched\":false}\n").unwrap();
        });
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        let dir = tempfile::tempdir().unwrap();
        let apk = dir.path().join("app.apk");
        std::fs::write(&apk, b"original apk bytes").unwrap();
        let task = tokio::spawn({
            let client = client;
            let apk = apk.clone();
            async move {
                client
                    .install(
                        apk,
                        None,
                        Instant::now() + Duration::from_secs(3),
                        CancellationToken::new(),
                    )
                    .await
            }
        });
        tokio::task::spawn_blocking(move || acked_rx.recv_timeout(Duration::from_secs(2)).unwrap())
            .await
            .unwrap();
        let moved = dir.path().join("old.apk");
        std::fs::rename(&apk, &moved).unwrap();
        std::fs::write(&apk, b"replacement bytes").unwrap();
        continue_tx.send(()).unwrap();
        task.await
            .unwrap()
            .unwrap_or_else(|e| panic!("install failed: {e}"));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn observed_in_place_mutation_is_refused_before_payload() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (acked_tx, acked_rx) = std::sync::mpsc::sync_channel(1);
        let (continue_tx, continue_rx) = std::sync::mpsc::sync_channel(1);
        let (body_tx, body_rx) = std::sync::mpsc::sync_channel(1);
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            let mut line = Vec::new();
            while !line.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                line.push(b[0]);
            }
            acked_tx.send(()).unwrap();
            continue_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            s.write_all(b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n")
                .unwrap();
            let mut body = Vec::new();
            let _ = s.read_to_end(&mut body);
            body_tx.send(body).unwrap();
        });
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        let apk = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(apk.path(), b"original").unwrap();
        let file = OpenOptions::new().write(true).open(apk.path()).unwrap();
        let task = tokio::spawn({
            let client = client;
            let path = apk.path().to_path_buf();
            async move {
                client
                    .install(
                        path,
                        None,
                        Instant::now() + Duration::from_secs(3),
                        CancellationToken::new(),
                    )
                    .await
            }
        });
        tokio::task::spawn_blocking(move || acked_rx.recv_timeout(Duration::from_secs(2)).unwrap())
            .await
            .unwrap();
        file.write_at(b"mutated!", 0).unwrap();
        continue_tx.send(()).unwrap();
        assert!(task.await.unwrap().is_err());
        assert!(
            body_rx
                .recv_timeout(Duration::from_secs(2))
                .unwrap()
                .is_empty()
        );
        server.join().unwrap();
    }

    #[tokio::test]
    async fn dropping_call_cancels_only_its_worker_and_closes_owned_connection() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (header_tx, header_rx) = std::sync::mpsc::sync_channel(1);
        let (closed_tx, closed_rx) = std::sync::mpsc::sync_channel(1);
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            let mut line = Vec::new();
            while !line.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                line.push(b[0]);
            }
            header_tx.send(()).unwrap();
            let mut tail = Vec::new();
            let _ = s.read_to_end(&mut tail);
            closed_tx.send(tail).unwrap();
        });
        let parent = CancellationToken::new();
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        let apk = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(apk.path(), b"apk").unwrap();
        let task = tokio::spawn({
            let parent = parent.clone();
            async move {
                client
                    .install(
                        apk.path().to_path_buf(),
                        None,
                        Instant::now() + Duration::from_secs(3),
                        parent,
                    )
                    .await
            }
        });
        tokio::task::spawn_blocking(move || {
            header_rx.recv_timeout(Duration::from_secs(2)).unwrap()
        })
        .await
        .unwrap();
        task.abort();
        tokio::task::spawn_blocking(move || {
            closed_rx.recv_timeout(Duration::from_secs(2)).unwrap()
        })
        .await
        .unwrap();
        assert!(!parent.is_cancelled());
        server.join().unwrap();
    }

    #[tokio::test]
    async fn stalled_ack_obeys_absolute_upload_deadline() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            let mut line = Vec::new();
            while !line.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                line.push(b[0]);
            }
            let mut rest = Vec::new();
            let _ = s.read_to_end(&mut rest);
        });
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        let apk = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(apk.path(), b"apk").unwrap();
        let started = Instant::now();
        let result = client
            .install(
                apk.path().to_path_buf(),
                None,
                started + Duration::from_millis(180),
                CancellationToken::new(),
            )
            .await;
        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(1));
        server.join().unwrap();
    }

    #[tokio::test]
    async fn upload_backpressure_expires_without_replaying_on_another_connection() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let endpoint = listener.local_addr().unwrap();
        let size: libc::c_int = 4096;
        unsafe {
            assert_eq!(
                libc::setsockopt(
                    std::os::fd::AsRawFd::as_raw_fd(&listener),
                    libc::SOL_SOCKET,
                    libc::SO_RCVBUF,
                    &size as *const _ as *const libc::c_void,
                    std::mem::size_of_val(&size) as libc::socklen_t,
                ),
                0
            );
        }
        let (drain_tx, drain_rx) = std::sync::mpsc::sync_channel(1);
        let server = std::thread::spawn(move || {
            let mut s = fixture_connection(&listener);
            listener.set_nonblocking(true).unwrap();
            s.set_nonblocking(false).unwrap();
            let mut header = Vec::new();
            while !header.ends_with(b"\n") {
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                header.push(b[0]);
            }
            s.write_all(b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n")
                .unwrap();
            // Keep the receiver stalled until the client has expired. Reading
            // immediately would test throughput rather than backpressure.
            drain_rx.recv_timeout(Duration::from_secs(3)).unwrap();
            let mut partial = Vec::new();
            s.read_to_end(&mut partial).unwrap();
            let second_connection = listener.accept();
            (partial.len(), second_connection)
        });
        let client = NativeInstallClient::new(
            endpoint,
            "123e4567-e89b-12d3-a456-426614174000".into(),
            "a".repeat(64),
        )
        .unwrap();
        let apk = tempfile::NamedTempFile::new().unwrap();
        let upload_size = 8 * 1024 * 1024;
        apk.as_file().set_len(upload_size).unwrap();
        let started = Instant::now();
        let result = client
            .install(
                apk.path().to_path_buf(),
                None,
                started + Duration::from_secs(1),
                CancellationToken::new(),
            )
            .await;
        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(2));
        drain_tx.send(()).unwrap();
        let (partial, second) = server.join().unwrap();
        assert!(partial < upload_size as usize);
        assert!(matches!(second, Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock));
    }

    #[test]
    fn response_codec_rejects_duplicate_unknown_wrongly_typed_and_wrong_session_fields() {
        assert!(
            parse_ack(
                b"{\"schema_version\":1,\"schema_version\":1,\"status\":\"ready_for_payload\"}\n"
            )
            .is_err()
        );
        assert!(
            parse_ack(b"{\"schema_version\":true,\"status\":\"ready_for_payload\"}\n").is_err()
        );
        assert!(
            parse_ack(b"{\"schema_version\":1,\"status\":\"ready_for_payload\",\"extra\":0}\n")
                .is_err()
        );
        assert!(parse_success(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"wrong\",\"launched\":false}\n", "right", None).is_err());
        assert!(parse_success(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"right\",\"launched\":0}\n", "right", None).is_err());
        assert!(parse_success(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"right\",\"launched\":false}\n", "right", Some("com.example.app".into())).is_err());
        assert!(parse_success(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"right\",\"launched\":false}\ntrailing", "right", None).is_err());
    }
}
// Authenticated APK installation over the already-owned loopback relay.

use anyhow::{Result, anyhow, bail};
use serde::de::{MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::time::{Duration, Instant};
use tokio_util::sync::CancellationToken;
use zeroize::{Zeroize, Zeroizing};

const MAX_APK_SIZE: u64 = 128 * 1024 * 1024;
const MAX_FRAME: usize = 4096;
const MAX_CHUNK: usize = 64 * 1024;
const IO_WAIT: Duration = Duration::from_millis(50);
const CONNECT_MAX: Duration = Duration::from_secs(5);

/// Client for one install operation on the caller-selected authenticated session relay.
pub struct NativeInstallClient {
    endpoint: SocketAddr,
    session_id: String,
    install_token: Zeroizing<String>,
}

impl std::fmt::Debug for NativeInstallClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("NativeInstallClient")
            .field("endpoint", &self.endpoint)
            .field("session_id", &self.session_id)
            .field("install_token", &"<redacted>")
            .finish()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeInstallResult {
    pub launched: bool,
    pub package: Option<String>,
}

impl NativeInstallClient {
    pub fn new(endpoint: SocketAddr, session_id: String, install_token: String) -> Result<Self> {
        let install_token = Zeroizing::new(install_token);
        if !endpoint.ip().is_loopback() || endpoint.port() == 0 {
            bail!("invalid native install endpoint");
        }
        if !canonical_uuid(&session_id) {
            bail!("invalid native install session id");
        }
        if !valid_hex(&install_token, 64) {
            bail!("invalid native install credential");
        }
        Ok(Self {
            endpoint,
            session_id,
            install_token,
        })
    }

    /// Installs one APK. A dropped call cancels its worker; connect cancellation is bounded
    /// by the single `connect_timeout` call (at most five seconds), while connected I/O
    /// checks cancellation between waits of at most 50 ms.
    pub async fn install(
        &self,
        apk: PathBuf,
        launch_package: Option<String>,
        deadline: Instant,
        cancel: CancellationToken,
    ) -> Result<NativeInstallResult> {
        let call_started = Instant::now();
        if launch_package.as_ref().is_some_and(|p| !valid_package(p)) {
            bail!("invalid native install package");
        }
        if deadline <= Instant::now() || cancel.is_cancelled() {
            bail!("native install cancelled or expired");
        }
        let child = cancel.child_token();
        let guard = CancelOnDrop(child.clone());
        let endpoint = self.endpoint;
        let session_id = self.session_id.clone();
        let secret = Zeroizing::new(self.install_token.to_string());
        let task = tokio::task::spawn_blocking(move || {
            install_worker(WorkerRequest {
                endpoint,
                session: session_id,
                token: secret,
                path: apk,
                package: launch_package,
                deadline,
                started: call_started,
                cancel: child,
            })
        });
        let result = task
            .await
            .map_err(|_| anyhow!("native install worker failed"))?;
        drop(guard);
        result
    }
}

struct CancelOnDrop(CancellationToken);
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

pub(crate) fn canonical_uuid(s: &str) -> bool {
    if s.len() != 36 || !s.is_ascii() || s.bytes().any(|b| b.is_ascii_uppercase()) {
        return false;
    }
    let bytes = s.as_bytes();
    for i in [8, 13, 18, 23] {
        if bytes[i] != b'-' {
            return false;
        }
    }
    s.bytes()
        .enumerate()
        .all(|(i, b)| [8, 13, 18, 23].contains(&i) || b.is_ascii_hexdigit())
}
pub(crate) fn valid_hex(s: &str, n: usize) -> bool {
    s.len() == n
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub(crate) fn valid_package(s: &str) -> bool {
    if s.len() > 255 || !s.is_ascii() {
        return false;
    }
    let parts: Vec<_> = s.split('.').collect();
    parts.len() >= 2
        && parts.iter().all(|part| {
            let mut b = part.bytes();
            matches!(b.next(), Some(c) if c.is_ascii_alphabetic() || c == b'_')
                && b.all(|c| c.is_ascii_alphanumeric() || c == b'_')
        })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Snapshot {
    dev: u64,
    ino: u64,
    len: u64,
    mtime: i64,
    mtime_nsec: i64,
}
impl Snapshot {
    fn read(file: &File) -> Result<Self> {
        let m = file.metadata().map_err(|_| anyhow!("cannot inspect APK"))?;
        if !m.is_file() || !(1..=MAX_APK_SIZE).contains(&m.len()) {
            bail!("APK must be a regular file from 1 byte through 128 MiB");
        }
        Ok(Self {
            dev: m.dev(),
            ino: m.ino(),
            len: m.len(),
            mtime: m.mtime(),
            mtime_nsec: m.mtime_nsec(),
        })
    }
    fn unchanged(self, file: &File) -> Result<()> {
        if Self::read(file)? != self {
            bail!("APK changed during installation");
        }
        Ok(())
    }
}

fn checkpoint(
    deadline: Instant,
    upload_deadline: Instant,
    cancel: &CancellationToken,
    uploading: bool,
) -> Result<()> {
    if cancel.is_cancelled() {
        bail!("native install cancelled");
    }
    let limit = if uploading {
        upload_deadline.min(deadline)
    } else {
        deadline
    };
    if Instant::now() >= limit {
        bail!("native install deadline expired");
    }
    Ok(())
}

fn io_budget(
    deadline: Instant,
    upload: Instant,
    cancel: &CancellationToken,
    uploading: bool,
    phase: Instant,
) -> Result<Duration> {
    checkpoint(deadline, upload, cancel, uploading)?;
    let limit = phase.min(deadline);
    let limit = if uploading { limit.min(upload) } else { limit };
    let remaining = limit.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        bail!("native install deadline expired");
    }
    Ok(IO_WAIT.min(remaining))
}

fn wait_io(
    stream: &TcpStream,
    reading: bool,
    deadline: Instant,
    upload: Instant,
    cancel: &CancellationToken,
    uploading: bool,
    phase: Instant,
) -> Result<()> {
    loop {
        let budget = io_budget(deadline, upload, cancel, uploading, phase)?;
        let mut descriptor = libc::pollfd {
            fd: stream.as_raw_fd(),
            events: if reading { libc::POLLIN } else { libc::POLLOUT },
            revents: 0,
        };
        let milliseconds = budget.as_millis() + u128::from(budget.subsec_nanos() % 1_000_000 != 0);
        let result = unsafe { libc::poll(&mut descriptor, 1, milliseconds as libc::c_int) };
        let error = (result < 0).then(std::io::Error::last_os_error);
        io_budget(deadline, upload, cancel, uploading, phase)?;
        if result >= 0 {
            if descriptor.revents & libc::POLLNVAL != 0 {
                bail!("native install connection failed");
            }
            return Ok(());
        }
        if error.is_none_or(|error| error.kind() != std::io::ErrorKind::Interrupted) {
            bail!("native install connection failed");
        }
    }
}

fn open_apk(path: &PathBuf) -> Result<File> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| anyhow!("cannot open APK"))
}

struct WorkerRequest {
    endpoint: SocketAddr,
    session: String,
    token: Zeroizing<String>,
    path: PathBuf,
    package: Option<String>,
    deadline: Instant,
    started: Instant,
    cancel: CancellationToken,
}

fn install_worker(request: WorkerRequest) -> Result<NativeInstallResult> {
    let WorkerRequest {
        endpoint,
        session,
        mut token,
        path,
        package,
        deadline,
        started,
        cancel,
    } = request;
    let upload_deadline = started
        .checked_add(Duration::from_secs(30))
        .unwrap_or(deadline);
    checkpoint(deadline, upload_deadline, &cancel, true)?;
    let mut file = open_apk(&path)?;
    let before = Snapshot::read(&file)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; MAX_CHUNK];
    let mut counted = 0u64;
    loop {
        checkpoint(deadline, upload_deadline, &cancel, true)?;
        let n = file
            .read(&mut buffer)
            .map_err(|_| anyhow!("cannot read APK"))?;
        if n == 0 {
            break;
        }
        counted += n as u64;
        if counted > before.len {
            bail!("APK changed during hashing");
        }
        hasher.update(&buffer[..n]);
    }
    if counted != before.len {
        bail!("APK changed during hashing");
    }
    before.unchanged(&file)?;
    let digest = hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    file.seek(SeekFrom::Start(0))
        .map_err(|_| anyhow!("cannot read APK"))?;
    checkpoint(deadline, upload_deadline, &cancel, true)?;
    let connect_limit = deadline
        .min(upload_deadline)
        .min(Instant::now() + CONNECT_MAX);
    let connect_wait = connect_limit.saturating_duration_since(Instant::now());
    if connect_wait.is_zero() {
        bail!("native install deadline expired");
    }
    let mut stream = TcpStream::connect_timeout(&endpoint, connect_wait)
        .map_err(|_| anyhow!("native install connection failed"))?;
    stream.set_nodelay(true).ok();
    stream
        .set_nonblocking(true)
        .map_err(|_| anyhow!("native install connection failed"))?;

    let mut header = Zeroizing::new(Vec::with_capacity(512));
    header.extend_from_slice(format!("{{\"schema_version\":1,\"op\":\"install\",\"session_id\":\"{session}\",\"install_token\":\"").as_bytes());
    header.extend_from_slice(token.as_bytes());
    header.extend_from_slice(
        format!(
            "\",\"apk_size\":{},\"apk_sha256\":\"{digest}\",\"launch\":{}",
            before.len,
            package.is_some()
        )
        .as_bytes(),
    );
    if let Some(p) = package.as_ref() {
        header.extend_from_slice(format!(",\"package\":\"{p}\"").as_bytes());
    }
    header.extend_from_slice(b"}\n");
    token.zeroize();
    if header.len() > MAX_FRAME {
        bail!("native install request exceeded its bound");
    }
    write_all_bounded(
        &mut stream,
        &header,
        deadline,
        upload_deadline,
        &cancel,
        true,
    )?;
    header.zeroize();
    let ack = read_frame(
        &mut stream,
        deadline.min(upload_deadline),
        &deadline,
        &upload_deadline,
        &cancel,
        true,
    )?;
    parse_ack(&ack)?;
    before.unchanged(&file)?;

    let mut sent = 0u64;
    let mut streamed_hash = Sha256::new();
    while sent < before.len {
        checkpoint(deadline, upload_deadline, &cancel, true)?;
        let want = ((before.len - sent) as usize).min(MAX_CHUNK);
        let n = file
            .read(&mut buffer[..want])
            .map_err(|_| anyhow!("cannot read APK"))?;
        if n == 0 {
            bail!("APK changed during upload");
        }
        streamed_hash.update(&buffer[..n]);
        write_all_bounded(
            &mut stream,
            &buffer[..n],
            deadline,
            upload_deadline,
            &cancel,
            true,
        )?;
        sent += n as u64;
    }
    let streamed_digest = streamed_hash
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if streamed_digest != digest {
        bail!("APK changed during upload");
    }
    let mut extra = [0u8; 1];
    if file
        .read(&mut extra)
        .map_err(|_| anyhow!("cannot read APK"))?
        != 0
    {
        bail!("APK changed during upload");
    }
    before.unchanged(&file)?;
    stream
        .shutdown(Shutdown::Write)
        .map_err(|_| anyhow!("native install connection failed"))?;
    let response_limit = deadline.min(started + Duration::from_secs(180));
    let response = read_final_frame(
        &mut stream,
        response_limit,
        deadline,
        &upload_deadline,
        &cancel,
    )?;
    let result = parse_success(&response, &session, package.clone())?;
    before.unchanged(&file)?;
    io_budget(deadline, upload_deadline, &cancel, false, response_limit)?;
    Ok(result)
}

fn write_all_bounded(
    stream: &mut TcpStream,
    mut bytes: &[u8],
    deadline: Instant,
    upload: Instant,
    cancel: &CancellationToken,
    uploading: bool,
) -> Result<()> {
    while !bytes.is_empty() {
        io_budget(deadline, upload, cancel, uploading, deadline)?;
        let result = stream.write(bytes);
        io_budget(deadline, upload, cancel, uploading, deadline)?;
        match result {
            Ok(0) => bail!("native install connection closed"),
            Ok(n) => bytes = &bytes[n..],
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                wait_io(stream, false, deadline, upload, cancel, uploading, deadline)?;
                continue;
            }
            Err(_) => bail!("native install connection failed"),
        }
    }
    Ok(())
}

fn read_frame(
    stream: &mut TcpStream,
    phase_limit: Instant,
    deadline: &Instant,
    upload: &Instant,
    cancel: &CancellationToken,
    uploading: bool,
) -> Result<Vec<u8>> {
    let limit = phase_limit.min(*deadline);
    let mut frame = Vec::new();
    loop {
        io_budget(*deadline, *upload, cancel, uploading, limit)?;
        let mut byte = [0u8; 1];
        let result = stream.read(&mut byte);
        io_budget(*deadline, *upload, cancel, uploading, limit)?;
        match result {
            Ok(0) => {
                if frame.is_empty() {
                    bail!("native install response missing");
                }
                return Ok(frame);
            }
            Ok(_) => {
                frame.push(byte[0]);
                if frame.len() > MAX_FRAME {
                    bail!("native install response exceeded its bound");
                }
                if byte[0] == b'\n' {
                    return Ok(frame);
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                wait_io(stream, true, *deadline, *upload, cancel, uploading, limit)?;
                continue;
            }
            Err(_) => bail!("native install connection failed"),
        }
    }
}

fn read_final_frame(
    stream: &mut TcpStream,
    phase_limit: Instant,
    deadline: Instant,
    upload: &Instant,
    cancel: &CancellationToken,
) -> Result<Vec<u8>> {
    let mut frame = Vec::new();
    let mut buffer = [0u8; 256];
    loop {
        io_budget(deadline, *upload, cancel, false, phase_limit)?;
        let result = stream.read(&mut buffer);
        io_budget(deadline, *upload, cancel, false, phase_limit)?;
        match result {
            Ok(0) => break,
            Ok(n) => {
                if frame.len() + n > MAX_FRAME {
                    bail!("native install response exceeded its bound");
                }
                frame.extend_from_slice(&buffer[..n]);
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    || e.kind() == std::io::ErrorKind::TimedOut =>
            {
                wait_io(stream, true, deadline, *upload, cancel, false, phase_limit)?;
                continue;
            }
            Err(_) => bail!("native install connection failed"),
        }
    }
    if frame.is_empty() || frame.last() != Some(&b'\n') {
        bail!("invalid native install response");
    }
    Ok(frame)
}

struct UniqueMap(BTreeMap<String, Value>);
impl<'de> Deserialize<'de> for UniqueMap {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = UniqueMap;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("an object")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut values = BTreeMap::new();
                while let Some((key, value)) = map.next_entry::<String, Value>()? {
                    if values.insert(key, value).is_some() {
                        return Err(serde::de::Error::custom("duplicate key"));
                    }
                }
                Ok(UniqueMap(values))
            }
        }
        d.deserialize_map(V)
    }
}
fn response(frame: &[u8]) -> Result<BTreeMap<String, Value>> {
    if frame.len() > MAX_FRAME
        || !frame.ends_with(b"\n")
        || frame[..frame.len() - 1].contains(&b'\n')
        || !frame.is_ascii()
    {
        bail!("invalid native install response");
    }
    let mut de = serde_json::Deserializer::from_slice(&frame[..frame.len() - 1]);
    let map = UniqueMap::deserialize(&mut de)
        .map_err(|_| anyhow!("invalid native install response"))?
        .0;
    de.end()
        .map_err(|_| anyhow!("invalid native install response"))?;
    Ok(map)
}
fn parse_ack(frame: &[u8]) -> Result<()> {
    let map = response(frame)?;
    if map.keys().map(String::as_str).collect::<Vec<_>>() == ["schema_version", "status"]
        && map.get("schema_version").and_then(Value::as_u64) == Some(1)
        && map.get("status").and_then(Value::as_str) == Some("ready_for_payload")
    {
        return Ok(());
    }
    bail!("native install was not authorized")
}
fn parse_success(
    frame: &[u8],
    session: &str,
    package: Option<String>,
) -> Result<NativeInstallResult> {
    let launched = package.is_some();
    let map = response(frame)?;
    if map.get("status").and_then(Value::as_str) == Some("error") {
        bail!("guest rejected native install");
    }
    if map.keys().map(String::as_str).collect::<Vec<_>>()
        != ["launched", "schema_version", "session_id", "status"]
        || map.get("schema_version").and_then(Value::as_u64) != Some(1)
        || map.get("status").and_then(Value::as_str) != Some("installed")
        || map.get("session_id").and_then(Value::as_str) != Some(session)
        || map.get("launched").and_then(Value::as_bool) != Some(launched)
        || !matches!(map.get("launched"), Some(Value::Bool(_)))
        || !matches!(map.get("schema_version"), Some(Value::Number(n)) if n.is_u64())
    {
        bail!("invalid native install result");
    }
    Ok(NativeInstallResult { launched, package })
}
