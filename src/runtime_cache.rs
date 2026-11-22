//! Locked, private runtime cache filesystem primitives.
//!
//! A successful entry lookup verifies cache structure and catalog-key binding only. It does not
//! hash or authenticate payload files; each acquirer must validate those before trusting them.

use anyhow::{Context, anyhow, bail, ensure};
use serde::{Deserialize, Serialize};
use std::ffi::{CStr, CString};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};

const ROOT_MARKER: &str = "cache.json";
const LOCK_FILE: &str = "cache.lock";
const ENTRIES_DIR: &str = "entries";
const STAGING_DIR: &str = "staging";
const ENTRY_MARKER: &str = "entry.json";
const STAGE_MARKER: &str = "stage.json";
const IDENTITY: &str = "ashnazg-runtime-cache";
const MAX_MARKER: usize = 4096;
const PRIVATE_DIR: u32 = 0o700;
const PRIVATE_FILE: u32 = 0o600;

#[cfg(test)]
thread_local! {
    static FAIL_AT: std::cell::Cell<Option<&'static str>> = const { std::cell::Cell::new(None) };
}

fn setup_checkpoint(_point: &str) -> anyhow::Result<()> {
    #[cfg(test)]
    if FAIL_AT.with(|point| point.get() == Some(_point)) {
        return Err(io::Error::from_raw_os_error(libc::EIO).into());
    }
    Ok(())
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RootMarker {
    schema: u32,
    identity: String,
    owner_uid: u32,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct EntryMarker {
    schema: u32,
    identity: String,
    owner_uid: u32,
    catalog_hash: String,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StageMarker {
    schema: u32,
    identity: String,
    owner_uid: u32,
}

/// Exclusive ownership of one runtime cache root.
///
/// The OS lock remains held until this value and every borrowed [`CacheStage`] are dropped.
pub struct CacheGuard {
    root_path: PathBuf,
    _root: File,
    _lock: File,
    entries: File,
    staging: File,
}

impl CacheGuard {
    /// Open or initialize an absolute canonical cache root and try its exclusive OS lock.
    /// Returns `Ok(None)` only when another process owns the lock.
    pub fn try_acquire(root: &Path) -> anyhow::Result<Option<Self>> {
        let (root_path, root) = open_or_create_root(root)?;
        validate_directory(&root, "cache root")?;
        // Serialize initialization before creating a control file. This avoids
        // a new-owner race between creating cache.lock and locking that file.
        if unsafe { libc::flock(root.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
                return Ok(None);
            }
            return Err(error).context("could not acquire cache directory lock");
        }
        let initial_names = directory_names(root.as_raw_fd())?;
        let empty_root = initial_names.is_empty();
        ensure!(
            empty_root
                || initial_names
                    .iter()
                    .any(|name| name == ROOT_MARKER.as_bytes()),
            "refusing nonempty unmarked cache root"
        );
        let mut created_lock = false;
        let lock = match openat_file(
            root.as_raw_fd(),
            LOCK_FILE.as_bytes(),
            libc::O_RDWR
                | libc::O_NONBLOCK
                | libc::O_NOFOLLOW
                | libc::O_CLOEXEC
                | if empty_root {
                    libc::O_CREAT | libc::O_EXCL
                } else {
                    0
                },
            PRIVATE_FILE,
        ) {
            Ok(created) => {
                created_lock = empty_root;
                if created_lock
                    && unsafe { libc::fchmod(created.as_raw_fd(), PRIVATE_FILE as libc::mode_t) }
                        != 0
                {
                    let error = io::Error::last_os_error();
                    remove_created_object(root.as_raw_fd(), LOCK_FILE, &created, false)?;
                    return Err(error).context("could not set cache lock mode");
                }
                created
            }
            Err(error)
                if error
                    .downcast_ref::<io::Error>()
                    .and_then(io::Error::raw_os_error)
                    == Some(libc::EEXIST) =>
            {
                openat_file(
                    root.as_raw_fd(),
                    LOCK_FILE.as_bytes(),
                    libc::O_RDWR | libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )?
            }
            Err(error) => return Err(error).context("could not create cache lock"),
        };
        let initialization = (|| {
            validate_regular(&lock, "cache lock", PRIVATE_FILE)?;
            ensure!(lock.metadata()?.len() == 0, "cache lock file must be empty");
            // Both locks are nonblocking. EINTR is an error, never a retry.
            if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() == Some(libc::EWOULDBLOCK) {
                    return Ok(None);
                }
                return Err(error).context("could not acquire cache lock");
            }
            if !created_lock {
                ensure!(
                    directory_names(root.as_raw_fd())?
                        .iter()
                        .any(|name| name == ROOT_MARKER.as_bytes()),
                    "refusing nonempty unmarked cache root"
                );
            }
            initialize_or_validate(&root).map(Some)
        })();
        let (entries, staging) = match initialization {
            Ok(Some(directories)) => directories,
            Ok(None) => {
                if created_lock {
                    remove_created_object(root.as_raw_fd(), LOCK_FILE, &lock, false)?;
                }
                return Ok(None);
            }
            Err(error) => {
                if created_lock {
                    remove_created_object(root.as_raw_fd(), LOCK_FILE, &lock, false)
                        .context("cache initialization failed and owned lock cleanup failed")?;
                }
                return Err(error);
            }
        };
        Ok(Some(Self {
            root_path,
            _root: root,
            _lock: lock,
            entries,
            staging,
        }))
    }

    /// Return an entry directory after checking its private marker and catalog key.
    /// This is structural validation only; it does not validate any payload hashes.
    pub fn lookup_entry(&self, catalog_hash: &str) -> anyhow::Result<Option<PathBuf>> {
        validate_key(catalog_hash)?;
        let name = catalog_hash.as_bytes();
        match openat_dir(self.entries.as_raw_fd(), name) {
            Ok(entry) => {
                validate_directory(&entry, "cache entry")?;
                validate_entry_marker(&entry, catalog_hash)?;
                Ok(Some(self.root_path.join(ENTRIES_DIR).join(catalog_hash)))
            }
            Err(error) if error.raw_os_error() == Some(libc::ENOENT) => Ok(None),
            Err(error) => Err(error).context("could not open cache entry"),
        }
    }

    /// Enumerate validated cache keys without adopting, pruning, or mutating entries.
    pub(crate) fn entry_keys(&self) -> anyhow::Result<Vec<String>> {
        let names = directory_names_at_most(self.entries.as_raw_fd(), 128)?;
        let mut keys = Vec::with_capacity(names.len());
        for name in names {
            let key = std::str::from_utf8(&name).context("cache entry name is not UTF-8")?;
            validate_key(key)?;
            let entry = openat_dir(self.entries.as_raw_fd(), &name)?;
            validate_directory(&entry, "cache entry")?;
            validate_entry_marker(&entry, key)?;
            keys.push(key.to_owned());
        }
        keys.sort();
        Ok(keys)
    }

    /// Create a private temporary stage owned by this guard.
    pub fn create_stage(&self) -> anyhow::Result<CacheStage<'_>> {
        let mut random = [0u8; 16];
        File::open("/dev/urandom")?.read_exact(&mut random)?;
        let name = format!(
            "stage-{}",
            random
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
        );
        let name_c = cstring(name.as_bytes())?;
        if unsafe {
            libc::mkdirat(
                self.staging.as_raw_fd(),
                name_c.as_ptr(),
                PRIVATE_DIR as libc::mode_t,
            )
        } != 0
        {
            return Err(io::Error::last_os_error()).context("could not create private cache stage");
        }
        let setup = (|| {
            setup_checkpoint("stage-setup")?;
            let directory = openat_dir(self.staging.as_raw_fd(), name.as_bytes())?;
            if unsafe { libc::fchmod(directory.as_raw_fd(), PRIVATE_DIR as libc::mode_t) } != 0 {
                return Err(io::Error::last_os_error()).context("could not set cache stage mode");
            }
            let metadata = directory.metadata()?;
            Ok((directory, metadata))
        })();
        let (directory, metadata) = match setup {
            Ok(prepared) => prepared,
            Err(error) => {
                remove_new_empty_directory(self.staging.as_raw_fd(), &name)
                    .context("cache stage setup failed and cleanup failed")?;
                return Err(error);
            }
        };
        let path = self.root_path.join(STAGING_DIR).join(&name);
        let stage = CacheStage {
            guard: self,
            path,
            name: name.into(),
            directory,
            device: metadata.dev(),
            inode: metadata.ino(),
            armed: true,
        };
        if let Err(error) = write_new_json(
            stage.directory.as_raw_fd(),
            STAGE_MARKER,
            &StageMarker {
                schema: 1,
                identity: IDENTITY.to_owned(),
                owner_uid: current_uid(),
            },
        ) {
            stage
                .abort()
                .context("stage marker creation failed and owned stage cleanup failed")?;
            return Err(error);
        }
        Ok(stage)
    }
}

/// A private staging directory whose borrow keeps its cache lock alive.
pub struct CacheStage<'guard> {
    guard: &'guard CacheGuard,
    path: PathBuf,
    name: std::ffi::OsString,
    directory: File,
    device: u64,
    inode: u64,
    armed: bool,
}

