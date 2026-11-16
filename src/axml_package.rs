//! Narrow package inference from compiled Android binary XML.

use anyhow::{Result, bail};
use std::time::Instant;
use tokio_util::sync::CancellationToken;

const MAX_INPUT: usize = 4 * 1024 * 1024;
const MAX_CHUNKS: usize = 65_536;
const MAX_DEPTH: usize = 256;
const MAX_ATTRIBUTES: usize = 4096;
const NO_INDEX: u32 = u32::MAX;

/// Extracts the package attribute from one compiled Android binary XML document.
///
/// This deliberately validates framing and only strings referenced by the small
/// subset of AXML used here. It is an inference helper, not Android's full parser.
pub(crate) fn package_name(
    bytes: &[u8],
    deadline: Instant,
    cancel: &CancellationToken,
) -> Result<String> {
    package_name_with_checkpoint(bytes, deadline, cancel, |_, _| {})
}

fn package_name_with_checkpoint(
    bytes: &[u8],
    deadline: Instant,
    cancel: &CancellationToken,
    mut checkpoint: impl FnMut(usize, &mut Work<'_>),
) -> Result<String> {
    let mut work = Work {
        deadline,
        cancel,
        remaining: MAX_INPUT * 2,
    };
    work.check()?;
    if bytes.len() < 8 || bytes.len() > MAX_INPUT {
        bail!("invalid binary XML size");
    }
    let kind = u16_at(bytes, 0)?;
    let header_size = usize::from(u16_at(bytes, 2)?);
    let document_size = usize::try_from(u32_at(bytes, 4)?)
        .map_err(|_| anyhow::anyhow!("invalid binary XML size"))?;
    if kind != 0x0003 || header_size != 8 || document_size != bytes.len() {
        bail!("invalid binary XML header");
    }

    let mut cursor = header_size;
    let mut pool = None;
    let mut root_seen = false;
    let mut root_closed = false;
    let mut package: Option<String> = None;
    let mut resource_map_seen = false;
    let mut nodes_started = false;
    let mut tags: Vec<(u32, u32)> = Vec::new();
    let mut namespaces: Vec<NamespaceScope> = Vec::new();
    let mut chunks = 0usize;

    while cursor < bytes.len() {
        work.check()?;
        chunks += 1;
        if chunks > MAX_CHUNKS {
            bail!("too many binary XML chunks");
        }
        let (chunk_type, chunk_header, chunk_size, end) = chunk(bytes, cursor)?;
        let body = cursor
            .checked_add(chunk_header)
            .ok_or_else(|| anyhow::anyhow!("invalid chunk bounds"))?;
        match chunk_type {
            0x0001 => {
                if pool.is_some() || chunks != 1 {
                    bail!("invalid string pool placement");
                }
                pool = Some(StringPool::new(bytes, cursor, chunk_header, chunk_size)?);
            }
            0x0180 => {
                if nodes_started
                    || resource_map_seen
                    || chunk_header != 8
                    || chunk_size < chunk_header
                    || (chunk_size - chunk_header) % 4 != 0
                {
                    bail!("invalid resource map");
                }
                resource_map_seen = true;
            }
            0x0100 | 0x0101 => {
                nodes_started = true;
                let strings = pool
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing string pool"))?;
                if chunk_header != 16 || chunk_size != 24 {
                    bail!("invalid namespace chunk");
                }
                let prefix = u32_at(bytes, body)?;
                let uri = u32_at(bytes, body + 4)?;
                if prefix != NO_INDEX {
                    strings.validate_string(prefix, &mut work)?;
                }
                if uri != NO_INDEX {
                    strings.validate_string(uri, &mut work)?;
                }
                if chunk_type == 0x0100 {
                    if root_closed
                        || namespaces
                            .last()
                            .is_some_and(|scope| scope.depth == tags.len() && scope.owner_started)
                    {
                        bail!("invalid namespace scope");
                    }
                    if namespaces.len() >= MAX_DEPTH {
                        bail!("namespace nesting limit exceeded");
                    }
                    namespaces.push(NamespaceScope {
                        prefix,
                        uri,
                        depth: tags.len(),
                        owner_started: false,
                    });
                } else {
                    let scope = namespaces
                        .pop()
                        .ok_or_else(|| anyhow::anyhow!("mismatched namespace end"))?;
                    if scope.prefix != prefix
                        || scope.uri != uri
                        || scope.depth != tags.len()
                        || !scope.owner_started
                    {
                        bail!("mismatched namespace end");
                    }
                }
            }
            0x0102 => {
                nodes_started = true;
                let strings = pool
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing string pool"))?;
                if root_closed
                    || chunk_header != 16
                    || chunk_size < 36
                    || namespaces
                        .last()
                        .is_some_and(|scope| scope.depth == tags.len() && scope.owner_started)
                {
                    bail!("invalid start element");
                }
                let ns = u32_at(bytes, body)?;
                let name = u32_at(bytes, body + 4)?;
                let attribute_start = usize::from(u16_at(bytes, body + 8)?);
                let attribute_size = usize::from(u16_at(bytes, body + 10)?);
                let attribute_count = usize::from(u16_at(bytes, body + 12)?);
                if ns != NO_INDEX {
                    strings.validate_index(ns)?;
                }
                strings.validate_index(name)?;
                strings.validate_string(name, &mut work)?;
                if attribute_count > MAX_ATTRIBUTES || attribute_size < 20 {
                    bail!("invalid attribute table");
                }
                let ext = body;
                let attrs = ext
                    .checked_add(attribute_start)
                    .ok_or_else(|| anyhow::anyhow!("invalid attribute bounds"))?;
                if attribute_start < 20 || attrs > end {
                    bail!("invalid attribute start");
                }
                let attr_bytes = attribute_count
                    .checked_mul(attribute_size)
                    .ok_or_else(|| anyhow::anyhow!("invalid attribute bounds"))?;
                let attrs_end = attrs
                    .checked_add(attr_bytes)
                    .ok_or_else(|| anyhow::anyhow!("invalid attribute bounds"))?;
                if attrs_end > end {
                    bail!("attribute table exceeds chunk");
                }
                if tags.len() >= MAX_DEPTH {
                    bail!("element nesting limit exceeded");
                }
                if tags.is_empty() {
                    if root_seen
                        || ns != NO_INDEX
                        || !strings.equals(name, "manifest", &mut work)?
                    {
                        bail!("invalid document root");
                    }
                    root_seen = true;
                    let mut found = false;
                    let mut root_package = None;
                    for i in 0..attribute_count {
                        work.check()?;
                        let at = attrs + i * attribute_size;
                        let ans = u32_at(bytes, at)?;
                        let aname = u32_at(bytes, at + 4)?;
                        let raw = u32_at(bytes, at + 8)?;
                        if ans != NO_INDEX {
                            strings.validate_index(ans)?;
                        }
                        strings.validate_index(aname)?;
                        strings.validate_string(aname, &mut work)?;
                        if raw != NO_INDEX {
                            strings.validate_index(raw)?;
                        }
                        let value_size = u16_at(bytes, at + 12)?;
                        let value_type = bytes[at + 15];
                        let value = u32_at(bytes, at + 16)?;
                        if value_size < 8 || bytes[at + 14] != 0 {
                            bail!("invalid typed attribute value");
                        }
                        if value_type == 3 {
                            strings.validate_index(value)?;
                        }
                        if ans == NO_INDEX && strings.equals(aname, "package", &mut work)? {
                            if found {
                                bail!("duplicate package attribute");
                            }
                            found = true;
                            if value_type != 3 {
                                bail!("package attribute is not a string");
                            }
                            let typed = strings.package_string(value, &mut work)?;
                            let selected = if raw == NO_INDEX {
                                typed
                            } else {
                                let raw_value = strings.package_string(raw, &mut work)?;
                                if raw_value != typed {
                                    bail!("conflicting package attribute values");
                                }
                                typed
                            };
                            if !crate::native_install::valid_package(&selected) {
                                bail!("invalid package attribute");
                            }
                            root_package = Some(selected);
                        }
                    }
                    package = Some(
                        root_package.ok_or_else(|| anyhow::anyhow!("missing package attribute"))?,
                    );
                } else {
                    for i in 0..attribute_count {
                        work.check()?;
                        let at = attrs + i * attribute_size;
                        let ans = u32_at(bytes, at)?;
                        let aname = u32_at(bytes, at + 4)?;
                        let raw = u32_at(bytes, at + 8)?;
                        if ans != NO_INDEX {
                            strings.validate_index(ans)?;
                        }
                        strings.validate_index(aname)?;
                        strings.validate_string(aname, &mut work)?;
                        if raw != NO_INDEX {
                            strings.validate_index(raw)?;
                        }
                        let value_type = bytes[at + 15];
                        if value_type == 3 {
                            strings.validate_index(u32_at(bytes, at + 16)?)?;
                        }
                    }
                }
                for scope in namespaces.iter_mut().rev() {
                    if scope.depth != tags.len() {
                        break;
                    }
                    scope.owner_started = true;
                }
                tags.push((ns, name));
            }
            0x0103 => {
                nodes_started = true;
                let strings = pool
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing string pool"))?;
                if chunk_header != 16
                    || chunk_size != 24
                    || tags.is_empty()
                    || namespaces
                        .last()
                        .is_some_and(|scope| scope.depth >= tags.len())
                {
                    bail!("invalid end element");
                }
                let ns = u32_at(bytes, body)?;
                let name = u32_at(bytes, body + 4)?;
                if ns != NO_INDEX {
                    strings.validate_index(ns)?;
                }
                strings.validate_index(name)?;
                strings.validate_string(name, &mut work)?;
                if tags.pop() != Some((ns, name)) {
                    bail!("mismatched end element");
                }
                if tags.is_empty() {
                    root_closed = true;
                }
            }
            0x0104 => {
                nodes_started = true;
                let strings = pool
                    .as_ref()
                    .ok_or_else(|| anyhow::anyhow!("missing string pool"))?;
                if tags.is_empty()
                    || chunk_header != 16
                    || chunk_size != 28
                    || namespaces
                        .last()
                        .is_some_and(|scope| scope.depth >= tags.len())
                {
                    bail!("invalid CDATA chunk");
                }
                let data = u32_at(bytes, body)?;
                strings.validate_string(data, &mut work)?;
                if u16_at(bytes, body + 4)? < 8 || bytes[body + 6] != 0 {
                    bail!("invalid CDATA value");
                }
                if bytes[body + 7] == 3 {
                    strings.validate_index(u32_at(bytes, body + 8)?)?;
                }
            }
            _ => bail!("unsupported binary XML chunk"),
        }
        if end <= cursor {
            bail!("binary XML made no progress");
        }
        cursor = end;
        checkpoint(chunks, &mut work);
    }
    work.check()?;
    if !root_seen || !root_closed || !tags.is_empty() || !namespaces.is_empty() {
        bail!("incomplete binary XML document");
    }
    package.ok_or_else(|| anyhow::anyhow!("missing package attribute"))
}

struct NamespaceScope {
    prefix: u32,
    uri: u32,
    depth: usize,
    owner_started: bool,
}

struct Work<'a> {
    deadline: Instant,
    cancel: &'a CancellationToken,
    remaining: usize,
}
impl Work<'_> {
    fn check(&self) -> Result<()> {
        if self.cancel.is_cancelled() || Instant::now() >= self.deadline {
            bail!("binary XML operation cancelled or expired");
        }
        Ok(())
    }
    fn charge(&mut self, amount: usize) -> Result<()> {
        self.check()?;
        self.remaining = self
            .remaining
            .checked_sub(amount)
            .ok_or_else(|| anyhow::anyhow!("binary XML string work limit exceeded"))?;
        Ok(())
    }
}

