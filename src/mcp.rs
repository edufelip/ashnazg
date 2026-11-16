use std::{sync::Arc, time::Duration};

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode, header::AUTHORIZATION},
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use rmcp::{
    ErrorData, ServerHandler,
    handler::server::{
        tool::ToolRouter,
        wrapper::{Json, Parameters},
    },
    model::{CallToolResult, ContentBlock, ServerCapabilities, ServerConfig},
    schemars, tool, tool_handler, tool_router,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{future::Future, pin::Pin};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::{Mutex, oneshot};
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

use crate::native_session::NativeSession;
use crate::{GuestError, HostConfig, app_install, validate_loopback_addr, validate_ui_act};

#[derive(Clone)]
pub struct AshnazgServer {
    config: HostConfig,
    native: Option<Arc<NativeInstallBackend>>,
    tool_router: ToolRouter<Self>,
}

#[derive(Clone)]
struct NativeInstallBackend {
    guest: crate::GuestClient,
    client: Arc<crate::native_install::NativeInstallClient>,
    serial: String,
    lifecycle: Arc<InstallLifecycle>,
}

struct InstallLifecycle {
    cancellation: CancellationToken,
    admission: Mutex<Admission>,
    tasks: TaskTracker,
}

#[derive(Default)]
struct Admission {
    closed: bool,
    active: bool,
}

struct CancelOnDrop(CancellationToken);

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

impl InstallLifecycle {
    fn new(cancellation: CancellationToken) -> Self {
        Self {
            cancellation,
            admission: Mutex::new(Admission::default()),
            tasks: TaskTracker::new(),
        }
    }

    async fn close_and_drain(&self) {
        {
            let mut admission = self.admission.lock().await;
            admission.closed = true;
            self.cancellation.cancel();
            self.tasks.close();
        }
        self.tasks.wait().await;
    }

    async fn install(
        self: &Arc<Self>,
        client: Arc<crate::native_install::NativeInstallClient>,
        path: String,
        launch: bool,
        package: Option<String>,
        request_cancel: CancellationToken,
    ) -> Result<crate::native_install::NativeInstallResult, ErrorData> {
        if request_cancel.is_cancelled() {
            return Err(ErrorData::internal_error(
                "native APK installation cancelled",
                None,
            ));
        }
        if !launch && package.is_some() {
            return Err(ErrorData::invalid_params(
                "package requires launch=true",
                None,
            ));
        }

        let operation_cancel = self.cancellation.child_token();
        let (tx, rx) = oneshot::channel();
        {
            let mut admission = self.admission.lock().await;
            if admission.closed || self.cancellation.is_cancelled() {
                return Err(ErrorData::internal_error(
                    "native install is shutting down",
                    None,
                ));
            }
            if admission.active {
                return Err(ErrorData::internal_error("native install is busy", None));
            }
            admission.active = true;
            let lifecycle = Arc::clone(self);
            let cancel = operation_cancel.clone();
            self.tasks.spawn(async move {
                let deadline = std::time::Instant::now() + Duration::from_secs(30);
                let result = if launch && package.is_none() {
                    client
                        .install_with_inferred_launch(
                            std::path::PathBuf::from(path),
                            deadline,
                            cancel,
                        )
                        .await
                } else {
                    client
                        .install(
                            std::path::PathBuf::from(path),
                            if launch { package } else { None },
                            deadline,
                            cancel,
                        )
                        .await
                }
                .map_err(|_| ErrorData::internal_error("native APK installation failed", None));
                let mut admission = lifecycle.admission.lock().await;
                admission.active = false;
                let _ = tx.send(result);
            });
        }

        let _request_guard = CancelOnDrop(operation_cancel.clone());
        tokio::select! {
            biased;
            _ = request_cancel.cancelled() => {
                operation_cancel.cancel();
                Err(ErrorData::internal_error("native APK installation cancelled", None))
            }
            result = rx => result.unwrap_or_else(|_| Err(ErrorData::internal_error("native APK installation failed", None))),
        }
    }
}

#[derive(Debug, Clone, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SnapshotParams {
    #[serde(default)]
    pub include_invisible: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Tap,
    LongPress,
    Type,
    Clear,
    ScrollTo,
    Back,
    DeepLink,
}

#[derive(Debug, Clone, Deserialize, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Target {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub test_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub snapshot_id: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    Up,
    Down,
    Left,
    Right,
}

#[derive(Debug, Clone, Deserialize, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ActParams {
    pub action: Action,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<Target>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub direction: Option<Direction>,
}

#[derive(Debug, Clone, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ScreenshotParams {
    #[serde(default)]
    pub format: Option<ScreenshotFormat>,
    #[serde(default)]
    pub som_overlay: bool,
}

#[derive(Debug, Clone, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum ScreenshotFormat {
    Webp,
    Png,
}

#[derive(Debug, Clone, Deserialize, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct LogsParams {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tag_filter: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub since_ms: Option<u64>,
}

#[derive(Debug, Clone, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct InstallParams {
    pub path: String,
    #[serde(default)]
    pub launch: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub package: Option<String>,
}

#[derive(Debug, Serialize, schemars::JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ScreenshotText {
    mime_type: String,
    snapshot_id: u64,
    marks: Value,
    width: u32,
    height: u32,
}

impl AshnazgServer {
    pub fn new(config: HostConfig) -> Self {
        Self {
            config,
            native: None,
            tool_router: Self::tool_router(),
        }
    }

    fn with_native(config: HostConfig, native: NativeInstallBackend) -> Self {
        Self {
            config,
            native: Some(Arc::new(native)),
            tool_router: Self::tool_router(),
        }
    }

    fn guest_error(error: GuestError) -> ErrorData {
        match error {
            GuestError::Remote { code, message } => {
                ErrorData::new(rmcp::model::ErrorCode(code as i32), message, None)
            }
            other => ErrorData::internal_error(other.to_string(), None),
        }
    }

