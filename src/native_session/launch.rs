use super::NativeSessionLaunch;
use anyhow::Result;
use sha2::{Digest, Sha256};
use std::{
    ffi::CString,
    fs::File,
    io::Read,
    os::fd::{AsRawFd, FromRawFd},
    os::unix::fs::MetadataExt,
    path::Path,
    time::Instant,
};
use tokio_util::sync::CancellationToken;

const HELPER_MAX: u64 = 64 * 1024 * 1024;
const APK_MAX: u64 = 128 * 1024 * 1024;
const GENERIC: &str = "native launch validation failed";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Identity {
    dev: u64,
    ino: u64,
    mode: u32,
    uid: u32,
    size: u64,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
}
impl Identity {
    fn from_meta(m: &std::fs::Metadata) -> Self {
        Self {
            dev: m.dev(),
            ino: m.ino(),
            mode: m.mode(),
            uid: m.uid(),
            size: m.size(),
            mtime: m.mtime(),
            mtime_nsec: m.mtime_nsec(),
            ctime: m.ctime(),
            ctime_nsec: m.ctime_nsec(),
        }
    }
    fn kind(self) -> u32 {
        self.mode & libc::S_IFMT as u32
    }
}

struct DirBinding {
    parent: File,
    name: CString,
    expected: Identity,
    held: File,
}
struct EntryBinding {
    parent: File,
    name: CString,
    expected: Option<Identity>,
    directory: bool,
}
struct CheckedFile {
    file: File,
    binding: EntryBinding,
    expected: Identity,
}
pub(super) struct VerifiedLaunch {
    dirs: Vec<DirBinding>,
    helper: CheckedFile,
    apk: CheckedFile,
    assets: EntryBinding,
    state: EntryBinding,
    state_marker: Option<EntryBinding>,
}

