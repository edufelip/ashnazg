use std::{
    env,
    io::Read,
    net::SocketAddr,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use anyhow::{Context, bail};
use serde_json::Value;

use crate::GuestClient;

const MAX_RPC_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone)]
pub struct HostConfig {
    pub guest: GuestClient,
    pub adb: String,
}

impl HostConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        let addr = env::var("ASHNAZG_AGENT_ADDR").unwrap_or_else(|_| "127.0.0.1:7777".into());
        let addr = validate_loopback_addr(&addr)?;
        let token = env::var("ASHNAZG_TOKEN").context("ASHNAZG_TOKEN is required for guest RPC")?;
        if token.is_empty() {
            bail!("ASHNAZG_TOKEN must not be empty");
        }
        Ok(Self {
            guest: GuestClient::new(addr, token),
            adb: env::var("ASHNAZG_ADB").unwrap_or_else(|_| "adb".into()),
        })
    }
}

pub fn validate_loopback_addr(input: &str) -> anyhow::Result<SocketAddr> {
    let addr: SocketAddr = input.parse().context("invalid socket address")?;
    if !addr.ip().is_loopback() {
        bail!("address must use a loopback IP");
    }
    Ok(addr)
}

pub fn validate_ui_act(params: &Value) -> anyhow::Result<()> {
    let object = params.as_object().context("params must be an object")?;
    let action = object
        .get("action")
        .and_then(Value::as_str)
        .context("action is required")?;
    match action {
        "tap" | "long_press" | "type" | "clear" | "scroll_to" => {
            let target = object
                .get("target")
                .and_then(Value::as_object)
                .context("target is required")?;
            let selectors = ["testId", "text", "id"]
                .iter()
                .filter(|k| target.get(**k).is_some_and(|v| !v.is_null()))
                .count();
            if selectors != 1 {
                bail!("target must contain exactly one of testId, text, or id");
            }
            if target.contains_key("id")
                && target.get("snapshotId").and_then(Value::as_u64).is_none()
            {
                bail!("id targets require snapshotId");
            }
            if target.contains_key("snapshotId") && !target.contains_key("id") {
                bail!("snapshotId is only valid with an id selector");
            }
            for key in ["testId", "text"] {
                if target.get(key).is_some_and(|v| !v.is_string()) {
                    bail!("{key} selector must be a string");
                }
            }
            if target.get("id").is_some_and(|v| !v.is_u64()) {
                bail!("id selector must be an unsigned integer");
            }
        }
        "back" => {
            if object.contains_key("target") {
                bail!("back does not accept a target");
            }
        }
        "deep_link" => {
            let uri = object
                .get("text")
                .and_then(Value::as_str)
                .context("deep_link requires text containing a URI")?;
            let (scheme, _) = uri
                .split_once(':')
                .context("deep_link text must be an absolute URI")?;
            if scheme.is_empty()
                || !scheme.as_bytes()[0].is_ascii_alphabetic()
                || !scheme
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'-' | b'.'))
            {
                bail!("deep_link text must have a valid URI scheme");
            }
        }
        _ => bail!("unsupported ui_act action"),
    }
    if !matches!(action, "type" | "deep_link") && object.contains_key("text") {
        bail!("text is only valid for type and deep_link actions");
    }
    if action != "scroll_to" && object.contains_key("direction") {
        bail!("direction is only valid for scroll_to actions");
    }
    if action == "deep_link" && object.contains_key("target") {
        bail!("deep_link does not accept a target");
    }
    if action == "type" && object.get("text").and_then(Value::as_str).is_none() {
        bail!("type requires text");
    }
    if action == "scroll_to"
        && object.get("direction").is_some()
        && !matches!(
            object.get("direction").and_then(Value::as_str),
            Some("up" | "down" | "left" | "right")
        )
    {
        bail!("scroll_to requires a valid direction");
    }
    let encoded = serde_json::to_vec(params)?;
    if encoded.len() > MAX_RPC_BYTES {
        bail!("params exceed 8 MiB limit");
    }
    Ok(())
}