    async fn guest_json(&self, method: &str, params: Value) -> Result<Value, ErrorData> {
        self.config
            .guest
            .call(method, params)
            .await
            .map_err(Self::guest_error)
    }
}

#[tool_router]
impl AshnazgServer {
    #[tool(
        name = "ui_snapshot",
        description = "Return the current semantic Android UI snapshot.",
        annotations(read_only_hint = true)
    )]
    pub async fn ui_snapshot(
        &self,
        Parameters(params): Parameters<SnapshotParams>,
    ) -> Result<Json<Value>, ErrorData> {
        self.guest_json(
            "ui_snapshot",
            serde_json::json!({"includeInvisible":params.include_invisible}),
        )
        .await
        .map(Json)
    }

    #[tool(
        name = "ui_act",
        description = "Perform a validated action on the Android UI and return a fresh snapshot.",
        annotations(destructive_hint = true)
    )]
    pub async fn ui_act(
        &self,
        Parameters(params): Parameters<ActParams>,
    ) -> Result<Json<Value>, ErrorData> {
        let value = serde_json::to_value(params)
            .map_err(|e| ErrorData::invalid_params(e.to_string(), None))?;
        validate_ui_act(&value).map_err(|e| ErrorData::invalid_params(e.to_string(), None))?;
        self.guest_json("ui_act", value).await.map(Json)
    }

    #[tool(
        name = "ui_screenshot",
        description = "Capture a guest screenshot; returns both screenshot image content and metadata.",
        annotations(read_only_hint = true)
    )]
    pub async fn ui_screenshot(
        &self,
        Parameters(params): Parameters<ScreenshotParams>,
    ) -> Result<CallToolResult, ErrorData> {
        let format = match params.format.unwrap_or(ScreenshotFormat::Webp) {
            ScreenshotFormat::Webp => "webp",
            ScreenshotFormat::Png => "png",
        };
        let value = self
            .guest_json(
                "ui_screenshot",
                serde_json::json!({"format":format,"somOverlay":params.som_overlay}),
            )
            .await?;
        let encoded = value.get("data").and_then(Value::as_str).ok_or_else(|| {
            ErrorData::internal_error("guest screenshot omitted image data", None)
        })?;
        let image = STANDARD.decode(encoded).map_err(|_| {
            ErrorData::internal_error("guest screenshot returned invalid base64 image data", None)
        })?;
        if image.len() > 8 * 1024 * 1024 {
            return Err(ErrorData::internal_error(
                "guest screenshot exceeds 8 MiB",
                None,
            ));
        }
        let mime = value
            .get("mimeType")
            .and_then(Value::as_str)
            .ok_or_else(|| ErrorData::internal_error("guest screenshot omitted MIME type", None))?;
        let signature_ok = match mime {
            "image/png" => image.starts_with(b"\x89PNG\r\n\x1a\n"),
            "image/webp" => image.len() >= 12 && &image[..4] == b"RIFF" && &image[8..12] == b"WEBP",
            _ => false,
        };
        if !signature_ok {
            return Err(ErrorData::internal_error(
                "guest screenshot format does not match its MIME type",
                None,
            ));
        }
        let metadata = ScreenshotText {
            mime_type: mime.to_owned(),
            snapshot_id: value
                .get("snapshotId")
                .and_then(Value::as_u64)
                .filter(|n| *n > 0)
                .ok_or_else(|| {
                    ErrorData::internal_error("guest screenshot omitted valid snapshotId", None)
                })?,
            marks: value
                .get("marks")
                .filter(|v| v.is_array())
                .cloned()
                .ok_or_else(|| {
                    ErrorData::internal_error("guest screenshot omitted marks array", None)
                })?,
            width: value
                .get("width")
                .and_then(Value::as_u64)
                .filter(|n| *n > 0 && *n <= u32::MAX as u64)
                .ok_or_else(|| {
                    ErrorData::internal_error("guest screenshot omitted valid width", None)
                })? as u32,
            height: value
                .get("height")
                .and_then(Value::as_u64)
                .filter(|n| *n > 0 && *n <= u32::MAX as u64)
                .ok_or_else(|| {
                    ErrorData::internal_error("guest screenshot omitted valid height", None)
                })? as u32,
        };
        let meta_json = serde_json::to_string(&metadata)
            .map_err(|e| ErrorData::internal_error(e.to_string(), None))?;
        Ok(CallToolResult::success(vec![
            ContentBlock::image(encoded.to_owned(), mime.to_owned()),
            ContentBlock::text(meta_json),
        ]))
    }

    #[tool(
        name = "app_logs",
        description = "Return bounded Android agent log lines.",
        annotations(read_only_hint = true)
    )]
    pub async fn app_logs(
        &self,
        Parameters(params): Parameters<LogsParams>,
    ) -> Result<Json<Value>, ErrorData> {
        self.guest_json(
            "app_logs",
            serde_json::to_value(params)
                .map_err(|e| ErrorData::invalid_params(e.to_string(), None))?,
        )
        .await
        .map(Json)
    }

    #[tool(
        name = "app_install",
        description = "Install an APK on the selected Android session; optionally launch the package.",
        annotations(destructive_hint = true)
    )]
    pub async fn app_install(
        &self,
        Parameters(params): Parameters<InstallParams>,
        request_cancel: CancellationToken,
    ) -> Result<Json<Value>, ErrorData> {
        if let Some(native) = &self.native {
            let result = native
                .lifecycle
                .install(
                    Arc::clone(&native.client),
                    params.path,
                    params.launch.unwrap_or(false),
                    params.package,
                    request_cancel,
                )
                .await?;
            return Ok(Json(serde_json::json!({
                "installed": true,
                "package": result.package,
                "serial": native.serial,
            })));
        }
        let config = self.config.clone();
        let path = params.path;
        let launch = params.launch.unwrap_or(false);
        let package = params.package;
        if !launch && package.is_some() {
            return Err(ErrorData::invalid_params(
                "package requires launch=true",
                None,
            ));
        }
        tokio::task::spawn_blocking(move || {
            app_install(
                &config.adb,
                std::path::Path::new(&path),
                launch,
                package.as_deref(),
            )
        })
        .await
        .map_err(|e| ErrorData::internal_error(e.to_string(), None))?
        .map(Json)
        .map_err(|e| ErrorData::internal_error(e.to_string(), None))
    }
}

