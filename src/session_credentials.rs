#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{Duration, Instant};
    use tokio_util::sync::CancellationToken;

    const SESSION: &str = "6f6f1234-1234-4234-8234-123456789abc";
    const RPC: &str = "1";
    const INSTALL: &str = "2";

    fn expectations() -> SessionExpectations {
        SessionExpectations {
            session_id: SESSION.into(),
            helper_pid: std::process::id(),
            manifest_source_sha: "a".repeat(40),
            agent_apk_sha256: "b".repeat(64),
            agent_addr: "127.0.0.1:17777".parse().unwrap(),
            install_addr: "127.0.0.1:17778".parse().unwrap(),
        }
    }

    fn credential_json(expected: &SessionExpectations) -> String {
        format!(
            r#"{{"schema_version":1,"session_id":"{}","helper_pid":{},"manifest_source_sha":"{}","agent_apk_sha256":"{}","rpc_token":"{}","install_token":"{}","agent_listen":"{}","install_listen":"{}"}}"#,
            expected.session_id,
            expected.helper_pid,
            expected.manifest_source_sha,
            expected.agent_apk_sha256,
            RPC.repeat(64),
            INSTALL.repeat(64),
            expected.agent_addr,
            expected.install_addr,
        )
    }

    fn fixture(content: &[u8]) -> tempfile::TempDir {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let file = dir.path().join(".ashnazg-session-v1.json");
        fs::write(file, content).unwrap();
        fs::set_permissions(
            dir.path().join(".ashnazg-session-v1.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        dir
    }

    fn read(
        dir: &std::path::Path,
        expected: &SessionExpectations,
    ) -> anyhow::Result<SessionCredentials> {
        // tempfile may return a macOS /var alias; production callers must pass canonical paths.
        let canonical = if dir.is_absolute() && dir.exists() {
            fs::canonicalize(dir).unwrap()
        } else {
            dir.to_path_buf()
        };
        read_private_credentials(
            &canonical,
            expected,
            Instant::now() + Duration::from_secs(2),
            &CancellationToken::new(),
        )
    }

    #[test]
    fn reads_swift_schema_and_builds_both_clients_without_exposing_tokens() {
        let expected = expectations();
        let dir = fixture(credential_json(&expected).as_bytes());
        let credentials = read(dir.path(), &expected).unwrap();
        assert_eq!(credentials.session_id(), SESSION);
        assert_eq!(credentials.agent_addr(), expected.agent_addr);
        assert_eq!(credentials.install_addr(), expected.install_addr);
        assert!(format!("{credentials:?}").contains("<redacted>"));
        assert!(!format!("{credentials:?}").contains(&RPC.repeat(64)));
        assert!(!format!("{credentials:?}").contains(&INSTALL.repeat(64)));
        assert!(format!("{:?}", credentials.install_client().unwrap()).contains("redacted"));
    }

    #[test]
    fn rejects_duplicate_escaped_unknown_and_malformed_schema_without_reflection() {
        let expected = expectations();
        let valid = credential_json(&expected);
        let cases = [
            valid.replacen(
                "\"schema_version\":1",
                "\"schema_version\":1,\"schema_\\u0076ersion\":1",
                1,
            ),
            valid.replacen(
                "\"schema_version\":1,",
                "\"extra\":\"SENTINEL\",\"schema_version\":1,",
                1,
            ),
            valid.replacen("\"schema_version\":1", "\"schema_version\":true", 1),
            valid.replace(
                &format!(",\"install_listen\":\"{}\"", expected.install_addr),
                "",
            ),
            valid.trim_end_matches('}').to_owned(),
        ];
        for content in cases {
            let dir = fixture(content.as_bytes());
            let error = read(dir.path(), &expected).unwrap_err().to_string();
            assert!(!error.contains("SENTINEL"));
            assert!(!error.contains(&expected.session_id));
        }
    }

    #[test]
    fn rejects_invalid_schema_values_hashes_tokens_and_endpoint_forms() {
        let expected = expectations();
        let valid = credential_json(&expected);
        let cases = [
            valid.replacen("\"schema_version\":1", "\"schema_version\":2", 1),
            valid.replacen("\"helper_pid\":", "\"helper_pid\":-1", 1),
            valid.replacen(&"a".repeat(40), &"A".repeat(40), 1),
            valid.replacen(&"b".repeat(64), &"b".repeat(63), 1),
            valid.replacen(&"1".repeat(64), "bad-token", 1),
            valid.replacen("127.0.0.1:17777", "127.0.0.1:017777", 1),
            valid.replacen("127.0.0.1:17777", "localhost:17777", 1),
        ];
        for content in cases {
            let dir = fixture(content.as_bytes());
            let error = read(dir.path(), &expected).unwrap_err();
            assert_eq!(error.to_string(), "session credentials unavailable");
            assert!(!format!("{error:?}").contains(&content));
        }
        let mut invalid_expected = expectations();
        invalid_expected.install_addr = invalid_expected.agent_addr;
        assert!(
            read_private_credentials(
                Path::new("/does/not/exist"),
                &invalid_expected,
                Instant::now() + Duration::from_secs(1),
                &CancellationToken::new(),
            )
            .is_err()
        );
    }

    #[test]
    fn rejects_each_identity_mismatch_and_equal_tokens() {
        let expected = expectations();
        let original = credential_json(&expected);
        let edits = [
            (
                "6f6f1234-1234-4234-8234-123456789abc",
                "7f6f1234-1234-4234-8234-123456789abc",
            ),
            (&expected.manifest_source_sha, &"c".repeat(40)),
            (&expected.agent_apk_sha256, &"c".repeat(64)),
            ("127.0.0.1:17777", "127.0.0.1:17779"),
            ("127.0.0.1:17778", "127.0.0.1:17780"),
        ];
        for (from, to) in edits {
            let content = original.replacen(from, to, 1);
            let dir = fixture(content.as_bytes());
            assert!(read(dir.path(), &expected).is_err());
        }
        let pid_mismatch = original.replacen(
            &format!("\"helper_pid\":{}", expected.helper_pid),
            "\"helper_pid\":2",
            1,
        );
        let dir = fixture(pid_mismatch.as_bytes());
        assert!(read(dir.path(), &expected).is_err());
        let equal = original.replace(&INSTALL.repeat(64), &RPC.repeat(64));
        let dir = fixture(equal.as_bytes());
        assert!(read(dir.path(), &expected).is_err());
    }

    #[test]
    fn rejects_unsafe_files_and_paths_without_mutating_caller_data() {
        use std::os::unix::fs::{PermissionsExt, symlink};
        let expected = expectations();
        let content = credential_json(&expected);
        let dir = fixture(content.as_bytes());
        let file = dir.path().join(".ashnazg-session-v1.json");
        fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read(dir.path(), &expected).is_err());
        fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();

        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(read(dir.path(), &expected).is_err());
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();

        let external = dir.path().with_extension("outside");
        fs::write(&external, &content).unwrap();
        let saved = fs::read(&file).unwrap();
        fs::remove_file(&file).unwrap();
        symlink(&external, &file).unwrap();
        assert!(read(dir.path(), &expected).is_err());
        assert_eq!(fs::read(&external).unwrap(), content.as_bytes());
        fs::remove_file(&file).unwrap();
        fs::write(&file, saved).unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
        fs::remove_file(external).unwrap();

        let link = dir.path().with_extension("link");
        symlink(dir.path(), &link).unwrap();
        assert!(
            read_private_credentials(
                &link,
                &expected,
                Instant::now() + Duration::from_secs(2),
                &CancellationToken::new(),
            )
            .is_err()
        );
        assert!(
            read_private_credentials(
                std::path::Path::new("relative/state"),
                &expected,
                Instant::now() + Duration::from_secs(2),
                &CancellationToken::new(),
            )
            .is_err()
        );
        assert!(
            read_private_credentials(
                &dir.path().join(".."),
                &expected,
                Instant::now() + Duration::from_secs(2),
                &CancellationToken::new(),
            )
            .is_err()
        );
        assert_eq!(fs::read(file).unwrap(), content.as_bytes());
    }

    #[test]
    fn rejects_fifo_empty_oversized_and_trailing_data_without_blocking_or_mutation() {
        use std::os::unix::fs::PermissionsExt;
        let expected = expectations();
        let valid = credential_json(&expected);
        for content in [
            Vec::new(),
            vec![b'x'; MAX_CREDENTIAL_BYTES + 1],
            [valid.as_bytes(), b"x"].concat(),
        ] {
            let dir = fixture(&content);
            assert!(read(dir.path(), &expected).is_err());
            assert_eq!(fs::read(dir.path().join(CREDENTIAL_FILE)).unwrap(), content);
        }
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let fifo = dir.path().join(CREDENTIAL_FILE);
        let c_fifo = CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(c_fifo.as_ptr(), 0o600) }, 0);
        let start = Instant::now();
        assert!(read(dir.path(), &expected).is_err());
        assert!(
            start.elapsed() < Duration::from_secs(1),
            "FIFO open blocked without a writer"
        );
        assert!(fifo.exists());
    }

    #[test]
    fn rejects_intermediate_symlinks_and_excessive_lexical_paths() {
        use std::os::unix::fs::symlink;
        let expected = expectations();
        let dir = fixture(credential_json(&expected).as_bytes());
        let canonical = fs::canonicalize(dir.path()).unwrap();
        let parent = canonical.parent().unwrap();
        let link_root = tempfile::tempdir().unwrap();
        let component_link = link_root.path().join("component-link");
        symlink(parent, &component_link).unwrap();
        let through_link = component_link.join(canonical.file_name().unwrap());
        assert!(
            read_private_credentials(
                &through_link,
                &expected,
                Instant::now() + Duration::from_secs(1),
                &CancellationToken::new(),
            )
            .is_err()
        );
        let too_long_string = format!("/{}", "a".repeat(MAX_PATH_BYTES));
        let too_long = Path::new(&too_long_string);
        assert!(
            read_private_credentials(
                too_long,
                &expected,
                Instant::now() + Duration::from_secs(1),
                &CancellationToken::new(),
            )
            .is_err()
        );
        let too_many_string = format!(
            "/{}",
            std::iter::repeat_n("x", MAX_COMPONENTS + 1)
                .collect::<Vec<_>>()
                .join("/")
        );
        let too_many = Path::new(&too_many_string);
        assert!(
            read_private_credentials(
                too_many,
                &expected,
                Instant::now() + Duration::from_secs(1),
                &CancellationToken::new(),
            )
            .is_err()
        );
        if let Some(path_text) = canonical.to_str()
            && let Some(suffix) = path_text.strip_prefix("/private/")
        {
            let alias_state = Path::new("/").join(suffix);
            if alias_state != canonical {
                assert!(
                    read_private_credentials(
                        &alias_state,
                        &expected,
                        Instant::now() + Duration::from_secs(1),
                        &CancellationToken::new(),
                    )
                    .is_err()
                );
            }
        }
    }

    #[test]
    fn cancellation_at_a_deterministic_read_checkpoint_refuses_success() {
        let expected = expectations();
        let dir = fixture(credential_json(&expected).as_bytes());
        let cancel = CancellationToken::new();
        let hook_cancel = cancel.clone();
        let mut hook = |point| {
            if point == Checkpoint::FileOpened {
                hook_cancel.cancel();
            }
        };
        assert!(
            read_private_credentials_impl(
                &fs::canonicalize(dir.path()).unwrap(),
                &expected,
                Instant::now() + Duration::from_secs(2),
                &cancel,
                &mut hook,
            )
            .is_err()
        );
    }

    #[test]
    fn observed_file_and_directory_replacements_are_preserved_and_refused() {
        use std::os::unix::fs::PermissionsExt;
        for replace_directory in [false, true] {
            let expected = expectations();
            let dir = fixture(credential_json(&expected).as_bytes());
            let state = fs::canonicalize(dir.path()).unwrap();
            let file = state.join(CREDENTIAL_FILE);
            let backup = state.with_extension("saved");
            let displaced = state.with_extension("displaced");
            let mut replaced = false;
            let mut hook = |point| {
                let should_replace = if replace_directory {
                    point == Checkpoint::StateOpened
                } else {
                    point == Checkpoint::FileOpened
                };
                if should_replace && !replaced {
                    if replace_directory {
                        fs::rename(&state, &backup).unwrap();
                        fs::create_dir(&state).unwrap();
                        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
                    } else {
                        fs::rename(&file, &displaced).unwrap();
                        fs::write(&file, credential_json(&expected)).unwrap();
                        fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
                    }
                    replaced = true;
                }
            };
            assert!(
                read_private_credentials_impl(
                    &state,
                    &expected,
                    Instant::now() + Duration::from_secs(2),
                    &CancellationToken::new(),
                    &mut hook,
                )
                .is_err()
            );
            assert!(replaced);
            if replace_directory {
                assert!(state.is_dir());
                assert!(backup.join(CREDENTIAL_FILE).exists());
                fs::remove_dir(&state).unwrap();
                fs::rename(&backup, &state).unwrap();
            } else {
                assert!(file.exists());
                assert!(displaced.exists());
                fs::remove_file(displaced).unwrap();
            }
        }
    }

    #[test]
    fn cancelled_and_expired_calls_fail_before_touching_missing_paths() {
        let expected = expectations();
        let missing = std::env::temp_dir().join("session-credentials-do-not-create");
        let cancel = CancellationToken::new();
        cancel.cancel();
        assert!(
            read_private_credentials(
                &missing,
                &expected,
                Instant::now() + Duration::from_secs(1),
                &cancel,
            )
            .is_err()
        );
        assert!(
            read_private_credentials(
                &missing,
                &expected,
                Instant::now() - Duration::from_secs(1),
                &CancellationToken::new(),
            )
            .is_err()
        );
        assert!(!missing.exists());
        let empty_state = tempfile::tempdir().unwrap();
        std::fs::set_permissions(
            empty_state.path(),
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .unwrap();
        assert!(read(empty_state.path(), &expected).is_err());
        assert!(!empty_state.path().join(CREDENTIAL_FILE).exists());
    }
}
// Read-only validation of the private credentials published for one owned helper session.
// The caller supplies every value that binds a file to its child and verified launch
// artifacts. This reader neither discovers nor authenticates a helper process.

