use std::{sync::Arc, time::Duration};

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode, header::AUTHORIZATION},
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use base64::{Engine as _, engine::general_purpose::STANDARD};
use futures_util::FutureExt;
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
    config: Option<HostConfig>,
    provider: NativeProvider,
    tool_router: ToolRouter<Self>,
    #[cfg(target_os = "macos")]
    ios: Option<std::sync::Arc<crate::ios_fixture::FixtureSession>>,
}

#[derive(Clone)]
enum NativeProvider {
    External,
    Ready(Arc<NativeInstallBackend>),
    Initializing(Arc<NativeReadiness>),
}

struct NativeReadiness {
    state: Mutex<Option<Result<Arc<NativeInstallBackend>, ()>>>,
    changed: tokio::sync::Notify,
    waiters: std::sync::atomic::AtomicUsize,
}

impl NativeReadiness {
    fn new() -> Self {
        Self {
            state: Mutex::new(None),
            changed: tokio::sync::Notify::new(),
            waiters: std::sync::atomic::AtomicUsize::new(0),
        }
    }

    async fn publish(&self, result: Result<Arc<NativeInstallBackend>, ()>) {
        *self.state.lock().await = Some(result);
        self.changed.notify_waiters();
    }

    async fn wait(
        &self,
        request_cancel: CancellationToken,
    ) -> Result<Arc<NativeInstallBackend>, ErrorData> {
        self.wait_until(
            request_cancel,
            tokio::time::Instant::now() + Duration::from_secs(30),
        )
        .await
    }

    async fn wait_until(
        &self,
        request_cancel: CancellationToken,
        deadline: tokio::time::Instant,
    ) -> Result<Arc<NativeInstallBackend>, ErrorData> {
        if request_cancel.is_cancelled() {
            return Err(ErrorData::internal_error("native request cancelled", None));
        }
        use std::sync::atomic::Ordering;
        loop {
            let current = self.waiters.load(Ordering::Acquire);
            if current >= 8 {
                return Err(ErrorData::internal_error(
                    "native runtime is busy; retry shortly",
                    None,
                ));
            }
            if self
                .waiters
                .compare_exchange(current, current + 1, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                break;
            }
        }
        struct Waiter<'a>(&'a std::sync::atomic::AtomicUsize);
        impl Drop for Waiter<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::AcqRel);
            }
        }
        let _waiter = Waiter(&self.waiters);
        loop {
            let changed = self.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            let state = self.state.lock().await.clone();
            if request_cancel.is_cancelled() {
                return Err(ErrorData::internal_error("native request cancelled", None));
            }
            if let Some(result) = state {
                return result.map_err(|_| {
                    ErrorData::internal_error("native runtime failed to start", None)
                });
            }
            tokio::select! {
                biased;
                _ = request_cancel.cancelled() => return Err(ErrorData::internal_error("native request cancelled", None)),
                _ = tokio::time::sleep_until(deadline) => return Err(ErrorData::internal_error("native runtime is still starting; retry the request", None)),
                _ = changed => {}
            }
        }
    }
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

        let deadline = std::time::Instant::now() + Duration::from_secs(180);
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
            config: Some(config),
            provider: NativeProvider::External,
            tool_router: Self::tool_router(),
            #[cfg(target_os = "macos")]
            ios: None,
        }
    }

    fn with_native(native: NativeInstallBackend) -> Self {
        Self {
            config: None,
            provider: NativeProvider::Ready(Arc::new(native)),
            tool_router: Self::tool_router(),
            #[cfg(target_os = "macos")]
            ios: None,
        }
    }

    fn with_readiness(readiness: Arc<NativeReadiness>) -> Self {
        Self {
            config: None,
            provider: NativeProvider::Initializing(readiness),
            tool_router: Self::tool_router(),
            #[cfg(target_os = "macos")]
            ios: None,
        }
    }

    /// The explicit iOS fixture entry point: exactly the three live ui_* tools
    /// are exposed, and the Android app tools are disabled.
    #[cfg(target_os = "macos")]
    fn with_ios(session: std::sync::Arc<crate::ios_fixture::FixtureSession>) -> Self {
        Self {
            config: None,
            provider: NativeProvider::External,
            tool_router: Self::tool_router()
                .with_disabled("app_install")
                .with_disabled("app_logs"),
            ios: Some(session),
        }
    }

    #[cfg(target_os = "macos")]
    fn ios_error(error: crate::ios_fixture::BridgeError) -> ErrorData {
        match error {
            crate::ios_fixture::BridgeError::Rejected(message) => {
                ErrorData::invalid_params(message, None)
            }
            crate::ios_fixture::BridgeError::Host(error) => {
                ErrorData::internal_error(error.to_string(), None)
            }
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

    async fn guest_json(
        &self,
        method: &str,
        params: Value,
        request_cancel: CancellationToken,
    ) -> Result<Value, ErrorData> {
        match &self.provider {
            NativeProvider::Ready(native) => {
                Self::native_guest_json(native, method, params, request_cancel).await
            }
            NativeProvider::Initializing(readiness) => {
                let native = readiness.wait(request_cancel.clone()).await?;
                Self::native_guest_json(&native, method, params, request_cancel).await
            }
            NativeProvider::External => self
                .config
                .as_ref()
                .ok_or_else(|| ErrorData::internal_error("native runtime failed to start", None))?
                .guest
                .call(method, params)
                .await
                .map_err(Self::guest_error),
        }
    }

    async fn native_guest_json(
        native: &NativeInstallBackend,
        method: &str,
        params: Value,
        request_cancel: CancellationToken,
    ) -> Result<Value, ErrorData> {
        tokio::select! {
            biased;
            _ = request_cancel.cancelled() => Err(ErrorData::internal_error("native request cancelled", None)),
            _ = native.lifecycle.cancellation.cancelled() => Err(ErrorData::internal_error("native runtime is stopping", None)),
            result = native.guest.call(method, params) => result.map_err(Self::guest_error),
        }
    }
}

/// Maps the tap and type actions both iOS sessions share onto the bridge
/// wire. Pure and host-independent so the focused tests execute the exact
/// mapping the live macOS sessions use.
#[cfg(any(target_os = "macos", test))]
fn bridge_tap_type_command(
    params: &ActParams,
    selectors_only: &'static str,
) -> Result<serde_json::Value, &'static str> {
    let target = params.target.as_ref();
    match params.action {
        Action::Tap => {
            let target = target.ok_or("tap requires a target")?;
            let mut command = serde_json::json!({"op": "ui_act", "action": "tap"});
            ios_selector_field(&mut command, target, selectors_only)?;
            Ok(command)
        }
        Action::Type => {
            let target = target.ok_or("type requires a target")?;
            // The shared schema permits an empty payload as a settled no-op.
            // The payload rides `value` so it never collides with the text
            // selector field.
            let payload = params.text.as_deref().ok_or("type requires text")?;
            let mut command = serde_json::json!({"op": "ui_act", "action": "type"});
            ios_selector_field(&mut command, target, selectors_only)?;
            command["value"] = serde_json::json!(payload);
            Ok(command)
        }
        _ => Err("only the tap and type actions map onto the shared bridge wire"),
    }
}

/// Maps one shared-schema ui_act request onto the private iOS fixture bridge
/// wire. Pure and host-independent so the focused tests execute the exact
/// mapping the live macOS session uses. Rejections are static strings, so a
/// typed payload can never leak into an error.
#[cfg(any(target_os = "macos", test))]
fn ios_bridge_command(params: &ActParams) -> Result<serde_json::Value, &'static str> {
    match params.action {
        Action::Tap | Action::Type => {
            bridge_tap_type_command(params, "iOS fixture selectors are testId or text only")
        }
        _ => Err("the iOS fixture bridge supports only the tap and type actions"),
    }
}

