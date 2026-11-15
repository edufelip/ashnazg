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
use tokio_util::sync::CancellationToken;

use crate::{GuestError, HostConfig, app_install, validate_loopback_addr, validate_ui_act};

#[derive(Clone)]
pub struct AshnazgServer {
    config: HostConfig,
    tool_router: ToolRouter<Self>,
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
        description = "Install an APK using adb on the explicitly selected ANDROID_SERIAL; optionally launch the package.",
        annotations(destructive_hint = true)
    )]
    pub async fn app_install(
        &self,
        Parameters(params): Parameters<InstallParams>,
    ) -> Result<Json<Value>, ErrorData> {
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

#[cfg(test)]
mod tests {
    use super::{AshnazgServer, http_app};
    use crate::GuestClient;
    use axum::{
        body::Body,
        http::{
            Request, StatusCode,
            header::{AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE},
        },
    };
    use std::net::SocketAddr;
    use tower::ServiceExt;

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
