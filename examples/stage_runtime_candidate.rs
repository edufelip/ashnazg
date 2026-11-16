//! Stage locally prepared release bytes for prepublication CLI/VM acceptance.
//! This is an explicit offline qualification utility, not network acquisition.
use anyhow::{Context, ensure};
use ashnazg::{
    runtime_cache::CacheGuard,
    runtime_catalog::{ArtifactRole, RuntimeCatalog},
    runtime_extract::extract_runtime,
};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::Path,
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn copy_verified(
    source: &Path,
    target: &Path,
    size: u64,
    hash: &str,
    mode: u32,
) -> anyhow::Result<()> {
    let mut input = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(source)?;
    let metadata = input.metadata()?;
    ensure!(
        metadata.is_file() && metadata.len() == size,
        "invalid candidate input"
    );
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(mode)
        .open(target)?;
    let mut digest = Sha256::new();
    let mut count = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let length = input.read(&mut buffer)?;
        if length == 0 {
            break;
        }
        count += length as u64;
        ensure!(count <= size, "candidate input grew");
        digest.update(&buffer[..length]);
        output.write_all(&buffer[..length])?;
    }
    ensure!(
        count == size && hex(&digest.finalize()) == hash,
        "candidate input hash mismatch"
    );
    output.sync_all()?;
    Ok(())
}

fn write_new(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    Ok(())
}

fn main() -> anyhow::Result<()> {
    let args: Vec<_> = std::env::args_os().collect();
    ensure!(
        args.len() == 4,
        "usage: stage_runtime_candidate CANDIDATE_DIRECTORY CACHE_ROOT SOURCE_SHA"
    );
    let directory = Path::new(&args[1]);
    let root = Path::new(&args[2]);
    let source = args[3].to_str().context("source must be UTF-8")?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(directory.join("ashnazg-runtime-v0.3.0.json"))?;
    ensure!(file.metadata()?.is_file(), "catalog must be regular");
    let mut bytes = Vec::new();
    file.take(64 * 1024 + 1).read_to_end(&mut bytes)?;
    let catalog = RuntimeCatalog::parse(&bytes, "0.3.0", source)?;
    let key = hex(&Sha256::digest(&bytes));
    let guard = CacheGuard::try_acquire(root)?.context("cache is busy")?;
    ensure!(
        guard.lookup_entry(&key)?.is_none(),
        "candidate already staged; refusing replacement"
    );
    let stage = guard.create_stage()?;
    write_new(&stage.path().join("catalog.json"), &bytes)?;
    for role in [
        ArtifactRole::Helper,
        ArtifactRole::Agent,
        ArtifactRole::Fixture,
    ] {
        let entry = catalog.artifact(role);
        copy_verified(
            &directory.join(entry.name()),
            &stage.path().join(entry.name()),
            entry.size(),
            entry.sha256(),
            if role == ArtifactRole::Helper {
                0o700
            } else {
                0o600
            },
        )?;
    }
    let assets = stage.path().join("assets");
    fs::create_dir(&assets)?;
    fs::set_permissions(&assets, fs::Permissions::from_mode(0o700))?;
    let extracted = extract_runtime(
        &directory.join(catalog.artifact(ArtifactRole::Guest).name()),
        &assets,
        &catalog,
        Instant::now() + Duration::from_secs(900),
        &CancellationToken::new(),
    )?;
    let members: Vec<_> = extracted
        .members
        .iter()
        .map(|(name, member)| {
            serde_json::json!({
                "name": name, "size": member.size, "sha256": member.sha256
            })
        })
        .collect();
    let receipt = serde_json::json!({
        "schema_version": 1, "version": "0.3.0", "source_sha": source,
        "catalog_sha256": key, "guest_size": extracted.compressed_size,
        "guest_sha256": extracted.compressed_sha256, "members": members
    });
    write_new(
        &stage.path().join("integrity.json"),
        &serde_json::to_vec(&receipt)?,
    )?;
    let promoted = stage.promote(&key)?;
    println!(
        "{}",
        serde_json::json!({"offline_staged": true, "source_sha": source,
        "catalog_sha256": key, "entry": promoted})
    );
    Ok(())
}
