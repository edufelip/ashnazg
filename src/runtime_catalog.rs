use anyhow::{Context, ensure};
use serde::Deserialize;
use std::collections::HashSet;

const MAX_CATALOG_BYTES: usize = 64 * 1024;
const MAX_GUEST_SIZE: u64 = 1024 * 1024 * 1024;
const MAX_SMALL_ARTIFACT_SIZE: u64 = 64 * 1024 * 1024;
const MAX_SOURCE_ARCHIVES: usize = 64;
const MAX_SOURCE_ARCHIVE_SIZE: u64 = 1024 * 1024 * 1024;
const RELEASE_BASE: &str = "https://github.com/edufelip/ashnazg/releases/download/";

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum ArtifactRole {
    Guest,
    Helper,
    Agent,
    Fixture,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawCatalog {
    schema_version: u64,
    version: String,
    release_tag: String,
    source_sha: String,
    artifacts: RawArtifacts,
    source_archives: Vec<CatalogEntry>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawArtifacts {
    guest: CatalogEntry,
    helper: CatalogEntry,
    agent: CatalogEntry,
    fixture: CatalogEntry,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogEntry {
    name: String,
    platform: String,
    source_sha: String,
    size: u64,
    sha256: String,
}

impl CatalogEntry {
    pub fn name(&self) -> &str {
        &self.name
    }
    pub fn platform(&self) -> &str {
        &self.platform
    }
    pub fn source_sha(&self) -> &str {
        &self.source_sha
    }
    pub fn size(&self) -> u64 {
        self.size
    }
    pub fn sha256(&self) -> &str {
        &self.sha256
    }
}

#[derive(Debug)]
pub struct RuntimeCatalog {
    version: String,
    release_tag: String,
    source_sha: String,
    artifacts: RawArtifacts,
    source_archives: Vec<CatalogEntry>,
}

impl RuntimeCatalog {
    pub fn parse(
        bytes: &[u8],
        expected_version: &str,
        expected_source_sha: &str,
    ) -> anyhow::Result<Self> {
        ensure!(
            bytes.len() <= MAX_CATALOG_BYTES,
            "runtime catalog exceeds the 64 KiB limit"
        );
        ensure!(
            valid_version(expected_version),
            "expected version must be stable semantic version X.Y.Z with optional build metadata"
        );
        ensure!(
            valid_hex(expected_source_sha, 40),
            "expected source SHA must be 40 lowercase hexadecimal characters"
        );
        std::str::from_utf8(bytes).context("runtime catalog is not valid UTF-8")?;

        let raw: RawCatalog = serde_json::from_slice(bytes).map_err(|error| {
            let message = error.to_string();
            if message.contains("release_tag") {
                anyhow::anyhow!("runtime catalog has no release_tag stamp; publish or provide a stamped release catalog")
            } else {
                anyhow::anyhow!("invalid runtime catalog JSON: {message}")
            }
        })?;
        ensure!(raw.schema_version == 1, "schema_version must be integer 1");
        ensure!(
            valid_version(&raw.version),
            "catalog version must be stable semantic version X.Y.Z with optional build metadata"
        );
        ensure!(
            raw.version == expected_version,
            "catalog version does not match this build; use the matching runtime release"
        );
        ensure!(
            raw.release_tag == format!("v{}", raw.version),
            "invalid release_tag stamp; it must equal v plus the catalog version"
        );
        ensure!(
            valid_hex(&raw.source_sha, 40),
            "catalog source_sha must be 40 lowercase hexadecimal characters"
        );
        ensure!(
            raw.source_sha == expected_source_sha,
            "catalog source_sha does not match this build; use the matching runtime release"
        );

        let mut names = HashSet::new();
        validate_entry(
            &raw.artifacts.guest,
            &raw.source_sha,
            &format!("ashnazg-microvm-v{}-linux-aarch64.tar.xz", raw.version),
            "linux-aarch64",
            MAX_GUEST_SIZE,
            "guest",
        )?;
        validate_entry(
            &raw.artifacts.helper,
            &raw.source_sha,
            &format!("ashnazg-vm-v{}-macos-aarch64", raw.version),
            "macos-aarch64",
            MAX_SMALL_ARTIFACT_SIZE,
            "helper",
        )?;
        validate_entry(
            &raw.artifacts.agent,
            &raw.source_sha,
            &format!("ashnazg-agent-v{}.apk", raw.version),
            "android",
            MAX_SMALL_ARTIFACT_SIZE,
            "agent",
        )?;
        validate_entry(
            &raw.artifacts.fixture,
            &raw.source_sha,
            &format!("ashnazg-fixture-v{}.apk", raw.version),
            "android",
            MAX_SMALL_ARTIFACT_SIZE,
            "fixture",
        )?;
        for entry in [
            &raw.artifacts.guest,
            &raw.artifacts.helper,
            &raw.artifacts.agent,
            &raw.artifacts.fixture,
        ] {
            ensure!(
                names.insert(entry.name.as_str()),
                "artifact names must be unique"
            );
        }

        ensure!(
            raw.source_archives.len() <= MAX_SOURCE_ARCHIVES,
            "source_archives may contain at most 64 parts"
        );
        let mut source_indices = HashSet::new();
        for entry in &raw.source_archives {
            let prefix = format!("ashnazg-runtime-sources-v{}-", raw.version);
            let suffix = ".tar.xz";
            ensure!(
                entry.name.starts_with(&prefix) && entry.name.ends_with(suffix),
                "invalid source archive name or index"
            );
            let index = entry
                .name
                .strip_prefix(&prefix)
                .and_then(|name| name.strip_suffix(suffix))
                .context("invalid source archive name or index")?;
            ensure!(
                index.len() == 3
                    && index.bytes().all(|byte| byte.is_ascii_digit())
                    && index != "000",
                "source archive index must be a three-digit nonzero part number"
            );
            ensure!(
                source_indices.insert(index),
                "source archive indices must be unique"
            );
            validate_entry(
                entry,
                &raw.source_sha,
                &entry.name,
                "source",
                MAX_SOURCE_ARCHIVE_SIZE,
                "source archive",
            )?;
            ensure!(
                names.insert(entry.name.as_str()),
                "artifact and source archive names must be unique"
            );
        }

        Ok(Self {
            version: raw.version,
            release_tag: raw.release_tag,
            source_sha: raw.source_sha,
            artifacts: raw.artifacts,
            source_archives: raw.source_archives,
        })
    }

    pub fn version(&self) -> &str {
        &self.version
    }
    pub fn release_tag(&self) -> &str {
        &self.release_tag
    }
    pub fn source_sha(&self) -> &str {
        &self.source_sha
    }

    pub fn artifact(&self, role: ArtifactRole) -> &CatalogEntry {
        match role {
            ArtifactRole::Guest => &self.artifacts.guest,
            ArtifactRole::Helper => &self.artifacts.helper,
            ArtifactRole::Agent => &self.artifacts.agent,
            ArtifactRole::Fixture => &self.artifacts.fixture,
        }
    }

    /// Returns listed source parts; an empty list is metadata, not source-closure proof.
    pub fn source_archives(&self) -> &[CatalogEntry] {
        &self.source_archives
    }

    pub fn download_url(&self, entry: &CatalogEntry) -> anyhow::Result<String> {
        let owned = [
            &self.artifacts.guest,
            &self.artifacts.helper,
            &self.artifacts.agent,
            &self.artifacts.fixture,
        ]
        .into_iter()
        .chain(self.source_archives.iter())
        .any(|candidate| std::ptr::eq(candidate, entry));
        ensure!(
            owned,
            "catalog entry does not belong to this validated catalog"
        );
        Ok(format!(
            "{}{}/{}",
            RELEASE_BASE,
            encode_component(&self.release_tag),
            encode_component(&entry.name)
        ))
    }
}

fn validate_entry(
    entry: &CatalogEntry,
    source_sha: &str,
    expected_name: &str,
    expected_platform: &str,
    max_size: u64,
    description: &str,
) -> anyhow::Result<()> {
    ensure!(entry.name == expected_name, "invalid {description} name");
    ensure!(
        !entry.name.contains('/')
            && !entry.name.contains('\\')
            && entry.name != "."
            && entry.name != "..",
        "unsafe {description} name"
    );
    ensure!(
        !entry.name.ends_with(".json"),
        "{description} cannot reference a catalog file"
    );
    ensure!(
        entry.platform == expected_platform,
        "invalid {description} platform"
    );
    ensure!(
        entry.source_sha == source_sha,
        "{description} source_sha does not match catalog"
    );
    ensure!(
        entry.size > 0 && entry.size <= max_size,
        "{description} size must be a positive integer no greater than {max_size}"
    );
    ensure!(valid_hex(&entry.sha256, 64), "invalid {description} sha256");
    Ok(())
}

fn valid_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_version(version: &str) -> bool {
    let (core, build) = match version.split_once('+') {
        Some((core, build)) if !build.is_empty() && !build.contains('+') => (core, Some(build)),
        Some(_) => return false,
        None => (version, None),
    };
    let mut numbers = core.split('.');
    if ![numbers.next(), numbers.next(), numbers.next()]
        .iter()
        .all(|part| {
            part.is_some_and(|part| {
                !part.is_empty()
                    && part.bytes().all(|byte| byte.is_ascii_digit())
                    && (part == "0" || !part.starts_with('0'))
            })
        })
        || numbers.next().is_some()
    {
        return false;
    }
    build.is_none_or(|build| {
        build.split('.').all(|part| {
            !part.is_empty()
                && part
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        })
    })
}

fn encode_component(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            use std::fmt::Write;
            write!(&mut encoded, "%{byte:02X}").expect("writing to String cannot fail");
        }
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    const VERSION: &str = "1.2.3+build.7";
    const SOURCE: &str = "0123456789abcdef0123456789abcdef01234567";

    fn fixture() -> Value {
        let entry = |name: String, platform: &str, size: u64| {
            json!({
                "name": name,
                "platform": platform,
                "source_sha": SOURCE,
                "size": size,
                "sha256": "a".repeat(64),
            })
        };
        json!({
            "schema_version": 1,
            "version": VERSION,
            "release_tag": format!("v{VERSION}"),
            "source_sha": SOURCE,
            "artifacts": {
                "guest": entry(format!("ashnazg-microvm-v{VERSION}-linux-aarch64.tar.xz"), "linux-aarch64", 1),
                "helper": entry(format!("ashnazg-vm-v{VERSION}-macos-aarch64"), "macos-aarch64", 1),
                "agent": entry(format!("ashnazg-agent-v{VERSION}.apk"), "android", 1),
                "fixture": entry(format!("ashnazg-fixture-v{VERSION}.apk"), "android", 1),
            },
            "source_archives": [],
        })
    }

    fn parse_value(value: &Value) -> anyhow::Result<RuntimeCatalog> {
        RuntimeCatalog::parse(&serde_json::to_vec(value)?, VERSION, SOURCE)
    }

    #[test]
    fn accepts_canonical_catalog_and_builds_fixed_urls_for_every_role() {
        let mut value = fixture();
        value["source_archives"] = json!([{
            "name": format!("ashnazg-runtime-sources-v{VERSION}-001.tar.xz"),
            "platform": "source",
            "source_sha": SOURCE,
            "size": 2,
            "sha256": "b".repeat(64),
        }]);
        let catalog = parse_value(&value).unwrap();
        assert_eq!(catalog.version(), VERSION);
        assert_eq!(catalog.source_sha(), SOURCE);
        for role in [
            ArtifactRole::Guest,
            ArtifactRole::Helper,
            ArtifactRole::Agent,
            ArtifactRole::Fixture,
        ] {
            let artifact = catalog.artifact(role);
            assert_eq!(artifact.source_sha(), SOURCE);
            assert!(catalog.download_url(artifact).unwrap().starts_with(
                "https://github.com/edufelip/ashnazg/releases/download/v1.2.3%2Bbuild.7/"
            ));
        }
        assert_eq!(
            catalog.download_url(&catalog.source_archives()[0]).unwrap(),
            "https://github.com/edufelip/ashnazg/releases/download/v1.2.3%2Bbuild.7/ashnazg-runtime-sources-v1.2.3%2Bbuild.7-001.tar.xz"
        );
    }

    #[test]
    fn binds_exact_expected_version_and_source() {
        let bytes = serde_json::to_vec(&fixture()).unwrap();
        assert!(RuntimeCatalog::parse(&bytes, "1.2.4", SOURCE).is_err());
        assert!(
            RuntimeCatalog::parse(&bytes, VERSION, "1123456789abcdef0123456789abcdef01234567")
                .is_err()
        );
        assert!(RuntimeCatalog::parse(&bytes, VERSION, "BAD").is_err());
    }

    #[test]
    fn rejects_missing_or_invalid_release_stamp_actionably() {
        let mut missing = fixture();
        missing.as_object_mut().unwrap().remove("release_tag");
        let error = parse_value(&missing).unwrap_err().to_string();
        assert!(error.contains("release_tag") || error.contains("stamp"));
        let mut invalid = fixture();
        invalid["release_tag"] = json!("v9.9.9");
        let error = parse_value(&invalid).unwrap_err().to_string();
        assert!(error.contains("release_tag") || error.contains("stamp"));
    }

    #[test]
    fn rejects_bad_json_size_and_strict_field_violations() {
        assert!(RuntimeCatalog::parse(b"{", VERSION, SOURCE).is_err());
        assert!(RuntimeCatalog::parse(&vec![b' '; 65_537], VERSION, SOURCE).is_err());
        assert!(RuntimeCatalog::parse(&[0xff], VERSION, SOURCE).is_err());
        let canonical = serde_json::to_string(&fixture()).unwrap();
        let duplicate = canonical.replacen(
            "\"schema_version\":1",
            "\"schema_version\":1,\"schema_version\":1",
            1,
        );
        assert!(RuntimeCatalog::parse(duplicate.as_bytes(), VERSION, SOURCE).is_err());
        let duplicate_entry = canonical.replacen("\"name\":", "\"name\":\"first\",\"name\":", 1);
        assert!(RuntimeCatalog::parse(duplicate_entry.as_bytes(), VERSION, SOURCE).is_err());
        let duplicate_source = canonical.replacen(
            "\"source_archives\":[]",
            "\"source_archives\":[{\"name\":\"a\",\"name\":\"b\"}]",
            1,
        );
        assert!(RuntimeCatalog::parse(duplicate_source.as_bytes(), VERSION, SOURCE).is_err());
        let mut unknown = fixture();
        unknown["unexpected"] = json!(true);
        assert!(parse_value(&unknown).is_err());
        let mut unknown_nested = fixture();
        unknown_nested["artifacts"]["guest"]["url"] = json!("https://evil.invalid/file");
        assert!(parse_value(&unknown_nested).is_err());
        let mut missing_role = fixture();
        missing_role["artifacts"]
            .as_object_mut()
            .unwrap()
            .remove("fixture");
        assert!(parse_value(&missing_role).is_err());
        let duplicate_role = canonical.replacen("\"helper\":", "\"guest\":{},\"helper\":", 1);
        assert!(RuntimeCatalog::parse(duplicate_role.as_bytes(), VERSION, SOURCE).is_err());
    }

    #[test]
    fn rejects_invalid_sizes_hashes_names_platforms_and_source_identity() {
        for raw in ["true", "1.0", "-1", "0", "18446744073709551616"] {
            let mut text = serde_json::to_string(&fixture()).unwrap();
            text = text.replacen("\"size\":1", &format!("\"size\":{raw}"), 1);
            assert!(
                RuntimeCatalog::parse(text.as_bytes(), VERSION, SOURCE).is_err(),
                "{raw}"
            );
        }
        for (size, role) in [("1073741825", "guest"), ("67108865", "helper")] {
            let mut value = fixture();
            value["artifacts"][role]["size"] = json!(size.parse::<u64>().unwrap());
            assert!(parse_value(&value).is_err());
        }
        let mut bad_hash = fixture();
        bad_hash["artifacts"]["agent"]["sha256"] = json!("A".repeat(64));
        assert!(parse_value(&bad_hash).is_err());
        let mut bad_name = fixture();
        bad_name["artifacts"]["guest"]["name"] = json!("../catalog.json");
        assert!(parse_value(&bad_name).is_err());
        let mut bad_platform = fixture();
        bad_platform["artifacts"]["fixture"]["platform"] = json!("linux-aarch64");
        assert!(parse_value(&bad_platform).is_err());
        let mut bad_source = fixture();
        bad_source["artifacts"]["helper"]["source_sha"] = json!("f".repeat(40));
        assert!(parse_value(&bad_source).is_err());
    }

    #[test]
    fn rejects_unsafe_versions_and_duplicate_or_excess_source_parts() {
        for version in ["v1.2.3", "1.2.3-rc.1", "01.2.3", "1.2.3/evil", "1.2"] {
            let mut value = fixture();
            value["version"] = json!(version);
            value["release_tag"] = json!(format!("v{version}"));
            assert!(
                RuntimeCatalog::parse(&serde_json::to_vec(&value).unwrap(), version, SOURCE)
                    .is_err()
            );
        }
        let source = |index: &str| {
            json!({
                "name": format!("ashnazg-runtime-sources-v{VERSION}-{index}.tar.xz"),
                "platform": "source", "source_sha": SOURCE, "size": 1, "sha256": "c".repeat(64)
            })
        };
        let mut duplicate = fixture();
        duplicate["source_archives"] = json!([source("001"), source("001")]);
        assert!(parse_value(&duplicate).is_err());
        let mut zero = fixture();
        zero["source_archives"] = json!([source("000")]);
        assert!(parse_value(&zero).is_err());
        let mut traversal = fixture();
        traversal["source_archives"] = json!([source("../1")]);
        assert!(parse_value(&traversal).is_err());
        let mut too_many = fixture();
        too_many["source_archives"] =
            Value::Array((1..=65).map(|i| source(&format!("{i:03}"))).collect());
        assert!(parse_value(&too_many).is_err());
        let mut oversized_part = fixture();
        oversized_part["source_archives"] = json!([{
            "name": format!("ashnazg-runtime-sources-v{VERSION}-001.tar.xz"),
            "platform": "source", "source_sha": SOURCE,
            "size": 1073741825u64, "sha256": "d".repeat(64)
        }]);
        assert!(parse_value(&oversized_part).is_err());
    }
}