#[tool_handler(router = self.tool_router)]
impl ServerHandler for AshnazgServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build()).with_server_info(
            rmcp::model::Implementation::new("ashnazg", env!("CARGO_PKG_VERSION")),
        )
    }
}

pub async fn serve_stdio(config: HostConfig) -> anyhow::Result<()> {
    use rmcp::ServiceExt;
    let server = AshnazgServer::new(config);
    server
        .serve(rmcp::transport::io::stdio())
        .await?
        .waiting()
        .await?;
    Ok(())
}

type NativeServeFuture = Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>>;
type NativeWaitFuture<'a> = Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send + 'a>>;
type NativeShutdownFuture = Pin<Box<dyn Future<Output = anyhow::Result<()>> + Send>>;

trait NativeSessionOwner: Send + 'static {
    fn guest_client(&self) -> anyhow::Result<crate::GuestClient>;
    fn install_client(&self) -> anyhow::Result<Arc<crate::native_install::NativeInstallClient>>;
    fn adb_addr(&self) -> std::net::SocketAddr;
    fn wait_terminated(&self) -> NativeWaitFuture<'_>;
    fn shutdown(self: Box<Self>) -> NativeShutdownFuture;
}

impl NativeSessionOwner for NativeSession {
    fn guest_client(&self) -> anyhow::Result<crate::GuestClient> {
        NativeSession::guest_client(self)
    }

    fn install_client(&self) -> anyhow::Result<Arc<crate::native_install::NativeInstallClient>> {
        NativeSession::install_client(self)
    }

    fn adb_addr(&self) -> std::net::SocketAddr {
        NativeSession::adb_addr(self)
    }

    fn wait_terminated(&self) -> NativeWaitFuture<'_> {
        Box::pin(async move { NativeSession::wait_terminated(self).await.map(|_| ()) })
    }

    fn shutdown(self: Box<Self>) -> NativeShutdownFuture {
        Box::pin(async move { (*self).shutdown().await.map(|_| ()) })
    }
}

fn native_server(backend: NativeInstallBackend) -> AshnazgServer {
    let config = HostConfig {
        guest: backend.guest.clone(),
        adb: String::new(),
    };
    AshnazgServer::with_native(config, backend)
}

async fn serve_native_transport<R, W>(
    backend: NativeInstallBackend,
    read: R,
    write: W,
    lifecycle: CancellationToken,
) -> anyhow::Result<()>
where
    R: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    use rmcp::ServiceExt;
    let server = native_server(backend);
    let initializing = server.serve_with_ct((read, write), lifecycle.clone());
    tokio::pin!(initializing);
    let running = tokio::select! {
        result = &mut initializing => match result {
            Ok(running) => running,
            Err(_) if lifecycle.is_cancelled() => return Ok(()),
            Err(_) => anyhow::bail!("native MCP serving failed"),
        },
        _ = lifecycle.cancelled() => {
            match initializing.await {
                Ok(running) => running,
                Err(_) => return Ok(()),
            }
        }
    };
    let waiting = running.waiting();
    tokio::pin!(waiting);
    tokio::select! {
        result = &mut waiting => result
            .map(|_| ())
            .map_err(|_| anyhow::anyhow!("native MCP serving failed")),
        _ = lifecycle.cancelled() => waiting.await
            .map(|_| ())
            .map_err(|_| anyhow::anyhow!("native MCP serving failed")),
    }
}

async fn serve_native_http_listener(
    backend: NativeInstallBackend,
    listener: tokio::net::TcpListener,
    bearer: String,
    lifecycle: CancellationToken,
) -> anyhow::Result<()> {
    let address = listener
        .local_addr()
        .map_err(|_| anyhow::anyhow!("native MCP serving failed"))?;
    validate_native_http_inputs(address, &bearer)
        .map_err(|_| anyhow::anyhow!("native MCP serving failed"))?;
    let app = native_http_app(backend, bearer, lifecycle.clone());
    axum::serve(listener, app)
        .with_graceful_shutdown(lifecycle.cancelled_owned())
        .await
        .map_err(|_| anyhow::anyhow!("native MCP serving failed"))
}

async fn run_native_owner<S, F>(
    session: S,
    lifecycle_token: CancellationToken,
    serve: F,
) -> anyhow::Result<()>
where
    S: NativeSessionOwner,
    F: FnOnce(NativeInstallBackend, CancellationToken) -> NativeServeFuture,
{
    let lifecycle = Arc::new(InstallLifecycle::new(lifecycle_token.clone()));
    let backend = match (session.guest_client(), session.install_client()) {
        (Ok(guest), Ok(client)) => Ok(NativeInstallBackend {
            guest,
            client,
            serial: session.adb_addr().to_string(),
            lifecycle: Arc::clone(&lifecycle),
        }),
        _ => Err(anyhow::anyhow!("native MCP serving failed")),
    };

    let server_result = match backend {
        Ok(backend) => {
            let serving = serve(backend, lifecycle_token.clone());
            tokio::pin!(serving);
            let terminated = session.wait_terminated();
            tokio::pin!(terminated);
            tokio::select! {
                biased;
                result = &mut serving => {
                    lifecycle_token.cancel();
                    result
                }
                _ = lifecycle_token.cancelled() => {
                    lifecycle_token.cancel();
                    serving.await
                }
                result = &mut terminated => {
                    lifecycle_token.cancel();
                    let serving_result = serving.await;
                    let _ = result;
                    serving_result.and(Err(anyhow::anyhow!("native helper exited unexpectedly")))
                }
            }
        }
        Err(error) => Err(error),
    };

    lifecycle.close_and_drain().await;
    let shutdown_result = Box::new(session)
        .shutdown()
        .await
        .map_err(|_| anyhow::anyhow!("native helper shutdown failed"));
    match (server_result, shutdown_result) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(_), Ok(())) => Err(anyhow::anyhow!("native MCP serving failed")),
        (Ok(()), Err(_)) => Err(anyhow::anyhow!("native helper shutdown failed")),
        (Err(_), Err(_)) => Err(anyhow::anyhow!(
            "native MCP serving failed; native helper shutdown failed"
        )),
    }
}