impl CacheStage<'_> {
    /// Filesystem path where the caller may prepare an entry payload.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Explicitly remove this stage, reporting cleanup errors.
    pub fn abort(mut self) -> anyhow::Result<()> {
        self.cleanup()?;
        self.armed = false;
        Ok(())
    }

    /// Atomically publish this stage under its exact catalog hash, without replacing an entry.
    pub fn promote(self, catalog_hash: &str) -> anyhow::Result<PathBuf> {
        match self.try_promote(catalog_hash) {
            Ok(path) => Ok(path),
            Err((stage, error)) => {
                drop(stage);
                Err(error)
            }
        }
    }

    /// Like [`Self::promote`], but returns ownership of the stage on failure so callers can
    /// explicitly abort and surface cleanup errors.
    pub(crate) fn try_promote(
        mut self,
        catalog_hash: &str,
    ) -> Result<PathBuf, (Self, anyhow::Error)> {
        let result = self.promote_inner(catalog_hash);
        match result {
            Ok(path) => {
                self.armed = false;
                Ok(path)
            }
            Err(error) => Err((self, error)),
        }
    }

    fn promote_inner(&mut self, catalog_hash: &str) -> anyhow::Result<PathBuf> {
        validate_key(catalog_hash)?;
        validate_directory(&self.directory, "cache stage")?;
        self.ensure_name_is_ours()?;
        write_new_json(
            self.directory.as_raw_fd(),
            ENTRY_MARKER,
            &EntryMarker {
                schema: 1,
                identity: IDENTITY.to_owned(),
                owner_uid: current_uid(),
                catalog_hash: catalog_hash.to_owned(),
            },
        )?;
        self.ensure_name_is_ours()?;
        let from = cstring(self.name.as_bytes())?;
        let to = cstring(catalog_hash.as_bytes())?;
        rename_noreplace(
            self.guard.staging.as_raw_fd(),
            &from,
            self.guard.entries.as_raw_fd(),
            &to,
        )
        .context("could not publish cache entry")?;
        Ok(self.guard.root_path.join(ENTRIES_DIR).join(catalog_hash))
    }

    fn cleanup(&mut self) -> anyhow::Result<()> {
        self.ensure_name_is_ours()?;
        remove_contents(self.directory.as_raw_fd())?;
        self.ensure_name_is_ours()?;
        let name = cstring(self.name.as_bytes())?;
        if unsafe {
            libc::unlinkat(
                self.guard.staging.as_raw_fd(),
                name.as_ptr(),
                libc::AT_REMOVEDIR,
            )
        } != 0
        {
            return Err(io::Error::last_os_error()).context("could not remove owned cache stage");
        }
        Ok(())
    }

    fn ensure_name_is_ours(&self) -> anyhow::Result<()> {
        let found = statat(self.guard.staging.as_raw_fd(), self.name.as_bytes())?;
        ensure!(
            found.dev() == self.device && found.ino() == self.inode && found.is_dir(),
            "cache stage path was replaced; preserving replacement"
        );
        Ok(())
    }
}