struct StringPool<'a> {
    bytes: &'a [u8],
    offsets: &'a [u8],
    count: usize,
    strings_start: usize,
    strings_end: usize,
    utf8: bool,
}
impl<'a> StringPool<'a> {
    fn new(bytes: &'a [u8], start: usize, header: usize, size: usize) -> Result<Self> {
        if header != 28 || size < header {
            bail!("invalid string pool header");
        }
        let count = usize::try_from(u32_at(bytes, start + 8)?)
            .map_err(|_| anyhow::anyhow!("invalid string pool count"))?;
        let styles = usize::try_from(u32_at(bytes, start + 12)?)
            .map_err(|_| anyhow::anyhow!("invalid string pool count"))?;
        let flags = u32_at(bytes, start + 16)?;
        let strings_start = usize::try_from(u32_at(bytes, start + 20)?)
            .map_err(|_| anyhow::anyhow!("invalid string pool offset"))?;
        let styles_start = usize::try_from(u32_at(bytes, start + 24)?)
            .map_err(|_| anyhow::anyhow!("invalid string pool offset"))?;
        let table_count = count
            .checked_add(styles)
            .ok_or_else(|| anyhow::anyhow!("invalid string pool count"))?;
        let table_bytes = table_count
            .checked_mul(4)
            .ok_or_else(|| anyhow::anyhow!("invalid string pool count"))?;
        let table_end = header
            .checked_add(table_bytes)
            .ok_or_else(|| anyhow::anyhow!("invalid string pool offsets"))?;
        if table_end > size
            || strings_start < table_end
            || strings_start > size
            || (styles != 0 && (styles_start < strings_start || styles_start > size))
            || (styles == 0 && styles_start != 0)
        {
            bail!("invalid string pool bounds");
        }
        let strings_end = if styles == 0 { size } else { styles_start };
        Ok(Self {
            bytes,
            offsets: &bytes[start + header..start + header + count * 4],
            count,
            strings_start: start + strings_start,
            strings_end: start + strings_end,
            utf8: flags & 0x100 != 0,
        })
    }
    fn validate_index(&self, index: u32) -> Result<()> {
        if usize::try_from(index).ok().is_none_or(|i| i >= self.count) {
            bail!("invalid string index");
        }
        Ok(())
    }
    fn item(&self, index: u32) -> Result<(usize, usize)> {
        self.validate_index(index)?;
        let off = usize::try_from(u32_at(self.offsets, index as usize * 4)?)
            .map_err(|_| anyhow::anyhow!("invalid string offset"))?;
        let at = self
            .strings_start
            .checked_add(off)
            .ok_or_else(|| anyhow::anyhow!("invalid string offset"))?;
        if at < self.strings_start || at >= self.strings_end {
            bail!("invalid string offset");
        }
        Ok((at, self.strings_end))
    }
    fn validate_string(&self, index: u32, work: &mut Work<'_>) -> Result<()> {
        let (at, limit) = self.item(index)?;
        if self.utf8 {
            let (units, p) = length8(self.bytes, at, limit)?;
            let (len, p) = length8(self.bytes, p, limit)?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid string length"))?;
            if end >= limit || self.bytes[end] != 0 {
                bail!("invalid UTF-8 string terminator");
            }
            work.charge(len)?;
            let value = std::str::from_utf8(&self.bytes[p..end])
                .map_err(|_| anyhow::anyhow!("invalid UTF-8 string"))?;
            if value.encode_utf16().count() != units {
                bail!("invalid UTF-8 string length");
            }
            work.check()?;
        } else {
            let (units, p) = length16(self.bytes, at, limit)?;
            let len = units
                .checked_mul(2)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            if end.checked_add(2).is_none_or(|term| term > limit) || u16_at(self.bytes, end)? != 0 {
                bail!("invalid UTF-16 string terminator");
            }
            work.charge(len)?;
            let (units, remainder) = self.bytes[p..end].as_chunks::<2>();
            if !remainder.is_empty() {
                bail!("invalid UTF-16 string length");
            }
            for (i, decoded) in
                char::decode_utf16(units.iter().map(|unit| u16::from_le_bytes(*unit))).enumerate()
            {
                if i % 128 == 0 {
                    work.check()?;
                }
                decoded.map_err(|_| anyhow::anyhow!("invalid UTF-16 string"))?;
            }
            work.check()?;
        }
        Ok(())
    }
    fn equals(&self, index: u32, expected: &str, work: &mut Work<'_>) -> Result<bool> {
        let (at, limit) = self.item(index)?;
        if self.utf8 {
            let (units, p) = length8(self.bytes, at, limit)?;
            let (len, p) = length8(self.bytes, p, limit)?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid string length"))?;
            if end >= limit || self.bytes[end] != 0 {
                bail!("invalid UTF-8 string terminator");
            }
            work.charge(len)?;
            let value = std::str::from_utf8(&self.bytes[p..end])
                .map_err(|_| anyhow::anyhow!("invalid UTF-8 string"))?;
            if value.encode_utf16().count() != units {
                bail!("invalid UTF-8 string length");
            }
            work.check()?;
            Ok(value == expected)
        } else {
            let (units, p) = length16(self.bytes, at, limit)?;
            let len = units
                .checked_mul(2)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            if end.checked_add(2).is_none_or(|term| term > limit) || u16_at(self.bytes, end)? != 0 {
                bail!("invalid UTF-16 string terminator");
            }
            work.charge(len)?;
            let mut chars = expected.chars();
            let mut equal = true;
            let (units, remainder) = self.bytes[p..end].as_chunks::<2>();
            if !remainder.is_empty() {
                bail!("invalid UTF-16 string length");
            }
            for (i, decoded) in
                char::decode_utf16(units.iter().map(|unit| u16::from_le_bytes(*unit))).enumerate()
            {
                if i % 128 == 0 {
                    work.check()?;
                }
                match decoded {
                    Ok(c) => {
                        if chars.next() != Some(c) {
                            equal = false;
                        }
                    }
                    Err(_) => bail!("invalid UTF-16 string"),
                }
            }
            if chars.next().is_some() {
                equal = false;
            }
            work.check()?;
            Ok(equal)
        }
    }
    fn package_string(&self, index: u32, work: &mut Work<'_>) -> Result<String> {
        let (at, limit) = self.item(index)?;
        let mut value = String::new();
        if self.utf8 {
            let (units, p) = length8(self.bytes, at, limit)?;
            let (len, p) = length8(self.bytes, p, limit)?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid string length"))?;
            if end >= limit || self.bytes[end] != 0 {
                bail!("invalid UTF-8 string terminator");
            }
            work.charge(len)?;
            let text = std::str::from_utf8(&self.bytes[p..end])
                .map_err(|_| anyhow::anyhow!("invalid UTF-8 string"))?;
            if text.encode_utf16().count() != units {
                bail!("invalid UTF-8 string length");
            }
            if text.len() > 255 || !text.is_ascii() {
                bail!("invalid package string");
            }
            value.push_str(text);
        } else {
            let (units, p) = length16(self.bytes, at, limit)?;
            if units > 255 {
                bail!("invalid package string");
            }
            let len = units
                .checked_mul(2)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            let end = p
                .checked_add(len)
                .ok_or_else(|| anyhow::anyhow!("invalid UTF-16 string length"))?;
            if end.checked_add(2).is_none_or(|term| term > limit) || u16_at(self.bytes, end)? != 0 {
                bail!("invalid UTF-16 string terminator");
            }
            work.charge(len)?;
            let (units, remainder) = self.bytes[p..end].as_chunks::<2>();
            if !remainder.is_empty() {
                bail!("invalid UTF-16 string length");
            }
            for [a, b] in units {
                let u = u16::from_le_bytes([*a, *b]);
                let c = char::decode_utf16(std::iter::once(u))
                    .next()
                    .unwrap()
                    .map_err(|_| anyhow::anyhow!("invalid UTF-16 package string"))?;
                if !c.is_ascii() {
                    bail!("invalid package string");
                }
                value.push(c);
            }
        }
        Ok(value)
    }
}

