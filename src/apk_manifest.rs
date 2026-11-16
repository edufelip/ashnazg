//! Bounded access to the manifest member of an already-open APK.
use anyhow::{Result, anyhow, bail};
use flate2::read::DeflateDecoder;
use rawzip::{CompressionMethod, MAX_CENTRAL_DIRECTORY_RECORD_SIZE, ReaderAt, ZipLocator};
use std::{fs::File, io::Read, time::Instant};
use tokio_util::sync::CancellationToken;

const MAX_FILE: u64 = 128 * 1024 * 1024;
const MAX_CDIR: u64 = 4 * 1024 * 1024;
const MAX_ENTRIES: u64 = 16_384;
const MAX_IO: u64 = 32 * 1024 * 1024;
const MAX_OUTPUT: u64 = 4 * 1024 * 1024;
const MAX_READ: usize = 64 * 1024;
const MAX_CALLS: u32 = 100_000;
const TARGET: &[u8] = b"AndroidManifest.xml";

fn check(deadline: Instant, cancel: &CancellationToken) -> Result<()> {
    if cancel.is_cancelled() || Instant::now() >= deadline {
        bail!("APK manifest read failed")
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Identity {
    dev: u64,
    ino: u64,
    size: u64,
    mt: i64,
    mn: i64,
    ct: i64,
    cn: i64,
}
#[cfg(unix)]
fn identity(f: &File) -> Result<Identity> {
    use std::os::unix::fs::MetadataExt;
    let m = f.metadata()?;
    Ok(Identity {
        dev: m.dev(),
        ino: m.ino(),
        size: m.len(),
        mt: m.mtime(),
        mn: m.mtime_nsec(),
        ct: m.ctime(),
        cn: m.ctime_nsec(),
    })
}
#[cfg(not(unix))]
fn identity(f: &File) -> Result<Identity> {
    let m = f.metadata()?;
    let t = m
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| anyhow!("APK manifest read failed"))?;
    Ok(Identity {
        dev: 0,
        ino: 0,
        size: m.len(),
        mt: t.as_secs() as i64,
        mn: t.subsec_nanos() as i64,
        ct: 0,
        cn: 0,
    })
}

struct Bounded<'a> {
    file: &'a File,
    length: u64,
    deadline: Instant,
    cancel: &'a CancellationToken,
    bytes: std::cell::Cell<u64>,
    calls: std::cell::Cell<u32>,
}
impl ReaderAt for Bounded<'_> {
    fn read_at(&self, out: &mut [u8], offset: u64) -> std::io::Result<usize> {
        let fail = || std::io::Error::other("bounded read failed");
        if check(self.deadline, self.cancel).is_err() || offset >= self.length || out.is_empty() {
            return if out.is_empty() { Ok(0) } else { Err(fail()) };
        }
        let n = out.len().min(MAX_READ).min((self.length - offset) as usize);
        let calls = self.calls.get().saturating_add(1);
        let bytes = self.bytes.get().saturating_add(n as u64);
        if calls > MAX_CALLS || bytes > MAX_IO {
            return Err(fail());
        }
        self.calls.set(calls);
        self.bytes.set(bytes);
        #[cfg(unix)]
        {
            use std::os::unix::fs::FileExt;
            self.file.read_at(&mut out[..n], offset)
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::FileExt;
            self.file.seek_read(&mut out[..n], offset)
        }
        #[cfg(not(any(unix, windows)))]
        {
            Err(fail())
        }
    }
}