impl Drop for CacheStage<'_> {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.cleanup();
            self.armed = false;
        }
    }
}

fn open_or_create_root(path: &Path) -> anyhow::Result<(PathBuf, File)> {
    ensure!(path.is_absolute(), "cache root path must be absolute");
    ensure!(
        path.components()
            .all(|part| matches!(part, Component::RootDir | Component::Normal(_))),
        "cache root path must not contain traversal components"
    );
    let root_path = path.to_path_buf();
    let mut parent = open_root_dir()?;
    let parts: Vec<_> = path.components().skip(1).collect();
    ensure!(!parts.is_empty(), "filesystem root cannot be a cache root");
    for component in &parts[..parts.len() - 1] {
        let Component::Normal(name) = component else {
            bail!("invalid cache root component")
        };
        parent = openat_dir(parent.as_raw_fd(), name.as_bytes())?;
    }
    validate_safe_parent(&parent)?;
    let parent_path = path
        .parent()
        .ok_or_else(|| anyhow!("cache root has no parent"))?;
    let canonical_parent = parent_path.canonicalize()?;
    ensure!(
        canonical_parent == parent_path,
        "cache root parent is not canonical"
    );
    let Component::Normal(name) = parts[parts.len() - 1] else {
        bail!("invalid cache root name")
    };
    ensure!(
        canonical_parent.join(name) == path,
        "cache root path is not canonical"
    );
    match openat_dir(parent.as_raw_fd(), name.as_bytes()) {
        Ok(root) => {
            ensure!(
                path.canonicalize()? == path,
                "cache root path is not canonical"
            );
            Ok((root_path, root))
        }
        Err(error) if error.raw_os_error() == Some(libc::ENOENT) => {
            let name = cstring(name.as_bytes())?;
            let created = if unsafe {
                libc::mkdirat(
                    parent.as_raw_fd(),
                    name.as_ptr(),
                    PRIVATE_DIR as libc::mode_t,
                )
            } == 0
            {
                true
            } else {
                let create_error = io::Error::last_os_error();
                if create_error.raw_os_error() != Some(libc::EEXIST) {
                    return Err(create_error).context("could not create cache root");
                }
                false
            };
            let root = openat_dir(parent.as_raw_fd(), name.as_bytes())?;
            if created
                && unsafe { libc::fchmod(root.as_raw_fd(), PRIVATE_DIR as libc::mode_t) } != 0
            {
                return Err(io::Error::last_os_error()).context("could not set cache root mode");
            }
            Ok((root_path, root))
        }
        Err(error) => Err(error).context("could not open cache root without following symlinks"),
    }
}

