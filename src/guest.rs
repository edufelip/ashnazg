use std::{
    net::SocketAddr,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::TcpStream,
    sync::Mutex,
    time::timeout,
};
use zeroize::Zeroizing;

const MAX_MESSAGE_BYTES: usize = 8 * 1024 * 1024;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const RPC_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, thiserror::Error)]
pub enum GuestError {
    #[error("guest connection failed")]
    Connect(#[source] std::io::Error),
    #[error("guest RPC timed out")]
    Timeout,
    #[error("guest connection failed")]
    Io(#[source] std::io::Error),
    #[error("guest message exceeds 8 MiB limit")]
    TooLarge,
    #[error("guest returned malformed JSON-RPC response")]
    Malformed,
    #[error("guest response ID did not match request")]
    Correlation,
    #[error("guest RPC error {code}: {message}")]
    Remote { code: i64, message: String },
    #[error("guest response omitted result")]
    MissingResult,
}

#[derive(Clone)]
pub struct GuestClient {
    addr: SocketAddr,
    token: Arc<String>,
    next_id: Arc<AtomicU64>,
    stream: Arc<Mutex<Option<BufReader<TcpStream>>>>,
    request_lock: Arc<Mutex<()>>,
    rpc_timeout: Duration,
}

impl GuestClient {
    pub fn new(addr: SocketAddr, token: String) -> Self {
        Self::with_timeout(addr, token, RPC_TIMEOUT)
    }

    fn with_timeout(addr: SocketAddr, token: String, rpc_timeout: Duration) -> Self {
        Self {
            addr,
            token: Arc::new(token),
            next_id: Arc::new(AtomicU64::new(1)),
            stream: Arc::new(Mutex::new(None)),
            request_lock: Arc::new(Mutex::new(())),
            rpc_timeout,
        }
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value, GuestError> {
        if !params.is_object() {
            return Err(GuestError::Malformed);
        }
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let mut request = json!({"jsonrpc":"2.0","id":id,"token":self.token.as_str(),"method":method,"params":params});
        let mut bytes =
            Zeroizing::new(serde_json::to_vec(&request).map_err(|_| GuestError::Malformed)?);
        request["token"] = Value::Null;
        drop(request);
        if bytes.len() + 1 > MAX_MESSAGE_BYTES {
            return Err(GuestError::TooLarge);
        }
        bytes.push(b'\n');

        let started = Instant::now();
        let _single_flight = timeout(self.rpc_timeout, self.request_lock.lock())
            .await
            .map_err(|_| GuestError::Timeout)?;
        let mut reader = self.stream.lock().await.take();
        if reader.is_none() {
            let remaining = self.rpc_timeout.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                return Err(GuestError::Timeout);
            }
            let socket = timeout(
                CONNECT_TIMEOUT.min(remaining),
                TcpStream::connect(self.addr),
            )
            .await
            .map_err(|_| GuestError::Timeout)?
            .map_err(GuestError::Connect)?;
            socket.set_nodelay(true).map_err(GuestError::Io)?;
            reader = Some(BufReader::new(socket));
        }
        let mut reader = reader.expect("stream initialized");
        let remaining = self.rpc_timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            return Err(GuestError::Timeout);
        }
        let result = timeout(remaining, async {
            reader
                .get_mut()
                .write_all(&bytes)
                .await
                .map_err(GuestError::Io)?;
            reader.get_mut().flush().await.map_err(GuestError::Io)?;
            let mut response = Vec::with_capacity(1024);
            loop {
                let available = reader.fill_buf().await.map_err(GuestError::Io)?;
                if available.is_empty() {
                    return Err(GuestError::Io(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "guest closed connection",
                    )));
                }
                let newline = available.iter().position(|b| *b == b'\n');
                let count = newline.map_or(available.len(), |i| i + 1);
                if response.len() + count > MAX_MESSAGE_BYTES {
                    return Err(GuestError::TooLarge);
                }
                response.extend_from_slice(&available[..count]);
                reader.consume(count);
                if newline.is_some() {
                    break;
                }
            }
            if response.last() == Some(&b'\n') {
                response.pop();
            }
            let parsed: Value =
                serde_json::from_slice(&response).map_err(|_| GuestError::Malformed)?;
            if parsed.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
                return Err(GuestError::Malformed);
            }
            if parsed.get("id") != Some(&json!(id)) {
                return Err(GuestError::Correlation);
            }
            if parsed.get("error").is_some() && parsed.get("result").is_some() {
                return Err(GuestError::Malformed);
            }
            if let Some(error) = parsed.get("error") {
                let code = error
                    .get("code")
                    .and_then(Value::as_i64)
                    .filter(|code| i32::try_from(*code).is_ok())
                    .ok_or(GuestError::Malformed)?;
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .ok_or(GuestError::Malformed)?;
                return Err(GuestError::Remote {
                    code,
                    message: message.to_owned(),
                });
            }
            parsed
                .get("result")
                .cloned()
                .ok_or(GuestError::MissingResult)
        })
        .await
        .unwrap_or(Err(GuestError::Timeout));
        if matches!(&result, Ok(_) | Err(GuestError::Remote { .. })) {
            *self.stream.lock().await = Some(reader);
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::{GuestClient, GuestError};
    use serde_json::json;
    use tokio::{
        io::{AsyncBufReadExt, BufReader},
        net::TcpListener,
        sync::oneshot,
        time::Duration,
    };

    #[tokio::test]
    async fn queued_request_has_a_bounded_deadline_and_is_not_sent() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (received_tx, received_rx) = oneshot::channel();
        let server = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let mut reader = BufReader::new(socket);
            let mut line = String::new();
            reader.read_line(&mut line).await.unwrap();
            received_tx.send(()).unwrap();
            let mut next_line = String::new();
            assert!(
                tokio::time::timeout(Duration::from_millis(150), reader.read_line(&mut next_line))
                    .await
                    .is_err(),
                "a queued request must not reach the occupied connection"
            );
        });
        let client = GuestClient::with_timeout(addr, "token".into(), Duration::from_secs(2));
        let mut queued_client = client.clone();
        queued_client.rpc_timeout = Duration::from_millis(50);
        let first = tokio::spawn({
            let client = client.clone();
            async move { client.call("ui_act", json!({"action":"tap"})).await }
        });
        received_rx.await.unwrap();
        assert!(matches!(
            queued_client.call("ui_snapshot", json!({})).await,
            Err(GuestError::Timeout)
        ));
        server.await.unwrap();
        assert!(first.await.unwrap().is_err());
    }
}