fn fail() -> anyhow::Error {
    anyhow::anyhow!(GENERIC)
}
fn check_time(deadline: Instant, cancel: &CancellationToken) -> Result<()> {
    if cancel.is_cancelled() || Instant::now() >= deadline {
        Err(fail())
    } else {
        Ok(())
    }
}
fn uid() -> u32 {
    unsafe { libc::geteuid() }
}
fn cstr(value: &std::ffi::CStr) -> CString {
    value.to_owned()
}
fn openat(parent: i32, name: &CString, flags: i32) -> Result<File> {
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(fail());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}
fn named(parent: &File, name: &CString) -> Result<Option<Identity>> {
    let mut raw = std::mem::MaybeUninit::<libc::stat>::uninit();
    let rc = unsafe {
        libc::fstatat(
            parent.as_raw_fd(),
            name.as_ptr(),
            raw.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if rc != 0 {
        let e = std::io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::ENOENT) {
            return Ok(None);
        }
        return Err(fail());
    }
    let s = unsafe { raw.assume_init() };
    Ok(Some(Identity {
        dev: s.st_dev as u64,
        ino: s.st_ino,
        mode: s.st_mode as u32,
        uid: s.st_uid,
        size: s.st_size.max(0) as u64,
        mtime: s.st_mtime,
        mtime_nsec: s.st_mtime_nsec,
        ctime: s.st_ctime,
        ctime_nsec: s.st_ctime_nsec,
    }))
}
fn directory_policy(id: Identity) -> Result<()> {
    if id.kind() != libc::S_IFDIR as u32 {
        return Err(fail());
    }
    let writable = id.mode & 0o022 != 0;
    if id.uid != uid() && id.uid != 0 {
        return Err(fail());
    }
    if writable && !(id.uid == 0 && id.mode & libc::S_ISVTX as u32 != 0) {
        return Err(fail());
    }
    Ok(())
}
fn private_directory(id: Identity) -> Result<()> {
    if id.kind() != libc::S_IFDIR as u32 || id.uid != uid() || id.mode & 0o7777 != 0o700 {
        return Err(fail());
    }
    Ok(())
}
fn valid_hex(text: &str, length: usize) -> bool {
    text.len() == length
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn same_directory(a: Identity, b: Identity) -> bool {
    a.dev == b.dev
        && a.ino == b.ino
        && a.kind() == libc::S_IFDIR as u32
        && b.kind() == libc::S_IFDIR as u32
        && a.uid == b.uid
        && a.mode == b.mode
}
fn walk_dirs(
    parts: &[CString],
    final_private: bool,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<(Vec<DirBinding>, File)> {
    check_time(deadline, cancel)?;
    let root = CString::new("/").unwrap();
    let mut held = openat(libc::AT_FDCWD, &root, libc::O_RDONLY | libc::O_DIRECTORY)?;
    let mut dirs = Vec::new();
    for (i, name) in parts.iter().enumerate() {
        check_time(deadline, cancel)?;
        let id = Identity::from_meta(&held.metadata().map_err(|_| fail())?);
        directory_policy(id)?;
        let next = openat(held.as_raw_fd(), name, libc::O_RDONLY | libc::O_DIRECTORY)?;
        let expected = Identity::from_meta(&next.metadata().map_err(|_| fail())?);
        if i + 1 == parts.len() && final_private {
            private_directory(expected)?;
        } else {
            directory_policy(expected)?;
        }
        dirs.push(DirBinding {
            parent: held,
            name: cstr(name),
            expected,
            held: next,
        });
        held = openat(
            dirs.last().unwrap().parent.as_raw_fd(),
            name,
            libc::O_RDONLY | libc::O_DIRECTORY,
        )?;
        if !same_directory(
            Identity::from_meta(&held.metadata().map_err(|_| fail())?),
            expected,
        ) {
            return Err(fail());
        }
    }
    if parts.is_empty() && final_private {
        private_directory(Identity::from_meta(&held.metadata().map_err(|_| fail())?))?;
    }
    Ok((dirs, held))
}
fn path_parts(path: &Path) -> Result<Vec<CString>> {
    crate::session_credentials::canonical_components(path).map_err(|_| fail())
}
fn directory_path(
    path: &Path,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<(Vec<DirBinding>, EntryBinding)> {
    let parts = path_parts(path)?;
    let (dirs, held) = walk_dirs(&parts, true, deadline, cancel)?;
    let (parent, name) = dirs
        .last()
        .map(|b| (b.parent.try_clone().map_err(|_| fail()), cstr(&b.name)))
        .unwrap_or_else(|| unreachable!());
    let parent = parent?;
    let expected = Identity::from_meta(&held.metadata().map_err(|_| fail())?);
    Ok((
        dirs,
        EntryBinding {
            parent,
            name,
            expected: Some(expected),
            directory: true,
        },
    ))
}
fn file_path(
    path: &Path,
    max: u64,
    executable: bool,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<(Vec<DirBinding>, CheckedFile)> {
    let parts = path_parts(path)?;
    let (leaf, parents) = parts.split_last().ok_or_else(fail)?;
    let (dirs, parent) = walk_dirs(parents, true, deadline, cancel)?;
    check_time(deadline, cancel)?;
    let file = openat(parent.as_raw_fd(), leaf, libc::O_RDONLY)?;
    let id = Identity::from_meta(&file.metadata().map_err(|_| fail())?);
    if id.kind() != libc::S_IFREG as u32
        || id.uid != uid()
        || id.size == 0
        || id.size > max
        || id.mode & 0o022 != 0
        || (executable && id.mode & 0o100 == 0)
    {
        return Err(fail());
    }
    let entry = EntryBinding {
        parent,
        name: cstr(leaf),
        expected: Some(id),
        directory: false,
    };
    if named(&entry.parent, &entry.name)? != Some(id) {
        return Err(fail());
    }
    Ok((
        dirs,
        CheckedFile {
            file,
            binding: entry,
            expected: id,
        },
    ))
}
fn compare_entry(binding: &EntryBinding) -> Result<()> {
    let actual = named(&binding.parent, &binding.name)?;
    let matches = match (actual, binding.expected) {
        (Some(actual), Some(expected)) if binding.directory => same_directory(actual, expected),
        (actual, expected) => actual == expected,
    };
    if !matches { Err(fail()) } else { Ok(()) }
}
fn compare_file(file: &CheckedFile) -> Result<()> {
    let now = Identity::from_meta(&file.file.metadata().map_err(|_| fail())?);
    if now != file.expected || compare_entry(&file.binding).is_err() {
        return Err(fail());
    }
    Ok(())
}
fn hash_helper(
    helper: &mut CheckedFile,
    expected: &str,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<()> {
    check_time(deadline, cancel)?;
    let before = Identity::from_meta(&helper.file.metadata().map_err(|_| fail())?);
    let mut hash = Sha256::new();
    let mut buf = [0u8; 65536];
    let mut total = 0u64;
    loop {
        check_time(deadline, cancel)?;
        let n = helper.file.read(&mut buf).map_err(|_| fail())?;
        if n == 0 {
            break;
        }
        total = total.saturating_add(n as u64);
        if total > HELPER_MAX {
            return Err(fail());
        }
        hash.update(&buf[..n]);
    }
    let after = Identity::from_meta(&helper.file.metadata().map_err(|_| fail())?);
    let digest = hash
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if before != after || before != helper.expected || digest != expected {
        return Err(fail());
    }
    compare_file(helper)
}

pub(super) fn validate(
    launch: &NativeSessionLaunch,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<VerifiedLaunch> {
    check_time(deadline, cancel)?;
    if !(15..=1800).contains(&launch.startup_timeout_secs)
        || !valid_hex(&launch.source_sha, 40)
        || !valid_hex(&launch.helper_sha256, 64)
        || !valid_hex(&launch.agent_apk_sha256, 64)
        || [launch.adb_port, launch.agent_port, launch.install_port]
            .iter()
            .any(|p| *p < 1024)
        || launch.adb_port == launch.agent_port
        || launch.adb_port == launch.install_port
        || launch.agent_port == launch.install_port
    {
        return Err(fail());
    }
    let (mut dirs, mut helper) = file_path(&launch.helper, HELPER_MAX, true, deadline, cancel)?;
    hash_helper(&mut helper, &launch.helper_sha256, deadline, cancel)?;
    let (more, apk) = file_path(&launch.agent_apk, APK_MAX, false, deadline, cancel)?;
    dirs.extend(more);
    let (more, assets) = directory_path(&launch.assets, deadline, cancel)?;
    dirs.extend(more);
    let state_parts = path_parts(&launch.state_dir)?;
    let (leaf, parents) = state_parts.split_last().ok_or_else(fail)?;
    let (more, state_parent) = walk_dirs(parents, true, deadline, cancel)?;
    dirs.extend(more);
    let state_id = named(&state_parent, leaf)?;
    if let Some(id) = state_id {
        private_directory(id)?;
    }
    let state = EntryBinding {
        parent: state_parent.try_clone().map_err(|_| fail())?,
        name: cstr(leaf),
        expected: state_id,
        directory: true,
    };
    let state_dir = if let Some(id) = state_id {
        let f = openat(
            state.parent.as_raw_fd(),
            leaf,
            libc::O_RDONLY | libc::O_DIRECTORY,
        )?;
        if !same_directory(Identity::from_meta(&f.metadata().map_err(|_| fail())?), id) {
            return Err(fail());
        }
        dirs.push(DirBinding {
            parent: state.parent.try_clone().map_err(|_| fail())?,
            name: cstr(leaf),
            expected: id,
            held: f.try_clone().map_err(|_| fail())?,
        });
        f
    } else {
        state_parent
    };
    let marker = CString::new(".ashnazg-session-v1.json").unwrap();
    if named(&state_dir, &marker)?.is_some() {
        return Err(fail());
    }
    let state_marker = if state_id.is_some() {
        Some(EntryBinding {
            parent: state_dir,
            name: marker,
            expected: None,
            directory: false,
        })
    } else {
        None
    };
    let verified = VerifiedLaunch {
        dirs,
        helper,
        apk,
        assets,
        state,
        state_marker,
    };
    verified.check_before_spawn(deadline, cancel)?;
    Ok(verified)
}
impl VerifiedLaunch {
    pub(super) fn check_before_spawn(
        &self,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> Result<()> {
        check_time(deadline, cancel)?;
        for dir in &self.dirs {
            check_time(deadline, cancel)?;
            let held = Identity::from_meta(&dir.held.metadata().map_err(|_| fail())?);
            if !same_directory(held, dir.expected)
                || named(&dir.parent, &dir.name)?
                    .is_none_or(|actual| !same_directory(actual, dir.expected))
            {
                return Err(fail());
            }
        }
        compare_file(&self.helper)?;
        compare_file(&self.apk)?;
        compare_entry(&self.assets)?;
        compare_entry(&self.state)?;
        if let Some(marker) = &self.state_marker {
            compare_entry(marker)?;
        }
        check_time(deadline, cancel)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt, time::Duration};

    struct Fixture {
        _tmp: tempfile::TempDir,
        launch: NativeSessionLaunch,
        hash: String,
    }
    fn fixture() -> Fixture {
        let tmp = tempfile::tempdir().unwrap();
        fs::set_permissions(tmp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        let helper = root.join("helper");
        fs::write(&helper, b"launch bytes").unwrap();
        fs::set_permissions(&helper, fs::Permissions::from_mode(0o700)).unwrap();
        let apk = root.join("agent.apk");
        fs::write(&apk, b"apk").unwrap();
        let assets = root.join("assets");
        fs::create_dir(&assets).unwrap();
        fs::set_permissions(&assets, fs::Permissions::from_mode(0o700)).unwrap();
        let state = root.join("state");
        fs::create_dir(&state).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
        let hash: String = Sha256::digest(b"launch bytes")
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Fixture {
            _tmp: tmp,
            launch: NativeSessionLaunch {
                helper,
                helper_sha256: hash.clone(),
                assets,
                state_dir: state,
                agent_apk: apk,
                agent_apk_sha256: "b".repeat(64),
                source_sha: "a".repeat(40),
                adb_port: 17777,
                agent_port: 17778,
                install_port: 17779,
                startup_timeout_secs: 60,
            },
            hash,
        }
    }
    fn deadline() -> Instant {
        Instant::now() + Duration::from_secs(3)
    }
    #[test]
    fn validates_private_fixture_and_retains_verified_launch() {
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        v.check_before_spawn(deadline(), &CancellationToken::new())
            .unwrap();
    }
    #[test]
    fn refuses_existing_state_marker_without_mutation() {
        let f = fixture();
        fs::write(
            f.launch.state_dir.join(".ashnazg-session-v1.json"),
            b"owned",
        )
        .unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn refuses_changed_helper_binding() {
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::rename(&f.launch.helper, f.launch.helper.with_extension("old")).unwrap();
        fs::write(&f.launch.helper, b"launch bytes").unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
    }
    #[test]
    fn refuses_cancelled_or_expired_validation() {
        let f = fixture();
        assert!(validate(&f.launch, Instant::now(), &CancellationToken::new()).is_err());
        let c = CancellationToken::new();
        c.cancel();
        assert!(validate(&f.launch, deadline(), &c).is_err());
    }
    #[test]
    fn refuses_bad_hash_and_duplicate_ports() {
        let mut f = fixture();
        f.launch.helper_sha256 = "0".repeat(64);
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        f.launch.helper_sha256 = f.hash;
        f.launch.install_port = f.launch.agent_port;
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn accepts_missing_state_without_creating_it_and_preserves_other_files() {
        let mut f = fixture();
        fs::remove_dir(&f.launch.state_dir).unwrap();
        let absent = f.launch.state_dir.clone();
        let marker = f.launch.assets.join("caller-data");
        fs::write(&marker, b"keep").unwrap();
        f.launch.state_dir = absent.clone();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        v.check_before_spawn(deadline(), &CancellationToken::new())
            .unwrap();
        assert!(!absent.exists());
        assert_eq!(fs::read(marker).unwrap(), b"keep");
    }
    #[test]
    fn refuses_marker_file_symlink_directory_and_fifo() {
        let cases = ["file", "symlink", "directory", "fifo"];
        for kind in cases {
            let f = fixture();
            let p = f.launch.state_dir.join(".ashnazg-session-v1.json");
            match kind {
                "file" => fs::write(&p, b"occupied").unwrap(),
                "symlink" => std::os::unix::fs::symlink("absent-target", &p).unwrap(),
                "directory" => fs::create_dir(&p).unwrap(),
                "fifo" => assert_eq!(
                    unsafe {
                        libc::mkfifo(
                            std::ffi::CString::new(p.as_os_str().as_encoded_bytes())
                                .unwrap()
                                .as_ptr(),
                            0o600,
                        )
                    },
                    0
                ),
                _ => unreachable!(),
            }
            assert!(
                validate(&f.launch, deadline(), &CancellationToken::new()).is_err(),
                "{kind}"
            );
        }
    }
    #[test]
    fn refuses_helper_and_apk_fifos_without_a_writer() {
        for helper in [true, false] {
            let f = fixture();
            let p = if helper {
                &f.launch.helper
            } else {
                &f.launch.agent_apk
            };
            fs::remove_file(p).unwrap();
            let c = std::ffi::CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        }
    }
    #[test]
    fn refuses_helper_empty_bad_mode_and_oversize() {
        let f = fixture();
        fs::write(&f.launch.helper, b"").unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        fs::set_permissions(&f.launch.helper, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        fs::set_permissions(&f.launch.helper, fs::Permissions::from_mode(0o722)).unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        let file = fs::OpenOptions::new()
            .write(true)
            .open(&f.launch.helper)
            .unwrap();
        file.set_len(HELPER_MAX + 1).unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn rejects_apk_empty_and_oversize() {
        let f = fixture();
        fs::write(&f.launch.agent_apk, b"").unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        fs::OpenOptions::new()
            .write(true)
            .open(&f.launch.agent_apk)
            .unwrap()
            .set_len(APK_MAX + 1)
            .unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn rejects_writable_private_parent_and_symlinked_path_components() {
        let f = fixture();
        fs::set_permissions(f._tmp.path(), fs::Permissions::from_mode(0o720)).unwrap();
        assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        let link = f._tmp.path().join("link");
        std::os::unix::fs::symlink(f._tmp.path(), &link).unwrap();
        let mut bad = f.launch;
        bad.assets = link.join("assets");
        assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        let link = f._tmp.path().join("helper-link");
        std::os::unix::fs::symlink(&f.launch.helper, &link).unwrap();
        let mut bad = f.launch;
        bad.helper = link;
        assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn rejects_writable_nonfinal_ancestor_and_other_uid_files_when_supported() {
        let f = fixture();
        let unsafe_parent = f._tmp.path().join("writable-ancestor");
        fs::create_dir(&unsafe_parent).unwrap();
        fs::set_permissions(&unsafe_parent, fs::Permissions::from_mode(0o777)).unwrap();
        let helper = unsafe_parent.join("helper");
        fs::copy(&f.launch.helper, &helper).unwrap();
        fs::set_permissions(&helper, fs::Permissions::from_mode(0o700)).unwrap();
        let mut bad = f.launch;
        bad.helper = helper;
        bad.helper_sha256 = Sha256::digest(b"launch bytes")
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());

        if uid() == 0 {
            let f = fixture();
            let c = std::ffi::CString::new(f.launch.helper.as_os_str().as_encoded_bytes()).unwrap();
            assert_eq!(unsafe { libc::chown(c.as_ptr(), 1, 1) }, 0);
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
            let f = fixture();
            let c =
                std::ffi::CString::new(f.launch.agent_apk.as_os_str().as_encoded_bytes()).unwrap();
            assert_eq!(unsafe { libc::chown(c.as_ptr(), 1, 1) }, 0);
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        }
    }
    #[test]
    fn refuses_noncanonical_tmp_or_var_alias_when_platform_has_one() {
        let f = fixture();
        let raw = f._tmp.path();
        let canonical = fs::canonicalize(raw).unwrap();
        if raw != canonical {
            let mut bad = f.launch;
            bad.assets = raw.join("assets");
            assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
        }
        let f = fixture();
        let canonical = fs::canonicalize(&f._tmp).unwrap();
        let canonical_text = canonical.to_string_lossy();
        if let Some(rest) = canonical_text.strip_prefix("/private/var/") {
            let mut bad = f.launch;
            bad.assets = Path::new("/var").join(rest).join("assets");
            assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
        }
    }
    #[test]
    fn rejects_raw_noncanonical_paths_without_normalizing() {
        let f = fixture();
        let canonical = fs::canonicalize(&f._tmp).unwrap();
        let mut bad = f.launch;
        bad.state_dir = canonical.join("child/../state");
        assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
        let f = fixture();
        let canonical = fs::canonicalize(&f._tmp).unwrap();
        let mut bad = f.launch;
        bad.assets = canonical.join("./assets");
        assert!(validate(&bad, deadline(), &CancellationToken::new()).is_err());
    }
    #[test]
    fn rejects_nonlowercase_or_wrong_length_identities_and_bounds() {
        for (field, value) in [
            ("source", "A".repeat(40)),
            ("source", "a".repeat(39)),
            ("helper", "B".repeat(64)),
            ("apk", "b".repeat(63)),
        ] {
            let mut f = fixture();
            match field {
                "source" => f.launch.source_sha = value,
                "helper" => f.launch.helper_sha256 = value,
                _ => f.launch.agent_apk_sha256 = value,
            }
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        }
        for timeout in [14, 1801] {
            let mut f = fixture();
            f.launch.startup_timeout_secs = timeout;
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        }
        for p in [0, 1023] {
            let mut f = fixture();
            f.launch.adb_port = p;
            assert!(validate(&f.launch, deadline(), &CancellationToken::new()).is_err());
        }
    }
    #[test]
    fn check_before_spawn_rejects_metadata_and_binding_changes_for_each_artifact_and_directory() {
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::write(&f.launch.helper, b"changed helper").unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        let old = f.launch.state_dir.with_extension("old");
        fs::rename(&f.launch.state_dir, &old).unwrap();
        fs::create_dir(&f.launch.state_dir).unwrap();
        fs::set_permissions(&f.launch.state_dir, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::write(&f.launch.agent_apk, b"changed").unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        let old = f.launch.agent_apk.with_extension("old");
        fs::rename(&f.launch.agent_apk, &old).unwrap();
        fs::write(&f.launch.agent_apk, b"apk").unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::set_permissions(f._tmp.path(), fs::Permissions::from_mode(0o750)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        fs::set_permissions(f._tmp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::set_permissions(&f.launch.assets, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::set_permissions(&f.launch.state_dir, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        let old = f.launch.assets.with_extension("old");
        fs::rename(&f.launch.assets, &old).unwrap();
        fs::create_dir(&f.launch.assets).unwrap();
        fs::set_permissions(&f.launch.assets, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
    }
    #[test]
    fn check_before_spawn_rejects_missing_state_creation_and_ancestor_replacement() {
        let f = fixture();
        fs::remove_dir(&f.launch.state_dir).unwrap();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        fs::create_dir(&f.launch.state_dir).unwrap();
        fs::set_permissions(&f.launch.state_dir, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
        let f = fixture();
        let v = validate(&f.launch, deadline(), &CancellationToken::new()).unwrap();
        let parent = f._tmp.path();
        let moved = parent.with_extension("moved");
        fs::rename(parent, &moved).unwrap();
        fs::create_dir(parent).unwrap();
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(
            v.check_before_spawn(deadline(), &CancellationToken::new())
                .is_err()
        );
    }
    #[test]
    fn directory_policy_allows_root_sticky_and_rejects_writable_without_sticky() {
        let mut sticky = Identity {
            dev: 0,
            ino: 1,
            mode: (libc::S_IFDIR as u32) | 0o1777,
            uid: 0,
            size: 0,
            mtime: 0,
            mtime_nsec: 0,
            ctime: 0,
            ctime_nsec: 0,
        };
        assert!(directory_policy(sticky).is_ok());
        sticky.mode &= !libc::S_ISVTX as u32;
        assert!(directory_policy(sticky).is_err());
    }
}