pub fn app_install(
    adb: &str,
    apk: &Path,
    launch_requested: bool,
    package: Option<&str>,
) -> anyhow::Result<Value> {
    let serial = env::var("ANDROID_SERIAL")
        .context("ANDROID_SERIAL is required; select an explicit Android device")?;
    if serial.trim().is_empty() {
        bail!("ANDROID_SERIAL must not be empty");
    }
    if !apk.is_file() {
        bail!("APK does not exist or is not a regular file");
    }
    let launch_package = if launch_requested {
        let package = match package {
            Some(package) => package.to_owned(),
            None => apk_package_name(apk)?,
        };
        if !valid_package_name(&package) {
            bail!("launch package is not a valid Android package name");
        }
        Some(package)
    } else {
        if package.is_some() {
            bail!("package requires launch=true");
        }
        None
    };
    let mut install = Command::new(adb);
    install
        .args(["-s", &serial, "install", "-r"])
        .arg(apk)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    run_adb(&mut install, Duration::from_secs(120), "adb install")?;
    if let Some(package) = launch_package.as_deref() {
        let mut query = Command::new(adb);
        query.args([
            "-s",
            &serial,
            "shell",
            "cmd",
            "package",
            "query-activities",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            package,
        ]);
        let query_output = run_capture(
            &mut query,
            Duration::from_secs(15),
            "adb query launcher activities",
        )?;
        let candidate = parse_resolved_activity(&String::from_utf8_lossy(&query_output), package)?;

        let mut resolve = Command::new(adb);
        resolve.args([
            "-s",
            &serial,
            "shell",
            "cmd",
            "package",
            "resolve-activity",
            "--brief",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-p",
            package,
        ]);
        let resolution = run_capture(
            &mut resolve,
            Duration::from_secs(15),
            "adb resolve launcher activity",
        )?;
        let component = parse_resolved_activity(&String::from_utf8_lossy(&resolution), package)?;
        if component != candidate {
            bail!(
                "resolved launcher activity did not match the unique queried launcher for package {package}"
            );
        }
        let mut launch = Command::new(adb);
        launch.args([
            "-s", &serial, "shell", "am", "start", "-W", "-n", &component,
        ]);
        let output = run_capture(&mut launch, Duration::from_secs(30), "adb launch")?;
        parse_am_start_output(&String::from_utf8_lossy(&output))?;
    }
    Ok(serde_json::json!({"installed":true,"package":launch_package,"serial":serial}))
}

fn apk_package_name(apk: &Path) -> anyhow::Result<String> {
    let aapt = aapt_executable()?;
    let mut command = Command::new(aapt);
    command.args(["dump", "badging"]).arg(apk);
    let stdout = run_capture(&mut command, Duration::from_secs(15), "aapt dump badging")?;
    parse_aapt_package(&String::from_utf8_lossy(&stdout))
}

fn aapt_executable() -> anyhow::Result<PathBuf> {
    if let Ok(path) = env::var("ASHNAZG_AAPT")
        && !path.trim().is_empty()
    {
        let path = PathBuf::from(path);
        if path.is_file() {
            return Ok(path);
        }
        bail!("ASHNAZG_AAPT does not point to a file: {}", path.display());
    }
    let sdk = env::var_os("ANDROID_SDK_ROOT")
        .or_else(|| env::var_os("ANDROID_HOME"))
        .map(PathBuf::from);
    if let Some(sdk) = sdk {
        let tools = sdk.join("build-tools");
        let mut candidates = std::fs::read_dir(&tools)
            .with_context(|| format!("cannot read Android build-tools at {}", tools.display()))?
            .filter_map(Result::ok)
            .map(|entry| entry.path().join("aapt"))
            .filter(|path| path.is_file())
            .collect::<Vec<_>>();
        candidates.sort();
        if let Some(path) = candidates.pop() {
            return Ok(path);
        }
    }
    if let Some(paths) = env::var_os("PATH") {
        for directory in env::split_paths(&paths) {
            let path = directory.join("aapt");
            if path.is_file() {
                return Ok(path);
            }
        }
    }
    bail!(
        "aapt was not found; set ANDROID_SDK_ROOT/ANDROID_HOME or ASHNAZG_AAPT, or pass --launch PACKAGE"
    )
}