fn initialize_or_validate(root: &File) -> anyhow::Result<(File, File)> {
    let names = directory_names(root.as_raw_fd())?;
    let marker_exists = names.iter().any(|name| name == ROOT_MARKER.as_bytes());
    if !marker_exists {
        ensure!(
            names.iter().all(|name| name == LOCK_FILE.as_bytes()),
            "refusing nonempty unmarked cache root"
        );
        // Control files and directories are created exclusively; caller data is never replaced.
        let mut entries = None;
        let mut staging = None;
        let result = (|| {
            entries = Some(create_directory(root.as_raw_fd(), ENTRIES_DIR)?);
            setup_checkpoint("root-staging")?;
            staging = Some(create_directory(root.as_raw_fd(), STAGING_DIR)?);
            write_new_json(
                root.as_raw_fd(),
                ROOT_MARKER,
                &RootMarker {
                    schema: 1,
                    identity: IDENTITY.to_owned(),
                    owner_uid: current_uid(),
                },
            )
        })();
        if let Err(error) = result {
            let mut cleanup_failed = false;
            for (name, directory) in [(STAGING_DIR, &staging), (ENTRIES_DIR, &entries)] {
                if let Some(directory) = directory {
                    cleanup_failed |=
                        remove_created_object(root.as_raw_fd(), name, directory, true).is_err();
                }
            }
            ensure!(
                !cleanup_failed,
                "cache initialization failed and directory cleanup failed"
            );
            return Err(error);
        }
        return Ok((entries.unwrap(), staging.unwrap()));
    }
    validate_root_marker(root.as_raw_fd())?;
    let mut allowed = vec![
        ROOT_MARKER.as_bytes().to_vec(),
        LOCK_FILE.as_bytes().to_vec(),
        ENTRIES_DIR.as_bytes().to_vec(),
        STAGING_DIR.as_bytes().to_vec(),
    ];
    allowed.sort();
    ensure!(
        names == allowed,
        "cache root contains unknown or missing top-level entries"
    );
    let lock = openat_file(
        root.as_raw_fd(),
        LOCK_FILE.as_bytes(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    )?;
    validate_regular(&lock, "cache lock", PRIVATE_FILE)?;
    ensure!(lock.metadata()?.len() == 0, "cache lock file must be empty");
    let entries = openat_dir(root.as_raw_fd(), ENTRIES_DIR.as_bytes())?;
    let staging = openat_dir(root.as_raw_fd(), STAGING_DIR.as_bytes())?;
    validate_directory(&entries, "cache entries directory")?;
    validate_directory(&staging, "cache staging directory")?;
    validate_existing_entries(&entries)?;
    validate_existing_stages(&staging)?;
    Ok((entries, staging))
}

fn validate_root_marker(root: RawFd) -> anyhow::Result<()> {
    let marker = read_json::<RootMarker>(root, ROOT_MARKER)?;
    ensure!(
        marker.schema == 1 && marker.identity == IDENTITY && marker.owner_uid == current_uid(),
        "invalid cache root marker"
    );
    Ok(())
}

fn validate_existing_entries(entries: &File) -> anyhow::Result<()> {
    for name in directory_names(entries.as_raw_fd())? {
        let key = std::str::from_utf8(&name).context("cache entry name is not UTF-8")?;
        validate_key(key)?;
        let entry = openat_dir(entries.as_raw_fd(), &name)?;
        validate_directory(&entry, "cache entry")?;
        validate_entry_marker(&entry, key)?;
    }
    Ok(())
}

fn validate_existing_stages(staging: &File) -> anyhow::Result<()> {
    for name in directory_names(staging.as_raw_fd())? {
        let stage = openat_dir(staging.as_raw_fd(), &name)?;
        validate_directory(&stage, "cache stage")?;
        let marker: StageMarker = read_json(stage.as_raw_fd(), STAGE_MARKER)?;
        ensure!(
            marker.schema == 1 && marker.identity == IDENTITY && marker.owner_uid == current_uid(),
            "invalid cache stage marker"
        );
    }
    Ok(())
}

fn validate_entry_marker(entry: &File, key: &str) -> anyhow::Result<()> {
    let marker = read_json::<EntryMarker>(entry.as_raw_fd(), ENTRY_MARKER)?;
    ensure!(
        marker.schema == 1
            && marker.identity == IDENTITY
            && marker.owner_uid == current_uid()
            && marker.catalog_hash == key,
        "invalid cache entry marker"
    );
    Ok(())
}

fn validate_key(key: &str) -> anyhow::Result<()> {
    ensure!(
        key.len() == 64
            && key
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "catalog hash must be exactly 64 lowercase hexadecimal characters"
    );
    Ok(())
}

fn create_directory(parent: RawFd, name: &str) -> anyhow::Result<File> {
    let name_c = cstring(name.as_bytes())?;
    if unsafe { libc::mkdirat(parent, name_c.as_ptr(), PRIVATE_DIR as libc::mode_t) } != 0 {
        return Err(io::Error::last_os_error()).with_context(|| format!("could not create {name}"));
    }
    let result = (|| {
        let dir = openat_dir(parent, name.as_bytes())?;
        if unsafe { libc::fchmod(dir.as_raw_fd(), PRIVATE_DIR as libc::mode_t) } != 0 {
            return Err(io::Error::last_os_error()).context("could not set private directory mode");
        }
        validate_directory(&dir, name)?;
        Ok(dir)
    })();
    if result.is_err() {
        remove_new_empty_directory(parent, name)
            .context("private directory setup failed and cleanup failed")?;
    }
    result
}

// Setup has not exposed this new empty directory to a caller. The private parent
// and cache lock exclude other legitimate writers; hostile same-UID mutation is
// outside this boundary. unlinkat never follows a substituted symlink.
fn remove_new_empty_directory(parent: RawFd, name: &str) -> anyhow::Result<()> {
    let name = cstring(name.as_bytes())?;
    if unsafe { libc::unlinkat(parent, name.as_ptr(), libc::AT_REMOVEDIR) } != 0 {
        return Err(io::Error::last_os_error()).context("could not roll back new empty directory");
    }
    Ok(())
}

fn remove_created_object(
    parent: RawFd,
    name: &str,
    held: &File,
    directory: bool,
) -> anyhow::Result<()> {
    let name = cstring(name.as_bytes())?;
    let expected = held.metadata()?;
    let mut found: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstatat(parent, name.as_ptr(), &mut found, libc::AT_SYMLINK_NOFOLLOW) } != 0 {
        return Err(io::Error::last_os_error()).context("could not inspect owned setup object");
    }
    ensure!(
        found.st_dev as u64 == expected.dev() && found.st_ino as u64 == expected.ino(),
        "cache setup path was replaced; preserving replacement"
    );
    let flags = if directory { libc::AT_REMOVEDIR } else { 0 };
    if unsafe { libc::unlinkat(parent, name.as_ptr(), flags) } != 0 {
        return Err(io::Error::last_os_error()).context("could not remove owned setup object");
    }
    Ok(())
}