async fn run_native_public<S, F>(
    session: S,
    cancel: CancellationToken,
    serve: F,
) -> anyhow::Result<()>
where
    S: NativeSessionOwner,
    F: FnOnce(NativeInstallBackend, CancellationToken) -> NativeServeFuture + Send + 'static,
{
    let lifecycle = cancel.child_token();
    let guard = CancelOnDrop(lifecycle.clone());
    let owner = tokio::spawn(run_native_owner(session, lifecycle, serve));
    let result = owner
        .await
        .map_err(|_| anyhow::anyhow!("native MCP lifecycle task failed"))?;
    drop(guard);
    result
}

/// Serves the native-backed MCP protocol over caller-owned asynchronous I/O.
/// The session and its helper remain owned until serving and install workers drain.
pub async fn serve_native_io<R, W>(
    session: NativeSession,
    read: R,
    write: W,
    cancel: CancellationToken,
) -> anyhow::Result<()>
where
    R: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    run_native_public(session, cancel, move |backend, lifecycle| {
        Box::pin(serve_native_transport(backend, read, write, lifecycle))
    })
    .await
}

/// Serves native MCP over an already-bound loopback listener with independent HTTP bearer auth.
pub async fn serve_native_http(
    session: NativeSession,
    listener: tokio::net::TcpListener,
    bearer: String,
    cancel: CancellationToken,
) -> anyhow::Result<()> {
    run_native_public(session, cancel, move |backend, lifecycle| {
        Box::pin(serve_native_http_listener(
            backend, listener, bearer, lifecycle,
        ))
    })
    .await
}

pub async fn serve_http(config: HostConfig, bind: &str) -> anyhow::Result<()> {
    let addr = validate_loopback_addr(bind)?;
    let token = std::env::var("ASHNAZG_HTTP_TOKEN")
        .map_err(|_| anyhow::anyhow!("ASHNAZG_HTTP_TOKEN is required for HTTP MCP"))?;
    if token.is_empty() {
        anyhow::bail!("ASHNAZG_HTTP_TOKEN must not be empty");
    }
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let app = http_app(config, token);
    tracing::info!(address = %addr, endpoint = "/mcp", "MCP Streamable HTTP listening");
    axum::serve(listener, app).await?;
    Ok(())
}

fn http_app(config: HostConfig, token: String) -> axum::Router {
    let service = rmcp::transport::streamable_http_server::StreamableHttpService::new(
        move || Ok(AshnazgServer::new(config.clone())),
        Arc::new(
            rmcp::transport::streamable_http_server::session::local::LocalSessionManager::default(),
        ),
        rmcp::transport::streamable_http_server::StreamableHttpServerConfig::default()
            .with_sse_keep_alive(Some(Duration::from_secs(15)))
            .with_cancellation_token(CancellationToken::new()),
    );
    axum::Router::new()
        .route_service("/mcp", service)
        .layer(tower_http::limit::RequestBodyLimitLayer::new(
            8 * 1024 * 1024,
        ))
        .layer(middleware::from_fn_with_state(token, authorize))
}

fn native_http_app(
    backend: NativeInstallBackend,
    token: String,
    lifecycle: CancellationToken,
) -> axum::Router {
    let service = rmcp::transport::streamable_http_server::StreamableHttpService::new(
        move || Ok(native_server(backend.clone())),
        Arc::new(
            rmcp::transport::streamable_http_server::session::local::LocalSessionManager::default(),
        ),
        rmcp::transport::streamable_http_server::StreamableHttpServerConfig::default()
            .with_sse_keep_alive(Some(Duration::from_secs(15)))
            .with_cancellation_token(lifecycle),
    );
    axum::Router::new()
        .route_service("/mcp", service)
        .layer(tower_http::limit::RequestBodyLimitLayer::new(
            8 * 1024 * 1024,
        ))
        .layer(middleware::from_fn_with_state(token, authorize))
}

async fn authorize(State(token): State<String>, request: Request<Body>, next: Next) -> Response {
    let expected = format!("Bearer {token}");
    let authorized = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|h| h.to_str().ok())
        .is_some_and(|actual| constant_time_eq(actual.as_bytes(), expected.as_bytes()));
    if authorized {
        next.run(request).await
    } else {
        StatusCode::UNAUTHORIZED.into_response()
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    use subtle::ConstantTimeEq;
    left.len() == right.len() && bool::from(left.ct_eq(right))
}

