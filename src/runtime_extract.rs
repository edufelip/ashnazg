//! Safe, bounded extraction for the pinned microVM guest runtime bundle.

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime_catalog::RuntimeCatalog;
    use sha2::{Digest, Sha256};
    use std::{fs, io::Write, os::unix::fs::PermissionsExt, time::Duration};
    use tar::{Builder, EntryType, Header};
    use tokio_util::sync::CancellationToken;
    use xz2::write::XzEncoder;

    const SOURCE: &str = "0123456789012345678901234567890123456789";
    const REDROID: &str = "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee";
    const NAMES: [&str; 11] = [
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

    fn contents(name: &str) -> Vec<u8> {
        match name {
            "kernel" => b"kernel bytes".to_vec(),
            "rootfs.ext4" => b"filesystem bytes".to_vec(),
            "manifest.json" => manifest(b"kernel bytes", b"filesystem bytes"),
            _ => format!("{name} contents").into_bytes(),
        }
    }

    fn manifest(kernel: &[u8], rootfs: &[u8]) -> Vec<u8> {
        let digest = |data: &[u8]| digest_hex(&Sha256::digest(data));
        format!(
            "{{\"schema_version\":1,\"version\":\"0.3.0\",\"source_sha\":\"{SOURCE}\",\"architecture\":\"aarch64\",\"files\":{{\"kernel\":{{\"sha256\":\"{}\",\"size\":{}}},\"rootfs.ext4\":{{\"sha256\":\"{}\",\"size\":{}}}}},\"guest\":{{\"kernel_version\":\"6.12.117\",\"base\":\"alpine-3.22.8-aarch64\",\"redroid_index\":\"{REDROID}\"}}}}",
            digest(kernel), kernel.len(), digest(rootfs), rootfs.len()
        ).into_bytes()
    }

    fn archive(items: &[(&str, Vec<u8>, EntryType)]) -> Vec<u8> {
        let mut tar = Builder::new(Vec::new());
        for (name, data, kind) in items {
            let mut header = Header::new_gnu();
            header.set_entry_type(*kind);
            header.set_size(data.len() as u64);
            header.set_mode(0o644);
            header.set_cksum();
            tar.append_data(&mut header, name, data.as_slice()).unwrap();
        }
        tar.finish().unwrap();
        let tar = tar.into_inner().unwrap();
        let mut encoder = XzEncoder::new(Vec::new(), 1);
        encoder.write_all(&tar).unwrap();
        encoder.finish().unwrap()
    }

    fn archive_with_raw_first_name(
        items: &[(&str, Vec<u8>, EntryType)],
        raw_name: &[u8],
    ) -> Vec<u8> {
        let mut tar = Builder::new(Vec::new());
        for (index, (name, data, kind)) in items.iter().enumerate() {
            let mut header = Header::new_gnu();
            header.set_entry_type(*kind);
            header.set_size(data.len() as u64);
            header.set_mode(0o644);
            if index == 0 {
                assert!(raw_name.len() < 100);
                let bytes = header.as_mut_bytes();
                bytes[..100].fill(0);
                bytes[..raw_name.len()].copy_from_slice(raw_name);
                header.set_cksum();
                tar.append(&header, data.as_slice()).unwrap();
            } else {
                header.set_cksum();
                tar.append_data(&mut header, name, data.as_slice()).unwrap();
            }
        }
        tar.finish().unwrap();
        let tar = tar.into_inner().unwrap();
        let mut encoder = XzEncoder::new(Vec::new(), 1);
        encoder.write_all(&tar).unwrap();
        encoder.finish().unwrap()
    }

    fn valid_archive() -> Vec<u8> {
        let items: Vec<_> = NAMES
            .iter()
            .map(|name| (*name, contents(name), EntryType::Regular))
            .collect();
        archive(&items)
    }

    fn catalog(bytes: &[u8]) -> RuntimeCatalog {
        let digest = digest_hex(&Sha256::digest(bytes));
        catalog_with(bytes, bytes.len() as u64, &digest)
    }

    fn catalog_with(_bytes: &[u8], declared_size: u64, digest: &str) -> RuntimeCatalog {
        catalog_with_version(declared_size, digest, "0.3.0")
    }

    fn catalog_with_version(declared_size: u64, digest: &str, version: &str) -> RuntimeCatalog {
        let json = format!(
            r#"{{"schema_version":1,"version":"0.3.0","release_tag":"v0.3.0","source_sha":"{SOURCE}","artifacts":{{"guest":{{"name":"ashnazg-microvm-v0.3.0-linux-aarch64.tar.xz","platform":"linux-aarch64","source_sha":"{SOURCE}","size":{},"sha256":"{digest}"}},"helper":{{"name":"ashnazg-vm-v0.3.0-macos-aarch64","platform":"macos-aarch64","source_sha":"{SOURCE}","size":1,"sha256":"{}"}},"agent":{{"name":"ashnazg-agent-v0.3.0.apk","platform":"android","source_sha":"{SOURCE}","size":1,"sha256":"{}"}},"fixture":{{"name":"ashnazg-fixture-v0.3.0.apk","platform":"android","source_sha":"{SOURCE}","size":1,"sha256":"{}"}}}},"source_archives":[]}}"#,
            declared_size,
            "0".repeat(64),
            "0".repeat(64),
            "0".repeat(64)
        );
        let json = json.replace("0.3.0", version);
        RuntimeCatalog::parse(json.as_bytes(), version, SOURCE).unwrap()
    }

    struct Fixture {
        temp: tempfile::TempDir,
        archive: std::path::PathBuf,
        output: std::path::PathBuf,
    }

    fn fixture(bytes: &[u8]) -> Fixture {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let archive = root.join("runtime.tar.xz");
        fs::write(&archive, bytes).unwrap();
        let output = root.join("out");
        fs::create_dir(&output).unwrap();
        fs::set_permissions(&output, fs::Permissions::from_mode(0o700)).unwrap();
        Fixture {
            temp,
            archive,
            output,
        }
    }

    fn run(bytes: &[u8]) -> anyhow::Result<ExtractionReceipt> {
        let f = fixture(bytes);
        extract_runtime(
            &f.archive,
            &f.output,
            &catalog(bytes),
            Instant::now() + Duration::from_secs(10),
            &CancellationToken::new(),
        )
    }

    #[test]
    fn extracts_all_pinned_regular_members_and_returns_hash_receipt() {
        let bytes = valid_archive();
        let f = fixture(&bytes);
        let receipt = extract_runtime(
            &f.archive,
            &f.output,
            &catalog(&bytes),
            Instant::now() + Duration::from_secs(10),
            &CancellationToken::new(),
        )
        .unwrap();
        assert_eq!(receipt.source_sha, SOURCE);
        assert_eq!(receipt.compressed_size, bytes.len() as u64);
        for name in NAMES {
            assert_eq!(fs::read(f.output.join(name)).unwrap(), contents(name));
            assert_eq!(
                fs::metadata(f.output.join(name))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
            assert!(receipt.members.contains_key(name));
        }
        assert_eq!(
            fs::metadata(&f.output).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn rejects_missing_duplicate_unexpected_and_nonregular_members() {
        let mut items: Vec<_> = NAMES
            .iter()
            .map(|name| (*name, contents(name), EntryType::Regular))
            .collect();
        items.pop();
        assert!(run(&archive(&items)).is_err());
        items.push(("kernel", b"dupe".to_vec(), EntryType::Regular));
        assert!(run(&archive(&items)).is_err());
        items.pop();
        items.push(("surprise", b"x".to_vec(), EntryType::Regular));
        assert!(run(&archive(&items)).is_err());
        items.pop();
        items.push(("kernel", b"x".to_vec(), EntryType::Symlink));
        assert!(run(&archive(&items)).is_err());
        for kind in [
            EntryType::Link,
            EntryType::Char,
            EntryType::Block,
            EntryType::Fifo,
            EntryType::Continuous,
            EntryType::GNUSparse,
            EntryType::GNULongName,
            EntryType::GNULongLink,
            EntryType::XHeader,
            EntryType::XGlobalHeader,
        ] {
            let mut unsafe_items: Vec<_> = NAMES
                .iter()
                .map(|name| (*name, contents(name), EntryType::Regular))
                .collect();
            unsafe_items[0].2 = kind;
            assert!(
                run(&archive(&unsafe_items)).is_err(),
                "accepted tar type {kind:?}"
            );
        }
    }

    #[test]
    fn rejects_unsafe_names_and_directories() {
        for bad in [
            b"../kernel".as_slice(),
            b"/kernel",
            b"dir/kernel",
            b"./kernel",
        ] {
            let items: Vec<_> = NAMES
                .iter()
                .map(|name| (*name, contents(name), EntryType::Regular))
                .collect();
            let bytes = archive_with_raw_first_name(&items, bad);
            assert!(
                run(&bytes).is_err(),
                "accepted {}",
                String::from_utf8_lossy(bad)
            );
        }
        let mut items: Vec<_> = NAMES
            .iter()
            .map(|name| (*name, contents(name), EntryType::Regular))
            .collect();
        items[0].2 = EntryType::Directory;
        assert!(run(&archive(&items)).is_err());
    }

    #[test]
    fn rejects_manifest_unknown_duplicate_and_pin_or_hash_mismatches() {
        let mut bad = String::from_utf8(manifest(b"kernel bytes", b"filesystem bytes")).unwrap();
        bad = bad.replace(
            "\"schema_version\":1",
            "\"schema_version\":1,\"unknown\":true",
        );
        assert!(run(&replace_manifest(&bad)).is_err());
        let dup = String::from_utf8(manifest(b"kernel bytes", b"filesystem bytes"))
            .unwrap()
            .replace(
                "\"schema_version\":1",
                "\"schema_version\":1,\"schema_version\":1",
            );
        assert!(run(&replace_manifest(&dup)).is_err());
        let base = String::from_utf8(manifest(b"kernel bytes", b"filesystem bytes")).unwrap();
        let kernel_sha = digest_hex(&Sha256::digest(b"kernel bytes"));
        assert!(
            run(&replace_manifest(
                &base.replace(&kernel_sha, &"0".repeat(64))
            ))
            .is_err()
        );
        assert!(
            run(&replace_manifest(&base.replacen(
                "\"size\":12",
                "\"size\":13",
                1
            )))
            .is_err()
        );
        for (from, to) in [
            ("0.3.0", "0.4.0"),
            (SOURCE, "9999999999999999999999999999999999999999"),
            ("aarch64", "x86_64"),
            ("6.12.117", "6.12.118"),
            ("alpine-3.22.8-aarch64", "alpine-other"),
            (REDROID, "sha256:bad"),
        ] {
            let altered = String::from_utf8(manifest(b"kernel bytes", b"filesystem bytes"))
                .unwrap()
                .replacen(from, to, 1);
            assert!(
                run(&replace_manifest(&altered)).is_err(),
                "accepted pin {from}"
            );
        }
        assert!(run(&replace_manifest("{}")).is_err());
    }

    fn replace_manifest(json: &str) -> Vec<u8> {
        let items: Vec<_> = NAMES
            .iter()
            .map(|name| {
                (
                    *name,
                    if *name == "manifest.json" {
                        json.as_bytes().to_vec()
                    } else {
                        contents(name)
                    },
                    EntryType::Regular,
                )
            })
            .collect();
        archive(&items)
    }

    #[test]
    fn rejects_a_matching_catalog_and_manifest_outside_the_helper_version_contract() {
        let json = String::from_utf8(manifest(b"kernel bytes", b"filesystem bytes"))
            .unwrap()
            .replace("0.3.0", "0.4.0");
        let bytes = replace_manifest(&json);
        let f = fixture(&bytes);
        let catalog = catalog_with_version(
            bytes.len() as u64,
            &digest_hex(&Sha256::digest(&bytes)),
            "0.4.0",
        );
        assert!(
            extract_runtime(
                &f.archive,
                &f.output,
                &catalog,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new(),
            )
            .is_err()
        );
    }

    #[test]
    fn rejects_compressed_truncation_corruption_and_trailing_bytes() {
        let valid = valid_archive();
        assert!(run(&valid[..valid.len() - 1]).is_err());
        let size_mismatch = fixture(&valid[..valid.len() - 1]);
        assert!(
            extract_runtime(
                &size_mismatch.archive,
                &size_mismatch.output,
                &catalog(&valid),
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
        let digest_mismatch = fixture(&valid);
        let wrong_catalog = catalog_with(&valid, valid.len() as u64, &"0".repeat(64));
        assert!(
            extract_runtime(
                &digest_mismatch.archive,
                &digest_mismatch.output,
                &wrong_catalog,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
        let mut corrupt = valid.clone();
        let middle = corrupt.len() / 2;
        corrupt[middle] ^= 0x80;
        assert!(run(&corrupt).is_err());
        let mut extra = valid;
        extra.extend_from_slice(b"trailing");
        assert!(run(&extra).is_err());
    }

    #[test]
    fn rejects_concatenated_hidden_tar_payload_and_nonzero_padding() {
        let first = archive(
            &NAMES
                .iter()
                .map(|name| (*name, contents(name), EntryType::Regular))
                .collect::<Vec<_>>(),
        );
        let hidden = archive(&[("hidden", b"payload".to_vec(), EntryType::Regular)]);
        let mut concat = first;
        concat.extend_from_slice(&hidden);
        assert!(run(&concat).is_err());
        let mut tail = valid_archive();
        tail.extend_from_slice(&[1, 0, 0, 0, 0, 0, 0, 0]);
        assert!(run(&tail).is_err());
    }

    #[test]
    fn limits_are_injectable_and_destination_is_exclusive_private_and_symlink_free() {
        let bytes = valid_archive();
        let f = fixture(&bytes);
        let cat = catalog(&bytes);
        assert!(
            extract_with_limits(
                &f.archive,
                &f.output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new(),
                ExtractLimits {
                    expanded: 1,
                    members: DEFAULT_MEMBER_LIMITS
                }
            )
            .is_err()
        );
        assert!(
            extract_with_limits(
                &f.archive,
                &f.output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new(),
                ExtractLimits {
                    expanded: MAX_EXPANDED_BYTES,
                    members: [1; 11],
                }
            )
            .is_err()
        );
        fs::write(f.output.join("keep"), b"caller file").unwrap();
        assert!(
            extract_runtime(
                &f.archive,
                &f.output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
        let archive_link = f.temp.path().join("archive-link");
        std::os::unix::fs::symlink(&f.archive, &archive_link).unwrap();
        let fresh_output = f.temp.path().join("out2");
        fs::create_dir(&fresh_output).unwrap();
        fs::set_permissions(&fresh_output, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            extract_runtime(
                &archive_link,
                &fresh_output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
        let public_output = f.temp.path().join("out3");
        fs::create_dir(&public_output).unwrap();
        fs::set_permissions(&public_output, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(
            extract_runtime(
                &f.archive,
                &public_output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
        let link = f.temp.path().join("link");
        std::os::unix::fs::symlink(&f.output, &link).unwrap();
        assert!(
            extract_runtime(
                &f.archive,
                &link,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &CancellationToken::new()
            )
            .is_err()
        );
    }

    #[test]
    fn honors_cancellation_and_expired_deadline_without_cleanup() {
        let bytes = valid_archive();
        let f = fixture(&bytes);
        let cat = catalog(&bytes);
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert!(
            extract_runtime(
                &f.archive,
                &f.output,
                &cat,
                Instant::now() + Duration::from_secs(10),
                &cancel
            )
            .is_err()
        );
        assert!(fs::read_dir(&f.output).unwrap().next().is_none());
        assert!(
            extract_runtime(
                &f.archive,
                &f.output,
                &cat,
                Instant::now() - Duration::from_secs(1),
                &CancellationToken::new()
            )
            .is_err()
        );
    }

    #[test]
    fn directory_iteration_distinguishes_io_error_from_end_of_directory() {
        let error = checked_directory_entry(|| {
            unsafe { *errno_pointer() = libc::EIO };
            std::ptr::null_mut()
        });
        assert_eq!(error.unwrap_err().raw_os_error(), Some(libc::EIO));
        unsafe { *errno_pointer() = libc::EIO };
        assert!(
            checked_directory_entry(std::ptr::null_mut)
                .unwrap()
                .is_none()
        );
    }
}
use crate::runtime_catalog::{ArtifactRole, RuntimeCatalog};
use anyhow::{Context, bail, ensure};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashSet},
    ffi::CString,
    fs::File,
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Component, Path},
    time::Instant,
};
use tokio_util::sync::CancellationToken;
use xz2::{
    read::XzDecoder,
    stream::{CONCATENATED, Stream},
};

const XZ_MEMORY_LIMIT: u64 = 128 * 1024 * 1024;
const MAX_EXPANDED_BYTES: u64 = 6 * 1024 * 1024 * 1024;
const IO_CHUNK: usize = 64 * 1024;
const MEMBER_NAMES: [&str; 11] = [
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
const DEFAULT_MEMBER_LIMITS: [u64; 11] = [
    128 * 1024 * 1024,
    4 * 1024 * 1024 * 1024,
    64 * 1024,
    2 * 1024 * 1024,
    64 * 1024,
    2 * 1024 * 1024,
    1024 * 1024,
    2 * 1024 * 1024,
    2 * 1024 * 1024,
    2 * 1024 * 1024,
    256 * 1024 * 1024,
];
const EXPECTED_KERNEL_VERSION: &str = "6.12.117";
const EXPECTED_BASE: &str = "alpine-3.22.8-aarch64";
const EXPECTED_REDROID_INDEX: &str =
    "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExtractedMember {
    pub size: u64,
    pub sha256: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExtractionReceipt {
    pub source_sha: String,
    pub compressed_size: u64,
    pub compressed_sha256: String,
    pub members: BTreeMap<String, ExtractedMember>,
}

#[derive(Clone, Copy)]
struct ExtractLimits {
    expanded: u64,
    members: [u64; 11],
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema_version: u64,
    version: String,
    source_sha: String,
    architecture: String,
    files: ManifestFiles,
    guest: ManifestGuest,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestFiles {
    kernel: ManifestFile,
    #[serde(rename = "rootfs.ext4")]
    rootfs_ext4: ManifestFile,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestFile {
    sha256: String,
    size: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestGuest {
    kernel_version: String,
    base: String,
    redroid_index: String,
}

/// Extracts a validated catalog's guest artifact into an empty private directory.
/// Partial files remain for the caller to discard after any error.
/// When called from async code via a blocking task, join that task before cleanup.
pub fn extract_runtime(
    archive_path: &Path,
    destination: &Path,
    catalog: &RuntimeCatalog,
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<ExtractionReceipt> {
    extract_with_limits(
        archive_path,
        destination,
        catalog,
        deadline,
        cancel,
        ExtractLimits {
            expanded: MAX_EXPANDED_BYTES,
            members: DEFAULT_MEMBER_LIMITS,
        },
    )
}

fn extract_with_limits(
    archive_path: &Path,
    destination: &Path,
    catalog: &RuntimeCatalog,
    deadline: Instant,
    cancel: &CancellationToken,
    limits: ExtractLimits,
) -> anyhow::Result<ExtractionReceipt> {
    checkpoint(deadline, cancel)?;
    let dest_fd = open_directory_path(destination)?;
    validate_destination(&dest_fd, destination)?;
    ensure!(
        directory_is_empty(&dest_fd)?,
        "runtime destination must be empty"
    );

    let archive = open_regular_path(archive_path)?;
    let expected = catalog.artifact(ArtifactRole::Guest);
    let metadata = archive.metadata()?;
    ensure!(
        metadata.len() == expected.size(),
        "guest archive size does not match catalog"
    );
    let mut source = CompressedInput {
        file: archive,
        count: 0,
        expected_size: expected.size(),
        hasher: Sha256::new(),
        deadline,
        cancel,
    };
    let stream = Stream::new_stream_decoder(XZ_MEMORY_LIMIT, CONCATENATED)
        .context("could not initialize XZ decoder")?;
    let decoder = XzDecoder::new_stream(&mut source, stream);
    let mut expanded = BoundedExpanded {
        inner: decoder,
        count: 0,
        limit: limits.expanded,
        deadline,
        cancel,
    };
    let mut seen = HashSet::new();
    let mut members = BTreeMap::new();
    let mut kernel = None;
    let mut rootfs = None;
    let mut manifest_bytes = None;

    {
        let mut archive = tar::Archive::new(&mut expanded);
        let entries = archive.entries()?.raw(true);
        for item in entries {
            checkpoint(deadline, cancel)?;
            let mut entry = item?;
            let header = entry.header();
            ensure!(
                matches!(header.entry_type().as_byte(), 0 | b'0'),
                "runtime archive contains a non-regular member"
            );
            let raw_name = header.path_bytes();
            let name = std::str::from_utf8(&raw_name)
                .context("runtime member name is not UTF-8")?
                .to_owned();
            ensure!(
                MEMBER_NAMES.contains(&name.as_str()),
                "runtime archive contains an unexpected member"
            );
            let index = MEMBER_NAMES
                .iter()
                .position(|candidate| *candidate == name)
                .unwrap();
            ensure!(
                seen.insert(name.clone()),
                "runtime archive contains a duplicate member"
            );
            let size = header.size()?;
            ensure!(size > 0, "runtime archive contains an empty member");
            ensure!(
                size <= limits.members[index],
                "runtime member exceeds its size limit"
            );

            let output = create_member(&dest_fd, &name)?;
            let mut hasher = Sha256::new();
            let mut member_size = 0u64;
            let mut this_manifest = if name == "manifest.json" {
                Some(Vec::with_capacity(size as usize))
            } else {
                None
            };
            let mut buffer = [0u8; IO_CHUNK];
            loop {
                checkpoint(deadline, cancel)?;
                let read = entry.read(&mut buffer)?;
                if read == 0 {
                    break;
                }
                ensure!(
                    member_size
                        .checked_add(read as u64)
                        .is_some_and(|next| next <= size && next <= limits.members[index]),
                    "runtime member exceeds its declared size or limit"
                );
                write_all_checked(&output, &buffer[..read], deadline, cancel)?;
                hasher.update(&buffer[..read]);
                if let Some(bytes) = &mut this_manifest {
                    bytes.extend_from_slice(&buffer[..read]);
                }
                member_size += read as u64;
            }
            ensure!(
                member_size == size,
                "runtime member length differs from its header"
            );
            let digest = digest_hex(&hasher.finalize());
            let record = ExtractedMember {
                size,
                sha256: digest,
            };
            if name == "kernel" {
                kernel = Some(record.clone());
            }
            if name == "rootfs.ext4" {
                rootfs = Some(record.clone());
            }
            if let Some(bytes) = this_manifest {
                manifest_bytes = Some(bytes);
            }
            members.insert(name.to_owned(), record);
        }
    }
    ensure!(
        seen.len() == MEMBER_NAMES.len(),
        "runtime archive is missing required members"
    );
    ensure!(
        kernel.is_some() && rootfs.is_some(),
        "runtime archive is missing kernel or rootfs"
    );
    validate_manifest(
        manifest_bytes
            .as_deref()
            .context("runtime archive is missing manifest.json")?,
        catalog,
        kernel.as_ref(),
        rootfs.as_ref(),
    )?;

    // The tar reader stops at its end markers. Drain the complete XZ stream and permit
    // only zero-valued decompressed padding after those markers.
    let mut tail = [0u8; IO_CHUNK];
    loop {
        checkpoint(deadline, cancel)?;
        let count = expanded.read(&mut tail)?;
        if count == 0 {
            break;
        }
        ensure!(
            tail[..count].iter().all(|byte| *byte == 0),
            "runtime archive has data after tar end markers"
        );
    }
    let mut decoder = expanded.inner;
    // XzDecoder's internal BufReader can hold source bytes beyond the decoder's final
    // output. Consume them too so compressed size and SHA cover the full artifact.
    let mut compressed_tail = [0u8; IO_CHUNK];
    loop {
        checkpoint(deadline, cancel)?;
        let count = decoder.get_mut().read(&mut compressed_tail)?;
        if count == 0 {
            break;
        }
        ensure!(
            compressed_tail[..count].iter().all(|byte| *byte == 0),
            "runtime archive has non-XZ trailing bytes"
        );
    }
    drop(decoder);
    checkpoint(deadline, cancel)?;
    let compressed_sha256 = digest_hex(&source.hasher.clone().finalize());
    ensure!(
        source.count == expected.size(),
        "guest archive was not read to its exact catalog size"
    );
    ensure!(
        compressed_sha256 == expected.sha256(),
        "guest archive SHA-256 does not match catalog"
    );
    Ok(ExtractionReceipt {
        source_sha: catalog.source_sha().to_owned(),
        compressed_size: source.count,
        compressed_sha256,
        members,
    })
}

pub(crate) fn validate_manifest(
    bytes: &[u8],
    catalog: &RuntimeCatalog,
    kernel: Option<&ExtractedMember>,
    rootfs: Option<&ExtractedMember>,
) -> anyhow::Result<()> {
    let manifest: Manifest = serde_json::from_slice(bytes).context("invalid runtime manifest")?;
    ensure!(
        manifest.schema_version == 1,
        "runtime manifest schema_version must be 1"
    );
    ensure!(
        manifest.version == "0.3.0" && manifest.version == catalog.version(),
        "runtime manifest version does not match the catalog and pinned helper contract"
    );
    ensure!(
        manifest.source_sha == catalog.source_sha(),
        "runtime manifest source does not match catalog"
    );
    ensure!(
        manifest.architecture == "aarch64",
        "runtime manifest architecture must be aarch64"
    );
    ensure!(
        manifest.guest.kernel_version == EXPECTED_KERNEL_VERSION,
        "runtime manifest kernel pin mismatch"
    );
    ensure!(
        manifest.guest.base == EXPECTED_BASE,
        "runtime manifest base pin mismatch"
    );
    ensure!(
        manifest.guest.redroid_index == EXPECTED_REDROID_INDEX,
        "runtime manifest redroid index pin mismatch"
    );
    if let Some(kernel) = kernel {
        ensure!(
            manifest.files.kernel.size == kernel.size
                && manifest.files.kernel.sha256 == kernel.sha256,
            "runtime manifest kernel digest or size mismatch"
        );
    }
    if let Some(rootfs) = rootfs {
        ensure!(
            manifest.files.rootfs_ext4.size == rootfs.size
                && manifest.files.rootfs_ext4.sha256 == rootfs.sha256,
            "runtime manifest rootfs digest or size mismatch"
        );
    }
    Ok(())
}

struct CompressedInput<'a> {
    file: File,
    count: u64,
    expected_size: u64,
    hasher: Sha256,
    deadline: Instant,
    cancel: &'a CancellationToken,
}

impl Read for CompressedInput<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        checkpoint_io(self.deadline, self.cancel)?;
        let len = buffer.len().min(IO_CHUNK);
        let read = self.file.read(&mut buffer[..len])?;
        let Some(next) = self.count.checked_add(read as u64) else {
            return Err(io::Error::other("compressed runtime size overflow"));
        };
        if next > self.expected_size {
            return Err(io::Error::other("compressed runtime exceeds catalog size"));
        }
        self.count = next;
        self.hasher.update(&buffer[..read]);
        Ok(read)
    }
}

struct BoundedExpanded<'a, R> {
    inner: R,
    count: u64,
    limit: u64,
    deadline: Instant,
    cancel: &'a CancellationToken,
}

impl<R: Read> Read for BoundedExpanded<'_, R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        checkpoint_io(self.deadline, self.cancel)?;
        if buffer.is_empty() {
            return Ok(0);
        }
        let remaining = self.limit.saturating_sub(self.count);
        let len = buffer
            .len()
            .min(IO_CHUNK)
            .min(remaining.saturating_add(1) as usize);
        let read = self.inner.read(&mut buffer[..len])?;
        self.count = self.count.saturating_add(read as u64);
        if self.count > self.limit {
            return Err(io::Error::other("expanded runtime exceeds limit"));
        }
        Ok(read)
    }
}

fn checkpoint(deadline: Instant, cancel: &CancellationToken) -> anyhow::Result<()> {
    if cancel.is_cancelled() {
        bail!("runtime extraction cancelled");
    }
    if Instant::now() >= deadline {
        bail!("runtime extraction deadline exceeded");
    }
    Ok(())
}

fn checkpoint_io(deadline: Instant, cancel: &CancellationToken) -> io::Result<()> {
    checkpoint(deadline, cancel).map_err(|error| io::Error::other(error.to_string()))
}

fn write_all_checked(
    file: &File,
    mut bytes: &[u8],
    deadline: Instant,
    cancel: &CancellationToken,
) -> anyhow::Result<()> {
    while !bytes.is_empty() {
        checkpoint(deadline, cancel)?;
        let count = (&*file).write(&bytes[..bytes.len().min(IO_CHUNK)])?;
        ensure!(count > 0, "short write while extracting runtime member");
        bytes = &bytes[count..];
    }
    Ok(())
}

fn digest_hex(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

fn open_directory_path(path: &Path) -> anyhow::Result<File> {
    ensure!(
        path.is_absolute()
            && path
                .components()
                .all(|component| matches!(component, Component::RootDir | Component::Normal(_))),
        "directory path must be canonical and absolute"
    );
    let mut current = open_root()?;
    for component in path.components().skip(1) {
        let Component::Normal(part) = component else {
            bail!("invalid directory path component");
        };
        current = openat_file(
            current.as_raw_fd(),
            part.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )?;
    }
    ensure!(
        path.canonicalize()? == path,
        "directory path is not canonical"
    );
    Ok(current)
}

fn open_regular_path(path: &Path) -> anyhow::Result<File> {
    ensure!(
        path.is_absolute()
            && path
                .components()
                .all(|component| matches!(component, Component::RootDir | Component::Normal(_))),
        "archive path must be absolute and contain no traversal components"
    );
    let mut parent = open_root()?;
    let parts: Vec<_> = path.components().skip(1).collect();
    ensure!(!parts.is_empty(), "archive path must name a regular file");
    for component in &parts[..parts.len() - 1] {
        let Component::Normal(part) = component else {
            bail!("invalid archive path component");
        };
        parent = openat_file(
            parent.as_raw_fd(),
            part.as_bytes(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )?;
    }
    let Component::Normal(name) = parts[parts.len() - 1] else {
        bail!("invalid archive filename");
    };
    let file = openat_file(
        parent.as_raw_fd(),
        name.as_bytes(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
    )?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.file_type().is_file(),
        "runtime archive is not a regular file"
    );
    Ok(file)
}

fn open_root() -> anyhow::Result<File> {
    let fd = unsafe {
        libc::open(
            c"/".as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn openat_file(parent: i32, name: &[u8], flags: i32) -> anyhow::Result<File> {
    let name = CString::new(name).context("path component contains NUL")?;
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn validate_destination(directory: &File, path: &Path) -> anyhow::Result<()> {
    let metadata = directory.metadata()?;
    ensure!(metadata.is_dir(), "runtime destination is not a directory");
    ensure!(
        metadata.uid() == unsafe { libc::getuid() },
        "runtime destination must belong to the current user"
    );
    ensure!(
        metadata.mode() & 0o7777 == 0o700,
        "runtime destination must have mode 0700"
    );
    ensure!(
        path.canonicalize()? == path,
        "runtime destination path is not canonical"
    );
    Ok(())
}

fn directory_is_empty(directory: &File) -> anyhow::Result<bool> {
    let duplicate = unsafe { libc::dup(directory.as_raw_fd()) };
    if duplicate < 0 {
        return Err(io::Error::last_os_error().into());
    }
    let stream = unsafe { libc::fdopendir(duplicate) };
    if stream.is_null() {
        unsafe {
            libc::close(duplicate);
        }
        return Err(io::Error::last_os_error().into());
    }
    let result = (|| -> io::Result<bool> {
        while let Some(name) = checked_directory_entry(|| unsafe { libc::readdir(stream) })? {
            if name != b"." && name != b".." {
                return Ok(false);
            }
        }
        Ok(true)
    })();
    let close_result = unsafe { libc::closedir(stream) };
    let empty = result.context("could not verify runtime destination emptiness")?;
    ensure!(
        close_result == 0,
        "could not close runtime destination directory stream"
    );
    Ok(empty)
}

fn checked_directory_entry(
    read: impl FnOnce() -> *mut libc::dirent,
) -> io::Result<Option<Vec<u8>>> {
    unsafe { *errno_pointer() = 0 };
    let entry = read();
    if entry.is_null() {
        let error = io::Error::last_os_error();
        return if error.raw_os_error() == Some(0) {
            Ok(None)
        } else {
            Err(error)
        };
    }
    Ok(Some(
        unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }
            .to_bytes()
            .to_vec(),
    ))
}

#[cfg(target_os = "macos")]
fn errno_pointer() -> *mut libc::c_int {
    unsafe { libc::__error() }
}

#[cfg(target_os = "linux")]
fn errno_pointer() -> *mut libc::c_int {
    unsafe { libc::__errno_location() }
}

fn create_member(directory: &File, name: &str) -> anyhow::Result<File> {
    let file = openat_file(
        directory.as_raw_fd(),
        name.as_bytes(),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
    )?;
    ensure!(
        unsafe { libc::fchmod(file.as_raw_fd(), 0o600) } == 0,
        "could not set extracted member mode"
    );
    Ok(file)
}