fn write_new_json<T: Serialize>(directory: RawFd, name: &str, value: &T) -> anyhow::Result<()> {
    let bytes = serde_json::to_vec(value)?;
    ensure!(bytes.len() <= MAX_MARKER, "cache marker exceeds size limit");
    let file = openat_file(
        directory,
        name.as_bytes(),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        PRIVATE_FILE,
    )?;
    let result = (|| {
        if unsafe { libc::fchmod(file.as_raw_fd(), PRIVATE_FILE as libc::mode_t) } != 0 {
            return Err(io::Error::last_os_error()).context("could not set private marker mode");
        }
        setup_checkpoint(name)?;
        let mut writer = &file;
        writer.write_all(&bytes)?;
        writer.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        remove_created_object(directory, name, &file, false)
            .context("cache marker write failed and cleanup failed")?;
    }
    result
}

fn read_json<T: for<'de> Deserialize<'de>>(directory: RawFd, name: &str) -> anyhow::Result<T> {
    let file = openat_file(
        directory,
        name.as_bytes(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        0,
    )?;
    validate_regular(&file, "cache marker", PRIVATE_FILE)?;
    ensure!(
        file.metadata()?.len() <= MAX_MARKER as u64,
        "cache marker exceeds size limit"
    );
    let mut bytes = Vec::new();
    file.take((MAX_MARKER + 1) as u64).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= MAX_MARKER, "cache marker exceeds size limit");
    serde_json::from_slice(&bytes).context("invalid cache marker JSON")
}

fn validate_safe_parent(file: &File) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_dir() && metadata.uid() == current_uid() && metadata.mode() & 0o022 == 0,
        "cache root parent must be an owned directory not writable by other users"
    );
    Ok(())
}

fn validate_directory(file: &File, label: &str) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_dir()
            && metadata.uid() == current_uid()
            && metadata.mode() & 0o7777 == PRIVATE_DIR,
        "{label} must be an owned mode-0700 directory (uid {}, mode {:o}, is_dir {})",
        metadata.uid(),
        metadata.mode() & 0o7777,
        metadata.is_dir()
    );
    Ok(())
}

