//! External iOS app manifest for the ashnazg MCP server.
//!
//! The reviewed first slice of the `--ios-app` interface: one explicit
//! manifest pins the app bundle, its bytes, its identity, its simulator
//! target, its readiness selector and its deep links before any simulator
//! exists. The manifest shape, digest, symlink and URI checks are pure and
//! host-independent so Linux runs them; opening the live session and the
//! Info.plist/`lipo`/`vtool` platform checks stay macOS-only.

use anyhow::{Context, bail};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

/// One readiness selector: exactly one of `testId` or `text`.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ReadySelector {
    #[serde(default)]
    pub test_id: Option<String>,
    #[serde(default)]
    pub text: Option<String>,
}

/// A booted, preinstalled disposable simulator retained by the benchmark.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BenchmarkSimulator {
    pub udid: String,
    pub name: String,
}

/// The reviewed minimal manifest for `ashnazg mcp --ios-app <manifest.json>`.
///
/// `appSha256` is REQUIRED and pins the whole staged `.app` tree with the
/// stable directory digest this module defines; `runtime` and `deviceType`
/// are explicit simulator identifiers (never auto-picked); `initialUrl` is
/// an optional loopback Metro URL; `readySelector` is required and bounds
/// the observed app readiness; `deepLinks` is the exact allowlist of
/// app-owned deep links `ui_act` may open.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppManifest {
    pub app_path: PathBuf,
    pub app_sha256: String,
    pub bundle_id: String,
    pub runtime: String,
    pub device_type: String,
    #[serde(default)]
    pub initial_url: Option<String>,
    pub ready_selector: ReadySelector,
    pub deep_links: Vec<String>,
    #[serde(default)]
    pub benchmark_simulator: Option<BenchmarkSimulator>,
    #[serde(default)]
    pub benchmark_xctestrun: Option<PathBuf>,
}

impl AppManifest {
    /// Reads, parses and validates one manifest file. Pure: no simulator,
    /// Apple tool or network access happens here.
    pub fn from_json_file(path: &Path) -> anyhow::Result<Self> {
        let bytes = std::fs::read(path)
            .with_context(|| format!("failed to read the app manifest at {}", path.display()))?;
        let manifest: Self = serde_json::from_slice(&bytes)
            .with_context(|| format!("the app manifest at {} is not valid JSON", path.display()))?;
        manifest
            .validate()
            .with_context(|| format!("the app manifest at {} was rejected", path.display()))?;
        Ok(manifest)
    }

    /// Checks identity against CoreSimulator without changing its state.
    pub fn verify_benchmark_simulator(&self, inventory: &serde_json::Value) -> anyhow::Result<()> {
        let simulator = self
            .benchmark_simulator
            .as_ref()
            .context("no benchmark simulator was selected")?;
        let entries = inventory
            .get("devices")
            .and_then(|devices| devices.get(&self.runtime))
            .and_then(serde_json::Value::as_array)
            .context("the benchmark runtime is missing from CoreSimulator inventory")?;
        let mut matches = entries.iter().filter(|entry| {
            entry["udid"]
                .as_str()
                .is_some_and(|id| id.eq_ignore_ascii_case(&simulator.udid))
        });
        let entry = matches
            .next()
            .context("the benchmark simulator was not found")?;
        if matches.next().is_some()
            || entry["name"].as_str() != Some(simulator.name.as_str())
            || entry["deviceTypeIdentifier"].as_str() != Some(self.device_type.as_str())
            || entry["state"].as_str() != Some("Booted")
            || entry["isAvailable"].as_bool() != Some(true)
        {
            bail!("the benchmark simulator did not match the pinned name, state or device type");
        }
        Ok(())
    }