fn length8(bytes: &[u8], at: usize, limit: usize) -> Result<(usize, usize)> {
    let first = *bytes
        .get(at)
        .filter(|_| at < limit)
        .ok_or_else(|| anyhow::anyhow!("truncated string length"))?;
    if first & 0x80 == 0 {
        return Ok((usize::from(first), at + 1));
    }
    let second_at = at
        .checked_add(1)
        .ok_or_else(|| anyhow::anyhow!("invalid string length"))?;
    let second = *bytes
        .get(second_at)
        .filter(|_| second_at < limit)
        .ok_or_else(|| anyhow::anyhow!("truncated string length"))?;
    Ok((
        ((usize::from(first & 0x7f)) << 8) | usize::from(second),
        at + 2,
    ))
}
fn length16(bytes: &[u8], at: usize, limit: usize) -> Result<(usize, usize)> {
    if at.checked_add(2).is_none_or(|end| end > limit) {
        bail!("truncated string length");
    }
    let first = usize::from(u16_at(bytes, at)?);
    if first & 0x8000 == 0 {
        return Ok((first, at + 2));
    }
    let second_at = at
        .checked_add(2)
        .ok_or_else(|| anyhow::anyhow!("invalid string length"))?;
    if second_at.checked_add(2).is_none_or(|end| end > limit) {
        bail!("truncated string length");
    }
    let second = usize::from(u16_at(bytes, second_at)?);
    Ok((((first & 0x7fff) << 16) | second, at + 4))
}
fn chunk(bytes: &[u8], at: usize) -> Result<(u16, usize, usize, usize)> {
    let kind = u16_at(bytes, at)?;
    let header = usize::from(u16_at(bytes, at + 2)?);
    let size = usize::try_from(u32_at(bytes, at + 4)?)
        .map_err(|_| anyhow::anyhow!("invalid chunk size"))?;
    let end = at
        .checked_add(size)
        .ok_or_else(|| anyhow::anyhow!("invalid chunk bounds"))?;
    if header < 8 || size < header || end > bytes.len() {
        bail!("invalid chunk bounds");
    }
    Ok((kind, header, size, end))
}
fn u16_at(bytes: &[u8], at: usize) -> Result<u16> {
    let slice = bytes
        .get(
            at..at
                .checked_add(2)
                .ok_or_else(|| anyhow::anyhow!("invalid offset"))?,
        )
        .ok_or_else(|| anyhow::anyhow!("truncated binary XML"))?;
    Ok(u16::from_le_bytes([slice[0], slice[1]]))
}
fn u32_at(bytes: &[u8], at: usize) -> Result<u32> {
    let slice = bytes
        .get(
            at..at
                .checked_add(4)
                .ok_or_else(|| anyhow::anyhow!("invalid offset"))?,
        )
        .ok_or_else(|| anyhow::anyhow!("truncated binary XML"))?;
    Ok(u32::from_le_bytes([slice[0], slice[1], slice[2], slice[3]]))
}

