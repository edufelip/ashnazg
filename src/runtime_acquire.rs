//! Cancellable runtime acquisition and verified offline cache reuse.

use crate::runtime_cache::{CacheGuard, CacheStage};
use crate::runtime_catalog::{ArtifactRole, RuntimeCatalog};
use crate::runtime_download::{CatalogDownload, DownloadReceipt, RuntimeDownloader};
use crate::runtime_extract::{self, ExtractedMember, ExtractionReceipt};
use anyhow::{Context, anyhow, bail, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::io::AsyncWrite;
use tokio_util::sync::CancellationToken;

const VERSION: &str = "0.3.2";
const MAX_CATALOG_BYTES: usize = 64 * 1024;
const MAX_INTEGRITY_BYTES: usize = 64 * 1024;
const IO_CHUNK: usize = 64 * 1024;
const PRIVATE_DIR: u32 = 0o700;
const PRIVATE_FILE: u32 = 0o600;
const HELPER_FILE: u32 = 0o700;
const ASSET_NAMES: [&str; 11] = [
    "kernel",
    "rootfs.ext4",
    "manifest.json",
    "provenance.json",
    "kernel.config",
    "kernel.generated.config",
    "NOTICES.md",
    "packages.lock.json",
    "runtime-components.json",
    "runtime-installed.db",
    "linux-6.12.117.tar.xz",
];

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RuntimeArtifacts {
    pub version: String,
    pub source_sha: String,
    pub catalog_sha256: String,
    pub helper: PathBuf,
    pub helper_sha256: String,
    pub agent_apk: PathBuf,
    pub agent_apk_sha256: String,
    pub fixture_apk: PathBuf,
    pub assets: PathBuf,
    pub cache_hit: bool,
}

#[derive(Deserialize)]
struct CatalogStamp {
    schema_version: u64,
    version: String,
    source_sha: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct MemberRecord {
    name: String,
    size: u64,
    sha256: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct IntegrityReceipt {
    schema_version: u64,
    version: String,
    source_sha: String,
    catalog_sha256: String,
    guest_size: u64,
    guest_sha256: String,
    members: Vec<MemberRecord>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EntryMarker {
    schema: u32,
    identity: String,
    owner_uid: u32,
    catalog_hash: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StageMarker {
    schema: u32,
    identity: String,
    owner_uid: u32,
}

struct ValidationContext<'a> {
    version: &'a str,
    source_sha: &'a str,
    key: &'a str,
    deadline: Instant,
    cancel: &'a CancellationToken,
    cache_hit: bool,
    promoted: bool,
}

/// Acquires the fixed 0.3.2 runtime, reusing a fully validated cache entry when available.
pub async fn acquire_runtime(
    cache_root: PathBuf,
    expected_version: String,
    expected_source_sha: String,
    deadline: Instant,
    cancel: CancellationToken,
) -> anyhow::Result<RuntimeArtifacts> {
    validate_request(&expected_version, &expected_source_sha, deadline, &cancel)?;
    acquire_runtime_with_source(
        cache_root,
        expected_version,
        expected_source_sha,
        deadline,
        cancel,
        Arc::new(ProductionSource::default()),
    )
    .await
}

async fn acquire_runtime_with_source(
    cache_root: PathBuf,
    expected_version: String,
    expected_source_sha: String,
    deadline: Instant,
    cancel: CancellationToken,
    source: Arc<dyn ArtifactSource>,
) -> anyhow::Result<RuntimeArtifacts> {
    validate_request(&expected_version, &expected_source_sha, deadline, &cancel)?;
    let worker_cancel = cancel.child_token();
    let drop_guard = CancelOnDrop(worker_cancel.clone());
    let worker = tokio::task::spawn_blocking(move || {
        run_worker(
            &cache_root,
            &expected_version,
            &expected_source_sha,
            deadline,
            &worker_cancel,
            source,
        )
    });
    let result = worker
        .await
        .map_err(|_| anyhow!("runtime acquisition worker failed"))?;
    drop(drop_guard);
    result
}

struct CancelOnDrop(CancellationToken);
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

fn validate_request(
    version: &str,
    source: &str,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<()> {
    ensure!(version == VERSION, "unsupported runtime version");
    ensure!(
        source.len() == 40
            && source
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "invalid runtime source SHA"
    );
    checkpoint(deadline, cancel)?;
    Ok(())
}

trait ArtifactSource: Send + Sync {
    fn fetch_catalog(
        &self,
        version: &str,
        source: &str,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> anyhow::Result<CatalogDownload>;
    fn fetch_artifact(
        &self,
        catalog: &RuntimeCatalog,
        entry: &crate::runtime_catalog::CatalogEntry,
        file: &mut File,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> anyhow::Result<DownloadReceipt>;
}

#[derive(Default)]
struct ProductionSource {
    resources: std::sync::Mutex<Option<(tokio::runtime::Runtime, RuntimeDownloader)>>,
}

impl ArtifactSource for ProductionSource {
    fn fetch_catalog(
        &self,
        version: &str,
        source: &str,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> anyhow::Result<CatalogDownload> {
        let mut resources = self
            .resources
            .lock()
            .map_err(|_| anyhow!("runtime downloader resources unavailable"))?;
        if resources.is_none() {
            resources.replace((
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .context("could not create private runtime downloader context")?,
                RuntimeDownloader::new()?,
            ));
        }
        let (runtime, downloader) = resources.as_ref().unwrap();
        runtime.block_on(downloader.fetch_catalog(
            version,
            source,
            tokio::time::Instant::from_std(deadline),
            cancel,
        ))
    }

    fn fetch_artifact(
        &self,
        catalog: &RuntimeCatalog,
        entry: &crate::runtime_catalog::CatalogEntry,
        file: &mut File,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> anyhow::Result<DownloadReceipt> {
        let mut resources = self
            .resources
            .lock()
            .map_err(|_| anyhow!("runtime downloader resources unavailable"))?;
        if resources.is_none() {
            resources.replace((
                tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .context("could not create private runtime downloader context")?,
                RuntimeDownloader::new()?,
            ));
        }
        let (runtime, downloader) = resources.as_ref().unwrap();
        let mut writer = SyncAsyncWriter {
            file,
            deadline,
            cancel,
        };
        runtime.block_on(downloader.fetch_entry(
            catalog,
            entry,
            &mut writer,
            tokio::time::Instant::from_std(deadline),
            cancel,
        ))
    }
}

struct SyncAsyncWriter<'a> {
    file: &'a mut File,
    deadline: Instant,
    cancel: &'a CancellationToken,
}

impl AsyncWrite for SyncAsyncWriter<'_> {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<io::Result<usize>> {
        if let Err(error) = checkpoint(self.deadline, self.cancel) {
            return std::task::Poll::Ready(Err(io::Error::other(error.to_string())));
        }
        let size = buf.len().min(IO_CHUNK);
        std::task::Poll::Ready(self.file.write(&buf[..size]))
    }
    fn poll_flush(
        mut self: std::pin::Pin<&mut Self>,
        _cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        if let Err(error) = checkpoint(self.deadline, self.cancel) {
            return std::task::Poll::Ready(Err(io::Error::other(error.to_string())));
        }
        std::task::Poll::Ready(self.file.flush())
    }
    fn poll_shutdown(
        self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<io::Result<()>> {
        self.poll_flush(cx)
    }
}

fn run_worker(
    root: &Path,
    version: &str,
    source_sha: &str,
    deadline: Instant,
    cancel: &CancellationToken,
    source: Arc<dyn ArtifactSource>,
) -> anyhow::Result<RuntimeArtifacts> {
    let guard = loop {
        checkpoint(deadline, cancel)?;
        if let Some(guard) = CacheGuard::try_acquire(root)? {
            break guard;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            bail!("runtime acquisition deadline exceeded");
        }
        std::thread::sleep(remaining.min(Duration::from_millis(25)));
    };
    if let Some(found) = find_cached(&guard, version, source_sha, deadline, cancel)? {
        return Ok(found);
    }

    let downloaded = source.fetch_catalog(version, source_sha, deadline, cancel)?;
    checkpoint(deadline, cancel)?;
    let catalog_bytes = downloaded.receipt.bytes;
    ensure!(
        catalog_hash(&catalog_bytes) == downloaded.receipt.sha256,
        "catalog receipt digest mismatch"
    );
    let catalog = RuntimeCatalog::parse(&catalog_bytes, version, source_sha)
        .context("downloaded runtime catalog validation failed")?;
    let key = downloaded.receipt.sha256;
    ensure!(
        guard.lookup_entry(&key)?.is_none(),
        "runtime cache destination already exists"
    );
    let stage = guard.create_stage()?;
    let result = populate_stage(
        &stage,
        &catalog,
        &catalog_bytes,
        &key,
        source.as_ref(),
        deadline,
        cancel,
    )
    .and_then(|()| {
        validate_entry(
            stage.path(),
            &catalog_bytes,
            &catalog,
            &ValidationContext {
                version,
                source_sha,
                key: &key,
                deadline,
                cancel,
                cache_hit: false,
                promoted: false,
            },
        )
        .map(|_| ())
    });
    let entry = match result {
        Ok(()) => match stage.try_promote(&key) {
            Ok(entry) => entry,
            Err((stage, error)) => {
                stage
                    .abort()
                    .context("runtime promotion failed and owned stage cleanup failed")?;
                return Err(error);
            }
        },
        Err(error) => {
            stage
                .abort()
                .context("runtime acquisition failed and owned stage cleanup failed")?;
            return Err(error);
        }
    };
    Ok(artifacts_at(
        &entry, &catalog, version, source_sha, &key, false,
    ))
}

fn find_cached(
    guard: &CacheGuard,
    version: &str,
    source_sha: &str,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<Option<RuntimeArtifacts>> {
    let mut matches = Vec::new();
    for key in guard.entry_keys()? {
        checkpoint(deadline, cancel)?;
        let path = guard
            .lookup_entry(&key)?
            .ok_or_else(|| anyhow!("runtime cache entry disappeared"))?;
        let catalog_path = path.join("catalog.json");
        let bytes = read_bounded(
            &catalog_path,
            MAX_CATALOG_BYTES,
            PRIVATE_FILE,
            deadline,
            cancel,
        )?;
        ensure!(
            catalog_hash(&bytes) == key,
            "cached catalog digest does not match entry key"
        );
        let stamp: CatalogStamp =
            serde_json::from_slice(&bytes).context("invalid cached catalog stamp")?;
        ensure!(stamp.schema_version == 1, "cached catalog schema must be 1");
        let catalog = RuntimeCatalog::parse(&bytes, &stamp.version, &stamp.source_sha)
            .context("cached catalog failed strict validation")?;
        if stamp.version == version && stamp.source_sha == source_sha {
            matches.push((key, path, bytes, catalog));
        }
    }
    ensure!(
        matches.len() <= 1,
        "multiple cache entries match the requested runtime stamp"
    );
    match matches.pop() {
        Some((key, path, bytes, catalog)) => validate_entry(
            &path,
            &bytes,
            &catalog,
            &ValidationContext {
                version,
                source_sha,
                key: &key,
                deadline,
                cancel,
                cache_hit: true,
                promoted: true,
            },
        )
        .map(Some),
        None => Ok(None),
    }
}

fn populate_stage(
    stage: &CacheStage<'_>,
    catalog: &RuntimeCatalog,
    catalog_bytes: &[u8],
    key: &str,
    source: &dyn ArtifactSource,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<()> {
    checkpoint(deadline, cancel)?;
    let stage_path = stage.path();
    write_new(
        stage_path.join("catalog.json"),
        catalog_bytes,
        PRIVATE_FILE,
        deadline,
        cancel,
    )?;
    let mut guest_path = None;
    for (role, name) in [
        (
            ArtifactRole::Guest,
            catalog.artifact(ArtifactRole::Guest).name(),
        ),
        (
            ArtifactRole::Helper,
            catalog.artifact(ArtifactRole::Helper).name(),
        ),
        (
            ArtifactRole::Agent,
            catalog.artifact(ArtifactRole::Agent).name(),
        ),
        (
            ArtifactRole::Fixture,
            catalog.artifact(ArtifactRole::Fixture).name(),
        ),
    ] {
        checkpoint(deadline, cancel)?;
        let path = stage_path.join(name);
        let mut file = create_new(&path, PRIVATE_FILE)?;
        let receipt =
            source.fetch_artifact(catalog, catalog.artifact(role), &mut file, deadline, cancel)?;
        file.sync_all()?;
        let expected = catalog.artifact(role);
        ensure!(
            receipt.size == expected.size() && receipt.sha256 == expected.sha256(),
            "runtime artifact receipt does not match catalog"
        );
        if role == ArtifactRole::Guest {
            guest_path = Some(path);
        } else if role == ArtifactRole::Helper {
            fs::set_permissions(&path, fs::Permissions::from_mode(HELPER_FILE))?;
        }
    }
    let guest_path = guest_path.context("guest artifact was not acquired")?;
    let assets_path = stage_path.join("assets");
    fs::create_dir(&assets_path)?;
    fs::set_permissions(&assets_path, fs::Permissions::from_mode(PRIVATE_DIR))?;
    let extraction =
        runtime_extract::extract_runtime(&guest_path, &assets_path, catalog, deadline, cancel)?;
    validate_extraction(&extraction, catalog)?;
    checkpoint(deadline, cancel)?;
    fs::remove_file(&guest_path)?;
    let members = extraction
        .members
        .iter()
        .map(|(name, member)| MemberRecord {
            name: name.clone(),
            size: member.size,
            sha256: member.sha256.clone(),
        })
        .collect();
    let receipt = IntegrityReceipt {
        schema_version: 1,
        version: catalog.version().to_owned(),
        source_sha: catalog.source_sha().to_owned(),
        catalog_sha256: key.to_owned(),
        guest_size: extraction.compressed_size,
        guest_sha256: extraction.compressed_sha256,
        members,
    };
    let json = serde_json::to_vec(&receipt)?;
    ensure!(
        json.len() <= MAX_INTEGRITY_BYTES,
        "runtime integrity receipt exceeds limit"
    );
    write_new(
        stage_path.join("integrity.json"),
        &json,
        PRIVATE_FILE,
        deadline,
        cancel,
    )?;
    for name in [
        "catalog.json",
        "integrity.json",
        catalog.artifact(ArtifactRole::Helper).name(),
        catalog.artifact(ArtifactRole::Agent).name(),
        catalog.artifact(ArtifactRole::Fixture).name(),
    ] {
        File::open(stage_path.join(name))?.sync_all()?;
    }
    File::open(stage_path)?.sync_all()?;
    checkpoint(deadline, cancel)?;
    Ok(())
}

fn validate_entry(
    path: &Path,
    catalog_bytes: &[u8],
    catalog: &RuntimeCatalog,
    context: &ValidationContext<'_>,
) -> anyhow::Result<RuntimeArtifacts> {
    let ValidationContext {
        version,
        source_sha,
        key,
        deadline,
        cancel,
        cache_hit,
        promoted,
    } = *context;
    validate_dir(path)?;
    let helper_name = catalog.artifact(ArtifactRole::Helper).name();
    let agent_name = catalog.artifact(ArtifactRole::Agent).name();
    let fixture_name = catalog.artifact(ArtifactRole::Fixture).name();
    let mut expected = vec![
        "catalog.json",
        "integrity.json",
        "stage.json",
        "assets",
        helper_name,
        agent_name,
        fixture_name,
    ];
    if promoted {
        expected.push("entry.json");
    }
    expected.sort_unstable();
    let mut found = list_names(path, expected.len(), deadline, cancel)?;
    found.sort();
    let expected_os: Vec<_> = expected.iter().map(std::ffi::OsString::from).collect();
    ensure!(
        found == expected_os,
        "runtime cache entry has unexpected or missing files"
    );
    let stored_catalog = read_bounded(
        &path.join("catalog.json"),
        MAX_CATALOG_BYTES,
        PRIVATE_FILE,
        deadline,
        cancel,
    )?;
    ensure!(
        stored_catalog == catalog_bytes && catalog_hash(&stored_catalog) == key,
        "runtime cache catalog changed"
    );
    let receipt_bytes = read_bounded(
        &path.join("integrity.json"),
        MAX_INTEGRITY_BYTES,
        PRIVATE_FILE,
        deadline,
        cancel,
    )?;
    let receipt: IntegrityReceipt =
        serde_json::from_slice(&receipt_bytes).context("invalid runtime integrity receipt")?;
    ensure!(
        receipt.schema_version == 1
            && receipt.version == version
            && receipt.source_sha == source_sha
            && receipt.catalog_sha256 == key,
        "runtime integrity receipt stamp mismatch"
    );
    let guest = catalog.artifact(ArtifactRole::Guest);
    ensure!(
        receipt.guest_size == guest.size() && receipt.guest_sha256 == guest.sha256(),
        "runtime integrity receipt guest identity mismatch"
    );
    if promoted {
        let marker: EntryMarker = serde_json::from_slice(&read_bounded(
            &path.join("entry.json"),
            4096,
            PRIVATE_FILE,
            deadline,
            cancel,
        )?)?;
        let uid = unsafe { libc::geteuid() };
        ensure!(
            marker.schema == 1
                && marker.identity == "ashnazg-runtime-cache"
                && marker.owner_uid == uid
                && marker.catalog_hash == key,
            "invalid cache entry marker"
        );
    }
    let stage_marker: StageMarker = serde_json::from_slice(&read_bounded(
        &path.join("stage.json"),
        4096,
        PRIVATE_FILE,
        deadline,
        cancel,
    )?)?;
    let uid = unsafe { libc::geteuid() };
    ensure!(
        stage_marker.schema == 1
            && stage_marker.identity == "ashnazg-runtime-cache"
            && stage_marker.owner_uid == uid,
        "invalid cache stage marker"
    );
    let mut seen = HashSet::new();
    ensure!(
        receipt.members.len() == ASSET_NAMES.len(),
        "runtime integrity receipt must contain exactly eleven members"
    );
    for record in &receipt.members {
        ensure!(
            ASSET_NAMES.contains(&record.name.as_str()) && seen.insert(record.name.as_str()),
            "invalid or duplicate runtime member record"
        );
        ensure!(
            record.size > 0
                && runtime_extract::member_size_limit(&record.name)
                    .is_some_and(|maximum| record.size <= maximum)
                && valid_sha256(&record.sha256),
            "invalid runtime member record"
        );
    }
    ensure!(
        seen.len() == ASSET_NAMES.len(),
        "runtime integrity receipt is missing a member"
    );
    validate_dir(&path.join("assets"))?;
    let mut assets = list_names(&path.join("assets"), ASSET_NAMES.len(), deadline, cancel)?;
    assets.sort();
    let mut expected_assets: Vec<_> = ASSET_NAMES.iter().map(std::ffi::OsString::from).collect();
    expected_assets.sort();
    ensure!(assets == expected_assets, "runtime assets layout mismatch");
    let receipt_map: std::collections::BTreeMap<_, _> = receipt
        .members
        .iter()
        .map(|record| (record.name.as_str(), record))
        .collect();
    for name in ASSET_NAMES {
        let record = receipt_map[name];
        validate_file(
            &path.join("assets").join(name),
            PRIVATE_FILE,
            Some(record.size),
        )?;
    }
    let kernel = receipt_map["kernel"];
    let rootfs = receipt_map["rootfs.ext4"];
    let manifest_path = path.join("assets/manifest.json");
    let manifest = read_bounded(&manifest_path, 64 * 1024, PRIVATE_FILE, deadline, cancel)?;
    ensure!(
        catalog_hash(&manifest) == receipt_map["manifest.json"].sha256,
        "cached boot manifest digest mismatch"
    );
    let member = |r: &MemberRecord| ExtractedMember {
        size: r.size,
        sha256: r.sha256.clone(),
    };
    runtime_extract::validate_manifest(
        &manifest,
        catalog,
        Some(&member(kernel)),
        Some(&member(rootfs)),
    )?;
    for (name, role, mode) in [
        (helper_name, ArtifactRole::Helper, HELPER_FILE),
        (agent_name, ArtifactRole::Agent, PRIVATE_FILE),
        (fixture_name, ArtifactRole::Fixture, PRIVATE_FILE),
    ] {
        let entry = catalog.artifact(role);
        validate_file(&path.join(name), mode, Some(entry.size()))?;
        ensure!(
            hash_file(&path.join(name), mode, deadline, cancel)? == entry.sha256(),
            "cached runtime artifact digest mismatch"
        );
    }
    checkpoint(deadline, cancel)?;
    Ok(artifacts_at(
        path, catalog, version, source_sha, key, cache_hit,
    ))
}

fn artifacts_at(
    path: &Path,
    catalog: &RuntimeCatalog,
    version: &str,
    source_sha: &str,
    key: &str,
    cache_hit: bool,
) -> RuntimeArtifacts {
    RuntimeArtifacts {
        version: version.to_owned(),
        source_sha: source_sha.to_owned(),
        catalog_sha256: key.to_owned(),
        helper: path.join(catalog.artifact(ArtifactRole::Helper).name()),
        helper_sha256: catalog.artifact(ArtifactRole::Helper).sha256().to_owned(),
        agent_apk: path.join(catalog.artifact(ArtifactRole::Agent).name()),
        agent_apk_sha256: catalog.artifact(ArtifactRole::Agent).sha256().to_owned(),
        fixture_apk: path.join(catalog.artifact(ArtifactRole::Fixture).name()),
        assets: path.join("assets"),
        cache_hit,
    }
}

fn validate_extraction(
    receipt: &ExtractionReceipt,
    catalog: &RuntimeCatalog,
) -> anyhow::Result<()> {
    ensure!(
        receipt.source_sha == catalog.source_sha(),
        "runtime extraction source mismatch"
    );
    let guest = catalog.artifact(ArtifactRole::Guest);
    ensure!(
        receipt.compressed_size == guest.size() && receipt.compressed_sha256 == guest.sha256(),
        "runtime extraction compressed identity mismatch"
    );
    ensure!(
        receipt.members.len() == ASSET_NAMES.len()
            && ASSET_NAMES
                .iter()
                .all(|name| receipt.members.contains_key(*name)),
        "runtime extraction member receipt mismatch"
    );
    Ok(())
}

fn validate_dir(path: &Path) -> anyhow::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    ensure!(
        meta.is_dir()
            && meta.uid() == unsafe { libc::geteuid() }
            && meta.mode() & 0o7777 == PRIVATE_DIR,
        "runtime directory must be owned mode 0700"
    );
    Ok(())
}

fn validate_file(path: &Path, mode: u32, size: Option<u64>) -> anyhow::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    ensure!(
        meta.is_file() && meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o7777 == mode,
        "runtime file must be owned regular mode {mode:o}"
    );
    if let Some(size) = size {
        ensure!(meta.len() == size, "runtime file size mismatch");
    }
    Ok(())
}

fn list_names(
    path: &Path,
    maximum: usize,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<Vec<std::ffi::OsString>> {
    let mut names = Vec::with_capacity(maximum);
    for entry in fs::read_dir(path)? {
        checkpoint(deadline, cancel)?;
        ensure!(
            names.len() < maximum,
            "runtime directory contains too many entries"
        );
        names.push(entry?.file_name());
    }
    Ok(names)
}

fn read_bounded(
    path: &Path,
    limit: usize,
    mode: u32,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<Vec<u8>> {
    validate_file(path, mode, None)?;
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    validate_open_file(&file, mode, Some(limit as u64))?;
    let mut out = Vec::with_capacity(limit.min(4096));
    let mut buf = [0u8; IO_CHUNK];
    loop {
        checkpoint(deadline, cancel)?;
        let chunk_len = buf.len().min(limit + 1 - out.len());
        let n = file.read(&mut buf[..chunk_len])?;
        if n == 0 {
            break;
        }
        out.extend_from_slice(&buf[..n]);
        ensure!(out.len() <= limit, "runtime metadata exceeds size limit");
    }
    Ok(out)
}

fn hash_file(
    path: &Path,
    mode: u32,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<String> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.mode() & 0o7777 == mode,
        "runtime hash target must be an owned mode-{mode:o} regular file"
    );
    let mut hash = Sha256::new();
    let mut buffer = [0u8; IO_CHUNK];
    loop {
        checkpoint(deadline, cancel)?;
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(hex(&hash.finalize()))
}

fn validate_open_file(file: &File, mode: u32, maximum_size: Option<u64>) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.mode() & 0o7777 == mode,
        "runtime metadata must be an owned mode-{mode:o} regular file"
    );
    if let Some(maximum_size) = maximum_size {
        ensure!(
            metadata.len() <= maximum_size,
            "runtime metadata exceeds size limit"
        );
    }
    Ok(())
}

fn create_new(path: &Path, mode: u32) -> anyhow::Result<File> {
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    file.set_permissions(fs::Permissions::from_mode(mode))?;
    Ok(file)
}

fn write_new(
    path: PathBuf,
    bytes: &[u8],
    mode: u32,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<()> {
    let mut file = create_new(&path, mode)?;
    let mut offset = 0;
    while offset < bytes.len() {
        checkpoint(deadline, cancel)?;
        let end = (offset + IO_CHUNK).min(bytes.len());
        file.write_all(&bytes[offset..end])?;
        offset = end;
    }
    file.sync_all()?;
    Ok(())
}

fn checkpoint(deadline: Instant, cancel: &CancellationToken) -> anyhow::Result<()> {
    ensure!(!cancel.is_cancelled(), "runtime acquisition cancelled");
    ensure!(
        Instant::now() < deadline,
        "runtime acquisition deadline exceeded"
    );
    Ok(())
}

fn catalog_hash(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_catalog::RuntimeCatalog;
    use crate::runtime_download::{CatalogReceipt, DownloadReceipt};
    use sha2::{Digest, Sha256};
    use std::collections::BTreeMap;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc;
    use std::time::{Duration, Instant};
    use tar::{Builder, EntryType, Header};
    use tokio_util::sync::CancellationToken;
    use xz2::write::XzEncoder;

    const SOURCE_SHA: &str = "0123456789abcdef0123456789abcdef01234567";

    struct FixtureSource {
        catalog: Vec<u8>,
        artifacts: BTreeMap<String, Vec<u8>>,
        calls: AtomicUsize,
        block_fetch: bool,
        entered: Mutex<Option<mpsc::Sender<()>>>,
    }

    impl ArtifactSource for FixtureSource {
        fn fetch_catalog(
            &self,
            version: &str,
            source: &str,
            _deadline: Instant,
            _cancel: &CancellationToken,
        ) -> anyhow::Result<CatalogDownload> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let catalog = RuntimeCatalog::parse(&self.catalog, version, source)?;
            Ok(CatalogDownload {
                catalog,
                receipt: CatalogReceipt {
                    sha256: catalog_hash(&self.catalog),
                    bytes: self.catalog.clone(),
                },
            })
        }
        fn fetch_artifact(
            &self,
            _catalog: &RuntimeCatalog,
            entry: &crate::runtime_catalog::CatalogEntry,
            file: &mut File,
            _deadline: Instant,
            _cancel: &CancellationToken,
        ) -> anyhow::Result<DownloadReceipt> {
            let call = self.calls.fetch_add(1, Ordering::SeqCst);
            if self.block_fetch && call == 1 {
                if let Some(tx) = self.entered.lock().unwrap().take() {
                    tx.send(()).unwrap();
                }
                loop {
                    checkpoint(_deadline, _cancel)?;
                    std::thread::sleep(Duration::from_millis(5));
                }
            }
            let bytes = self
                .artifacts
                .get(entry.name())
                .context("fixture artifact unavailable")?;
            file.write_all(bytes)?;
            Ok(DownloadReceipt {
                size: bytes.len() as u64,
                sha256: digest(bytes),
                final_host: "fixture.invalid".to_owned(),
            })
        }
    }

    fn private_tempdir() -> tempfile::TempDir {
        tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(PRIVATE_DIR))
            .tempdir()
            .unwrap()
    }

    fn digest(bytes: &[u8]) -> String {
        bytes
            .iter()
            .fold(Sha256::new(), |mut h, b| {
                h.update([*b]);
                h
            })
            .finalize()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    fn archive_fixture(source_sha: &str) -> Vec<u8> {
        let kernel = b"tiny kernel";
        let rootfs = b"tiny rootfs";
        let manifest = format!(r#"{{"schema_version":1,"version":"0.3.2","source_sha":"{source_sha}","architecture":"aarch64","files":{{"kernel":{{"sha256":"{}","size":{}}},"rootfs.ext4":{{"sha256":"{}","size":{}}}}},"guest":{{"kernel_version":"6.12.117","base":"alpine-3.22.8-aarch64","redroid_index":"sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee"}}}}"#, digest(kernel), kernel.len(), digest(rootfs), rootfs.len()).into_bytes();
        let contents: BTreeMap<&str, Vec<u8>> = [
            ("kernel", kernel.to_vec()),
            ("rootfs.ext4", rootfs.to_vec()),
            ("manifest.json", manifest),
            ("provenance.json", b"{}".to_vec()),
            ("kernel.config", b"config".to_vec()),
            ("kernel.generated.config", b"generated".to_vec()),
            ("NOTICES.md", b"notice".to_vec()),
            ("packages.lock.json", b"{}".to_vec()),
            ("runtime-components.json", b"{}".to_vec()),
            ("runtime-installed.db", b"db".to_vec()),
            ("linux-6.12.117.tar.xz", b"source".to_vec()),
        ]
        .into_iter()
        .collect();
        let mut tar = Builder::new(Vec::new());
        for name in ASSET_NAMES {
            let bytes = &contents[name];
            let mut header = Header::new_gnu();
            header.set_entry_type(EntryType::Regular);
            header.set_size(bytes.len() as u64);
            header.set_mode(0o600);
            header.set_cksum();
            tar.append_data(&mut header, name, bytes.as_slice())
                .unwrap();
        }
        tar.finish().unwrap();
        let tar_bytes = tar.into_inner().unwrap();
        let mut xz = XzEncoder::new(Vec::new(), 1);
        xz.write_all(&tar_bytes).unwrap();
        xz.finish().unwrap()
    }

    fn fixture_source() -> FixtureSource {
        fixture_source_for(SOURCE_SHA)
    }

    fn fixture_source_for(source_sha: &str) -> FixtureSource {
        let guest = archive_fixture(source_sha);
        let helper = b"helper bytes".to_vec();
        let agent = b"agent bytes".to_vec();
        let fixture = b"fixture bytes".to_vec();
        let catalog = format!(r#"{{"schema_version":1,"version":"0.3.2","release_tag":"v0.3.2","source_sha":"{source_sha}","artifacts":{{"guest":{{"name":"ashnazg-microvm-v0.3.2-linux-aarch64.tar.xz","platform":"linux-aarch64","source_sha":"{source_sha}","size":{},"sha256":"{}"}},"helper":{{"name":"ashnazg-vm-v0.3.2-macos-aarch64","platform":"macos-aarch64","source_sha":"{source_sha}","size":{},"sha256":"{}"}},"agent":{{"name":"ashnazg-agent-v0.3.2.apk","platform":"android","source_sha":"{source_sha}","size":{},"sha256":"{}"}},"fixture":{{"name":"ashnazg-fixture-v0.3.2.apk","platform":"android","source_sha":"{source_sha}","size":{},"sha256":"{}"}}}},"source_archives":[]}}"#, guest.len(), digest(&guest), helper.len(), digest(&helper), agent.len(), digest(&agent), fixture.len(), digest(&fixture)).into_bytes();
        let artifacts = [
            (
                "ashnazg-microvm-v0.3.2-linux-aarch64.tar.xz".to_owned(),
                guest,
            ),
            ("ashnazg-vm-v0.3.2-macos-aarch64".to_owned(), helper),
            ("ashnazg-agent-v0.3.2.apk".to_owned(), agent),
            ("ashnazg-fixture-v0.3.2.apk".to_owned(), fixture),
        ]
        .into_iter()
        .collect();
        FixtureSource {
            catalog,
            artifacts,
            calls: AtomicUsize::new(0),
            block_fetch: false,
            entered: Mutex::new(None),
        }
    }

    #[tokio::test]
    async fn cold_acquisition_promotes_then_reuses_offline_cache() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let first = acquire_runtime_with_source(
            root.clone(),
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        assert!(!first.cache_hit);
        assert_eq!(source.calls.load(Ordering::SeqCst), 5);
        let second = acquire_runtime_with_source(
            root,
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        assert!(second.cache_hit);
        assert_eq!(first.version, second.version);
        assert_eq!(first.source_sha, second.source_sha);
        assert_eq!(first.catalog_sha256, second.catalog_sha256);
        assert_eq!(first.helper, second.helper);
        assert_eq!(first.helper_sha256, digest(b"helper bytes"));
        assert_eq!(first.agent_apk_sha256, digest(b"agent bytes"));
        assert_eq!(first.helper_sha256, second.helper_sha256);
        assert_eq!(first.agent_apk_sha256, second.agent_apk_sha256);
        assert_eq!(first.agent_apk, second.agent_apk);
        assert_eq!(first.fixture_apk, second.fixture_apk);
        assert_eq!(first.assets, second.assets);
        assert_eq!(source.calls.load(Ordering::SeqCst), 5);
    }

    #[tokio::test]
    async fn corrupt_cached_helper_is_refused_without_network_fallback() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let result = acquire_runtime_with_source(
            root.clone(),
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        fs::write(&result.helper, b"corrupt").unwrap();
        let calls = source.calls.load(Ordering::SeqCst);
        assert!(
            acquire_runtime_with_source(
                root,
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_secs(10),
                CancellationToken::new(),
                source.clone()
            )
            .await
            .is_err()
        );
        assert_eq!(source.calls.load(Ordering::SeqCst), calls);
    }

    #[tokio::test]
    async fn oversized_cached_asset_and_matching_receipt_are_refused() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let result = acquire_runtime_with_source(
            root.clone(),
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        let oversized = 2 * 1024 * 1024 + 1;
        OpenOptions::new()
            .write(true)
            .open(result.assets.join("provenance.json"))
            .unwrap()
            .set_len(oversized)
            .unwrap();
        let receipt_path = result.assets.parent().unwrap().join("integrity.json");
        let mut receipt: IntegrityReceipt =
            serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
        receipt
            .members
            .iter_mut()
            .find(|member| member.name == "provenance.json")
            .unwrap()
            .size = oversized;
        fs::write(receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        let calls = source.calls.load(Ordering::SeqCst);
        let error = acquire_runtime_with_source(
            root,
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("member record"), "{error:#}");
        assert_eq!(source.calls.load(Ordering::SeqCst), calls);
    }

    #[tokio::test]
    async fn duplicate_integrity_members_are_refused() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let result = acquire_runtime_with_source(
            root.clone(),
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        let receipt_path = result.assets.parent().unwrap().join("integrity.json");
        let mut receipt: serde_json::Value =
            serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
        let first = receipt["members"][0].clone();
        receipt["members"].as_array_mut().unwrap().push(first);
        fs::write(receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        let calls = source.calls.load(Ordering::SeqCst);
        assert!(
            acquire_runtime_with_source(
                root,
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_secs(10),
                CancellationToken::new(),
                source.clone()
            )
            .await
            .is_err()
        );
        assert_eq!(source.calls.load(Ordering::SeqCst), calls);
    }

    #[tokio::test]
    async fn cached_helper_symlink_fifo_and_wrong_mode_are_refused() {
        for kind in ["symlink", "fifo", "mode"] {
            let parent = private_tempdir();
            let root = parent.path().canonicalize().unwrap().join("runtime-cache");
            let source = Arc::new(fixture_source());
            let result = acquire_runtime_with_source(
                root.clone(),
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_secs(10),
                CancellationToken::new(),
                source.clone(),
            )
            .await
            .unwrap();
            let target = parent.path().join("replacement");
            fs::remove_file(&result.helper).unwrap();
            match kind {
                "symlink" => {
                    fs::write(&target, b"not the helper").unwrap();
                    symlink(&target, &result.helper).unwrap();
                }
                "fifo" => {
                    let path =
                        std::ffi::CString::new(result.helper.as_os_str().as_bytes()).unwrap();
                    assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o600) }, 0);
                }
                _ => {
                    fs::write(&result.helper, b"helper bytes").unwrap();
                    fs::set_permissions(&result.helper, fs::Permissions::from_mode(0o600)).unwrap();
                }
            }
            let calls = source.calls.load(Ordering::SeqCst);
            assert!(
                acquire_runtime_with_source(
                    root,
                    VERSION.to_owned(),
                    SOURCE_SHA.to_owned(),
                    Instant::now() + Duration::from_secs(10),
                    CancellationToken::new(),
                    source.clone(),
                )
                .await
                .is_err(),
                "accepted cached helper {kind}"
            );
            assert_eq!(source.calls.load(Ordering::SeqCst), calls);
        }
    }

    #[tokio::test]
    async fn cached_catalog_hash_corruption_is_refused() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let result = acquire_runtime_with_source(
            root.clone(),
            VERSION.to_owned(),
            SOURCE_SHA.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source.clone(),
        )
        .await
        .unwrap();
        let catalog_path = result.helper.parent().unwrap().join("catalog.json");
        let mut bytes = fs::read(&catalog_path).unwrap();
        bytes.push(b' ');
        fs::write(catalog_path, bytes).unwrap();
        let calls = source.calls.load(Ordering::SeqCst);
        assert!(
            acquire_runtime_with_source(
                root,
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_secs(10),
                CancellationToken::new(),
                source.clone(),
            )
            .await
            .is_err()
        );
        assert_eq!(source.calls.load(Ordering::SeqCst), calls);
    }

    #[tokio::test]
    async fn occupied_lock_respects_deadline_without_fetching() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let held = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let source = Arc::new(fixture_source());
        assert!(
            acquire_runtime_with_source(
                root,
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_millis(60),
                CancellationToken::new(),
                source.clone()
            )
            .await
            .is_err()
        );
        assert_eq!(source.calls.load(Ordering::SeqCst), 0);
        drop(held);
    }

    #[tokio::test]
    async fn dropping_acquisition_cancels_worker_and_worker_cleans_its_stage() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let (entered_tx, entered_rx) = mpsc::channel();
        let mut fake = fixture_source();
        fake.block_fetch = true;
        fake.entered = Mutex::new(Some(entered_tx));
        let source = Arc::new(fake);
        let task_root = root.clone();
        let task_source = source.clone();
        let parent_cancel = CancellationToken::new();
        let task_cancel = parent_cancel.clone();
        let task = tokio::spawn(async move {
            acquire_runtime_with_source(
                task_root,
                VERSION.to_owned(),
                SOURCE_SHA.to_owned(),
                Instant::now() + Duration::from_secs(10),
                task_cancel,
                task_source,
            )
            .await
        });
        tokio::task::spawn_blocking(move || {
            entered_rx.recv_timeout(Duration::from_secs(2)).unwrap()
        })
        .await
        .unwrap();
        task.abort();
        let _ = task.await;
        assert!(!parent_cancel.is_cancelled());
        let limit = Instant::now() + Duration::from_secs(2);
        loop {
            if let Some(guard) = CacheGuard::try_acquire(&root).unwrap() {
                assert!(guard.entry_keys().unwrap().is_empty());
                drop(guard);
                break;
            }
            assert!(Instant::now() < limit, "worker did not release cache lock");
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(fs::read_dir(root.join("staging")).unwrap().count(), 0);
    }

    #[tokio::test]
    async fn unrelated_valid_runtime_remains_available_after_failed_cold_acquisition() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let other_sha = "1123456789abcdef0123456789abcdef01234567";
        let other = Arc::new(fixture_source_for(other_sha));
        let first = acquire_fixture(&root, other_sha, other.clone())
            .await
            .unwrap();
        let before = fs::read(&first.helper).unwrap();
        let mut bad = fixture_source();
        bad.artifacts
            .get_mut("ashnazg-agent-v0.3.2.apk")
            .unwrap()
            .push(0);
        assert!(
            acquire_fixture(&root, SOURCE_SHA, Arc::new(bad))
                .await
                .is_err()
        );
        assert_eq!(fs::read_dir(root.join("staging")).unwrap().count(), 0);
        assert_eq!(fs::read_dir(root.join("entries")).unwrap().count(), 1);
        assert_eq!(fs::read(&first.helper).unwrap(), before);
        let calls = other.calls.load(Ordering::SeqCst);
        assert!(
            acquire_fixture(&root, other_sha, other.clone())
                .await
                .unwrap()
                .cache_hit
        );
        assert_eq!(other.calls.load(Ordering::SeqCst), calls);
        let requested = Arc::new(fixture_source());
        assert!(
            !acquire_fixture(&root, SOURCE_SHA, requested.clone())
                .await
                .unwrap()
                .cache_hit
        );
        let calls = requested.calls.load(Ordering::SeqCst);
        assert!(
            acquire_fixture(&root, SOURCE_SHA, requested.clone())
                .await
                .unwrap()
                .cache_hit
        );
        assert_eq!(requested.calls.load(Ordering::SeqCst), calls);
    }

    async fn acquire_fixture(
        root: &Path,
        sha: &str,
        source: Arc<FixtureSource>,
    ) -> anyhow::Result<RuntimeArtifacts> {
        acquire_runtime_with_source(
            root.to_owned(),
            VERSION.to_owned(),
            sha.to_owned(),
            Instant::now() + Duration::from_secs(10),
            CancellationToken::new(),
            source,
        )
        .await
    }

    #[tokio::test]
    async fn failed_download_and_extraction_never_publish_or_leave_stages() {
        for failure in ["missing", "truncated", "manifest_source"] {
            let parent = private_tempdir();
            let root = parent.path().canonicalize().unwrap().join("runtime-cache");
            let mut source = fixture_source();
            if failure == "missing" {
                source.artifacts.remove("ashnazg-vm-v0.3.2-macos-aarch64");
            } else {
                let guest_name = "ashnazg-microvm-v0.3.2-linux-aarch64.tar.xz";
                let guest = if failure == "truncated" {
                    b"truncated XZ".to_vec()
                } else {
                    archive_fixture("1123456789abcdef0123456789abcdef01234567")
                };
                let mut catalog: serde_json::Value =
                    serde_json::from_slice(&source.catalog).unwrap();
                catalog["artifacts"]["guest"]["size"] = guest.len().into();
                catalog["artifacts"]["guest"]["sha256"] = digest(&guest).into();
                source.catalog = serde_json::to_vec(&catalog).unwrap();
                source.artifacts.insert(guest_name.to_owned(), guest);
            }
            let error = acquire_fixture(&root, SOURCE_SHA, Arc::new(source))
                .await
                .unwrap_err();
            assert!(
                !error.to_string().contains("worker failed"),
                "{failure}: {error:#}"
            );
            assert_eq!(
                fs::read_dir(root.join("staging")).unwrap().count(),
                0,
                "{failure}"
            );
            assert_eq!(
                fs::read_dir(root.join("entries")).unwrap().count(),
                0,
                "{failure}"
            );
        }
    }

    #[tokio::test]
    async fn malformed_cached_receipts_fail_without_network_or_mutation() {
        for failure in ["missing", "unknown", "size", "hash", "source"] {
            let parent = private_tempdir();
            let root = parent.path().canonicalize().unwrap().join("runtime-cache");
            let source = Arc::new(fixture_source());
            let first = acquire_fixture(&root, SOURCE_SHA, source.clone())
                .await
                .unwrap();
            let path = first.helper.parent().unwrap().join("integrity.json");
            let mut receipt: serde_json::Value =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            match failure {
                "missing" => {
                    receipt["members"].as_array_mut().unwrap().pop();
                }
                "unknown" => {
                    receipt["members"][0]["name"] = "unexpected".into();
                }
                "size" => {
                    receipt["members"][0]["size"] = 0.into();
                }
                "hash" => {
                    receipt["members"][0]["sha256"] = "bad".into();
                }
                _ => {
                    receipt["source_sha"] = "1123456789abcdef0123456789abcdef01234567".into();
                }
            }
            let bytes = serde_json::to_vec(&receipt).unwrap();
            fs::write(&path, &bytes).unwrap();
            let calls = source.calls.load(Ordering::SeqCst);
            assert!(
                acquire_fixture(&root, SOURCE_SHA, source.clone())
                    .await
                    .is_err(),
                "{failure}"
            );
            assert_eq!(source.calls.load(Ordering::SeqCst), calls);
            assert_eq!(fs::read(path).unwrap(), bytes);
        }
    }

    #[tokio::test]
    async fn two_catalogs_for_the_same_stamp_are_ambiguous() {
        let parent = private_tempdir();
        let root = parent.path().canonicalize().unwrap().join("runtime-cache");
        let source = Arc::new(fixture_source());
        let first = acquire_fixture(&root, SOURCE_SHA, source.clone())
            .await
            .unwrap();
        {
            let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
            let stage = guard.create_stage().unwrap();
            let entry = first.helper.parent().unwrap();
            for item in fs::read_dir(entry).unwrap() {
                let item = item.unwrap();
                let name = item.file_name();
                if name == "entry.json" || name == "stage.json" {
                    continue;
                }
                let target = stage.path().join(&name);
                if name == "assets" {
                    fs::create_dir(&target).unwrap();
                    fs::set_permissions(&target, fs::Permissions::from_mode(PRIVATE_DIR)).unwrap();
                    for asset in fs::read_dir(item.path()).unwrap() {
                        let asset = asset.unwrap();
                        fs::copy(asset.path(), target.join(asset.file_name())).unwrap();
                    }
                } else {
                    fs::copy(item.path(), target).unwrap();
                }
            }
            let mut catalog = source.catalog.clone();
            catalog.push(b'\n');
            let key = catalog_hash(&catalog);
            fs::write(stage.path().join("catalog.json"), catalog).unwrap();
            let path = stage.path().join("integrity.json");
            let mut receipt: IntegrityReceipt =
                serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            receipt.catalog_sha256 = key.clone();
            fs::write(path, serde_json::to_vec(&receipt).unwrap()).unwrap();
            stage.promote(&key).unwrap();
        }
        let calls = source.calls.load(Ordering::SeqCst);
        let error = acquire_fixture(&root, SOURCE_SHA, source.clone())
            .await
            .unwrap_err();
        assert!(
            error.to_string().contains("multiple cache entries"),
            "{error:#}"
        );
        assert_eq!(source.calls.load(Ordering::SeqCst), calls);
    }

    #[tokio::test]
    async fn rejects_unsupported_version_before_touching_cache() {
        let parent = private_tempdir();
        let root = parent.path().join("must-not-exist");
        let result = acquire_runtime(
            root.clone(),
            "0.4.0".to_owned(),
            "0123456789abcdef0123456789abcdef01234567".to_owned(),
            Instant::now() + Duration::from_secs(2),
            CancellationToken::new(),
        )
        .await;
        assert!(result.is_err());
        assert!(!root.exists());
    }
}