use anyhow::{Result, bail};
use serde::Deserialize;
use std::ffi::CString;
use std::fs::File;
use std::io::Read;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::time::Instant;
use tokio_util::sync::CancellationToken;
use zeroize::Zeroizing;

const CREDENTIAL_FILE: &str = ".ashnazg-session-v1.json";
const MAX_PATH_BYTES: usize = 4096;
const MAX_COMPONENTS: usize = 256;
const MAX_CREDENTIAL_BYTES: usize = 4096;

/// Identity and launch values supplied by the owner of the spawned helper.
pub struct SessionExpectations {
    pub session_id: String,
    pub helper_pid: u32,
    pub manifest_source_sha: String,
    pub agent_apk_sha256: String,
    pub agent_addr: SocketAddr,
    pub install_addr: SocketAddr,
}

/// Validated credentials for one caller-identified helper session.
pub struct SessionCredentials {
    session_id: String,
    helper_pid: u32,
    manifest_source_sha: String,
    agent_apk_sha256: String,
    agent_addr: SocketAddr,
    install_addr: SocketAddr,
    rpc_token: Zeroizing<String>,
    install_token: Zeroizing<String>,
}

impl std::fmt::Debug for SessionCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SessionCredentials")
            .field("session_id", &self.session_id)
            .field("helper_pid", &self.helper_pid)
            .field("manifest_source_sha", &self.manifest_source_sha)
            .field("agent_apk_sha256", &self.agent_apk_sha256)
            .field("agent_addr", &self.agent_addr)
            .field("install_addr", &self.install_addr)
            .field("rpc_token", &"<redacted>")
            .field("install_token", &"<redacted>")
            .finish()
    }
}