#[cfg(test)]
mod tests {
    use super::{MAX_ATTRIBUTES, MAX_CHUNKS, MAX_INPUT, NO_INDEX, package_name};
    use std::time::{Duration, Instant};
    use tokio_util::sync::CancellationToken;

    const FIXTURE_DIR: &str = "rust-tests/fixtures/apk-manifests";

    fn parse(bytes: &[u8]) -> anyhow::Result<String> {
        package_name(
            bytes,
            Instant::now() + Duration::from_secs(2),
            &CancellationToken::new(),
        )
    }

    #[test]
    fn extracts_packages_from_compiled_apk_manifests() {
        for (file, expected) in [
            ("native.axml", "dev.ashnazg.fixture"),
            ("compose.axml", "com.ashnazg.fixture.compose"),
            ("reactnative.axml", "com.ashnazg.fixture.reactnative"),
        ] {
            let bytes = std::fs::read(format!("{FIXTURE_DIR}/{file}")).unwrap();
            assert_eq!(parse(&bytes).unwrap(), expected, "{file}");
        }
    }

    #[test]
    fn rejects_text_xml_and_framing_errors() {
        assert!(parse(br#"<manifest package="com.example.app"/>"#).is_err());
        assert!(parse(&[]).is_err());
        assert!(parse(&[3, 0, 8, 0, 0, 0, 0, 0]).is_err());
    }

    #[test]
    fn checks_cancellation_and_deadline_before_work() {
        let token = CancellationToken::new();
        token.cancel();
        assert!(package_name(b"", Instant::now() + Duration::from_secs(1), &token).is_err());
        assert!(
            package_name(
                b"",
                Instant::now() - Duration::from_millis(1),
                &CancellationToken::new()
            )
            .is_err()
        );
    }

    #[test]
    fn missing_or_invalid_package_values_are_rejected() {
        assert!(parse(&compiled(true, "manifest", &[], 1)).is_err());
        for value in [
            "",
            "single",
            "com..app",
            "com.1app",
            "com.app-evil",
            "com.app\0evil",
            "com.é",
        ] {
            assert!(parse(&compiled(true, "manifest", &[package_attr(value)], 1)).is_err());
        }
        let long = format!("com.{}", "a".repeat(252));
        assert!(parse(&compiled(true, "manifest", &[package_attr(&long)], 1)).is_err());
    }

    #[test]
    fn cancellation_and_expiry_during_scan_and_before_success_are_rejected() {
        let bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        // Three chunks: string pool, root start, root end. Check both mid-scan
        // interruption and interruption after the last chunk has been parsed.
        for at in [1, 3] {
            for expire in [false, true] {
                let token = CancellationToken::new();
                let mut observed = false;
                let result = super::package_name_with_checkpoint(
                    &bytes,
                    Instant::now() + Duration::from_secs(2),
                    &token,
                    |chunk, work| {
                        if chunk == at {
                            observed = true;
                            if expire {
                                work.deadline = Instant::now();
                            } else {
                                token.cancel();
                            }
                        }
                    },
                );
                assert!(observed);
                assert!(result.is_err());
            }
        }
    }

    #[test]
    fn errors_do_not_include_untrusted_input() {
        let bytes = b"secret-untrusted-package-data";
        let error = parse(bytes).unwrap_err().to_string();
        assert!(!error.contains("secret-untrusted-package-data"));
    }

    #[derive(Clone)]
    struct Attribute {
        name: String,
        namespace: bool,
        raw: Option<String>,
        typed: Option<(u8, String)>,
    }

    fn compiled(utf8: bool, root: &str, attrs: &[Attribute], depth: usize) -> Vec<u8> {
        let mut strings = vec![root.to_owned(), "package".to_owned(), "item".to_owned()];
        for attr in attrs {
            strings.push(attr.name.clone());
            if let Some(value) = &attr.raw {
                strings.push(value.clone());
            }
            if let Some((3, value)) = &attr.typed {
                strings.push(value.clone());
            }
        }
        let index = |s: &str| -> u32 { strings.iter().position(|item| item == s).unwrap() as u32 };
        let mut pool_data = Vec::new();
        let mut offsets = Vec::new();
        for value in &strings {
            offsets.extend_from_slice(&(pool_data.len() as u32).to_le_bytes());
            if utf8 {
                let units = value.encode_utf16().count();
                put_len8(&mut pool_data, units);
                put_len8(&mut pool_data, value.len());
                pool_data.extend_from_slice(value.as_bytes());
                pool_data.push(0);
            } else {
                let units: Vec<u16> = value.encode_utf16().collect();
                put_len16(&mut pool_data, units.len());
                for unit in units {
                    pool_data.extend_from_slice(&unit.to_le_bytes());
                }
                pool_data.extend_from_slice(&0u16.to_le_bytes());
            }
        }
        let pool_header = 28usize;
        let table_size = strings.len() * 4;
        let pool_size = pool_header + table_size + pool_data.len();
        let mut pool = Vec::new();
        put_chunk_header(&mut pool, 1, 28, pool_size);
        pool.extend_from_slice(&(strings.len() as u32).to_le_bytes());
        pool.extend_from_slice(&0u32.to_le_bytes());
        pool.extend_from_slice(&(if utf8 { 0x100u32 } else { 0 }).to_le_bytes());
        pool.extend_from_slice(&((pool_header + table_size) as u32).to_le_bytes());
        pool.extend_from_slice(&0u32.to_le_bytes());
        pool.extend_from_slice(&offsets);
        pool.extend_from_slice(&pool_data);

        let mut chunks = pool;
        let mut start = Vec::new();
        let start_size = 36 + attrs.len() * 20;
        put_chunk_header(&mut start, 0x0102, 16, start_size);
        start.extend_from_slice(&1u32.to_le_bytes());
        start.extend_from_slice(&NO_INDEX.to_le_bytes());
        start.extend_from_slice(&NO_INDEX.to_le_bytes());
        start.extend_from_slice(&index(root).to_le_bytes());
        start.extend_from_slice(&20u16.to_le_bytes());
        start.extend_from_slice(&20u16.to_le_bytes());
        start.extend_from_slice(&(attrs.len() as u16).to_le_bytes());
        start.extend_from_slice(&[0; 6]);
        for attr in attrs {
            start.extend_from_slice(
                &(if attr.namespace {
                    index("item")
                } else {
                    NO_INDEX
                })
                .to_le_bytes(),
            );
            start.extend_from_slice(&index(&attr.name).to_le_bytes());
            start.extend_from_slice(
                &attr
                    .raw
                    .as_deref()
                    .map(index)
                    .unwrap_or(NO_INDEX)
                    .to_le_bytes(),
            );
            start.extend_from_slice(&8u16.to_le_bytes());
            start.push(0);
            let (kind, value) = attr
                .typed
                .as_ref()
                .map(|(k, v)| (*k, if *k == 3 { index(v) } else { 0 }))
                .unwrap_or((0, 0));
            start.push(kind);
            start.extend_from_slice(&value.to_le_bytes());
        }
        chunks.extend_from_slice(&start);
        for _ in 1..depth {
            chunks.extend_from_slice(&empty_start_chunk(index("item")));
        }
        for _ in 1..depth {
            chunks.extend_from_slice(&element_chunk(0x0103, index("item")));
        }
        chunks.extend_from_slice(&element_chunk(0x0103, index(root)));
        let total = chunks.len() + 8;
        let mut result = Vec::new();
        put_chunk_header(&mut result, 3, 8, total);
        result.extend_from_slice(&chunks);
        result
    }

    fn element_chunk(kind: u16, name: u32) -> Vec<u8> {
        let mut out = Vec::new();
        put_chunk_header(&mut out, kind, 16, 24);
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&name.to_le_bytes());
        out
    }

    fn cdata_chunk(string_index: u32) -> Vec<u8> {
        let mut out = Vec::new();
        put_chunk_header(&mut out, 0x0104, 16, 28);
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&string_index.to_le_bytes());
        out.extend_from_slice(&8u16.to_le_bytes());
        out.push(0);
        out.push(3);
        out.extend_from_slice(&string_index.to_le_bytes());
        out
    }