fn validate_regular(file: &File, label: &str, mode: u32) -> anyhow::Result<()> {
    let metadata = file.metadata()?;
    ensure!(
        metadata.is_file() && metadata.uid() == current_uid() && metadata.mode() & 0o7777 == mode,
        "{label} must be an owned mode-{mode:o} regular file"
    );
    Ok(())
}

fn open_root_dir() -> anyhow::Result<File> {
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

fn openat_file(parent: RawFd, name: &[u8], flags: i32, mode: u32) -> anyhow::Result<File> {
    let name = cstring(name)?;
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            flags,
            mode as libc::mode_t as libc::c_uint,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn openat_dir(parent: RawFd, name: &[u8]) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn cstring(value: &[u8]) -> anyhow::Result<CString> {
    CString::new(value).context("filesystem name contains NUL")
}

fn current_uid() -> u32 {
    unsafe { libc::getuid() }
}

fn statat(parent: RawFd, name: &[u8]) -> anyhow::Result<fs::Metadata> {
    let c_name = cstring(name)?;
    let fd = unsafe {
        libc::openat(
            parent,
            c_name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error().into());
    }
    let file = unsafe { File::from_raw_fd(fd) };
    Ok(file.metadata()?)
}

fn directory_names(fd: RawFd) -> anyhow::Result<Vec<Vec<u8>>> {
    directory_names_at_most(fd, usize::MAX)
}

fn directory_names_at_most(fd: RawFd, maximum: usize) -> anyhow::Result<Vec<Vec<u8>>> {
    // dup would share the directory offset and make a later validation see EOF.
    let duplicate = openat_dir(fd, b".")?.into_raw_fd();
    let stream = unsafe { libc::fdopendir(duplicate) };
    if stream.is_null() {
        unsafe {
            libc::close(duplicate);
        }
        return Err(io::Error::last_os_error().into());
    }
    let result = (|| -> io::Result<Vec<Vec<u8>>> {
        let mut names = Vec::new();
        loop {
            unsafe {
                *errno_pointer() = 0;
            }
            let entry = unsafe { libc::readdir(stream) };
            if entry.is_null() {
                let error = io::Error::last_os_error();
                if error.raw_os_error() == Some(0) {
                    break;
                }
                return Err(error);
            }
            let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
            if name != b"." && name != b".." {
                if names.len() == maximum {
                    return Err(io::Error::other("directory contains too many entries"));
                }
                names.push(name.to_vec());
            }
        }
        names.sort();
        Ok(names)
    })();
    let close_result = unsafe { libc::closedir(stream) };
    let names = result?;
    ensure!(close_result == 0, "could not close cache directory stream");
    Ok(names)
}

fn remove_contents(fd: RawFd) -> anyhow::Result<()> {
    for name in directory_names(fd)? {
        let c_name = cstring(&name)?;
        match openat_dir(fd, &name) {
            Ok(child) => {
                remove_contents(child.as_raw_fd())?;
                if unsafe { libc::unlinkat(fd, c_name.as_ptr(), libc::AT_REMOVEDIR) } != 0 {
                    return Err(io::Error::last_os_error())
                        .context("could not remove stage directory");
                }
            }
            Err(error)
                if error.raw_os_error() == Some(libc::ENOTDIR)
                    || error.raw_os_error() == Some(libc::ELOOP) =>
            {
                if unsafe { libc::unlinkat(fd, c_name.as_ptr(), 0) } != 0 {
                    return Err(io::Error::last_os_error()).context("could not remove stage file");
                }
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn rename_noreplace(old_dir: RawFd, old: &CStr, new_dir: RawFd, new: &CStr) -> io::Result<()> {
    #[cfg(target_os = "linux")]
    let result = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            old_dir,
            old.as_ptr(),
            new_dir,
            new.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    #[cfg(target_os = "macos")]
    let result = unsafe {
        renameatx_np(
            old_dir,
            old.as_ptr(),
            new_dir,
            new.as_ptr(),
            libc::RENAME_EXCL,
        ) as libc::c_long
    };
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    let result: libc::c_long = {
        let _ = (old_dir, old, new_dir, new);
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "atomic no-replace directory rename is unsupported",
        ));
    };
    if result != 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(target_os = "macos")]
unsafe extern "C" {
    fn renameatx_np(
        fromfd: RawFd,
        from: *const libc::c_char,
        tofd: RawFd,
        to: *const libc::c_char,
        flags: u32,
    ) -> libc::c_int;
}

#[cfg(target_os = "linux")]
const _: () = {
    assert!(libc::RENAME_NOREPLACE == 1);
};

#[cfg(target_os = "macos")]
fn errno_pointer() -> *mut libc::c_int {
    unsafe { libc::__error() }
}
#[cfg(target_os = "linux")]
fn errno_pointer() -> *mut libc::c_int {
    unsafe { libc::__errno_location() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::fs::symlink;
    use tempfile::TempDir;

    fn private_tempdir() -> TempDir {
        tempfile::Builder::new()
            .permissions(fs::Permissions::from_mode(0o700))
            .tempdir()
            .unwrap()
    }

    fn canonical_parent(parent: &TempDir) -> PathBuf {
        parent.path().canonicalize().unwrap()
    }

    fn private_root(parent: &TempDir) -> PathBuf {
        let path = canonical_parent(parent).join("cache");
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    const KEY: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    #[test]
    fn initializes_and_holds_exclusive_lock_until_guard_drop() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        assert!(CacheGuard::try_acquire(&root).unwrap().is_none());
        drop(guard);
        assert!(CacheGuard::try_acquire(&root).unwrap().is_some());
    }

    #[test]
    fn simultaneous_initialization_has_one_lock_owner() {
        let parent = private_tempdir();
        let root = canonical_parent(&parent).join("racing-cache");
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
        let (tx, rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let release_rx = std::sync::Arc::new(std::sync::Mutex::new(release_rx));
        let mut workers = Vec::new();
        for _ in 0..2 {
            let root = root.clone();
            let barrier = barrier.clone();
            let tx = tx.clone();
            let release_rx = release_rx.clone();
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                let acquired = CacheGuard::try_acquire(&root).unwrap();
                tx.send(acquired.is_some()).unwrap();
                if let Some(guard) = acquired {
                    let _guard = guard;
                    release_rx.lock().unwrap().recv().unwrap();
                }
            }));
        }
        drop(tx);
        barrier.wait();
        let first = rx.recv().unwrap();
        let second = rx.recv().unwrap();
        assert_ne!(first, second);
        release_tx.send(()).unwrap();
        for worker in workers {
            worker.join().unwrap();
        }
    }

    #[test]
    fn initializes_missing_root_and_refuses_nonempty_unmarked_root() {
        let parent = private_tempdir();
        let root = canonical_parent(&parent).join("new-cache");
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        drop(guard);
        let other = canonical_parent(&parent).join("caller-cache");
        fs::create_dir(&other).unwrap();
        fs::set_permissions(&other, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(other.join("caller-data"), "keep").unwrap();
        assert!(CacheGuard::try_acquire(&other).is_err());
        assert_eq!(fs::read(other.join("caller-data")).unwrap(), b"keep");
        let names: Vec<_> = fs::read_dir(&other)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from("caller-data")]);
    }

    #[test]
    fn refuses_insecure_root_symlink_and_unknown_top_level_entry() {
        let parent = private_tempdir();
        let insecure = canonical_parent(&parent).join("loose");
        fs::create_dir(&insecure).unwrap();
        fs::set_permissions(&insecure, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(CacheGuard::try_acquire(&insecure).is_err());
        let link = canonical_parent(&parent).join("linked");
        symlink(&insecure, &link).unwrap();
        assert!(CacheGuard::try_acquire(&link).is_err());
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        drop(guard);
        fs::write(root.join("unknown"), "keep").unwrap();
        assert!(CacheGuard::try_acquire(&root).is_err());
        assert_eq!(fs::read(root.join("unknown")).unwrap(), b"keep");
    }

    #[test]
    fn refuses_unknown_or_malformed_private_marker() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        drop(CacheGuard::try_acquire(&root).unwrap().unwrap());
        fs::write(
            root.join(ROOT_MARKER),
            br#"{"schema":1,"identity":"ashnazg-runtime-cache","owner_uid":0,"extra":true}"#,
        )
        .unwrap();
        assert!(CacheGuard::try_acquire(&root).is_err());
    }

    #[test]
    fn abort_and_drop_remove_private_owned_stage() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let stage = guard.create_stage().unwrap();
        let path = stage.path().to_path_buf();
        fs::write(path.join("partial"), "data").unwrap();
        stage.abort().unwrap();
        assert!(!path.exists());
        let stage = guard.create_stage().unwrap();
        let path = stage.path().to_path_buf();
        drop(stage);
        assert!(!path.exists());
    }

    #[test]
    fn stage_cleanup_preserves_replacement_directory() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let stage = guard.create_stage().unwrap();
        let path = stage.path().to_path_buf();
        let original = path.with_extension("original");
        fs::rename(&path, &original).unwrap();
        fs::create_dir(&path).unwrap();
        fs::write(path.join("keep"), "replacement").unwrap();
        drop(stage);
        assert_eq!(fs::read(path.join("keep")).unwrap(), b"replacement");
        fs::remove_dir_all(original).unwrap();
    }

    #[test]
    fn explicit_abort_reports_replacement_and_preserves_it() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let stage = guard.create_stage().unwrap();
        let path = stage.path().to_path_buf();
        let original = path.with_extension("moved");
        fs::rename(&path, &original).unwrap();
        fs::create_dir(&path).unwrap();
        fs::write(path.join("keep"), "replacement").unwrap();
        assert!(stage.abort().is_err());
        assert_eq!(fs::read(path.join("keep")).unwrap(), b"replacement");
        fs::remove_dir_all(original).unwrap();
    }

    #[test]
    fn rejects_noncanonical_or_bad_keys() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        for key in ["", "../", &"A".repeat(64), &"0".repeat(63)] {
            assert!(guard.lookup_entry(key).is_err());
        }
    }

    #[test]
    fn promotion_is_noreplace_and_lookup_checks_marker_not_payload_hashes() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let first = guard.create_stage().unwrap();
        fs::write(first.path().join("opaque-payload"), "first").unwrap();
        let entry = first.promote(KEY).unwrap();
        assert_eq!(fs::read(entry.join("opaque-payload")).unwrap(), b"first");
        assert_eq!(guard.lookup_entry(KEY).unwrap().unwrap(), entry);
        let second = guard.create_stage().unwrap();
        fs::write(second.path().join("opaque-payload"), "second").unwrap();
        assert!(second.promote(KEY).is_err());
        assert_eq!(fs::read(entry.join("opaque-payload")).unwrap(), b"first");
    }

    #[test]
    fn recoverable_promotion_error_returns_owned_stage_for_explicit_cleanup() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let first = guard.create_stage().unwrap();
        fs::write(first.path().join("opaque-payload"), "first").unwrap();
        let entry = first.promote(KEY).unwrap();
        let second = guard.create_stage().unwrap();
        let stage_path = second.path().to_path_buf();
        fs::write(stage_path.join("opaque-payload"), "second").unwrap();
        let (second, error) = second.try_promote(KEY).unwrap_err();
        assert!(error.to_string().contains("publish cache entry"));
        second.abort().unwrap();
        assert!(!stage_path.exists());
        assert_eq!(fs::read(entry.join("opaque-payload")).unwrap(), b"first");
    }

    #[test]
    fn entry_keys_enumerates_valid_owned_entries() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let entry = guard.create_stage().unwrap().promote(KEY).unwrap();
        assert_eq!(guard.entry_keys().unwrap(), vec![KEY.to_owned()]);
        assert!(entry.join(ENTRY_MARKER).is_file());
        assert!(entry.join(STAGE_MARKER).is_file());
    }

    #[test]
    fn lookup_rejects_entry_marker_bound_to_another_key() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let entry = guard.create_stage().unwrap().promote(KEY).unwrap();
        fs::write(
            entry.join(ENTRY_MARKER),
            serde_json::to_vec(&EntryMarker {
                schema: 1,
                identity: IDENTITY.to_owned(),
                owner_uid: current_uid(),
                catalog_hash: "f".repeat(64),
            })
            .unwrap(),
        )
        .unwrap();
        assert!(guard.lookup_entry(KEY).is_err());
    }

    #[test]
    fn stale_stages_are_validated_but_never_adopted_or_pruned() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        let stage = guard.create_stage().unwrap();
        let path = stage.path().to_path_buf();
        std::mem::forget(stage);
        drop(guard);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        assert!(path.exists());
        drop(guard);
        assert!(path.exists());
    }

    #[test]
    fn failed_root_setup_rolls_back_control_files_and_can_be_retried() {
        for point in ["root-staging", ROOT_MARKER] {
            let parent = private_tempdir();
            let root = private_root(&parent);
            FAIL_AT.with(|failure| failure.set(Some(point)));
            let result = CacheGuard::try_acquire(&root);
            FAIL_AT.with(|failure| failure.set(None));
            assert!(result.is_err());
            assert!(
                fs::read_dir(&root).unwrap().next().is_none(),
                "left partial state after {point}"
            );
            assert!(CacheGuard::try_acquire(&root).unwrap().is_some());
        }
    }

    #[test]
    fn failed_stage_setup_leaves_no_unmarked_stage_and_cache_can_reopen() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        FAIL_AT.with(|failure| failure.set(Some("stage-setup")));
        let result = guard.create_stage();
        FAIL_AT.with(|failure| failure.set(None));
        assert!(result.is_err());
        drop(result);
        assert!(
            fs::read_dir(root.join(STAGING_DIR))
                .unwrap()
                .next()
                .is_none()
        );
        drop(guard);
        assert!(CacheGuard::try_acquire(&root).unwrap().is_some());
    }

    #[test]
    fn failed_stage_marker_write_explicitly_cleans_owned_stage() {
        let parent = private_tempdir();
        let root = private_root(&parent);
        let guard = CacheGuard::try_acquire(&root).unwrap().unwrap();
        FAIL_AT.with(|failure| failure.set(Some(STAGE_MARKER)));
        let result = guard.create_stage();
        FAIL_AT.with(|failure| failure.set(None));
        assert!(result.is_err());
        assert!(
            fs::read_dir(root.join(STAGING_DIR))
                .unwrap()
                .next()
                .is_none()
        );
    }
}