    /// Rejects every wrong-bundle, wrong-digest, wrong-path and wrong-URI
    /// manifest before a simulator is ever created.
    pub fn validate(&self) -> anyhow::Result<()> {
        if let Some(simulator) = &self.benchmark_simulator {
            let id = simulator.udid.as_bytes();
            let name = simulator
                .name
                .strip_prefix("AshnazgIosBenchmark-")
                .unwrap_or("");
            if id.len() != 36
                || !id.iter().enumerate().all(|(index, byte)| {
                    if matches!(index, 8 | 13 | 18 | 23) {
                        *byte == b'-'
                    } else {
                        byte.is_ascii_hexdigit()
                    }
                })
                || name.len() != 12
                || !name.bytes().all(|byte| byte.is_ascii_hexdigit())
            {
                bail!(
                    "benchmarkSimulator requires a simulator UDID and a disposable benchmark name"
                );
            }
        }
        if let Some(runner) = &self.benchmark_xctestrun
            && (self.benchmark_simulator.is_none()
                || !runner.is_absolute()
                || runner.extension().and_then(|ext| ext.to_str()) != Some("xctestrun"))
        {
            bail!("benchmarkXctestrun requires an absolute .xctestrun path and benchmarkSimulator");
        }
        if self.app_path.as_os_str().is_empty() {
            bail!("appPath is required");
        }
        if !self.app_path.is_absolute() {
            bail!(
                "appPath {} must be an absolute path",
                self.app_path.display()
            );
        }
        if !self
            .app_path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".app"))
        {
            bail!(
                "appPath {} must name a .app bundle directory",
                self.app_path.display()
            );
        }
        let digest = &self.app_sha256;
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            bail!("appSha256 must be exactly 64 lowercase hex characters");
        }
        let bundle_id = &self.bundle_id;
        if bundle_id.is_empty()
            || !bundle_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'-' | b'_'))
        {
            bail!("bundleId must be nonempty bundle identifier characters");
        }
        if self.runtime.is_empty() {
            bail!("runtime must name an explicit iOS Simulator runtime");
        }
        if self.device_type.is_empty() {
            bail!("deviceType must name an explicit simulator device type");
        }
        if let Some(initial_url) = self.initial_url.as_deref()
            && !is_loopback_http_url(initial_url)
        {
            bail!("initialUrl must be an http(s) URL on a loopback host, got {initial_url:?}");
        }
        match (&self.ready_selector.test_id, &self.ready_selector.text) {
            (Some(test_id), None) if !test_id.is_empty() => {}
            (None, Some(text)) if !text.is_empty() => {}
            _ => bail!("readySelector must be exactly one nonempty testId or text"),
        }
        for deep_link in &self.deep_links {
            let (scheme, _) = deep_link
                .split_once(':')
                .with_context(|| format!("deep link {deep_link:?} must be an absolute URI"))?;
            if scheme.is_empty()
                || !scheme
                    .bytes()
                    .next()
                    .is_some_and(|b| b.is_ascii_alphabetic())
                || !scheme
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.'))
            {
                bail!("deep link {deep_link:?} must have a valid URI scheme");
            }
            if scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https") {
                bail!("deep link {deep_link:?} must be an app scheme, not http(s)");
            }
        }
        Ok(())
    }
}

/// True when `url` is an http(s) URL whose authority is exactly a loopback
/// host with an optional numeric port (1..65535) and no userinfo, control
/// characters, or backslashes. Metro runs on the host loopback; anything
/// else is rejected as a wrong URI before the session starts.
pub fn is_loopback_http_url(url: &str) -> bool {
    if url
        .bytes()
        .any(|byte| byte <= 0x20 || byte == 0x7f || byte == b'\\')
    {
        return false;
    }
    let Some((scheme, rest)) = url.split_once("://") else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case("http") && !scheme.eq_ignore_ascii_case("https") {
        return false;
    }
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if authority.is_empty() || authority.contains('@') {
        return false;
    }
    let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
        let Some(end) = bracketed.find(']') else {
            return false;
        };
        let host = &authority[..end + 2];
        let tail = &bracketed[end + 1..];
        if tail.is_empty() {
            (host, None)
        } else {
            match tail.strip_prefix(':') {
                Some(port) => (host, Some(port)),
                None => return false,
            }
        }
    } else {
        match authority.rsplit_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (authority, None),
        }
    };
    if !host.eq_ignore_ascii_case("127.0.0.1")
        && !host.eq_ignore_ascii_case("localhost")
        && !host.eq_ignore_ascii_case("[::1]")
    {
        return false;
    }
    match port {
        None => true,
        Some(port) => {
            (1..=5).contains(&port.len())
                && port.bytes().all(|byte| byte.is_ascii_digit())
                && port
                    .parse::<u32>()
                    .is_ok_and(|value| (1..=65535).contains(&value))
        }
    }
}