    fn empty_start_chunk(name: u32) -> Vec<u8> {
        let mut out = Vec::new();
        put_chunk_header(&mut out, 0x0102, 16, 36);
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&name.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&20u16.to_le_bytes());
        out.extend_from_slice(&0u16.to_le_bytes());
        out.extend_from_slice(&[0; 6]);
        out
    }

    fn namespace_chunk(start: bool) -> Vec<u8> {
        let mut out = Vec::new();
        put_chunk_header(&mut out, if start { 0x0100 } else { 0x0101 }, 16, 24);
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&NO_INDEX.to_le_bytes());
        out.extend_from_slice(&2u32.to_le_bytes());
        out.extend_from_slice(&2u32.to_le_bytes());
        out
    }

    fn put_chunk_header(out: &mut Vec<u8>, kind: u16, header: usize, size: usize) {
        out.extend_from_slice(&kind.to_le_bytes());
        out.extend_from_slice(&(header as u16).to_le_bytes());
        out.extend_from_slice(&(size as u32).to_le_bytes());
    }

    fn put_len8(out: &mut Vec<u8>, len: usize) {
        if len < 128 {
            out.push(len as u8);
        } else {
            out.push(((len >> 8) as u8) | 0x80);
            out.push(len as u8);
        }
    }

    fn put_len16(out: &mut Vec<u8>, len: usize) {
        if len < 0x8000 {
            out.extend_from_slice(&(len as u16).to_le_bytes());
        } else {
            out.extend_from_slice(&(((len >> 16) as u16) | 0x8000).to_le_bytes());
            out.extend_from_slice(&(len as u16).to_le_bytes());
        }
    }

    fn package_attr(value: &str) -> Attribute {
        Attribute {
            name: "package".into(),
            namespace: false,
            raw: None,
            typed: Some((3, value.into())),
        }
    }

    fn start_offset(bytes: &[u8]) -> usize {
        let pool = 8;
        pool + u32::from_le_bytes(bytes[pool + 4..pool + 8].try_into().unwrap()) as usize
    }

    fn update_document_size(bytes: &mut [u8]) {
        let size = bytes.len() as u32;
        bytes[4..8].copy_from_slice(&size.to_le_bytes());
    }

    #[test]
    fn supports_utf8_and_utf16_pools_and_multibyte_lengths() {
        let long_package = format!("com.{}.app", "a".repeat(122));
        for utf8 in [true, false] {
            let bytes = compiled(utf8, "manifest", &[package_attr(&long_package)], 1);
            assert_eq!(parse(&bytes).unwrap(), long_package);
        }
        let long_name = "x".repeat(32_768);
        let attrs = vec![
            package_attr("org.example.app"),
            Attribute {
                name: long_name,
                namespace: false,
                raw: None,
                typed: None,
            },
            Attribute {
                name: "field😀".into(),
                namespace: false,
                raw: None,
                typed: None,
            },
        ];
        let bytes = compiled(false, "manifest", &attrs, 1);
        assert_eq!(parse(&bytes).unwrap(), "org.example.app");
    }

    #[test]
    fn accepts_typed_only_or_equal_raw_and_typed_values() {
        assert_eq!(
            parse(&compiled(
                true,
                "manifest",
                &[package_attr("org.example.app")],
                1
            ))
            .unwrap(),
            "org.example.app"
        );
        let mut attr = package_attr("org.example.app");
        attr.raw = Some("org.example.app".into());
        assert_eq!(
            parse(&compiled(true, "manifest", &[attr], 1)).unwrap(),
            "org.example.app"
        );
    }

    #[test]
    fn rejects_conflicts_duplicates_namespaced_and_non_string_package_values() {
        let mut conflict = package_attr("org.example.app");
        conflict.raw = Some("org.other.app".into());
        let duplicate = [
            package_attr("org.example.app"),
            package_attr("org.example.two"),
        ];
        let mut namespaced = package_attr("org.example.app");
        namespaced.namespace = true;
        let mut non_string = package_attr("org.example.app");
        non_string.typed = Some((1, String::new()));
        for attrs in [
            vec![conflict],
            duplicate.to_vec(),
            vec![namespaced],
            vec![non_string],
        ] {
            assert!(parse(&compiled(true, "manifest", &attrs, 1)).is_err());
        }
    }

    #[test]
    fn rejects_bad_roots_and_unclosed_or_overdeep_documents() {
        assert!(
            parse(&compiled(
                true,
                "other",
                &[package_attr("org.example.app")],
                1
            ))
            .is_err()
        );
        assert!(
            parse(&compiled(
                true,
                "manifest",
                &[package_attr("org.example.app")],
                257
            ))
            .is_err()
        );
        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        bytes.truncate(bytes.len() - 24);
        let size = bytes.len() as u32;
        bytes[4..8].copy_from_slice(&size.to_le_bytes());
        assert!(parse(&bytes).is_err());
    }

    #[test]
    fn rejects_hostile_declared_pool_counts_and_chunk_sizes() {
        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        bytes[16..20].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(parse(&bytes).is_err());
        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        bytes[20..24].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(parse(&bytes).is_err());
        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        bytes[12..16].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(parse(&bytes).is_err());
    }

    #[test]
    fn rejects_overflowing_string_lengths_and_invalid_typed_indices() {
        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let pool = 8;
        let strings_start =
            u32::from_le_bytes(bytes[pool + 20..pool + 24].try_into().unwrap()) as usize;
        let value_offset_at = pool + 28 + 4 * 4;
        let value_offset = u32::from_le_bytes(
            bytes[value_offset_at..value_offset_at + 4]
                .try_into()
                .unwrap(),
        ) as usize;
        let value = pool + strings_start + value_offset;
        bytes[value + 1] = 0xff;
        assert!(
            parse(&bytes).is_err(),
            "oversized UTF-8 length was accepted"
        );

        let mut bytes = compiled(false, "manifest", &[package_attr("org.example.app")], 1);
        let pool = 8;
        let strings_start =
            u32::from_le_bytes(bytes[pool + 20..pool + 24].try_into().unwrap()) as usize;
        let value_offset = u32::from_le_bytes(
            bytes[pool + 28 + 4 * 4..pool + 32 + 4 * 4]
                .try_into()
                .unwrap(),
        ) as usize;
        let value = pool + strings_start + value_offset;
        bytes[value..value + 4].copy_from_slice(&[0xff, 0xff, 0xff, 0xff]);
        assert!(
            parse(&bytes).is_err(),
            "overflowing UTF-16 length was accepted"
        );

        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let start = start_offset(&bytes);
        bytes[start + 52..start + 56].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(
            parse(&bytes).is_err(),
            "invalid typed string index was accepted"
        );
    }

    #[test]
    fn rejects_bad_offsets_terminators_attribute_strides_and_counts() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let mut bytes = original.clone();
        bytes[36..40].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(
            parse(&bytes).is_err(),
            "bad first string offset was accepted"
        );

        let mut bytes = original.clone();
        let pool = 8;
        let strings_start =
            u32::from_le_bytes(bytes[pool + 20..pool + 24].try_into().unwrap()) as usize;
        let value_offset_at = pool + 28 + 4 * 4;
        let value_offset = u32::from_le_bytes(
            bytes[value_offset_at..value_offset_at + 4]
                .try_into()
                .unwrap(),
        ) as usize;
        let terminator = pool + strings_start + value_offset + b"org.example.app".len();
        bytes[terminator] = b'!';
        assert!(
            parse(&bytes).is_err(),
            "unterminated package string was accepted"
        );

        let start = start_offset(&original);
        let mut bytes = original.clone();
        bytes[start + 26..start + 28].copy_from_slice(&0u16.to_le_bytes());
        assert!(parse(&bytes).is_err(), "zero attribute stride was accepted");
        let mut bytes = original.clone();
        bytes[start + 28..start + 30].copy_from_slice(&((MAX_ATTRIBUTES + 1) as u16).to_le_bytes());
        assert!(
            parse(&bytes).is_err(),
            "oversized attribute count was accepted"
        );
    }

    #[test]
    fn rejects_mismatched_end_tags_multiple_roots_and_unknown_chunks() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let mut bytes = original.clone();
        let end = bytes.len() - 24;
        bytes[end + 20..end + 24].copy_from_slice(&2u32.to_le_bytes());
        assert!(parse(&bytes).is_err());

        let mut bytes = original.clone();
        let mut extra = empty_start_chunk(0);
        extra.extend_from_slice(&element_chunk(0x0103, 0));
        bytes.extend_from_slice(&extra);
        update_document_size(&mut bytes);
        assert!(parse(&bytes).is_err());

        let mut bytes = original;
        put_chunk_header(&mut bytes, 0x0120, 8, 8);
        update_document_size(&mut bytes);
        assert!(parse(&bytes).is_err());

        let mut bytes = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        bytes.push(0);
        update_document_size(&mut bytes);
        assert!(
            parse(&bytes).is_err(),
            "trailing partial chunk was accepted"
        );
    }

    #[test]
    fn enforces_input_and_total_chunk_bounds() {
        assert!(parse(&vec![0; MAX_INPUT + 1]).is_err());
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let end = original.len() - 24;
        let mut bytes = original[..end].to_vec();
        for _ in 0..=MAX_CHUNKS - 2 {
            bytes.extend_from_slice(&cdata_chunk(0));
        }
        bytes.extend_from_slice(&original[end..]);
        update_document_size(&mut bytes);
        assert!(
            parse(&bytes).is_err(),
            "excessive XML chunk count was accepted"
        );
    }

    #[test]
    fn namespace_scope_must_enclose_its_element() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let start = start_offset(&original);
        let mut valid = original.clone();
        valid.splice(start..start, namespace_chunk(true));
        valid.extend_from_slice(&namespace_chunk(false));
        update_document_size(&mut valid);
        assert_eq!(parse(&valid).unwrap(), "org.example.app");

        let mut premature = original.clone();
        premature.splice(start..start, namespace_chunk(true));
        let end = premature.len() - 24;
        premature.splice(end..end, namespace_chunk(false));
        update_document_size(&mut premature);
        assert!(
            parse(&premature).is_err(),
            "namespace ended inside its owning root"
        );

        let mut unowned = original;
        let mut scope = namespace_chunk(true);
        scope.extend_from_slice(&namespace_chunk(false));
        unowned.splice(start..start, scope);
        update_document_size(&mut unowned);
        assert!(
            parse(&unowned).is_err(),
            "namespace ended before an owning element"
        );
    }

    #[test]
    fn resource_map_must_precede_all_xml_node_events() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let start = start_offset(&original);
        let mut map = Vec::new();
        put_chunk_header(&mut map, 0x0180, 8, 8);
        let mut valid = original.clone();
        valid.splice(start..start, map.clone());
        update_document_size(&mut valid);
        assert_eq!(parse(&valid).unwrap(), "org.example.app");

        let mut late = original;
        let mut prefix = namespace_chunk(true);
        prefix.extend_from_slice(&map);
        late.splice(start..start, prefix);
        late.extend_from_slice(&namespace_chunk(false));
        update_document_size(&mut late);
        assert!(
            parse(&late).is_err(),
            "resource map followed a namespace node"
        );
    }

    #[test]
    fn nested_namespaces_close_before_the_parent_element() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 2);
        let start = start_offset(&original);
        let child = start + 56;
        let root_end = original.len() - 24;
        let mut valid = original[..start].to_vec();
        valid.extend_from_slice(&namespace_chunk(true));
        valid.extend_from_slice(&original[start..child]);
        valid.extend_from_slice(&namespace_chunk(true));
        valid.extend_from_slice(&original[child..root_end]);
        let child_namespace_end = valid.len();
        valid.extend_from_slice(&namespace_chunk(false));
        valid.extend_from_slice(&original[root_end..]);
        valid.extend_from_slice(&namespace_chunk(false));
        update_document_size(&mut valid);
        assert_eq!(parse(&valid).unwrap(), "org.example.app");

        let mut crossing = valid;
        crossing[child_namespace_end..child_namespace_end + 48].rotate_left(24);
        assert!(
            parse(&crossing).is_err(),
            "parent closed before child namespace"
        );
    }

    #[test]
    fn rejects_malformed_resource_maps_and_cdata_frames() {
        let original = compiled(true, "manifest", &[package_attr("org.example.app")], 1);
        let start = start_offset(&original);
        for (header, size) in [(8, 10), (12, 12)] {
            let mut map = Vec::new();
            put_chunk_header(&mut map, 0x0180, header, size);
            map.resize(size, 0);
            let mut malformed = original.clone();
            malformed.splice(start..start, map);
            update_document_size(&mut malformed);
            assert!(parse(&malformed).is_err());
        }

        let end = original.len() - 24;
        let mut valid = original.clone();
        valid.splice(end..end, cdata_chunk(0));
        update_document_size(&mut valid);
        assert_eq!(parse(&valid).unwrap(), "org.example.app");
        let mut malformed = valid.clone();
        malformed[end + 2..end + 4].copy_from_slice(&12u16.to_le_bytes());
        assert!(parse(&malformed).is_err());
        let mut malformed = valid;
        malformed[end + 20..end + 22].copy_from_slice(&0u16.to_le_bytes());
        assert!(parse(&malformed).is_err());
    }
}