/// Maps one shared-schema ui_act request onto the external iOS app bridge
/// wire. Pure and host-independent so the focused tests execute the exact
/// mapping the live macOS session uses. `deep_link` must name an exact
/// allowlisted app-owned URI; `scroll_to` carries one explicit direction
/// because the bridge dispatches exactly one bounded swipe. Rejections
/// are static strings, so a typed payload can never leak into an error.
#[cfg(any(target_os = "macos", test))]
fn ios_app_bridge_command(
    params: &ActParams,
    allowlist: &[String],
) -> Result<serde_json::Value, &'static str> {
    match params.action {
        Action::Tap | Action::Type => {
            bridge_tap_type_command(params, "iOS app selectors are testId or text only")
        }
        Action::ScrollTo => {
            let target = params
                .target
                .as_ref()
                .ok_or("scroll_to requires a target")?;
            // One bounded native swipe needs an explicit direction; the
            // shared schema keeps the field optional, so the app bridge
            // fails closed instead of inventing a default.
            let direction = params
                .direction
                .as_ref()
                .ok_or("scroll_to requires a direction")?;
            let mut command =
                serde_json::json!({"op": "ui_act", "action": "scroll_to", "direction": direction});
            ios_selector_field(
                &mut command,
                target,
                "iOS app selectors are testId or text only",
            )?;
            Ok(command)
        }
        Action::DeepLink => {
            let url = params
                .text
                .as_deref()
                .ok_or("deep_link requires text containing a URI")?;
            if allowlist.iter().any(|allowed| allowed == url) {
                Ok(serde_json::json!({"op": "ui_act", "action": "deep_link", "url": url}))
            } else {
                Err("deep link is not in the app manifest allowlist")
            }
        }
        _ => {
            Err("the iOS app bridge supports only the tap, type, scroll_to, and deep_link actions")
        }
    }
}