/// Stages `source` into the owned `target` directory while hashing with the
/// pinned Meer `hashApp` convention (`apps/mobile/ios/ci_scripts/debug-smoke-proof.cjs`
/// at e6aca58675085368f1eb21fa92db4446e8581e6b), and returns the stable
/// digest of the staged tree: depth-first walk with entries sorted by name,
/// one SHA-256 frame per entry for
/// `["<relative path>",<mode & 0o777>,<directory ? 0 : lstat size>]`
/// (JSON text, UTF-8), file bytes streamed after their frame, and a symlink
/// contributing its read target text after its frame — so retargeted
/// symlinks and changed modes both change the digest.
///
/// Each symlink is validated via its canonical target inside the canonical
/// source root (a dangling link is rejected), an absolute target is
/// rejected because the installed copy must stay relocatable, and the
/// staged copy preserves the relative target, file modes and directory
/// modes — so re-hashing the staged tree reproduces this digest and
/// `simctl install` installs exactly the bytes that were hashed.
pub fn stage_tree_pinned(source: &Path, target: &Path) -> anyhow::Result<String> {
    let root = fs::canonicalize(source)
        .with_context(|| format!("failed to resolve {}", source.display()))?;
    let mut hasher = Sha256::new();
    let files = stage_dir(source, target, source, &root, &mut hasher)
        .with_context(|| format!("failed to stage {}", source.display()))?;
    if files == 0 {
        bail!("{} contains no files to pin", source.display());
    }
    Ok(hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

fn stage_dir(
    dir: &Path,
    staged_dir: &Path,
    rel_root: &Path,
    canonical_root: &Path,
    hasher: &mut Sha256,
) -> anyhow::Result<usize> {
    let entries: Vec<_> = fs::read_dir(dir)
        .with_context(|| format!("failed to read {}", dir.display()))?
        .collect::<Result<_, _>>()
        .with_context(|| format!("failed to read {}", dir.display()))?;
    // Node's readdirSync().sort() compares UTF-16 code units; encode each
    // validated name the same way so entry order matches hashApp exactly.
    // Undecodable names are rejected instead of silently lossy-hashed.
    let mut sorted = Vec::with_capacity(entries.len());
    for entry in entries {
        let name = entry.file_name();
        let text = name.to_str().with_context(|| {
            format!(
                "app tree contains a non-UTF-8 name: {}",
                entry.path().display()
            )
        })?;
        sorted.push((text.encode_utf16().collect::<Vec<u16>>(), entry));
    }
    sorted.sort_by(|left, right| left.0.cmp(&right.0));
    let mut files = 0usize;
    for (_, entry) in sorted {
        let stat = entry
            .metadata()
            .with_context(|| format!("failed to stat {}", entry.path().display()))?;
        let mode = entry_mode(&stat);
        let file_type = stat.file_type();
        let to = staged_dir.join(entry.file_name());
        let relative = relative_path(&entry.path(), rel_root);
        let size = if file_type.is_dir() { 0 } else { stat.len() };
        // The hashApp frame is the JSON encoding of [relpath, mode, size];
        // serde_json produces the same bytes JSON.stringify does.
        hasher.update(&serde_json::to_vec(&(relative.as_str(), mode, size))?);
        if file_type.is_dir() {
            fs::create_dir_all(&to)
                .with_context(|| format!("failed to create {}", to.display()))?;
            files += stage_dir(&entry.path(), &to, rel_root, canonical_root, hasher)?;
            // The preserved mode lands after the children exist, so a
            // read-only source directory (0555) still stages.
            apply_mode(&to, mode)?;
        } else if file_type.is_symlink() {
            let link = fs::read_link(entry.path())
                .with_context(|| format!("failed to read the symlink {relative}"))?;
            if link.is_absolute() {
                bail!(
                    "app tree contains an absolute symlink {relative}; the staged copy must stay relocatable"
                );
            }
            let canonical = fs::canonicalize(entry.path());
            if !canonical.is_ok_and(|canonical| {
                canonical.starts_with(canonical_root) && canonical != canonical_root
            }) {
                bail!("app tree contains a dangling or escaping symlink: {relative}");
            }
            hasher.update(link.as_os_str().as_encoded_bytes());
            #[cfg(unix)]
            std::os::unix::fs::symlink(&link, &to)
                .with_context(|| format!("failed to stage the symlink {relative}"))?;
            #[cfg(not(unix))]
            bail!("app tree contains a symlink {relative}, which this platform cannot stage");
        } else if file_type.is_file() {
            files += 1;
            copy_pinned(&entry.path(), &to, hasher, size)?;
            apply_mode(&to, mode)?;
        } else {
            bail!("app tree contains a non-regular entry: {relative}");
        }
    }
    Ok(files)
}

fn relative_path(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .expect("walked paths stay inside the root")
        .components()
        .map(|component| {
            component
                .as_os_str()
                .to_str()
                .expect("entry names were validated as UTF-8")
        })
        .collect::<Vec<_>>()
        .join("/")
}

#[cfg(unix)]
fn entry_mode(stat: &fs::Metadata) -> u32 {
    use std::os::unix::fs::PermissionsExt;
    stat.permissions().mode() & 0o777
}

#[cfg(not(unix))]
fn entry_mode(_stat: &fs::Metadata) -> u32 {
    0
}

#[cfg(unix)]
fn apply_mode(path: &Path, mode: u32) -> anyhow::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .with_context(|| format!("failed to set the mode of {}", path.display()))?;
    Ok(())
}

#[cfg(not(unix))]
fn apply_mode(_path: &Path, _mode: u32) -> anyhow::Result<()> {
    Ok(())
}

fn copy_pinned(from: &Path, to: &Path, hasher: &mut Sha256, expected: u64) -> anyhow::Result<()> {
    let mut reader =
        fs::File::open(from).with_context(|| format!("failed to open {}", from.display()))?;
    let mut writer =
        fs::File::create(to).with_context(|| format!("failed to create {}", to.display()))?;
    let mut buffer = vec![0u8; 1024 * 1024];
    let mut copied = 0u64;
    loop {
        let read = io::Read::read(&mut reader, &mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        io::Write::write_all(&mut writer, &buffer[..read])?;
        copied += read as u64;
    }
    if copied != expected {
        bail!("{} changed size while it was staged", from.display());
    }
    Ok(())
}

#[cfg(test)]
mod shape_tests {
    //! Host-independent shape, hash and symlink tests for the pure parts
    //! of the `--ios-app` slice; the live session stays macOS-only.

    use super::*;
    use serde_json::{Value, json};
    use std::fs;

    fn manifest_value(body: Value) -> Value {
        let mut base = json!({
            "appPath": "/tmp/Probe.app",
            "appSha256": "a".repeat(64),
            "bundleId": "dev.example.app",
            "runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-5",
            "deviceType": "com.apple.CoreSimulator.SimDeviceType.iPhone-17",
            "readySelector": {"testId": "home-ready"},
            "deepLinks": [],
        });
        if let (Some(base), Some(body)) = (base.as_object_mut(), body.as_object()) {
            for (key, value) in body {
                base.insert(key.clone(), value.clone());
            }
        }
        base
    }

    fn parse_manifest(body: Value) -> AppManifest {
        serde_json::from_value(manifest_value(body)).expect("test manifest must parse")
    }

    #[test]
    fn benchmark_simulator_requires_a_disposable_name_and_canonical_udid() {
        let value =
            |udid: &str, name: &str| json!({"benchmarkSimulator": {"udid": udid, "name": name}});
        let udid = "FD81F957-6715-4C2B-98BC-444BB15D3043";
        let name = "AshnazgIosBenchmark-86bbd0d8a961";
        assert!(parse_manifest(value(udid, name)).validate().is_ok());
        for (id, label) in [
            ("booted", name),
            (udid, "Personal iPhone"),
            (udid, "AshnazgIosBenchmark-"),
        ] {
            assert!(parse_manifest(value(id, label)).validate().is_err());
        }
    }

    #[test]
    fn benchmark_test_runner_requires_a_disposable_simulator_and_absolute_xctestrun() {
        let runner = json!({"benchmarkXctestrun": "/var/tmp/Probe.xctestrun"});
        assert!(parse_manifest(runner).validate().is_err());
        let value = |path| {
            json!({
                "benchmarkSimulator": {
                    "udid": "FD81F957-6715-4C2B-98BC-444BB15D3043",
                    "name": "AshnazgIosBenchmark-86bbd0d8a961",
                },
                "benchmarkXctestrun": path,
            })
        };
        assert!(
            parse_manifest(value("/var/tmp/Probe.xctestrun"))
                .validate()
                .is_ok()
        );
        assert!(
            parse_manifest(value("relative.xctestrun"))
                .validate()
                .is_err()
        );
        assert!(
            parse_manifest(value("/var/tmp/Probe.app"))
                .validate()
                .is_err()
        );
    }

    #[test]
    fn benchmark_simulator_must_match_the_booted_device_in_the_pinned_runtime() {
        let manifest = parse_manifest(json!({"benchmarkSimulator": {
            "udid": "FD81F957-6715-4C2B-98BC-444BB15D3043",
            "name": "AshnazgIosBenchmark-86bbd0d8a961",
        }}));
        let entry = json!({
            "udid": "fd81f957-6715-4c2b-98bc-444bb15d3043",
            "name": "AshnazgIosBenchmark-86bbd0d8a961",
            "state": "Booted",
            "isAvailable": true,
            "deviceTypeIdentifier": manifest.device_type,
        });
        let inventory = |entries| json!({"devices": {(&manifest.runtime): entries}});
        assert!(
            manifest
                .verify_benchmark_simulator(&inventory(vec![entry.clone()]))
                .is_ok()
        );
        let mut wrong = entry.clone();
        wrong["state"] = json!("Shutdown");
        assert!(
            manifest
                .verify_benchmark_simulator(&inventory(vec![wrong]))
                .is_err()
        );
        assert!(
            manifest
                .verify_benchmark_simulator(&inventory(vec![entry.clone(), entry]))
                .is_err()
        );
    }

    #[test]
    fn minimal_manifest_with_empty_allowlist_validates() {
        let manifest = parse_manifest(json!({}));
        assert!(manifest.validate().is_ok());
        let by_text = parse_manifest(json!({"readySelector": {"text": "Entrar"}}));
        assert!(by_text.validate().is_ok());
    }

    #[test]
    fn app_path_must_be_absolute_and_an_app_bundle_directory() {
        let relative = parse_manifest(json!({"appPath": "relative/Probe.app"}));
        assert!(
            relative.validate().is_err(),
            "a relative appPath must fail before staging"
        );
        let plain = parse_manifest(json!({"appPath": "/tmp/Probe"}));
        assert!(
            plain.validate().is_err(),
            "a non-.app directory must fail before staging"
        );
        assert!(parse_manifest(json!({})).validate().is_ok());
    }

    #[test]
    fn manifest_rejects_unknown_fields_wrong_types_and_missing_fields() {
        let unknown = serde_json::from_value::<AppManifest>(manifest_value(json!({
            "unexpected": true
        })));
        assert!(unknown.is_err(), "unknown fields must be rejected");
        let wrong_type = serde_json::from_value::<AppManifest>(manifest_value(json!({
            "deepLinks": "nope"
        })));
        assert!(wrong_type.is_err(), "deepLinks must be an array");
        let missing_selector = json!({
            "appPath": "/tmp/Probe.app",
            "appSha256": "a".repeat(64),
            "bundleId": "dev.example.app",
            "runtime": "r",
            "deviceType": "d",
            "deepLinks": [],
        });
        let missing = serde_json::from_value::<AppManifest>(missing_selector);
        assert!(missing.is_err(), "readySelector is required");
    }

    #[test]
    fn manifest_requires_exact_lowercase_hex_digest_and_sane_identity() {
        for digest in ["", "ABC", &"A".repeat(64), &"g".repeat(64)] {
            let mut manifest = parse_manifest(json!({}));
            manifest.app_sha256 = digest.to_owned();
            assert!(manifest.validate().is_err(), "digest {digest:?} must fail");
        }
        for bundle_id in ["", "has space", "bad/id"] {
            let mut manifest = parse_manifest(json!({}));
            manifest.bundle_id = bundle_id.to_owned();
            assert!(
                manifest.validate().is_err(),
                "bundle id {bundle_id:?} must fail"
            );
        }
        for empty in [
            parse_manifest(json!({"runtime": ""})).runtime,
            parse_manifest(json!({"deviceType": ""})).device_type,
        ] {
            assert!(empty.is_empty());
        }
        assert!(parse_manifest(json!({"runtime": ""})).validate().is_err());
        assert!(
            parse_manifest(json!({"deviceType": ""}))
                .validate()
                .is_err()
        );
    }

    #[test]
    fn initial_url_must_be_a_loopback_http_url() {
        for accepted in [
            "http://127.0.0.1:8081",
            "https://localhost:8081/index.bundle?platform=ios",
            "http://[::1]:8081",
            "http://127.0.0.1",
            "http://LOCALHOST:8081",
            "http://127.0.0.1:8081?disableOnboarding=1",
        ] {
            let manifest = parse_manifest(json!({"initialUrl": accepted}));
            assert!(
                manifest.validate().is_ok(),
                "initialUrl {accepted} must pass"
            );
        }
        for wrong in [
            "http://192.168.0.2:8081",
            "http://0.0.0.0:8081",
            "exp://127.0.0.1:8081",
            "file:///tmp/app",
            "http://localhost:notaport",
            "http://127.0.0.1:99999",
            // Malicious authorities: the port field must not smuggle another
            // host, credentials, or a malformed IPv6 literal.
            "http://[::1]:@external.invalid/",
            "http://127.0.0.1:8080@external.invalid/",
            "http://[::1@evil]:8081/",
            "http://127.0.0.1:0",
            "http://127.0.0.1:8081/..\\evil",
        ] {
            let manifest = parse_manifest(json!({"initialUrl": wrong}));
            assert!(manifest.validate().is_err(), "initialUrl {wrong} must fail");
        }
        let control = parse_manifest(json!({"initialUrl": "http://127.0.0.1:8081/\u{0}"}));
        assert!(
            control.validate().is_err(),
            "a control character anywhere in the URL must fail"
        );
    }

    #[test]
    fn deep_links_must_be_absolute_app_schemes() {
        let manifest = parse_manifest(json!({"deepLinks": ["dev.example.app://shop/17"]}));
        assert!(manifest.validate().is_ok());
        for wrong in [
            "",
            "no-colon",
            "1bad://route",
            "http://127.0.0.1:8081/x",
            "https://example.com/y",
            "://missing-scheme",
        ] {
            let manifest = parse_manifest(json!({"deepLinks": [wrong]}));
            assert!(
                manifest.validate().is_err(),
                "deep link {wrong:?} must fail"
            );
        }
    }

    #[test]
    fn ready_selector_requires_exactly_one_nonempty_field() {
        let mut both = parse_manifest(json!({}));
        both.ready_selector = ReadySelector {
            test_id: Some("home-ready".into()),
            text: Some("Entrar".into()),
        };
        assert!(both.validate().is_err());
        let mut empty = parse_manifest(json!({}));
        empty.ready_selector = ReadySelector {
            test_id: None,
            text: None,
        };
        assert!(empty.validate().is_err());
        let mut blank = parse_manifest(json!({}));
        blank.ready_selector = ReadySelector {
            test_id: Some(String::new()),
            text: None,
        };
        assert!(blank.validate().is_err());
    }

    fn stage_tree(case: &str) -> (tempfile::TempDir, tempfile::TempDir, String) {
        let source = tempfile::tempdir().expect("source temp dir");
        let staged = tempfile::tempdir().expect("staged temp dir");
        let root = source.path();
        fs::create_dir(root.join("Sub")).unwrap();
        fs::write(root.join("Probe"), b"#!/bin/true\n").unwrap();
        fs::write(root.join("Sub/nested.txt"), b"one").unwrap();
        fs::write(root.join("Info.plist"), b"plist-bytes").unwrap();
        match case {
            "nested" => {}
            "nested-again" => {
                fs::write(root.join("Info.plist"), b"plist-bytes-2").unwrap();
            }
            _ => unreachable!("unknown case {case}"),
        }
        let digest = stage_tree_pinned(root, staged.path()).expect("staging must succeed");
        (source, staged, digest)
    }

    /// The exact fixture the pinned Meer `hashApp` (node, verbatim from
    /// `debug-smoke-proof.cjs` at e6aca58675085368f1eb21fa92db4446e8581e6b)
    /// hashed on this host: entries `Alias.plist -> Info.plist`, `Info.plist`
    /// (0644, "plist-bytes"), `Meer` (0755, "#!/bin/true\n") and
    /// `Sub/nested.txt` (0600, "one"). The recorded digests prove the Rust
    /// staging reproduces the Meer convention byte-for-byte, and that
    /// retargeting a symlink or changing a mode changes the digest.
    fn meer_parity_fixture() -> (tempfile::TempDir, Vec<(String, &'static str)>) {
        use std::os::unix::fs::PermissionsExt;
        let source = tempfile::tempdir().expect("parity temp dir");
        let root = source.path();
        fs::create_dir(root.join("Sub")).unwrap();
        fs::set_permissions(root.join("Sub"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(root.join("Meer"), b"#!/bin/true\n").unwrap();
        fs::set_permissions(root.join("Meer"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(root.join("Info.plist"), b"plist-bytes").unwrap();
        fs::set_permissions(root.join("Info.plist"), fs::Permissions::from_mode(0o644)).unwrap();
        fs::write(root.join("Sub/nested.txt"), b"one").unwrap();
        fs::set_permissions(
            root.join("Sub/nested.txt"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        std::os::unix::fs::symlink("Info.plist", root.join("Alias.plist")).unwrap();
        (
            source,
            vec![
                (
                    "Info.plist".into(),
                    "bc5165568ee895beb0453a6cc62f679c3e6281d3aee02987455e4f3254d07cd2",
                ),
                (
                    "Meer".into(),
                    "3c0c41dbf49a5596b566243372a9dc5910f4822d31bb9bb851b8328ff5127aa5",
                ),
                (
                    "chmod 0700 Meer".into(),
                    "6e8334c1ce4f9a6fa425d0a3c50ef62e5348c068465b39b154a43b7630f01bd3",
                ),
            ],
        )
    }

    /// A second, independently generated `hashApp` reference fixture:
    /// `Alias -> Contents/tool` (0777 symlink), a 0755 `Contents` dir, and
    /// `Contents/tool` + `Contents/other` both 0755 with the raw bytes
    /// `frozen\x00test\n`. Recorded digests cover the retarget and the
    /// post-retarget chmod, so identical bytes at two tree positions, a
    /// directory-bearing symlink target, binary content, and mode changes
    /// are all pinned cross-tool.
    fn independent_reference_fixture() -> (tempfile::TempDir, Vec<(String, &'static str)>) {
        use std::os::unix::fs::PermissionsExt;
        let source = tempfile::tempdir().expect("reference temp dir");
        let root = source.path();
        fs::create_dir(root.join("Contents")).unwrap();
        fs::set_permissions(root.join("Contents"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(root.join("Contents/tool"), b"frozen\x00test\n").unwrap();
        fs::set_permissions(
            root.join("Contents/tool"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        fs::write(root.join("Contents/other"), b"frozen\x00test\n").unwrap();
        fs::set_permissions(
            root.join("Contents/other"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        std::os::unix::fs::symlink("Contents/tool", root.join("Alias")).unwrap();
        (
            source,
            vec![
                (
                    "base".into(),
                    "9976ea2eac46c31103de5f24ab75ee1045708a2912297f20be0a6b83a21ec030",
                ),
                (
                    "Alias -> Contents/other".into(),
                    "78cf9325fdf2920055f431c3f673970abce0d54f8b8f315fa8b4363488f8b0f4",
                ),
                (
                    "chmod 0644 Contents/tool".into(),
                    "14b057df05bb62ecbfdd6c2f8f21a2ab93cdea0999d96b5c6829382bd95b7e59",
                ),
            ],
        )
    }

    #[test]
    fn pinned_digest_matches_the_independent_hashapp_reference() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let (source, cases) = independent_reference_fixture();
            let staged = tempfile::tempdir().unwrap();
            let base = stage_tree_pinned(source.path(), staged.path()).unwrap();
            assert_eq!(
                base, cases[0].1,
                "the staged digest must match the independent reference run"
            );
            let mut previous = base.clone();
            for (label, expected) in cases.into_iter().skip(1) {
                match label.as_str() {
                    "Alias -> Contents/other" => {
                        let _ = std::fs::remove_file(source.path().join("Alias"));
                        std::os::unix::fs::symlink("Contents/other", source.path().join("Alias"))
                            .unwrap();
                    }
                    "chmod 0644 Contents/tool" => {
                        fs::set_permissions(
                            source.path().join("Contents/tool"),
                            fs::Permissions::from_mode(0o644),
                        )
                        .unwrap();
                    }
                    _ => unreachable!(),
                }
                let restaged = tempfile::tempdir().unwrap();
                let digest = stage_tree_pinned(source.path(), restaged.path()).unwrap();
                assert_eq!(
                    digest, expected,
                    "the {label} digest must change and match the reference"
                );
                assert_ne!(digest, previous, "the {label} change must rebind appSha256");
                previous = digest;
            }
            // Twin files differ only by tree position: their frames keep
            // the digest unambiguous even with identical bytes.
            let staged_again = tempfile::tempdir().unwrap();
            let restaged = stage_tree_pinned(staged.path(), staged_again.path()).unwrap();
            assert_eq!(
                restaged, base,
                "the staged copy must reproduce the source digest exactly"
            );
            let link = fs::read_link(staged.path().join("Alias")).unwrap();
            assert_eq!(link, Path::new("Contents/tool"));
        }
    }
    #[test]
    fn pinned_digest_matches_the_meer_hashapp_convention() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let (source, cases) = meer_parity_fixture();
            let staged = tempfile::tempdir().unwrap();
            let base = stage_tree_pinned(source.path(), staged.path()).unwrap();
            assert_eq!(
                base, cases[0].1,
                "the staged digest must match the real pinned hashApp digest"
            );
            let mut previous = base.clone();
            for (label, expected) in cases.into_iter().skip(1) {
                match label.as_str() {
                    "Meer" => {
                        let _ = std::fs::remove_file(source.path().join("Alias.plist"));
                        std::os::unix::fs::symlink("Meer", source.path().join("Alias.plist"))
                            .unwrap();
                    }
                    "chmod 0700 Meer" => {
                        fs::set_permissions(
                            source.path().join("Meer"),
                            fs::Permissions::from_mode(0o700),
                        )
                        .unwrap();
                    }
                    _ => unreachable!(),
                }
                let restaged = tempfile::tempdir().unwrap();
                let digest = stage_tree_pinned(source.path(), restaged.path()).unwrap();
                assert_eq!(
                    digest, expected,
                    "the {label} digest must change and match hashApp"
                );
                assert_ne!(digest, previous, "the {label} change must rebind appSha256");
                previous = digest;
            }
            // Re-hashing the staged copy reproduces its own digest: the
            // pinned bytes are exactly the bytes that would be installed.
            let staged_again = tempfile::tempdir().unwrap();
            let restaged = stage_tree_pinned(staged.path(), staged_again.path()).unwrap();
            assert_eq!(
                restaged, base,
                "the staged copy must reproduce the source digest exactly"
            );
            let link = fs::read_link(staged.path().join("Alias.plist")).unwrap();
            assert_eq!(link, Path::new("Info.plist"));
        }
    }

    #[test]
    fn pinned_staging_is_stable_and_location_independent() {
        let first = stage_tree("nested");
        let second = stage_tree("nested");
        assert_eq!(first.2, second.2, "identical trees must hash identically");
        let changed = stage_tree("nested-again");
        assert_ne!(
            first.2, changed.2,
            "a changed file must change the staged digest"
        );
        assert!(
            changed
                .1
                .path()
                .join("Info.plist")
                .metadata()
                .is_ok_and(|meta| meta.len() == b"plist-bytes-2".len() as u64)
        );
    }

    #[test]
    fn pinned_staging_preserves_in_tree_symlinks_and_rejects_escaping_ones() {
        #[cfg(unix)]
        {
            let (source, _staged, digest) = stage_tree("nested");
            std::os::unix::fs::symlink("Info.plist", source.path().join("Alias.plist")).unwrap();
            let restaged = tempfile::tempdir().unwrap();
            let aliased = stage_tree_pinned(source.path(), restaged.path()).unwrap();
            assert_ne!(
                aliased, digest,
                "a symlink's frame and target text must rebind the digest"
            );
            let link = std::fs::read_link(restaged.path().join("Alias.plist")).unwrap();
            assert_eq!(link, Path::new("Info.plist"));

            let dangling = tempfile::tempdir().unwrap();
            let dangling_staged = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink("Missing", dangling.path().join("broken")).unwrap();
            assert!(
                stage_tree_pinned(dangling.path(), dangling_staged.path()).is_err(),
                "a dangling symlink must be rejected via its canonical target"
            );

            let absolute = tempfile::tempdir().unwrap();
            let absolute_staged = tempfile::tempdir().unwrap();
            fs::write(absolute.path().join("real"), b"real").unwrap();
            std::os::unix::fs::symlink(
                absolute.path().join("real"),
                absolute.path().join("linked"),
            )
            .unwrap();
            assert!(
                stage_tree_pinned(absolute.path(), absolute_staged.path()).is_err(),
                "an absolute symlink must be rejected: the installed copy must stay relocatable"
            );

            let escaping = tempfile::tempdir().unwrap();
            let escaping_staged = tempfile::tempdir().unwrap();
            fs::create_dir_all(escaping.path().join("Frameworks")).unwrap();
            std::os::unix::fs::symlink(
                "../../etc/hostname",
                escaping.path().join("Frameworks/escape"),
            )
            .unwrap();
            assert!(
                stage_tree_pinned(escaping.path(), escaping_staged.path()).is_err(),
                "a relative symlink escaping the tree must be rejected"
            );
        }

        let empty_source = tempfile::tempdir().unwrap();
        let empty_staged = tempfile::tempdir().unwrap();
        assert!(
            stage_tree_pinned(empty_source.path(), empty_staged.path()).is_err(),
            "an empty tree is not an app bundle"
        );
    }
}