/// Reads and validates the exact root AndroidManifest.xml member from an open APK descriptor.
pub fn read_android_manifest(
    file: &File,
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<Vec<u8>> {
    read_android_manifest_inner(file, deadline, cancel, |_| {})
}

fn read_android_manifest_inner(
    file: &File,
    deadline: Instant,
    cancel: &CancellationToken,
    before_payload: impl FnOnce(&File),
) -> Result<Vec<u8>> {
    check(deadline, cancel)?;
    let before = identity(file).map_err(|_| anyhow!("APK manifest read failed"))?;
    if !file
        .metadata()
        .map_err(|_| anyhow!("APK manifest read failed"))?
        .is_file()
        || !(1..=MAX_FILE).contains(&before.size)
    {
        bail!("APK manifest read failed")
    }
    let io = Bounded {
        file,
        length: before.size,
        deadline,
        cancel,
        bytes: std::cell::Cell::new(0),
        calls: std::cell::Cell::new(0),
    };
    let mut scratch = vec![0; MAX_CENTRAL_DIRECTORY_RECORD_SIZE];
    let archive = ZipLocator::new()
        .max_search_space(65_535 + 22)
        .locate_in_reader(&io, &mut scratch, before.size)
        .map_err(|_| anyhow!("APK manifest read failed"))?;
    let cd = archive.directory_offset();
    let eo = archive.eocd_offset();
    if archive.end_offset() != before.size || cd > eo || eo > before.size || eo - cd > MAX_CDIR {
        bail!("APK manifest read failed")
    }
    let hint = archive.entries_hint();
    if !(1..=MAX_ENTRIES).contains(&hint) {
        bail!("APK manifest read failed")
    }
    let mut entries = archive.entries(&mut scratch);
    let mut count = 0u64;
    let mut found = None;
    while let Some(e) = entries
        .next_entry()
        .map_err(|_| anyhow!("APK manifest read failed"))?
    {
        check(deadline, cancel)?;
        count += 1;
        if count > MAX_ENTRIES {
            bail!("APK manifest read failed")
        }
        if e.file_path().as_ref() == TARGET {
            if found.is_some() {
                bail!("APK manifest read failed")
            }
            found = Some((
                e.wayfinder(),
                e.compression_method(),
                e.flags(),
                e.compressed_size_hint(),
                e.uncompressed_size_hint(),
                e.crc32(),
                e.local_header_offset(),
            ));
        }
    }
    if count != hint {
        bail!("APK manifest read failed")
    }
    let (way, method, flags, compressed, uncompressed, crc, local_offset) =
        found.ok_or_else(|| anyhow!("APK manifest read failed"))?;
    if flags.is_encrypted()
        || uncompressed == 0
        || uncompressed > MAX_OUTPUT
        || local_offset >= cd
        || !matches!(
            method,
            CompressionMethod::STORE | CompressionMethod::DEFLATE
        )
    {
        bail!("APK manifest read failed")
    }
    let entry = archive
        .get_entry(way)
        .map_err(|_| anyhow!("APK manifest read failed"))?;
    let mut local_buf = vec![0u8; u16::MAX as usize * 2];
    let local = entry
        .local_header(&mut local_buf)
        .map_err(|_| anyhow!("APK manifest read failed"))?;
    if local.file_path().as_ref() != TARGET
        || local.flags() != flags
        || local.compression_method() != method
    {
        bail!("APK manifest read failed")
    }
    let (start, end) = entry.compressed_data_range();
    if end < start || end > cd || end > before.size || end - start != compressed {
        bail!("APK manifest read failed")
    }
    before_payload(file);
    let compressed_reader = entry.reader();
    let mut out = Vec::with_capacity(uncompressed as usize);
    let mut buf = [0u8; 8192];
    if method == CompressionMethod::STORE {
        let mut verified = entry.verifying_reader(compressed_reader);
        loop {
            check(deadline, cancel)?;
            let n = verified
                .read(&mut buf)
                .map_err(|_| anyhow!("APK manifest read failed"))?;
            if n == 0 {
                break;
            }
            if out.len().saturating_add(n) > MAX_OUTPUT as usize {
                bail!("APK manifest read failed")
            }
            out.extend_from_slice(&buf[..n]);
        }
    } else {
        let mut decoder = DeflateDecoder::new(compressed_reader);
        {
            let mut verified = entry.verifying_reader(&mut decoder);
            loop {
                check(deadline, cancel)?;
                let n = verified
                    .read(&mut buf)
                    .map_err(|_| anyhow!("APK manifest read failed"))?;
                if n == 0 {
                    break;
                }
                if out.len().saturating_add(n) > MAX_OUTPUT as usize {
                    bail!("APK manifest read failed")
                }
                out.extend_from_slice(&buf[..n]);
            }
        }
        if decoder.total_in() != compressed {
            bail!("APK manifest read failed")
        }
    }
    if out.len() as u64 != uncompressed {
        bail!("APK manifest read failed")
    }
    let _ = crc;
    check(deadline, cancel)?;
    if identity(file).map_err(|_| anyhow!("APK manifest read failed"))? != before {
        bail!("APK manifest read failed")
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::{Compression, write::DeflateEncoder};
    use std::{io::Write, time::Duration};
    use tempfile::NamedTempFile;

    fn zip(payload: &[u8], deflate: bool, tail: &[u8]) -> Vec<u8> {
        let compressed = if deflate {
            let mut e = DeflateEncoder::new(Vec::new(), Compression::default());
            e.write_all(payload).unwrap();
            let mut v = e.finish().unwrap();
            v.extend_from_slice(tail);
            v
        } else {
            payload.to_vec()
        };
        let method = if deflate { 8u16 } else { 0 };
        let crc = rawzip::crc32(payload);
        let mut b = Vec::new();
        b.extend_from_slice(&0x04034b50u32.to_le_bytes());
        b.extend_from_slice(&20u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&method.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&crc.to_le_bytes());
        b.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
        b.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        b.extend_from_slice(&(TARGET.len() as u16).to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(TARGET);
        b.extend_from_slice(&compressed);
        let cd = b.len() as u32;
        b.extend_from_slice(&0x02014b50u32.to_le_bytes());
        b.extend_from_slice(&20u16.to_le_bytes());
        b.extend_from_slice(&20u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&method.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&crc.to_le_bytes());
        b.extend_from_slice(&(compressed.len() as u32).to_le_bytes());
        b.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        b.extend_from_slice(&(TARGET.len() as u16).to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(&0u32.to_le_bytes());
        b.extend_from_slice(TARGET);
        let cds = b.len() as u32 - cd;
        b.extend_from_slice(&0x06054b50u32.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b.extend_from_slice(&1u16.to_le_bytes());
        b.extend_from_slice(&1u16.to_le_bytes());
        b.extend_from_slice(&cds.to_le_bytes());
        b.extend_from_slice(&cd.to_le_bytes());
        b.extend_from_slice(&0u16.to_le_bytes());
        b
    }

    fn duplicate_zip(payload: &[u8]) -> Vec<u8> {
        let single = zip(payload, false, &[]);
        let old_eocd = single.len() - 22;
        let cd =
            u32::from_le_bytes(single[old_eocd + 16..old_eocd + 20].try_into().unwrap()) as usize;
        let local = &single[..cd];
        let central = &single[cd..old_eocd];
        let mut both = Vec::new();
        both.extend_from_slice(local);
        both.extend_from_slice(local);
        let new_cd = both.len() as u32;
        both.extend_from_slice(central);
        let mut second = central.to_vec();
        second[42..46].copy_from_slice(&(local.len() as u32).to_le_bytes());
        both.extend_from_slice(&second);
        let cd_size = both.len() as u32 - new_cd;
        both.extend_from_slice(&0x06054b50u32.to_le_bytes());
        both.extend_from_slice(&0u16.to_le_bytes());
        both.extend_from_slice(&0u16.to_le_bytes());
        both.extend_from_slice(&2u16.to_le_bytes());
        both.extend_from_slice(&2u16.to_le_bytes());
        both.extend_from_slice(&cd_size.to_le_bytes());
        both.extend_from_slice(&new_cd.to_le_bytes());
        both.extend_from_slice(&0u16.to_le_bytes());
        both
    }
    fn read(bytes: &[u8]) -> Result<Vec<u8>> {
        let mut f = NamedTempFile::new()?;
        f.write_all(bytes)?;
        read_android_manifest(
            f.as_file(),
            Instant::now() + Duration::from_secs(2),
            &CancellationToken::new(),
        )
    }

    #[test]
    fn reads_stored_and_deflated_and_actual_compiled_manifests() {
        let fixture = include_bytes!("../rust-tests/fixtures/apk-manifests/native.axml");
        assert_eq!(
            read(&zip(b"binary xml", false, &[])).unwrap(),
            b"binary xml"
        );
        assert_eq!(read(&zip(b"binary xml", true, &[])).unwrap(), b"binary xml");
        for data in [
            include_bytes!("../rust-tests/fixtures/apk-manifests/native.axml").as_slice(),
            include_bytes!("../rust-tests/fixtures/apk-manifests/compose.axml").as_slice(),
            include_bytes!("../rust-tests/fixtures/apk-manifests/reactnative.axml").as_slice(),
        ] {
            assert_eq!(read(&zip(data, false, &[])).unwrap(), data);
        }
        assert!(!fixture.is_empty());
    }
    #[test]
    fn rejects_trailing_deflate_bytes_and_cancelled_work() {
        assert!(read(&zip(b"binary xml", true, b"trailing")).is_err());
        let f = NamedTempFile::new().unwrap();
        let token = CancellationToken::new();
        token.cancel();
        assert!(
            read_android_manifest(f.as_file(), Instant::now() + Duration::from_secs(1), &token)
                .is_err()
        );
    }

    #[test]
    fn descriptor_position_is_unchanged_and_open_descriptor_survives_path_replace() {
        use std::io::{Seek, SeekFrom};
        let mut f = NamedTempFile::new().unwrap();
        let valid = zip(b"payload", false, &[]);
        f.write_all(&valid).unwrap();
        f.as_file().seek(SeekFrom::Start(3)).unwrap();
        let original = f.path().to_owned();
        let replacement = f.path().with_extension("replacement");
        std::fs::write(&replacement, b"not the original descriptor").unwrap();
        std::fs::rename(&replacement, &original).unwrap();
        assert_eq!(
            read_android_manifest(
                f.as_file(),
                Instant::now() + Duration::from_secs(2),
                &CancellationToken::new()
            )
            .unwrap(),
            b"payload"
        );
        assert_eq!(f.as_file().stream_position().unwrap(), 3);
    }

    #[test]
    fn rejects_corrupted_crc_and_expired_deadline() {
        let mut corrupted = zip(b"payload", false, &[]);
        corrupted[30 + TARGET.len()] = b'P';
        assert!(read(&corrupted).is_err());
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(&zip(b"payload", false, &[])).unwrap();
        assert!(
            read_android_manifest(f.as_file(), Instant::now(), &CancellationToken::new()).is_err()
        );
    }

    #[test]
    fn rejects_missing_encrypted_truncated_and_malformed_archive_metadata() {
        let mut missing = zip(b"payload", false, &[]);
        let cd = u32::from_le_bytes(
            missing[missing.len() - 6..missing.len() - 2]
                .try_into()
                .unwrap(),
        ) as usize;
        missing[30] = b"X"[0];
        missing[cd + 46] = b"X"[0];
        assert!(read(&missing).is_err());

        let mut encrypted = zip(b"payload", false, &[]);
        let cd = u32::from_le_bytes(
            encrypted[encrypted.len() - 6..encrypted.len() - 2]
                .try_into()
                .unwrap(),
        ) as usize;
        encrypted[6] = 1;
        encrypted[cd + 8] = 1;
        assert!(read(&encrypted).is_err());

        let mut bad_count = zip(b"payload", false, &[]);
        let eocd = bad_count.len() - 22;
        bad_count[eocd + 8..eocd + 12].copy_from_slice(&0u32.to_le_bytes());
        assert!(read(&bad_count).is_err());
        let mut too_many = zip(b"payload", false, &[]);
        let eocd = too_many.len() - 22;
        too_many[eocd + 8..eocd + 12].copy_from_slice(&16_385u32.to_le_bytes());
        assert!(read(&too_many).is_err());
        let mut bad_range = zip(b"payload", false, &[]);
        let eocd = bad_range.len() - 22;
        bad_range[eocd + 16..eocd + 20].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(read(&bad_range).is_err());
        let truncated = zip(b"payload", false, &[]);
        assert!(read(&truncated[..truncated.len() - 10]).is_err());
        assert!(read(&duplicate_zip(b"payload")).is_err());
    }

    #[test]
    fn rejects_advertised_manifest_output_over_cap() {
        let mut bytes = zip(b"payload", false, &[]);
        let cd = u32::from_le_bytes(bytes[bytes.len() - 6..bytes.len() - 2].try_into().unwrap())
            as usize;
        bytes[22..26].copy_from_slice(&((MAX_OUTPUT as u32) + 1).to_le_bytes());
        bytes[cd + 24..cd + 28].copy_from_slice(&((MAX_OUTPUT as u32) + 1).to_le_bytes());
        assert!(read(&bytes).is_err());
    }

    #[test]
    fn deterministic_in_place_mutation_checkpoint_refuses_success() {
        use std::os::unix::fs::FileExt;
        let mut f = NamedTempFile::new().unwrap();
        f.write_all(&zip(b"payload", false, &[])).unwrap();
        let position = 30 + TARGET.len() as u64;
        let changed = b"P";
        let result = read_android_manifest_inner(
            f.as_file(),
            Instant::now() + Duration::from_secs(2),
            &CancellationToken::new(),
            |file| {
                file.write_at(changed, position).unwrap();
            },
        );
        assert!(result.is_err());
    }
}