fn parse_aapt_package(output: &str) -> anyhow::Result<String> {
    for line in output.lines() {
        if let Some(rest) = line.strip_prefix("package: ")
            && let Some(rest) = rest.strip_prefix("name='")
            && let Some((name, _)) = rest.split_once('\'')
        {
            if valid_package_name(name) {
                return Ok(name.to_owned());
            }
            bail!("aapt returned an invalid Android package name");
        }
    }
    bail!(
        "could not find APK package name; set ASHNAZG_AAPT to an Android SDK build-tools aapt executable or pass a package name"
    )
}

fn parse_resolved_activity(output: &str, package: &str) -> anyhow::Result<String> {
    let mut components = Vec::new();
    for line in output.lines().map(str::trim) {
        let raw = line
            .strip_prefix("ComponentInfo{")
            .and_then(|line| line.strip_suffix('}'))
            .unwrap_or(line);
        if raw.contains('/') && !raw.chars().any(char::is_whitespace) {
            components.push(validate_component(raw, package)?);
        }
    }
    components.sort();
    components.dedup();
    match components.as_slice() {
        [] => bail!("no launcher activity found for package {package}"),
        [component] => Ok(component.clone()),
        _ => bail!("multiple launcher activities found for package {package}"),
    }
}

fn validate_component(component: &str, package: &str) -> anyhow::Result<String> {
    let (component_package, class) = component
        .split_once('/')
        .context("launcher activity did not contain a package/class component")?;
    if component_package != package || class.is_empty() || class.contains('/') {
        bail!("launcher activity package did not match the installed package");
    }
    let relative_class = if let Some(relative) = class.strip_prefix(&format!("{package}.")) {
        relative
    } else if let Some(relative) = class.strip_prefix('.') {
        relative
    } else if !class.contains('.') {
        class
    } else {
        bail!("launcher activity class was outside the installed package");
    };
    if !valid_java_name(relative_class) {
        bail!("launcher activity class contains invalid characters");
    }
    let canonical_class = if class.contains('.') || class.contains('$') {
        class.to_owned()
    } else {
        format!(".{class}")
    };
    Ok(format!("{package}/{canonical_class}"))
}

fn valid_java_name(name: &str) -> bool {
    !name.is_empty()
        && name.split('.').all(|part| {
            !part.is_empty()
                && (part.as_bytes()[0].is_ascii_alphabetic()
                    || matches!(part.as_bytes()[0], b'_' | b'$'))
                && part
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'$'))
        })
}

fn parse_am_start_output(output: &str) -> anyhow::Result<()> {
    let mut saw_ok = false;
    for line in output.lines().map(str::trim) {
        if line.starts_with("Error:") || line.starts_with("Exception:") {
            bail!("adb launch failed: {line}");
        }
        if let Some(status) = line.strip_prefix("Status:") {
            if status.trim() != "ok" {
                bail!("adb launch returned status: {}", status.trim());
            }
            saw_ok = true;
        }
    }
    if !saw_ok {
        bail!("adb launch did not report Status: ok");
    }
    Ok(())
}