fn validate_native_http_inputs(addr: std::net::SocketAddr, bearer: &str) -> anyhow::Result<()> {
    use axum::http::HeaderValue;
    if !addr.ip().is_loopback() {
        anyhow::bail!("native HTTP listener must use a loopback address");
    }
    if bearer.is_empty() || bearer.len() > 4096 || HeaderValue::from_str(bearer).is_err() {
        anyhow::bail!("native HTTP bearer must be a valid bounded header value");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        AshnazgServer, NativeSessionOwner, NativeShutdownFuture, NativeWaitFuture, http_app,
        run_native_owner, run_native_public, serve_native_http_listener, serve_native_transport,
        validate_native_http_inputs,
    };
    use crate::GuestClient;
    use axum::{
        body::Body,
        http::{
            Request, StatusCode,
            header::{AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE},
        },
    };
    use std::net::SocketAddr;
    use std::sync::{Arc, Mutex as StdMutex};
    use std::time::Duration;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio_util::sync::CancellationToken;
    use tower::ServiceExt;

    struct FixtureOwner {
        events: Arc<StdMutex<Vec<&'static str>>>,
        install_endpoint: SocketAddr,
        termination:
            Arc<tokio::sync::Mutex<Option<tokio::sync::oneshot::Receiver<anyhow::Result<()>>>>>,
        shutdown_fails: bool,
    }

    impl NativeSessionOwner for FixtureOwner {
        fn guest_client(&self) -> anyhow::Result<crate::GuestClient> {
            Ok(crate::GuestClient::new(
                "127.0.0.1:7777".parse().unwrap(),
                "fixture-guest-token".into(),
            ))
        }

        fn install_client(
            &self,
        ) -> anyhow::Result<Arc<crate::native_install::NativeInstallClient>> {
            Ok(Arc::new(crate::native_install::NativeInstallClient::new(
                self.install_endpoint,
                "00000000-0000-4000-8000-000000000000".into(),
                "a".repeat(64),
            )?))
        }

        fn adb_addr(&self) -> SocketAddr {
            "127.0.0.1:4321".parse().unwrap()
        }

        fn wait_terminated(&self) -> NativeWaitFuture<'_> {
            let termination = Arc::clone(&self.termination);
            Box::pin(async move {
                let receiver = termination.lock().await.take();
                match receiver {
                    Some(receiver) => receiver
                        .await
                        .unwrap_or_else(|_| Err(anyhow::anyhow!("fixture helper ended"))),
                    None => std::future::pending().await,
                }
            })
        }

        fn shutdown(self: Box<Self>) -> NativeShutdownFuture {
            Box::pin(async move {
                self.events.lock().unwrap().push("shutdown");
                if self.shutdown_fails {
                    Err(anyhow::anyhow!("fixture shutdown"))
                } else {
                    Ok(())
                }
            })
        }
    }

    fn fixture_owner(
        events: Arc<StdMutex<Vec<&'static str>>>,
        install_endpoint: SocketAddr,
    ) -> FixtureOwner {
        FixtureOwner {
            events,
            install_endpoint,
            termination: Arc::new(tokio::sync::Mutex::new(None)),
            shutdown_fails: false,
        }
    }

    #[tokio::test]
    async fn native_io_serves_real_mcp_messages_and_joins_owner_after_eof() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let parent = CancellationToken::new();
        let upload_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let upload_addr = upload_listener.local_addr().unwrap();
        let (client, server) = tokio::io::duplex(32 * 1024);
        let (read, write) = tokio::io::split(server);
        let owner_events = Arc::clone(&events);
        let parent_for_server = parent.clone();
        let server_task = tokio::spawn(async move {
            run_native_owner(
                fixture_owner(owner_events, upload_addr),
                parent_for_server.child_token(),
                move |backend, ct| {
                    assert_eq!(backend.serial, "127.0.0.1:4321");
                    Box::pin(serve_native_transport(backend, read, write, ct))
                },
            )
            .await
        });

        let (read, mut write) = tokio::io::split(client);
        let mut read = BufReader::new(read);
        write.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"fixture\",\"version\":\"1\"}}}\n").await.unwrap();
        let mut line = String::new();
        tokio::time::timeout(Duration::from_secs(2), read.read_line(&mut line))
            .await
            .unwrap()
            .unwrap();
        let init: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(init["id"], 1);
        line.clear();
        write.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}\n").await.unwrap();
        loop {
            line.clear();
            tokio::time::timeout(Duration::from_secs(2), read.read_line(&mut line))
                .await
                .unwrap()
                .unwrap();
            let value: serde_json::Value = serde_json::from_str(&line).unwrap();
            if value["id"] == 2 {
                let tools = value["result"]["tools"].as_array().unwrap();
                assert_eq!(tools.len(), 5);
                let install = tools
                    .iter()
                    .find(|tool| tool["name"] == "app_install")
                    .unwrap();
                let schema = install["inputSchema"].to_string();
                for field in ["path", "launch", "package"] {
                    assert!(schema.contains(field));
                }
                break;
            }
        }
        write.shutdown().await.unwrap();
        tokio::time::timeout(Duration::from_secs(8), server_task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
        assert!(!parent.is_cancelled());
    }

    fn native_upload_peer(
        listener: std::net::TcpListener,
        expected: usize,
    ) -> std::thread::JoinHandle<Vec<(serde_json::Value, Vec<u8>)>> {
        std::thread::spawn(move || {
            use std::io::{BufRead, Read, Write};
            let mut received = Vec::new();
            for _ in 0..expected {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
                let mut header_line = String::new();
                reader.read_line(&mut header_line).unwrap();
                let header: serde_json::Value = serde_json::from_str(&header_line).unwrap();
                let size = header["apk_size"].as_u64().unwrap() as usize;
                stream
                    .write_all(b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n")
                    .unwrap();
                let mut body = vec![0; size];
                reader.read_exact(&mut body).unwrap();
                use sha2::Digest;
                let digest = sha2::Sha256::digest(&body)
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>();
                assert_eq!(header["apk_sha256"], digest);
                let mut trailing = [0; 1];
                assert_eq!(reader.read(&mut trailing).unwrap(), 0);
                let launched = header["launch"].as_bool().unwrap();
                let reply = serde_json::json!({
                    "schema_version":1,
                    "status":"installed",
                    "session_id":"00000000-0000-4000-8000-000000000000",
                    "launched":launched,
                });
                stream.write_all(format!("{reply}\n").as_bytes()).unwrap();
                received.push((header, body));
            }
            received
        })
    }

    #[tokio::test]
    async fn native_mcp_install_dispatches_inferred_explicit_and_no_launch_requests() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = listener.local_addr().unwrap();
        let upload_peer = native_upload_peer(listener, 3);
        let events = Arc::new(StdMutex::new(Vec::new()));
        let parent = CancellationToken::new();
        let (client, server) = tokio::io::duplex(64 * 1024);
        let (server_read, server_write) = tokio::io::split(server);
        let owner_events = Arc::clone(&events);
        let owner_parent = parent.clone();
        let server_task = tokio::spawn(async move {
            run_native_owner(
                fixture_owner(owner_events, endpoint),
                owner_parent.child_token(),
                move |backend, ct| {
                    Box::pin(serve_native_transport(
                        backend,
                        server_read,
                        server_write,
                        ct,
                    ))
                },
            )
            .await
        });

        let inferred = crate::apk_manifest::fixture_apk(
            include_bytes!("../rust-tests/fixtures/apk-manifests/native.axml"),
            false,
            &[],
        );
        let explicit = b"valid only for explicit native install".to_vec();
        let no_launch = b"valid only for no-launch native install".to_vec();
        let files = [inferred, explicit, no_launch]
            .into_iter()
            .map(|bytes| {
                let file = tempfile::NamedTempFile::new().unwrap();
                std::fs::write(file.path(), bytes).unwrap();
                file
            })
            .collect::<Vec<_>>();

        let (client_read, mut client_write) = tokio::io::split(client);
        let mut client_read = BufReader::new(client_read);
        client_write.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"fixture\",\"version\":\"1\"}}}\n").await.unwrap();
        let mut line = String::new();
        client_read.read_line(&mut line).await.unwrap();
        client_write
            .write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n")
            .await
            .unwrap();

        let requests = [
            serde_json::json!({"path":files[0].path(),"launch":true}),
            serde_json::json!({"path":files[1].path(),"launch":true,"package":"com.explicit.app"}),
            serde_json::json!({"path":files[2].path(),"launch":false}),
        ];
        for (index, arguments) in requests.into_iter().enumerate() {
            let request = serde_json::json!({
                "jsonrpc":"2.0", "id":index + 2, "method":"tools/call",
                "params":{"name":"app_install","arguments":arguments},
            });
            client_write
                .write_all(format!("{request}\n").as_bytes())
                .await
                .unwrap();
            line.clear();
            tokio::time::timeout(Duration::from_secs(5), client_read.read_line(&mut line))
                .await
                .unwrap()
                .unwrap();
            let response: serde_json::Value = serde_json::from_str(&line).unwrap();
            let text = response["result"]["content"][0]["text"].as_str().unwrap();
            let result: serde_json::Value = serde_json::from_str(text).unwrap();
            assert_eq!(result["installed"], true);
            assert_eq!(result["serial"], "127.0.0.1:4321");
            assert_eq!(
                result["package"],
                match index {
                    0 => serde_json::json!("dev.ashnazg.fixture"),
                    1 => serde_json::json!("com.explicit.app"),
                    _ => serde_json::Value::Null,
                }
            );
        }
        for (id, arguments, expected_message) in [
            (
                5,
                serde_json::json!({"path":files[2].path(),"launch":true}),
                "native APK installation failed",
            ),
            (
                6,
                serde_json::json!({"path":files[2].path(),"launch":false,"package":"com.invalid.app"}),
                "package requires launch=true",
            ),
        ] {
            let request = serde_json::json!({
                "jsonrpc":"2.0", "id":id, "method":"tools/call",
                "params":{"name":"app_install","arguments":arguments},
            });
            client_write
                .write_all(format!("{request}\n").as_bytes())
                .await
                .unwrap();
            line.clear();
            tokio::time::timeout(Duration::from_secs(5), client_read.read_line(&mut line))
                .await
                .unwrap()
                .unwrap();
            let response: serde_json::Value = serde_json::from_str(&line).unwrap();
            let error = response["result"]["content"][0]["text"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| response["error"]["message"].as_str().unwrap().to_owned());
            assert!(
                error.contains(expected_message),
                "unexpected MCP error: {response}"
            );
            assert!(!error.contains("valid only for"));
        }
        client_write.shutdown().await.unwrap();
        tokio::time::timeout(Duration::from_secs(8), server_task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let received = upload_peer.join().unwrap();
        assert_eq!(received[0].0["package"], "dev.ashnazg.fixture");
        assert_eq!(received[0].0["launch"], true);
        assert_eq!(received[1].0["package"], "com.explicit.app");
        assert_eq!(received[2].0["launch"], false);
        for ((header, body), file) in received.iter().zip(files) {
            assert_eq!(header["apk_size"], body.len());
            assert_eq!(body, &std::fs::read(file.path()).unwrap());
        }
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
        assert!(!parent.is_cancelled());
    }

    #[tokio::test]
    async fn native_io_eof_before_initialize_fails_and_still_shuts_down_owner() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let (client, server) = tokio::io::duplex(1024);
        let (read, write) = tokio::io::split(server);
        let owner_events = Arc::clone(&events);
        let task = tokio::spawn(async move {
            run_native_owner(
                fixture_owner(owner_events, listener.local_addr().unwrap()),
                CancellationToken::new(),
                move |backend, ct| Box::pin(serve_native_transport(backend, read, write, ct)),
            )
            .await
        });
        let (read, mut write) = tokio::io::split(client);
        write.shutdown().await.unwrap();
        drop(read);
        let error = tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(error.to_string(), "native MCP serving failed");
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
    }

    #[tokio::test]
    async fn native_io_cancellation_before_initialize_joins_owner_cleanly() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let (client, server) = tokio::io::duplex(1024);
        let (read, write) = tokio::io::split(server);
        let cancel = CancellationToken::new();
        let owner_cancel = cancel.clone();
        let owner_events = Arc::clone(&events);
        let task = tokio::spawn(async move {
            run_native_owner(
                fixture_owner(owner_events, listener.local_addr().unwrap()),
                owner_cancel.child_token(),
                move |backend, ct| Box::pin(serve_native_transport(backend, read, write, ct)),
            )
            .await
        });
        tokio::task::yield_now().await;
        cancel.cancel();
        let result = tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap();
        result.unwrap();
        drop(client);
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
        assert!(cancel.is_cancelled());
    }

    #[tokio::test]
    async fn helper_exit_stops_native_serving_before_shutdown() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let (client, server) = tokio::io::duplex(1024);
        let (read, write) = tokio::io::split(server);
        let (exit_tx, exit_rx) = tokio::sync::oneshot::channel();
        let owner = fixture_owner(Arc::clone(&events), listener.local_addr().unwrap());
        *owner.termination.lock().await = Some(exit_rx);
        let task = tokio::spawn(async move {
            run_native_owner(owner, CancellationToken::new(), move |backend, ct| {
                Box::pin(serve_native_transport(backend, read, write, ct))
            })
            .await
        });
        tokio::task::yield_now().await;
        exit_tx.send(Err(anyhow::anyhow!("helper exited"))).unwrap();
        let error = tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(error.to_string(), "native MCP serving failed");
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
        drop(client);
    }

    #[tokio::test]
    async fn serving_and_shutdown_failures_are_both_reported_generically() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let mut owner = fixture_owner(Arc::clone(&events), listener.local_addr().unwrap());
        owner.shutdown_fails = true;
        let error = run_native_owner(owner, CancellationToken::new(), |_backend, _ct| {
            Box::pin(async { Err(anyhow::anyhow!("private serving detail")) })
        })
        .await
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "native MCP serving failed; native helper shutdown failed"
        );
        assert!(!error.to_string().contains("private"));
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
    }

    #[tokio::test]
    async fn native_http_authenticates_and_releases_listener_on_cancellation() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let install_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let owner = fixture_owner(Arc::clone(&events), install_listener.local_addr().unwrap());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let cancel = CancellationToken::new();
        let task_cancel = cancel.clone();
        let task = tokio::spawn(async move {
            run_native_owner(
                owner,
                task_cancel.child_token(),
                move |backend, lifecycle| {
                    Box::pin(serve_native_http_listener(
                        backend,
                        listener,
                        "independent-http-secret".into(),
                        lifecycle,
                    ))
                },
            )
            .await
        });

        let client = reqwest::Client::new();
        let mut response = None;
        for _ in 0..100 {
            if let Ok(result) = client.get(format!("http://{address}/mcp")).send().await {
                response = Some(result);
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        assert_eq!(
            response.unwrap().status(),
            reqwest::StatusCode::UNAUTHORIZED
        );
        cancel.cancel();
        tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let rebound = tokio::net::TcpListener::bind(address).await.unwrap();
        drop(rebound);
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
    }

    #[tokio::test]
    async fn cancelled_install_keeps_admission_until_stalled_worker_is_joined() {
        use std::io::{BufRead, Read};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (accepted_tx, accepted_rx) = tokio::sync::oneshot::channel();
        let peer = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut reader = std::io::BufReader::new(stream);
            let mut header = String::new();
            reader.read_line(&mut header).unwrap();
            let _ = accepted_tx.send(());
            let mut remainder = Vec::new();
            reader.read_to_end(&mut remainder).unwrap();
            assert!(remainder.is_empty());
        });
        let client = Arc::new(
            crate::native_install::NativeInstallClient::new(
                endpoint,
                "00000000-0000-4000-8000-000000000000".into(),
                "a".repeat(64),
            )
            .unwrap(),
        );
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), b"valid fixture payload").unwrap();
        let lifecycle = Arc::new(super::InstallLifecycle::new(CancellationToken::new()));
        let request_cancel = CancellationToken::new();
        let install = {
            let lifecycle = Arc::clone(&lifecycle);
            let client = Arc::clone(&client);
            let path = file.path().to_string_lossy().into_owned();
            let request_cancel = request_cancel.clone();
            tokio::spawn(async move {
                lifecycle
                    .install(client, path, false, None, request_cancel)
                    .await
            })
        };
        tokio::time::timeout(Duration::from_secs(2), accepted_rx)
            .await
            .unwrap()
            .unwrap();
        let busy = lifecycle
            .install(
                Arc::clone(&client),
                file.path().to_string_lossy().into_owned(),
                false,
                None,
                CancellationToken::new(),
            )
            .await
            .unwrap_err();
        assert!(busy.to_string().contains("busy"));
        request_cancel.cancel();
        let cancelled = tokio::time::timeout(Duration::from_secs(2), install)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert!(cancelled.to_string().contains("cancelled"));
        lifecycle.close_and_drain().await;
        assert!(!lifecycle.admission.lock().await.active);
        peer.join().unwrap();
    }

    #[tokio::test]
    async fn dropping_install_caller_cancels_and_drains_tracked_worker() {
        use std::io::{BufRead, Read};

        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = listener.local_addr().unwrap();
        let (accepted_tx, accepted_rx) = tokio::sync::oneshot::channel();
        let peer = std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut reader = std::io::BufReader::new(stream);
            let mut header = String::new();
            reader.read_line(&mut header).unwrap();
            let _ = accepted_tx.send(());
            let mut remainder = Vec::new();
            reader.read_to_end(&mut remainder).unwrap();
            assert!(remainder.is_empty());
        });
        let client = Arc::new(
            crate::native_install::NativeInstallClient::new(
                endpoint,
                "00000000-0000-4000-8000-000000000000".into(),
                "b".repeat(64),
            )
            .unwrap(),
        );
        let file = tempfile::NamedTempFile::new().unwrap();
        std::fs::write(file.path(), b"payload while caller is dropped").unwrap();
        let lifecycle = Arc::new(super::InstallLifecycle::new(CancellationToken::new()));
        let operation = {
            let lifecycle = Arc::clone(&lifecycle);
            let client = Arc::clone(&client);
            let path = file.path().to_string_lossy().into_owned();
            tokio::spawn(async move {
                lifecycle
                    .install(client, path, false, None, CancellationToken::new())
                    .await
            })
        };
        tokio::time::timeout(Duration::from_secs(2), accepted_rx)
            .await
            .unwrap()
            .unwrap();
        operation.abort();
        lifecycle.close_and_drain().await;
        assert!(!lifecycle.admission.lock().await.active);
        peer.join().unwrap();
    }

    #[tokio::test]
    async fn dropping_public_native_future_detaches_cleanup_without_cancelling_parent() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let owner = fixture_owner(Arc::clone(&events), listener.local_addr().unwrap());
        let parent = CancellationToken::new();
        let future = run_native_public(owner, parent.clone(), move |_backend, lifecycle| {
            Box::pin(async move {
                lifecycle.cancelled().await;
                Ok(())
            })
        });
        let caller = tokio::spawn(future);
        tokio::task::yield_now().await;
        caller.abort();
        assert!(caller.await.unwrap_err().is_cancelled());
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if events.lock().unwrap().contains(&"shutdown") {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert!(!parent.is_cancelled());
    }

    #[test]
    fn native_http_requires_loopback_and_a_bounded_header_value() {
        assert!(
            validate_native_http_inputs("127.0.0.1:0".parse().unwrap(), "independent-token")
                .is_ok()
        );
        assert!(validate_native_http_inputs("0.0.0.0:8080".parse().unwrap(), "token").is_err());
        assert!(validate_native_http_inputs("127.0.0.1:8080".parse().unwrap(), "").is_err());
        assert!(
            validate_native_http_inputs("127.0.0.1:8080".parse().unwrap(), "bad\ntoken").is_err()
        );
        assert!(
            validate_native_http_inputs("127.0.0.1:8080".parse().unwrap(), &"x".repeat(4097))
                .is_err()
        );
    }

    fn parse_mcp_response(body: &[u8]) -> serde_json::Value {
        if let Ok(value) = serde_json::from_slice(body) {
            return value;
        }
        let text = String::from_utf8_lossy(body);
        let data = text
            .lines()
            .filter_map(|line| line.strip_prefix("data: "))
            .find(|line| !line.is_empty())
            .expect("SSE body must include a data event");
        serde_json::from_str(data).expect("SSE data must contain JSON-RPC")
    }

    #[test]
    fn exposes_only_the_five_implemented_tools_with_typed_action_schema() {
        let tools = AshnazgServer::tool_router().list_all();
        let mut names = tools
            .iter()
            .map(|tool| tool.name.as_ref())
            .collect::<Vec<_>>();
        names.sort_unstable();
        assert_eq!(
            names,
            [
                "app_install",
                "app_logs",
                "ui_act",
                "ui_screenshot",
                "ui_snapshot"
            ]
        );
        let act = tools.iter().find(|tool| tool.name == "ui_act").unwrap();
        let schema = serde_json::to_value(&act.input_schema).unwrap();
        let text = schema.to_string();
        for action in [
            "tap",
            "long_press",
            "type",
            "clear",
            "scroll_to",
            "back",
            "deep_link",
        ] {
            assert!(
                text.contains(action),
                "ui_act schema omitted action {action}: {text}"
            );
        }
        for field in ["testId", "text", "id", "snapshotId"] {
            assert!(
                text.contains(field),
                "ui_act schema omitted selector field {field}: {text}"
            );
        }
        assert!(!text.contains("device_boot"));
    }

    #[tokio::test]
    async fn streamable_http_requires_bearer_and_enforces_body_limit() {
        let config = crate::HostConfig {
            guest: GuestClient::new(
                "127.0.0.1:7777".parse::<SocketAddr>().unwrap(),
                "guest-token".into(),
            ),
            adb: "adb".into(),
        };
        let app = http_app(config, "http-secret".into());
        let unauthorized = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header("host", "localhost")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let oversized = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header("host", "localhost")
                    .header(AUTHORIZATION, "Bearer http-secret")
                    .header(CONTENT_TYPE, "application/json")
                    .header(CONTENT_LENGTH, (8 * 1024 * 1024 + 1).to_string())
                    .body(Body::from(vec![b'x'; 8 * 1024 * 1024 + 1]))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(oversized.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn streamable_http_negotiates_protocol_and_lists_implemented_tools() {
        let config = crate::HostConfig {
            guest: GuestClient::new(
                "127.0.0.1:7777".parse::<SocketAddr>().unwrap(),
                "guest-token".into(),
            ),
            adb: "adb".into(),
        };
        let app = http_app(config, "http-secret".into());
        let init = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/mcp")
                    .header("host", "localhost")
                    .header(AUTHORIZATION, "Bearer http-secret")
                    .header(CONTENT_TYPE, "application/json")
                    .header("accept", "application/json, text/event-stream")
                    .body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        if init.status() != StatusCode::OK {
            let status = init.status();
            let body = axum::body::to_bytes(init.into_body(), 1024 * 1024)
                .await
                .unwrap();
            panic!(
                "initialize failed with {status}: {}",
                String::from_utf8_lossy(&body)
            );
        }
        let session = init.headers().get("mcp-session-id").cloned();
        let body = axum::body::to_bytes(init.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let init_json = parse_mcp_response(&body);
        let version = init_json["result"]["protocolVersion"]
            .as_str()
            .expect("server must return negotiated protocol version")
            .to_owned();

        let mut request = Request::builder()
            .method("POST")
            .uri("/mcp")
            .header("host", "localhost")
            .header(AUTHORIZATION, "Bearer http-secret")
            .header(CONTENT_TYPE, "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("mcp-protocol-version", version);
        if let Some(session) = session {
            request = request.header("mcp-session-id", session);
        }
        let listed = app
            .oneshot(
                request
                    .body(Body::from(
                        r#"{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}"#,
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(listed.status(), StatusCode::OK);
        let body = axum::body::to_bytes(listed.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let tools = parse_mcp_response(&body);
        let names = tools["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|tool| tool["name"].as_str())
            .collect::<Vec<_>>();
        assert_eq!(names.len(), 5);
        assert!(!names.contains(&"device_boot"));
    }
}