impl SessionCredentials {
    pub fn session_id(&self) -> &str {
        &self.session_id
    }
    pub fn helper_pid(&self) -> u32 {
        self.helper_pid
    }
    pub fn manifest_source_sha(&self) -> &str {
        &self.manifest_source_sha
    }
    pub fn agent_apk_sha256(&self) -> &str {
        &self.agent_apk_sha256
    }
    pub fn agent_addr(&self) -> SocketAddr {
        self.agent_addr
    }
    pub fn install_addr(&self) -> SocketAddr {
        self.install_addr
    }

    /// Makes the existing RPC client with an owned copy of its token.
    pub fn guest_client(&self) -> crate::GuestClient {
        crate::GuestClient::new(self.agent_addr, self.rpc_token.to_string())
    }

    /// Makes the existing native install client with an owned copy of its token.
    pub fn install_client(&self) -> Result<crate::native_install::NativeInstallClient> {
        crate::native_install::NativeInstallClient::new(
            self.install_addr,
            self.session_id.clone(),
            self.install_token.to_string(),
        )
    }
}

struct SecretString(Zeroizing<String>);

impl<'de> Deserialize<'de> for SecretString {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        String::deserialize(deserializer).map(|value| Self(Zeroizing::new(value)))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CredentialDocument {
    schema_version: u64,
    session_id: String,
    helper_pid: u32,
    manifest_source_sha: String,
    agent_apk_sha256: String,
    rpc_token: SecretString,
    install_token: SecretString,
    agent_listen: String,
    install_listen: String,
}

/// Reads and validates the fixed credential file without creating or changing filesystem data.
pub fn read_private_credentials(
    state_dir: &Path,
    expected: &SessionExpectations,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<SessionCredentials> {
    #[cfg(test)]
    {
        read_private_credentials_impl(state_dir, expected, deadline, cancel, &mut |_| {})
    }
    #[cfg(not(test))]
    {
        read_private_credentials_impl(state_dir, expected, deadline, cancel)
    }
}

fn validate_expectations(expected: &SessionExpectations) -> Result<()> {
    if !crate::native_install::canonical_uuid(&expected.session_id)
        || !(2..=i32::MAX as u32).contains(&expected.helper_pid)
        || !crate::native_install::valid_hex(&expected.manifest_source_sha, 40)
        || !crate::native_install::valid_hex(&expected.agent_apk_sha256, 64)
        || !valid_endpoint(expected.agent_addr)
        || !valid_endpoint(expected.install_addr)
        || expected.agent_addr.port() == expected.install_addr.port()
    {
        bail!("invalid session credential expectations");
    }
    Ok(())
}

fn valid_endpoint(addr: SocketAddr) -> bool {
    addr.ip() == IpAddr::V4(Ipv4Addr::LOCALHOST) && (1024..=u16::MAX).contains(&addr.port())
}

fn check_active(deadline: Instant, cancel: &CancellationToken) -> Result<()> {
    if cancel.is_cancelled() || Instant::now() >= deadline {
        bail!("session credentials unavailable");
    }
    Ok(())
}

#[derive(Clone, Copy)]
struct Identity {
    dev: u64,
    ino: u64,
    mode: u32,
    uid: u32,
    size: u64,
    mtime: (i64, i64),
    ctime: (i64, i64),
}

impl Identity {
    fn from_metadata(metadata: &std::fs::Metadata) -> Self {
        Self {
            dev: metadata.dev(),
            ino: metadata.ino(),
            mode: metadata.mode(),
            uid: metadata.uid(),
            size: metadata.size(),
            mtime: (metadata.mtime(), metadata.mtime_nsec()),
            ctime: (metadata.ctime(), metadata.ctime_nsec()),
        }
    }

    fn same_file(self, other: Self) -> bool {
        self.dev == other.dev
            && self.ino == other.ino
            && self.mode == other.mode
            && self.uid == other.uid
            && self.size == other.size
            && self.mtime == other.mtime
            && self.ctime == other.ctime
    }

    fn same_directory(self, other: Self) -> bool {
        self.dev == other.dev
            && self.ino == other.ino
            && self.mode == other.mode
            && self.uid == other.uid
    }
}

fn open_dir_at(parent: i32, name: &CString) -> Result<File> {
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY
                | libc::O_DIRECTORY
                | libc::O_NOFOLLOW
                | libc::O_NONBLOCK
                | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        bail!("session credentials unavailable");
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn open_root() -> Result<File> {
    let root = CString::new("/").expect("constant has no nul");
    open_dir_at(libc::AT_FDCWD, &root)
}

fn canonical_components(path: &Path) -> Result<Vec<CString>> {
    let bytes = path.as_os_str().as_bytes();
    if bytes.is_empty() || bytes.len() > MAX_PATH_BYTES || bytes[0] != b'/' || bytes.contains(&0) {
        bail!("session credentials unavailable");
    }
    if bytes == b"/" || bytes.last() == Some(&b'/') {
        bail!("session credentials unavailable");
    }
    let mut result = Vec::new();
    for part in bytes[1..].split(|byte| *byte == b'/') {
        if part.is_empty() || part == b"." || part == b".." {
            bail!("session credentials unavailable");
        }
        result.push(
            CString::new(part).map_err(|_| anyhow::anyhow!("session credentials unavailable"))?,
        );
    }
    if result.is_empty() || result.len() > MAX_COMPONENTS {
        bail!("session credentials unavailable");
    }
    Ok(result)
}

fn check_directory(file: &File, uid: u32) -> Result<Identity> {
    let metadata = file
        .metadata()
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    let identity = Identity::from_metadata(&metadata);
    if !metadata.is_dir() || identity.uid != uid || identity.mode & 0o7777 != 0o700 {
        bail!("session credentials unavailable");
    }
    Ok(identity)
}

fn same_named(parent: &File, name: &CString, held: Identity, directory: bool) -> Result<()> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    let result = unsafe {
        libc::fstatat(
            parent.as_raw_fd(),
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if result != 0 {
        bail!("session credentials unavailable");
    }
    let stat = unsafe { stat.assume_init() };
    let named = stat_identity(&stat);
    if !(if directory {
        held.same_directory(named)
    } else {
        held.same_file(named)
    }) {
        bail!("session credentials unavailable");
    }
    Ok(())
}

fn stat_identity(stat: &libc::stat) -> Identity {
    #[cfg(target_os = "macos")]
    let (mtime, ctime) = (
        (stat.st_mtime, stat.st_mtime_nsec),
        (stat.st_ctime, stat.st_ctime_nsec),
    );
    #[cfg(not(target_os = "macos"))]
    let (mtime, ctime) = (
        (stat.st_mtime, stat.st_mtime_nsec),
        (stat.st_ctime, stat.st_ctime_nsec),
    );
    Identity {
        dev: stat.st_dev as u64,
        ino: stat.st_ino,
        mode: stat.st_mode as u32,
        uid: stat.st_uid,
        size: stat.st_size.max(0) as u64,
        mtime,
        ctime,
    }
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Checkpoint {
    StateOpened,
    FileOpened,
    ReadComplete,
    BeforeSuccess,
}

fn read_private_credentials_impl(
    state_dir: &Path,
    expected: &SessionExpectations,
    deadline: Instant,
    cancel: &CancellationToken,
    #[cfg(test)] checkpoint: &mut dyn FnMut(Checkpoint),
) -> Result<SessionCredentials> {
    validate_expectations(expected)?;
    check_active(deadline, cancel)?;
    let components = canonical_components(state_dir)?;
    let uid = unsafe { libc::geteuid() };
    let mut current = open_root()?;
    for component in &components[..components.len() - 1] {
        check_active(deadline, cancel)?;
        let next = open_dir_at(current.as_raw_fd(), component)?;
        current = next;
    }
    let parent = current;
    let state_name = components.last().expect("validated nonempty path");
    let state = open_dir_at(parent.as_raw_fd(), state_name)?;
    let state_identity = check_directory(&state, uid)?;
    same_named(&parent, state_name, state_identity, true)?;
    #[cfg(test)]
    checkpoint(Checkpoint::StateOpened);
    check_active(deadline, cancel)?;

    let file_name = CString::new(CREDENTIAL_FILE).expect("constant has no nul");
    let fd = unsafe {
        libc::openat(
            state.as_raw_fd(),
            file_name.as_ptr(),
            libc::O_RDONLY | libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        bail!("session credentials unavailable");
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    let file_metadata = file
        .metadata()
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    let file_identity = Identity::from_metadata(&file_metadata);
    if !file_metadata.is_file()
        || file_identity.uid != uid
        || file_identity.mode & 0o7777 != 0o600
        || !(1..=MAX_CREDENTIAL_BYTES as u64).contains(&file_identity.size)
    {
        bail!("session credentials unavailable");
    }
    same_named(&state, &file_name, file_identity, false)?;
    #[cfg(test)]
    checkpoint(Checkpoint::FileOpened);

    let mut bytes = Zeroizing::new(Vec::with_capacity(file_identity.size as usize + 1));
    let mut buf = [0u8; 512];
    loop {
        check_active(deadline, cancel)?;
        let remaining = MAX_CREDENTIAL_BYTES + 1 - bytes.len();
        if remaining == 0 {
            bail!("session credentials unavailable");
        }
        let chunk_len = remaining.min(buf.len());
        let count = file
            .read(&mut buf[..chunk_len])
            .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
        if count == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..count]);
        if bytes.len() > MAX_CREDENTIAL_BYTES {
            bail!("session credentials unavailable");
        }
    }
    #[cfg(test)]
    checkpoint(Checkpoint::ReadComplete);
    if bytes.is_empty() || bytes.len() as u64 != file_identity.size {
        bail!("session credentials unavailable");
    }
    same_named(&state, &file_name, file_identity, false)?;
    let after_file = file
        .metadata()
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    if !file_identity.same_file(Identity::from_metadata(&after_file)) {
        bail!("session credentials unavailable");
    }
    let after_state = check_directory(&state, uid)?;
    if !state_identity.same_directory(after_state) {
        bail!("session credentials unavailable");
    }
    same_named(&parent, state_name, state_identity, true)?;

    let doc: CredentialDocument = serde_json::from_slice(&bytes)
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    if doc.schema_version != 1
        || doc.helper_pid < 2
        || doc.helper_pid > i32::MAX as u32
        || doc.session_id != expected.session_id
        || doc.helper_pid != expected.helper_pid
        || doc.manifest_source_sha != expected.manifest_source_sha
        || doc.agent_apk_sha256 != expected.agent_apk_sha256
        || !crate::native_install::canonical_uuid(&doc.session_id)
        || !crate::native_install::valid_hex(&doc.manifest_source_sha, 40)
        || !crate::native_install::valid_hex(&doc.agent_apk_sha256, 64)
        || !crate::native_install::valid_hex(&doc.rpc_token.0, 64)
        || !crate::native_install::valid_hex(&doc.install_token.0, 64)
        || doc.rpc_token.0 == doc.install_token.0
        || doc.agent_listen != expected.agent_addr.to_string()
        || doc.install_listen != expected.install_addr.to_string()
    {
        bail!("session credentials unavailable");
    }
    let agent_addr = doc
        .agent_listen
        .parse::<SocketAddr>()
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    let install_addr = doc
        .install_listen
        .parse::<SocketAddr>()
        .map_err(|_| anyhow::anyhow!("session credentials unavailable"))?;
    if agent_addr != expected.agent_addr || install_addr != expected.install_addr {
        bail!("session credentials unavailable");
    }
    check_active(deadline, cancel)?;
    #[cfg(test)]
    checkpoint(Checkpoint::BeforeSuccess);
    check_active(deadline, cancel)?;
    Ok(SessionCredentials {
        session_id: doc.session_id,
        helper_pid: doc.helper_pid,
        manifest_source_sha: doc.manifest_source_sha,
        agent_apk_sha256: doc.agent_apk_sha256,
        agent_addr,
        install_addr,
        rpc_token: doc.rpc_token.0,
        install_token: doc.install_token.0,
    })
}