fn run_adb(command: &mut Command, deadline: Duration, label: &str) -> anyhow::Result<()> {
    let mut child = command
        .spawn()
        .with_context(|| format!("failed to start {label}"))?;
    let started = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .with_context(|| format!("failed while waiting for {label}"))?
        {
            if !status.success() {
                bail!("{label} exited with status {status}");
            }
            return Ok(());
        }
        if started.elapsed() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            bail!(
                "{label} exceeded its {} second deadline and was terminated",
                deadline.as_secs()
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}

fn run_capture(command: &mut Command, deadline: Duration, label: &str) -> anyhow::Result<Vec<u8>> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("failed to start {label}"))?;
    let mut stdout = child.stdout.take().context("stdout pipe missing")?;
    let mut stderr = child.stderr.take().context("stderr pipe missing")?;
    let out_thread = thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stdout.read_to_end(&mut bytes);
        bytes
    });
    let err_thread = thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stderr.read_to_end(&mut bytes);
        bytes
    });
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child
            .try_wait()
            .with_context(|| format!("failed while waiting for {label}"))?
        {
            break status;
        }
        if started.elapsed() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            bail!(
                "{label} exceeded its {} second deadline and was terminated",
                deadline.as_secs()
            );
        }
        thread::sleep(Duration::from_millis(100));
    };
    let mut stdout = out_thread
        .join()
        .map_err(|_| anyhow::anyhow!("{label} stdout reader failed"))?;
    let stderr = err_thread
        .join()
        .map_err(|_| anyhow::anyhow!("{label} stderr reader failed"))?;
    if !status.success() {
        bail!(
            "{label} failed: {}",
            String::from_utf8_lossy(&stderr).trim()
        );
    }
    if !stderr.is_empty() {
        stdout.extend_from_slice(b"\n");
        stdout.extend_from_slice(&stderr);
    }
    Ok(stdout)
}

fn valid_package_name(name: &str) -> bool {
    !name.is_empty()
        && name.split('.').all(|part| {
            !part.is_empty()
                && part.as_bytes()[0].is_ascii_alphabetic()
                && part.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_')
        })
}

#[cfg(test)]
mod tests {
    use super::{parse_aapt_package, parse_am_start_output, parse_resolved_activity};

    #[test]
    fn parses_and_validates_apk_package_name() {
        assert_eq!(
            parse_aapt_package(
                "application-label:'Fixture'\npackage: name='dev.ashnazg.fixture' versionCode='1'\n"
            )
            .unwrap(),
            "dev.ashnazg.fixture"
        );
        assert!(
            parse_aapt_package("package: name='dev;touch /tmp/pwned' versionCode='1'\n").is_err()
        );
        assert!(parse_aapt_package("no package line").is_err());
    }

    #[test]
    fn resolves_one_valid_launcher_component_only() {
        assert_eq!(
            parse_resolved_activity(
                "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\ncom.example.app/.MainActivity\n",
                "com.example.app"
            ).unwrap(),
            "com.example.app/.MainActivity"
        );
        assert!(parse_resolved_activity("No activity found\n", "com.example.app").is_err());
        assert!(
            parse_resolved_activity(
                "com.example.app/.One\ncom.example.app/.Two\n",
                "com.example.app"
            )
            .is_err()
        );
        assert!(parse_resolved_activity("com.other/.MainActivity\n", "com.example.app").is_err());
        assert!(
            parse_resolved_activity("com.example.app/.MainActivity;touch\n", "com.example.app")
                .is_err()
        );
        assert!(
            parse_resolved_activity(
                "ComponentInfo{com.example.app/com.example.app.MainActivity}\n",
                "com.example.app"
            )
            .is_ok()
        );
    }

    #[test]
    fn accepts_successful_am_start_and_surfaces_exit_zero_errors() {
        assert!(parse_am_start_output("Starting: Intent { cmp=com.example.app/.MainActivity }\nStatus: ok\nActivity: com.example.app/.MainActivity\nComplete\n").is_ok());
        assert!(
            parse_am_start_output(
                "Starting: Intent { }\nError: Activity not started, unable to resolve Intent\n"
            )
            .is_err()
        );
        assert!(parse_am_start_output("Starting: Intent { }\nStatus: timeout\n").is_err());
    }
}