#[cfg(any(target_os = "macos", test))]
fn ios_selector_field(
    command: &mut serde_json::Value,
    target: &Target,
    selectors_only: &'static str,
) -> Result<(), &'static str> {
    if let Some(test_id) = target.test_id.as_deref() {
        command["testId"] = serde_json::json!(test_id);
    } else if let Some(text) = target.text.as_deref() {
        command["text"] = serde_json::json!(text);
    } else {
        return Err(selectors_only);
    }
    Ok(())
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
        request_cancel: CancellationToken,
    ) -> Result<Json<Value>, ErrorData> {
        #[cfg(target_os = "macos")]
        if let Some(ios) = &self.ios {
            let snapshot = ios
                .snapshot(&request_cancel)
                .await
                .map_err(Self::ios_error)?;
            return Ok(Json(snapshot));
        }
        self.guest_json(
            "ui_snapshot",
            serde_json::json!({"includeInvisible":params.include_invisible}),
            request_cancel,
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
        request_cancel: CancellationToken,
    ) -> Result<Json<Value>, ErrorData> {
        let value = serde_json::to_value(&params)
            .map_err(|e| ErrorData::invalid_params(e.to_string(), None))?;
        validate_ui_act(&value).map_err(|e| ErrorData::invalid_params(e.to_string(), None))?;
        #[cfg(target_os = "macos")]
        if let Some(ios) = &self.ios {
            let command = if ios.is_app_launch() {
                ios_app_bridge_command(&params, ios.deep_links())
                    .map_err(|message| ErrorData::invalid_params(message.to_owned(), None))?
            } else {
                ios_bridge_command(&params)
                    .map_err(|message| ErrorData::invalid_params(message.to_owned(), None))?
            };
            let acted = ios
                .act(&command, &request_cancel)
                .await
                .map_err(Self::ios_error)?;
            return Ok(Json(acted));
        }
        self.guest_json("ui_act", value, request_cancel)
            .await
            .map(Json)
    }

    #[tool(
        name = "ui_screenshot",
        description = "Capture a guest screenshot; returns both screenshot image content and metadata.",
        annotations(read_only_hint = true)
    )]
    pub async fn ui_screenshot(
        &self,
        Parameters(params): Parameters<ScreenshotParams>,
        request_cancel: CancellationToken,
    ) -> Result<CallToolResult, ErrorData> {
        #[cfg(target_os = "macos")]
        if let Some(ios) = &self.ios {
            match params.format.as_ref() {
                None | Some(ScreenshotFormat::Png) => {}
                Some(ScreenshotFormat::Webp) => {
                    return Err(ErrorData::invalid_params(
                        "the iOS fixture bridge captures PNG screenshots only",
                        None,
                    ));
                }
            }
            if params.som_overlay {
                return Err(ErrorData::invalid_params(
                    "the iOS fixture bridge does not support somOverlay",
                    None,
                ));
            }
            let (png, sequence, counter) = ios
                .screenshot(&request_cancel)
                .await
                .map_err(Self::ios_error)?;
            let (width, height) = crate::ios_fixture::png_dimensions(&png)
                .ok_or_else(|| ErrorData::internal_error("bridge screenshot is not a PNG", None))?;
            let mut metadata = serde_json::json!({
                "mimeType": "image/png",
                "snapshotId": sequence,
                "marks": [],
                "width": width,
                "height": height,
                "platform": ios.platform(),
            });
            // Only the fixture reports a counter for state correlation; an
            // external app session reports its observed image only.
            if let Some(counter) = counter {
                metadata["counterLabel"] = serde_json::json!(counter);
            }
            return Ok(CallToolResult::success(vec![
                ContentBlock::image(STANDARD.encode(&png), "image/png".to_owned()),
                ContentBlock::text(metadata.to_string()),
            ]));
        }
        let format = match params.format.unwrap_or(ScreenshotFormat::Webp) {
            ScreenshotFormat::Webp => "webp",
            ScreenshotFormat::Png => "png",
        };
        let value = self
            .guest_json(
                "ui_screenshot",
                serde_json::json!({"format":format,"somOverlay":params.som_overlay}),
                request_cancel,
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
        request_cancel: CancellationToken,
    ) -> Result<Json<Value>, ErrorData> {
        self.guest_json(
            "app_logs",
            serde_json::to_value(params)
                .map_err(|e| ErrorData::invalid_params(e.to_string(), None))?,
            request_cancel,
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
        if !params.launch.unwrap_or(false) && params.package.is_some() {
            return Err(ErrorData::invalid_params(
                "package requires launch=true",
                None,
            ));
        }
        let native = match &self.provider {
            NativeProvider::Ready(native) => Some(Arc::clone(native)),
            NativeProvider::Initializing(readiness) => {
                Some(readiness.wait(request_cancel.clone()).await?)
            }
            NativeProvider::External => None,
        };
        if let Some(native) = native {
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
        let config = self
            .config
            .clone()
            .ok_or_else(|| ErrorData::internal_error("native runtime failed to start", None))?;
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

/// Serves the explicit macOS-only iOS fixture session over stdio. Exactly the
/// three live ui_* tools are exposed for the fixture bridge session.
#[cfg(target_os = "macos")]
pub async fn serve_stdio_ios(
    session: std::sync::Arc<crate::ios_fixture::FixtureSession>,
) -> anyhow::Result<()> {
    use rmcp::ServiceExt;
    let server = AshnazgServer::with_ios(session);
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

trait NativeSessionOwner: Send + Sync + 'static {
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
    AshnazgServer::with_native(backend)
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

async fn serve_initializing_transport<R, W>(
    readiness: Arc<NativeReadiness>,
    read: R,
    write: W,
    lifecycle: CancellationToken,
) -> anyhow::Result<()>
where
    R: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    use rmcp::ServiceExt;
    let server = AshnazgServer::with_readiness(readiness);
    let initializing = server.serve_with_ct((read, write), lifecycle.clone());
    tokio::pin!(initializing);
    let running = tokio::select! {
        result = &mut initializing => match result {
            Ok(running) => running,
            Err(_) if lifecycle.is_cancelled() => return Ok(()),
            Err(_) => anyhow::bail!("native MCP serving failed"),
        },
        _ = lifecycle.cancelled() => match initializing.await {
            Ok(running) => running,
            Err(_) => return Ok(()),
        }
    };
    let waiting = running.waiting();
    tokio::pin!(waiting);
    tokio::select! {
        result = &mut waiting => result.map(|_| ()).map_err(|_| anyhow::anyhow!("native MCP serving failed")),
        _ = lifecycle.cancelled() => waiting.await.map(|_| ()).map_err(|_| anyhow::anyhow!("native MCP serving failed")),
    }
}

fn initializing_http_app(
    readiness: Arc<NativeReadiness>,
    bearer: String,
    lifecycle: CancellationToken,
) -> axum::Router {
    let service = rmcp::transport::streamable_http_server::StreamableHttpService::new(
        move || Ok(AshnazgServer::with_readiness(Arc::clone(&readiness))),
        Arc::new(
            rmcp::transport::streamable_http_server::session::local::LocalSessionManager::default(),
        ),
        rmcp::transport::streamable_http_server::StreamableHttpServerConfig::default()
            .with_sse_keep_alive(Some(Duration::from_secs(15)))
            .with_cancellation_token(lifecycle.clone()),
    );
    axum::Router::new()
        .route_service("/mcp", service)
        .layer(tower_http::limit::RequestBodyLimitLayer::new(
            8 * 1024 * 1024,
        ))
        .layer(middleware::from_fn_with_state(bearer, authorize))
        .layer(middleware::from_fn_with_state(
            lifecycle,
            cancel_native_http_request,
        ))
}

fn initializing_serve_result(
    result: Result<anyhow::Result<()>, tokio::task::JoinError>,
) -> anyhow::Result<()> {
    result.unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")))
}

async fn run_initializing_owner<Fut, S, F>(
    initialize: Fut,
    readiness: Arc<NativeReadiness>,
    lifecycle_token: CancellationToken,
    serve: F,
) -> anyhow::Result<()>
where
    Fut: Future<Output = anyhow::Result<S>> + Send + 'static,
    S: NativeSessionOwner,
    F: FnOnce(Arc<NativeReadiness>, CancellationToken) -> NativeServeFuture + Send + 'static,
{
    let lifecycle = Arc::new(InstallLifecycle::new(lifecycle_token.clone()));
    let serve_readiness = Arc::clone(&readiness);
    let serve_token = lifecycle_token.clone();
    let mut serving = tokio::spawn(async move {
        // Include construction as well as polling in the unwind boundary.
        std::panic::AssertUnwindSafe(async move { serve(serve_readiness, serve_token).await })
            .catch_unwind()
            .await
            .unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")))
    });
    let initialize = std::panic::AssertUnwindSafe(initialize).catch_unwind();
    tokio::pin!(initialize);
    let mut serving_result = None;
    let init_result = tokio::select! {
        result = &mut initialize => result,
        result = &mut serving => {
            serving_result = Some(result);
            lifecycle_token.cancel();
            initialize.await
        }
        _ = lifecycle_token.cancelled() => initialize.await,
    };
    let init_panicked = init_result.is_err();
    let init_result =
        init_result.unwrap_or_else(|_| Err(anyhow::anyhow!("native runtime failed to start")));
    let session = match init_result {
        Ok(session) => session,
        Err(_) => {
            readiness.publish(Err(())).await;
            let cancelled_during_init = lifecycle_token.is_cancelled();
            let result = match serving_result {
                Some(result) => result,
                None if cancelled_during_init => serving.await,
                None => tokio::select! {
                    _ = lifecycle_token.cancelled() => serving.await,
                    result = &mut serving => result,
                },
            };
            lifecycle_token.cancel();
            lifecycle.close_and_drain().await;
            let serving_ok = initializing_serve_result(result).is_ok();
            return match (serving_ok, cancelled_during_init && !init_panicked) {
                (true, true) => Ok(()),
                (false, true) => Err(anyhow::anyhow!("native MCP serving failed")),
                (true, false) => Err(anyhow::anyhow!("native runtime failed to start")),
                (false, false) => Err(anyhow::anyhow!(
                    "native runtime failed to start; native MCP serving failed"
                )),
            };
        }
    };

    // Keep the unique session owner outside all serving/factory unwind paths.
    let mut serving_joined = serving_result.is_some();
    let server_result = std::panic::AssertUnwindSafe(async {
        let backend = match (session.guest_client(), session.install_client()) {
            (Ok(guest), Ok(client)) => Ok(Arc::new(NativeInstallBackend {
                guest,
                client,
                serial: session.adb_addr().to_string(),
                lifecycle: Arc::clone(&lifecycle),
            })),
            _ => Err(()),
        };
        let backend_ok = backend.is_ok();
        if lifecycle_token.is_cancelled() {
            readiness.publish(Err(())).await;
        } else {
            readiness.publish(backend).await;
        }
        let result = if let Some(result) = serving_result.take() {
            initializing_serve_result(result)
        } else {
            let terminated = session.wait_terminated();
            tokio::pin!(terminated);
            let result = tokio::select! {
                result = &mut serving => initializing_serve_result(result),
                _ = lifecycle_token.cancelled() => initializing_serve_result((&mut serving).await),
                _ = &mut terminated => {
                    lifecycle_token.cancel();
                    let serving_result = initializing_serve_result((&mut serving).await);
                    serving_result.and(Err(anyhow::anyhow!("native helper exited unexpectedly")))
                }
            };
            serving_joined = true;
            result
        };
        if backend_ok {
            result
        } else {
            result.and(Err(anyhow::anyhow!("native runtime failed to start")))
        }
    })
    .catch_unwind()
    .await
    .unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")));

    lifecycle_token.cancel();
    readiness.publish(Err(())).await;
    if !serving_joined {
        // A factory panic may happen before the normal serving wait begins.
        // Cancel and join that task before tearing down its session.
        let _ = (&mut serving).await;
    }
    lifecycle.close_and_drain().await;
    let shutdown_result = Box::new(session).shutdown().await;
    match (server_result.is_ok(), shutdown_result.is_ok()) {
        (true, true) => Ok(()),
        (false, true) => Err(anyhow::anyhow!("native MCP serving failed")),
        (true, false) => Err(anyhow::anyhow!("native helper shutdown failed")),
        (false, false) => Err(anyhow::anyhow!(
            "native MCP serving failed; native helper shutdown failed"
        )),
    }
}

async fn run_initializing_public<Fut, S, Init, F>(
    initialize: Init,
    cancel: CancellationToken,
    serve: F,
) -> anyhow::Result<()>
where
    Fut: Future<Output = anyhow::Result<S>> + Send + 'static,
    S: NativeSessionOwner,
    Init: FnOnce(CancellationToken) -> Fut + Send + 'static,
    F: FnOnce(Arc<NativeReadiness>, CancellationToken) -> NativeServeFuture + Send + 'static,
{
    let lifecycle = cancel.child_token();
    let guard = CancelOnDrop(lifecycle.clone());
    let readiness = Arc::new(NativeReadiness::new());
    let initialize_token = lifecycle.clone();
    let initialize = async move { initialize(initialize_token).await };
    let owner = tokio::spawn(run_initializing_owner(
        initialize, readiness, lifecycle, serve,
    ));
    let result = owner
        .await
        .map_err(|_| anyhow::anyhow!("native MCP lifecycle task failed"))?;
    drop(guard);
    result
}

/// Starts native MCP immediately while one retained initializer prepares the runtime.
pub async fn serve_initializing_native_io<R, W, Init, InitFuture>(
    initialize: Init,
    read: R,
    write: W,
    cancel: CancellationToken,
) -> anyhow::Result<()>
where
    R: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
    Init: FnOnce(CancellationToken) -> InitFuture + Send + 'static,
    InitFuture: Future<Output = anyhow::Result<NativeSession>> + Send + 'static,
{
    run_initializing_public(initialize, cancel, move |readiness, lifecycle| {
        Box::pin(serve_initializing_transport(
            readiness, read, write, lifecycle,
        ))
    })
    .await
}

/// Starts authenticated loopback MCP after validating the listener and bearer.
pub async fn serve_initializing_native_http<Init, InitFuture>(
    initialize: Init,
    listener: tokio::net::TcpListener,
    bearer: String,
    cancel: CancellationToken,
) -> anyhow::Result<()>
where
    Init: FnOnce(CancellationToken) -> InitFuture + Send + 'static,
    InitFuture: Future<Output = anyhow::Result<NativeSession>> + Send + 'static,
{
    let address = listener
        .local_addr()
        .map_err(|_| anyhow::anyhow!("native MCP serving failed"))?;
    validate_native_http_inputs(address, &bearer)
        .map_err(|_| anyhow::anyhow!("native MCP serving failed"))?;
    run_initializing_public(initialize, cancel, move |readiness, lifecycle| {
        Box::pin(async move {
            axum::serve(
                listener,
                initializing_http_app(readiness, bearer, lifecycle.clone()),
            )
            .with_graceful_shutdown(lifecycle.cancelled_owned())
            .await
            .map_err(|_| anyhow::anyhow!("native MCP serving failed"))
        })
    })
    .await
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
    // The session stays outside the unwind boundary so a panic in transport
    // construction or polling cannot drop its unique cleanup join handle.
    let server_result = std::panic::AssertUnwindSafe(async {
    let backend = match (session.guest_client(), session.install_client()) {
        (Ok(guest), Ok(client)) => Ok(NativeInstallBackend {
            guest,
            client,
            serial: session.adb_addr().to_string(),
            lifecycle: Arc::clone(&lifecycle),
        }),
        _ => Err(anyhow::anyhow!("native MCP serving failed")),
    };

    match backend {
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
    }
    }).catch_unwind().await.unwrap_or_else(|_| Err(anyhow::anyhow!("native MCP serving failed")));

    lifecycle_token.cancel();
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
            .with_cancellation_token(lifecycle.clone()),
    );
    axum::Router::new()
        .route_service("/mcp", service)
        .layer(tower_http::limit::RequestBodyLimitLayer::new(
            8 * 1024 * 1024,
        ))
        .layer(middleware::from_fn_with_state(token, authorize))
        .layer(middleware::from_fn_with_state(
            lifecycle,
            cancel_native_http_request,
        ))
}

async fn cancel_native_http_request(
    State(cancel): State<CancellationToken>,
    request: Request<Body>,
    next: Next,
) -> Response {
    tokio::select! {
        biased;
        _ = cancel.cancelled() => StatusCode::SERVICE_UNAVAILABLE.into_response(),
        response = next.run(request) => response,
    }
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
        ActParams, Action, AshnazgServer, Direction, NativeReadiness, NativeSessionOwner,
        NativeShutdownFuture, NativeWaitFuture, Target, http_app, initializing_http_app,
        ios_app_bridge_command, ios_bridge_command, run_initializing_public, run_native_owner,
        run_native_public, serve_initializing_native_io, serve_initializing_transport,
        serve_native_http_listener, serve_native_transport, validate_native_http_inputs,
        validate_ui_act,
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
        guest_endpoint: SocketAddr,
        install_endpoint: SocketAddr,
        termination:
            Arc<tokio::sync::Mutex<Option<tokio::sync::oneshot::Receiver<anyhow::Result<()>>>>>,
        shutdown_fails: bool,
    }

    impl NativeSessionOwner for FixtureOwner {
        fn guest_client(&self) -> anyhow::Result<crate::GuestClient> {
            Ok(crate::GuestClient::new(
                self.guest_endpoint,
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
            guest_endpoint: "127.0.0.1:0".parse().unwrap(),
            install_endpoint,
            termination: Arc::new(tokio::sync::Mutex::new(None)),
            shutdown_fails: false,
        }
    }

    #[tokio::test]
    async fn native_readiness_caps_waiters_and_keeps_cancellation_local() {
        let readiness = Arc::new(NativeReadiness::new());
        let mut request_cancels = Vec::new();
        let mut waiters = Vec::new();
        for _ in 0..8 {
            let cancel = CancellationToken::new();
            request_cancels.push(cancel.clone());
            let readiness = Arc::clone(&readiness);
            waiters.push(tokio::spawn(async move { readiness.wait(cancel).await }));
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while readiness.waiters.load(std::sync::atomic::Ordering::Acquire) != 8 {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let capped = readiness
            .wait(CancellationToken::new())
            .await
            .err()
            .unwrap();
        assert!(capped.to_string().contains("busy"));
        request_cancels[0].cancel();
        assert!(
            waiters
                .remove(0)
                .await
                .unwrap()
                .err()
                .unwrap()
                .to_string()
                .contains("cancelled")
        );
        readiness.publish(Err(())).await;
        for waiter in waiters {
            assert!(
                waiter
                    .await
                    .unwrap()
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("failed to start")
            );
        }
    }

    #[tokio::test]
    async fn readiness_does_not_return_a_backend_to_an_already_cancelled_request() {
        let owner = fixture_owner(
            Arc::new(StdMutex::new(Vec::new())),
            "127.0.0.1:4321".parse().unwrap(),
        );
        let readiness = NativeReadiness::new();
        readiness
            .publish(Ok(Arc::new(super::NativeInstallBackend {
                guest: owner.guest_client().unwrap(),
                client: owner.install_client().unwrap(),
                serial: owner.adb_addr().to_string(),
                lifecycle: Arc::new(super::InstallLifecycle::new(CancellationToken::new())),
            })))
            .await;
        let request = CancellationToken::new();
        request.cancel();
        let error = readiness
            .wait(request)
            .await
            .err()
            .expect("cancelled request must not acquire backend");
        assert!(error.to_string().contains("cancelled"));
    }

    #[tokio::test]
    async fn initializing_owner_preserves_serving_failure_when_init_is_cancelled() {
        for panic_during_poll in [false, true] {
            let parent = CancellationToken::new();
            let result = run_initializing_public(
                |lifecycle| async move {
                    lifecycle.cancelled().await;
                    Err::<FixtureOwner, _>(anyhow::anyhow!("fixture cancelled during startup"))
                },
                parent.clone(),
                move |_, _| {
                    Box::pin(async move {
                        assert!(!panic_during_poll, "fixture serving panic");
                        Err(anyhow::anyhow!("fixture serving failure"))
                    })
                },
            )
            .await;
            assert!(
                result.is_err(),
                "serving failure must not become successful cancellation"
            );
            assert!(!parent.is_cancelled());
        }
    }

    #[tokio::test]
    async fn pending_readiness_deadline_expires_and_releases_its_slot() {
        let readiness = NativeReadiness::new();
        let error = readiness
            .wait_until(CancellationToken::new(), tokio::time::Instant::now())
            .await
            .err()
            .unwrap();
        assert!(error.to_string().contains("still starting"));
        assert_eq!(
            readiness.waiters.load(std::sync::atomic::Ordering::Acquire),
            0
        );
        // The production entry point supplies one 30-second absolute deadline
        // to this same loop; no progress notification recreates that deadline.
        assert!(readiness.state.lock().await.is_none());
    }

    #[tokio::test]
    async fn initializing_owner_ready_paths_join_shutdown_and_preserve_failures() {
        for mode in 0..5 {
            let parent = CancellationToken::new();
            let events = Arc::new(StdMutex::new(Vec::new()));
            let mut owner = fixture_owner(Arc::clone(&events), "127.0.0.1:4321".parse().unwrap());
            owner.shutdown_fails = mode == 4;
            let (exited, exit) = tokio::sync::oneshot::channel();
            if mode == 1 {
                *owner.termination.lock().await = Some(exit);
            }
            let result = run_initializing_public(
                move |_| async move { Ok(owner) },
                parent.clone(),
                move |readiness, lifecycle| {
                    assert_ne!(mode, 2, "fixture serving constructor panic");
                    Box::pin(async move {
                        readiness
                            .wait(CancellationToken::new())
                            .await
                            .map_err(|_| anyhow::anyhow!("fixture never ready"))?;
                        match mode {
                            1 => {
                                let _ = exited.send(Err(anyhow::anyhow!("fixture helper exit")));
                                lifecycle.cancelled().await;
                                Ok(())
                            }
                            3 => panic!("fixture ready serving panic"),
                            _ => {
                                lifecycle.cancel();
                                Ok(())
                            }
                        }
                    })
                },
            )
            .await;
            assert_eq!(
                *events.lock().unwrap(),
                ["shutdown"],
                "mode {mode} lost session cleanup"
            );
            assert!(!parent.is_cancelled());
            assert_eq!(
                result.is_ok(),
                mode == 0,
                "mode {mode} lost a serving/helper/shutdown failure"
            );
        }
    }

    #[tokio::test]
    async fn serving_constructor_panic_still_awaits_initialization_cleanup() {
        let cleaned = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let cleaned_by_initializer = Arc::clone(&cleaned);
        let parent = CancellationToken::new();
        let result = run_initializing_public(
            move |lifecycle| async move {
                lifecycle.cancelled().await;
                cleaned_by_initializer.store(true, std::sync::atomic::Ordering::Release);
                Err::<FixtureOwner, _>(anyhow::anyhow!("fixture init cancelled"))
            },
            parent.clone(),
            |_, _| panic!("fixture constructor panic"),
        )
        .await;
        assert!(result.is_err());
        assert!(cleaned.load(std::sync::atomic::Ordering::Acquire));
        assert!(!parent.is_cancelled());
    }

    #[tokio::test]
    async fn initializer_constructor_panic_cancels_and_joins_transport() {
        let served = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let served_by_transport = Arc::clone(&served);
        let parent = CancellationToken::new();
        let result = run_initializing_public(
            |_| -> std::future::Ready<anyhow::Result<FixtureOwner>> {
                panic!("fixture initializer constructor panic")
            },
            parent.clone(),
            move |readiness, _| {
                Box::pin(async move {
                    let result = readiness.wait(CancellationToken::new()).await;
                    served_by_transport.store(true, std::sync::atomic::Ordering::Release);
                    assert!(result.is_err());
                    Ok(())
                })
            },
        )
        .await;
        assert!(result.is_err());
        assert!(served.load(std::sync::atomic::Ordering::Acquire));
        assert!(!parent.is_cancelled());
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

    #[tokio::test]
    async fn initializing_native_io_handshakes_and_lists_while_one_initializer_is_pending() {
        let (client, server) = tokio::io::duplex(32 * 1024);
        let (read, write) = tokio::io::split(server);
        let cancel = CancellationToken::new();
        let initializer_cancelled = cancel.clone();
        let starts = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let starts_by_initializer = Arc::clone(&starts);
        let server_task = tokio::spawn(async move {
            serve_initializing_native_io(
                move |lifecycle| async move {
                    starts_by_initializer.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    lifecycle.cancelled().await;
                    Err(anyhow::anyhow!("fixture startup failure"))
                },
                read,
                write,
                cancel,
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
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&line).unwrap()["id"],
            1
        );
        line.clear();
        write.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}\n").await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                line.clear();
                read.read_line(&mut line).await.unwrap();
                let value: serde_json::Value = serde_json::from_str(&line).unwrap();
                if value["id"] == 2 {
                    assert_eq!(value["result"]["tools"].as_array().unwrap().len(), 5);
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert_eq!(starts.load(std::sync::atomic::Ordering::SeqCst), 1);
        initializer_cancelled.cancel();
        tokio::time::timeout(Duration::from_secs(3), server_task)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn initialization_failure_keeps_mcp_available_and_hides_initializer_details() {
        let (client, server) = tokio::io::duplex(32 * 1024);
        let (read, write) = tokio::io::split(server);
        let cancel = CancellationToken::new();
        let (client_read, mut client_write) = tokio::io::split(client);
        let mut client_read = BufReader::new(client_read);
        let task = tokio::spawn(run_initializing_public(
            |_lifecycle| async {
                Err::<FixtureOwner, _>(anyhow::anyhow!("private fixture credential"))
            },
            cancel,
            move |readiness, lifecycle| {
                Box::pin(serve_initializing_transport(
                    readiness, read, write, lifecycle,
                ))
            },
        ));
        client_write.write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"fixture\",\"version\":\"1\"}}}\n").await.unwrap();
        let mut line = String::new();
        tokio::time::timeout(Duration::from_secs(2), client_read.read_line(&mut line))
            .await
            .unwrap()
            .unwrap();
        line.clear();
        client_write.write_all(b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}\n{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"app_logs\",\"arguments\":{}}}\n").await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                line.clear();
                client_read.read_line(&mut line).await.unwrap();
                let value: serde_json::Value = serde_json::from_str(&line).unwrap();
                if value["id"] == 2 {
                    let response = value.to_string();
                    assert!(response.contains("native runtime failed to start"));
                    assert!(!response.contains("private fixture credential"));
                    break;
                }
            }
        })
        .await
        .unwrap();
        client_write.shutdown().await.unwrap();
        let result = tokio::time::timeout(Duration::from_secs(3), task)
            .await
            .unwrap()
            .unwrap();
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("native runtime failed to start")
        );
    }

    #[tokio::test]
    async fn dropping_initializing_caller_retains_and_cancels_initializer_cleanup() {
        let (client, server) = tokio::io::duplex(1024);
        let (read, write) = tokio::io::split(server);
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (cleaned_tx, cleaned_rx) = tokio::sync::oneshot::channel();
        let (served_tx, served_rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(run_initializing_public(
            move |lifecycle| async move {
                let _ = started_tx.send(());
                lifecycle.cancelled().await;
                let _ = cleaned_tx.send(());
                Err::<FixtureOwner, _>(anyhow::anyhow!("fixture cancelled"))
            },
            CancellationToken::new(),
            move |readiness, lifecycle| {
                Box::pin(async move {
                    let result =
                        serve_initializing_transport(readiness, read, write, lifecycle).await;
                    let _ = served_tx.send(());
                    result
                })
            },
        ));
        tokio::time::timeout(Duration::from_secs(2), started_rx)
            .await
            .unwrap()
            .unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        drop(client);
        tokio::time::timeout(Duration::from_secs(2), cleaned_rx)
            .await
            .unwrap()
            .unwrap();
        tokio::time::timeout(Duration::from_secs(2), served_rx)
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn initializing_transport_eof_cancels_and_joins_pending_initializer() {
        let (client, server) = tokio::io::duplex(1024);
        let (read, write) = tokio::io::split(server);
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (cleaned_tx, cleaned_rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(run_initializing_public(
            move |lifecycle| async move {
                let _ = started_tx.send(());
                lifecycle.cancelled().await;
                let _ = cleaned_tx.send(());
                Err::<FixtureOwner, _>(anyhow::anyhow!("fixture cancelled"))
            },
            CancellationToken::new(),
            move |readiness, lifecycle| {
                Box::pin(serve_initializing_transport(
                    readiness, read, write, lifecycle,
                ))
            },
        ));
        tokio::time::timeout(Duration::from_secs(2), started_rx)
            .await
            .unwrap()
            .unwrap();
        drop(client);
        tokio::time::timeout(Duration::from_secs(2), cleaned_rx)
            .await
            .unwrap()
            .unwrap();
        let error = tokio::time::timeout(Duration::from_secs(2), task)
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        // EOF before MCP initialize is a serving failure even though pending
        // initializer cancellation and cleanup completed successfully.
        assert!(error.to_string().contains("serving failed"));
    }

    fn accept_native_fixture(listener: &std::net::TcpListener) -> std::net::TcpStream {
        listener.set_nonblocking(true).unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        let stream = loop {
            match listener.accept() {
                Ok((stream, _)) => break stream,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(
                        std::time::Instant::now() < deadline,
                        "fixture accept expired"
                    );
                    std::thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("fixture accept failed: {error}"),
            }
        };
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        stream
    }

    fn native_upload_peer(
        listener: std::net::TcpListener,
        expected: usize,
    ) -> std::thread::JoinHandle<Vec<(serde_json::Value, Vec<u8>)>> {
        std::thread::spawn(move || {
            use std::io::{BufRead, Read, Write};
            let mut received = Vec::new();
            for _ in 0..expected {
                let mut stream = accept_native_fixture(&listener);
                let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
                let mut header_line = String::new();
                reader.read_line(&mut header_line).unwrap();
                let header: serde_json::Value = serde_json::from_str(&header_line).unwrap();
                let size = header["apk_size"].as_u64().unwrap() as usize;
                assert!(size <= 64 * 1024, "fixture body exceeded its cap");
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
    async fn native_http_shutdown_does_not_wait_for_an_incomplete_request_body() {
        let events = Arc::new(StdMutex::new(Vec::new()));
        let install_listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let owner = fixture_owner(Arc::clone(&events), install_listener.local_addr().unwrap());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let cancel = CancellationToken::new();
        let task_cancel = cancel.clone();
        let mut task = tokio::spawn(run_native_owner(
            owner,
            task_cancel.child_token(),
            move |backend, ct| {
                Box::pin(serve_native_http_listener(
                    backend,
                    listener,
                    "body-secret".into(),
                    ct,
                ))
            },
        ));
        let stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let mut stream = BufReader::new(stream);
        stream.get_mut().write_all(format!(
            "POST /mcp HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer body-secret\r\nContent-Type: application/json\r\nAccept: application/json, text/event-stream\r\nContent-Length: 1024\r\nExpect: 100-continue\r\n\r\n"
        ).as_bytes()).await.unwrap();
        let mut interim = String::new();
        tokio::time::timeout(Duration::from_secs(2), stream.read_line(&mut interim))
            .await
            .unwrap()
            .unwrap();
        assert!(interim.starts_with("HTTP/1.1 100"));
        cancel.cancel();
        let result = tokio::time::timeout(Duration::from_secs(2), &mut task).await;
        // Release the peer and join even when the old implementation times out.
        drop(stream);
        let timed_out = result.is_err();
        if timed_out {
            tokio::time::timeout(Duration::from_secs(3), task)
                .await
                .unwrap()
                .unwrap()
                .unwrap();
        } else {
            result.unwrap().unwrap().unwrap();
        }
        assert!(
            !timed_out,
            "HTTP shutdown waited for the client's unfinished body"
        );
        assert_eq!(*events.lock().unwrap(), ["shutdown"]);
        drop(tokio::net::TcpListener::bind(address).await.unwrap());
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
    async fn native_install_allows_package_manager_completion_after_the_upload_budget() {
        // A real-clock regression is intentional: the native worker uses std
        // Instant, so Tokio's paused clock cannot exercise this phase boundary.
        use std::io::{BufRead, Read, Write};
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let peer = std::thread::spawn(move || {
            let until = std::time::Instant::now() + Duration::from_secs(3);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(value) => break value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(std::time::Instant::now() < until);
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("fixture accept failed: {error}"),
                }
            };
            stream.set_nonblocking(false).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut reader = std::io::BufReader::new(stream.try_clone().unwrap());
            let mut header = String::new();
            reader.read_line(&mut header).unwrap();
            stream
                .write_all(b"{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n")
                .unwrap();
            let mut body = Vec::new();
            reader.take(1024).read_to_end(&mut body).unwrap();
            assert_eq!(body, b"fixture with deferred installation result");
            std::thread::sleep(Duration::from_secs(31));
            let _ = stream.write_all(b"{\"schema_version\":1,\"status\":\"installed\",\"session_id\":\"00000000-0000-4000-8000-000000000000\",\"launched\":false}\n");
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
        std::fs::write(file.path(), b"fixture with deferred installation result").unwrap();
        let lifecycle = Arc::new(super::InstallLifecycle::new(CancellationToken::new()));
        let result = tokio::time::timeout(
            Duration::from_secs(40),
            lifecycle.install(
                client,
                file.path().to_string_lossy().into_owned(),
                false,
                None,
                CancellationToken::new(),
            ),
        )
        .await
        .unwrap();
        lifecycle.close_and_drain().await;
        peer.join().unwrap();
        assert!(
            result.is_ok(),
            "native adapter shortened the installer's result deadline"
        );
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

    #[tokio::test]
    async fn native_serving_panics_still_join_session_shutdown_before_returning() {
        for during_poll in [false, true] {
            let events = Arc::new(StdMutex::new(Vec::new()));
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let owner = fixture_owner(Arc::clone(&events), listener.local_addr().unwrap());
            let parent = CancellationToken::new();
            let result = run_native_public(owner, parent.clone(), move |_, _| {
                assert!(during_poll, "fixture constructor panic");
                Box::pin(async { panic!("fixture polling panic") })
            })
            .await;
            assert!(result.is_err());
            assert_eq!(*events.lock().unwrap(), ["shutdown"]);
            assert_eq!(result.unwrap_err().to_string(), "native MCP serving failed");
            assert!(!parent.is_cancelled());
        }
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
    fn acceptance_schema_fixture_matches_all_production_tool_inputs() {
        let actual: serde_json::Map<String, serde_json::Value> = AshnazgServer::tool_router()
            .list_all()
            .into_iter()
            .map(|tool| {
                (
                    tool.name.to_string(),
                    serde_json::to_value(tool.input_schema).unwrap(),
                )
            })
            .collect();
        let expected: serde_json::Value =
            serde_json::from_str(include_str!("../runtime/mcp-tools.schema.json")).unwrap();
        assert_eq!(serde_json::Value::Object(actual), expected);
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

    fn ios_target(test_id: Option<&str>, text: Option<&str>) -> Target {
        Target {
            test_id: test_id.map(str::to_owned),
            text: text.map(str::to_owned),
            id: None,
            snapshot_id: None,
        }
    }

    fn ios_act_params(action: Action, target: Option<Target>, text: Option<String>) -> ActParams {
        ActParams {
            action,
            target,
            text,
            direction: None,
        }
    }

    #[test]
    fn ios_tap_mapping_is_unchanged_on_the_bridge_wire() {
        let by_test_id = ios_act_params(
            Action::Tap,
            Some(ios_target(Some("fixture-increment"), None)),
            None,
        );
        assert_eq!(
            ios_bridge_command(&by_test_id).unwrap(),
            serde_json::json!({"op": "ui_act", "action": "tap", "testId": "fixture-increment"})
        );
        let by_text = ios_act_params(Action::Tap, Some(ios_target(None, Some("Increment"))), None);
        assert_eq!(
            ios_bridge_command(&by_text).unwrap(),
            serde_json::json!({"op": "ui_act", "action": "tap", "text": "Increment"})
        );
        assert_eq!(
            ios_bridge_command(&ios_act_params(Action::Tap, None, None)).unwrap_err(),
            "tap requires a target"
        );
        let by_id = Target {
            test_id: None,
            text: None,
            id: Some(3),
            snapshot_id: Some(1),
        };
        assert_eq!(
            ios_bridge_command(&ios_act_params(Action::Tap, Some(by_id), None)).unwrap_err(),
            "iOS fixture selectors are testId or text only"
        );
    }

    #[test]
    fn ios_type_routes_the_payload_as_a_separate_bridge_value() {
        let into_test_id = ios_act_params(
            Action::Type,
            Some(ios_target(Some("fixture-input"), None)),
            Some("Ashnazg".into()),
        );
        assert_eq!(
            ios_bridge_command(&into_test_id).unwrap(),
            serde_json::json!({
                "op": "ui_act",
                "action": "type",
                "testId": "fixture-input",
                "value": "Ashnazg",
            })
        );
        let into_text = ios_act_params(
            Action::Type,
            Some(ios_target(None, Some("Probe text"))),
            Some("-fixture".into()),
        );
        assert_eq!(
            ios_bridge_command(&into_text).unwrap(),
            serde_json::json!({
                "op": "ui_act",
                "action": "type",
                "text": "Probe text",
                "value": "-fixture",
            })
        );
        let no_payload = ios_act_params(
            Action::Type,
            Some(ios_target(Some("fixture-input"), None)),
            Some(String::new()),
        );
        assert_eq!(
            ios_bridge_command(&no_payload).unwrap()["value"],
            serde_json::json!("")
        );
    }

    #[test]
    fn ios_type_rejects_missing_target_missing_payload_and_id_selectors() {
        assert_eq!(
            ios_bridge_command(&ios_act_params(Action::Type, None, Some("x".into()))).unwrap_err(),
            "type requires a target"
        );
        assert_eq!(
            ios_bridge_command(&ios_act_params(
                Action::Type,
                Some(ios_target(Some("fixture-input"), None)),
                None,
            ))
            .unwrap_err(),
            "type requires text"
        );
        let by_id = Target {
            test_id: None,
            text: None,
            id: Some(2),
            snapshot_id: Some(1),
        };
        assert_eq!(
            ios_bridge_command(&ios_act_params(Action::Type, Some(by_id), Some("x".into())))
                .unwrap_err(),
            "iOS fixture selectors are testId or text only"
        );
    }

    #[test]
    fn ios_unsupported_actions_are_rejected_behind_the_same_boundary() {
        for (action, target, text) in [
            (
                Action::LongPress,
                Some(ios_target(Some("fixture-increment"), None)),
                None,
            ),
            (
                Action::Clear,
                Some(ios_target(Some("fixture-input"), None)),
                None,
            ),
            (
                Action::ScrollTo,
                Some(ios_target(Some("fixture-increment"), None)),
                None,
            ),
            (Action::Back, None, None),
            (Action::DeepLink, None, Some("ashnazg://fixture".into())),
        ] {
            let params = ios_act_params(action, target, text);
            // Rejections are static strings, so no typed payload can leak into one.
            assert_eq!(
                ios_bridge_command(&params).unwrap_err(),
                "the iOS fixture bridge supports only the tap and type actions"
            );
        }
    }

    #[test]
    fn ios_app_tap_and_type_reuse_the_shared_bridge_wire_mapping() {
        let allowlist: Vec<String> = Vec::new();
        let tap = ios_act_params(
            Action::Tap,
            Some(ios_target(Some("submit-button"), None)),
            None,
        );
        assert_eq!(
            ios_app_bridge_command(&tap, &allowlist).unwrap(),
            serde_json::json!({"op": "ui_act", "action": "tap", "testId": "submit-button"})
        );
        // Ordinary tap covers every enabled hittable control, including a
        // submit button; the wire never special-cases it.
        let typed = ios_act_params(
            Action::Type,
            Some(ios_target(None, Some("E-mail"))),
            Some("synthetic-user@example.invalid".into()),
        );
        assert_eq!(
            ios_app_bridge_command(&typed, &allowlist).unwrap(),
            serde_json::json!({
                "op": "ui_act", "action": "type",
                "text": "E-mail", "value": "synthetic-user@example.invalid",
            })
        );
        let by_id = Target {
            test_id: None,
            text: None,
            id: Some(3),
            snapshot_id: Some(1),
        };
        assert_eq!(
            ios_app_bridge_command(&ios_act_params(Action::Tap, Some(by_id), None), &allowlist)
                .unwrap_err(),
            "iOS app selectors are testId or text only"
        );
        assert_eq!(
            ios_app_bridge_command(&ios_act_params(Action::Tap, None, None), &allowlist)
                .unwrap_err(),
            "tap requires a target"
        );
    }

    #[test]
    fn ios_app_deep_link_routes_only_exact_allowlisted_uris() {
        let allowlist = vec!["dev.example.app://shop/17".to_owned()];
        let allowed = ios_act_params(
            Action::DeepLink,
            None,
            Some("dev.example.app://shop/17".into()),
        );
        assert_eq!(
            ios_app_bridge_command(&allowed, &allowlist).unwrap(),
            serde_json::json!({
                "op": "ui_act", "action": "deep_link", "url": "dev.example.app://shop/17",
            })
        );
        for uri in [
            "dev.example.app://shop/17/extra",
            "other://route",
            "dev.example.app://shop/18",
        ] {
            let longer = ios_act_params(Action::DeepLink, None, Some(uri.into()));
            assert_eq!(
                ios_app_bridge_command(&longer, &allowlist).unwrap_err(),
                "deep link is not in the app manifest allowlist",
                "the exact URI {uri} must be allowlisted, not just its scheme"
            );
        }
        let missing_text = ios_act_params(Action::DeepLink, None, None);
        assert_eq!(
            ios_app_bridge_command(&missing_text, &allowlist).unwrap_err(),
            "deep_link requires text containing a URI"
        );
    }

    #[test]
    fn ios_app_unsupported_actions_are_rejected_behind_the_same_boundary() {
        let allowlist = vec!["dev.example.app://shop/17".to_owned()];
        for (action, target, text) in [
            (
                Action::LongPress,
                Some(ios_target(Some("submit-button"), None)),
                None,
            ),
            (Action::Clear, Some(ios_target(Some("field"), None)), None),
            (Action::Back, None, None),
        ] {
            let params = ios_act_params(action, target, text);
            // Rejections are static strings, so no typed payload can leak into one.
            assert_eq!(
                ios_app_bridge_command(&params, &allowlist).unwrap_err(),
                "the iOS app bridge supports only the tap, type, scroll_to, and deep_link actions"
            );
        }
    }

    #[test]
    fn ios_app_scroll_to_routes_the_shared_schema_for_reveal_journeys() {
        let allowlist: Vec<String> = Vec::new();
        let reveal_password = ActParams {
            action: Action::ScrollTo,
            target: Some(ios_target(Some("login-password-input"), None)),
            text: None,
            direction: Some(Direction::Down),
        };
        assert_eq!(
            ios_app_bridge_command(&reveal_password, &allowlist).unwrap(),
            serde_json::json!({
                "op": "ui_act", "action": "scroll_to",
                "testId": "login-password-input", "direction": "down",
            })
        );
        let reveal_submit = ActParams {
            action: Action::ScrollTo,
            target: Some(ios_target(None, Some("Entrar"))),
            text: None,
            direction: Some(Direction::Down),
        };
        assert_eq!(
            ios_app_bridge_command(&reveal_submit, &allowlist).unwrap(),
            serde_json::json!({
                "op": "ui_act", "action": "scroll_to",
                "text": "Entrar", "direction": "down",
            })
        );
        // The shared schema keeps the direction optional (the Android path
        // owns any default); one bounded native swipe needs an explicit
        // direction, so the app bridge fails closed instead of inventing one.
        let mut missing_direction = reveal_password.clone();
        missing_direction.direction = None;
        assert_eq!(
            ios_app_bridge_command(&missing_direction, &allowlist).unwrap_err(),
            "scroll_to requires a direction"
        );
        let no_target = ActParams {
            action: Action::ScrollTo,
            target: None,
            text: None,
            direction: Some(Direction::Down),
        };
        assert_eq!(
            ios_app_bridge_command(&no_target, &allowlist).unwrap_err(),
            "scroll_to requires a target"
        );
        let by_id = Target {
            test_id: None,
            text: None,
            id: Some(7),
            snapshot_id: Some(2),
        };
        assert_eq!(
            ios_app_bridge_command(
                &ActParams {
                    action: Action::ScrollTo,
                    target: Some(by_id),
                    text: None,
                    direction: Some(Direction::Down),
                },
                &allowlist
            )
            .unwrap_err(),
            "iOS app selectors are testId or text only"
        );
    }

    #[test]
    fn ios_type_mapping_runs_behind_the_shared_ui_act_validation() {
        // The production sequence: serialize, validate, then map.
        let typed = ios_act_params(
            Action::Type,
            Some(ios_target(Some("fixture-input"), None)),
            Some("Ashnazg".into()),
        );
        let value = serde_json::to_value(&typed).unwrap();
        validate_ui_act(&value).unwrap();
        assert_eq!(
            ios_bridge_command(&typed).unwrap(),
            serde_json::json!({
                "op": "ui_act", "action": "type",
                "testId": "fixture-input", "value": "Ashnazg",
            })
        );
        let missing_text = ios_act_params(
            Action::Type,
            Some(ios_target(Some("fixture-input"), None)),
            None,
        );
        assert!(
            validate_ui_act(&serde_json::to_value(&missing_text).unwrap())
                .unwrap_err()
                .to_string()
                .contains("type requires text")
        );
        let tap_with_text = ios_act_params(
            Action::Tap,
            Some(ios_target(Some("fixture-increment"), None)),
            Some("x".into()),
        );
        assert!(
            validate_ui_act(&serde_json::to_value(&tap_with_text).unwrap())
                .unwrap_err()
                .to_string()
                .contains("text is only valid for type and deep_link actions")
        );
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

    #[tokio::test]
    async fn initializing_http_authenticates_initialize_list_and_native_tool_call() {
        let install = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let guest = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let guest_endpoint = guest.local_addr().unwrap();
        let peer = std::thread::spawn(move || {
            use std::io::{BufRead, Write};
            let mut stream = accept_native_fixture(&guest);
            let mut request = String::new();
            std::io::BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut request)
                .unwrap();
            let request: serde_json::Value = serde_json::from_str(&request).unwrap();
            assert_eq!(request["method"], "app_logs");
            assert_eq!(request["token"], "fixture-guest-token");
            let response =
                serde_json::json!({"jsonrpc":"2.0","id":request["id"],"result":{"logs":[]}});
            stream
                .write_all(format!("{response}\n").as_bytes())
                .unwrap();
        });
        let events = Arc::new(StdMutex::new(Vec::new()));
        let mut owner = fixture_owner(events, install.local_addr().unwrap());
        owner.guest_endpoint = guest_endpoint;
        let lifecycle = Arc::new(super::InstallLifecycle::new(CancellationToken::new()));
        let backend = Arc::new(super::NativeInstallBackend {
            guest: owner.guest_client().unwrap(),
            client: owner.install_client().unwrap(),
            serial: owner.adb_addr().to_string(),
            lifecycle,
        });
        let readiness = Arc::new(NativeReadiness::new());
        readiness.publish(Ok(backend)).await;
        let cancel = CancellationToken::new();
        let app = initializing_http_app(
            Arc::clone(&readiness),
            "native-http-secret".into(),
            cancel.clone(),
        );
        let init = app.clone().oneshot(
            Request::builder().method("POST").uri("/mcp").header("host", "localhost")
                .header(AUTHORIZATION, "Bearer native-http-secret").header(CONTENT_TYPE, "application/json")
                .header("accept", "application/json, text/event-stream")
                .body(Body::from(r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}"#)).unwrap()
        ).await.unwrap();
        assert_eq!(init.status(), StatusCode::OK);
        let session = init.headers().get("mcp-session-id").cloned();
        let body = axum::body::to_bytes(init.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let protocol = parse_mcp_response(&body)["result"]["protocolVersion"]
            .as_str()
            .unwrap()
            .to_owned();
        let mut list = Request::builder()
            .method("POST")
            .uri("/mcp")
            .header("host", "localhost")
            .header(AUTHORIZATION, "Bearer native-http-secret")
            .header(CONTENT_TYPE, "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("mcp-protocol-version", &protocol);
        if let Some(session) = &session {
            list = list.header("mcp-session-id", session);
        }
        let listed = app
            .clone()
            .oneshot(
                list.body(Body::from(
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
        assert_eq!(
            parse_mcp_response(&body)["result"]["tools"]
                .as_array()
                .unwrap()
                .len(),
            5
        );
        let mut call = Request::builder()
            .method("POST")
            .uri("/mcp")
            .header("host", "localhost")
            .header(AUTHORIZATION, "Bearer native-http-secret")
            .header(CONTENT_TYPE, "application/json")
            .header("accept", "application/json, text/event-stream")
            .header("mcp-protocol-version", protocol);
        if let Some(session) = session {
            call = call.header("mcp-session-id", session);
        }
        let called = app.clone().oneshot(call.body(Body::from(r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"app_logs","arguments":{}}}"#)).unwrap()).await.unwrap();
        assert_eq!(called.status(), StatusCode::OK);
        let body = axum::body::to_bytes(called.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let response = parse_mcp_response(&body);
        assert_ne!(response["result"]["isError"], true);
        let result: serde_json::Value =
            serde_json::from_str(response["result"]["content"][0]["text"].as_str().unwrap())
                .unwrap();
        assert_eq!(result["logs"], serde_json::json!([]));
        peer.join().unwrap();
        cancel.cancel();
        drop(install);
    }
}
