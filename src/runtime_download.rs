//! Bounded, deadline-aware HTTPS acquisition of a release catalog and its artifacts.

use crate::runtime_catalog::{CatalogEntry, RuntimeCatalog, catalog_url};
use anyhow::{Result, anyhow, bail};
use reqwest::header::{ACCEPT_ENCODING, CONTENT_ENCODING, CONTENT_LENGTH, HeaderMap, LOCATION};
use reqwest::{Client, Url, redirect::Policy};
use sha2::{Digest, Sha256};
use std::{future::Future, time::Duration};
use tokio::{
    io::{AsyncWrite, AsyncWriteExt},
    time::{Instant, timeout_at},
};
use tokio_util::sync::CancellationToken;

const MAX_CATALOG_BYTES: usize = 64 * 1024;
const MAX_REDIRECTS: usize = 5;
const ALLOWED_HOSTS: [&str; 4] = [
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
];

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CatalogReceipt {
    pub bytes: Vec<u8>,
    pub sha256: String,
}

#[derive(Debug)]
pub struct CatalogDownload {
    pub catalog: RuntimeCatalog,
    pub receipt: CatalogReceipt,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DownloadReceipt {
    pub size: u64,
    pub sha256: String,
    pub final_host: String,
}

/// HTTPS-only downloader with redirects disabled inside reqwest and validated manually.
pub struct RuntimeDownloader {
    client: Client,
}

impl RuntimeDownloader {
    pub fn new() -> Result<Self> {
        let client = Client::builder()
            .https_only(true)
            .redirect(Policy::none())
            .build()
            .map_err(|_| anyhow!("could not initialize runtime downloader"))?;
        Ok(Self { client })
    }

    pub async fn fetch_catalog(
        &self,
        expected_version: &str,
        expected_source_sha: &str,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> Result<CatalogDownload> {
        let initial =
            catalog_url(expected_version).map_err(|_| anyhow!("invalid runtime catalog stamp"))?;
        if expected_source_sha.len() != 40
            || !expected_source_sha
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            bail!("invalid runtime catalog stamp");
        }
        guarded(deadline, cancel, async {
            let transport = ReqwestTransport {
                client: &self.client,
            };
            fetch_catalog_with(
                &transport,
                &initial,
                expected_version,
                expected_source_sha,
                deadline,
            )
            .await
        })
        .await
    }

    pub async fn fetch_entry<W>(
        &self,
        catalog: &RuntimeCatalog,
        entry: &CatalogEntry,
        writer: &mut W,
        deadline: Instant,
        cancel: &CancellationToken,
    ) -> Result<DownloadReceipt>
    where
        W: AsyncWrite + Unpin,
    {
        let initial = catalog
            .download_url(entry)
            .map_err(|_| anyhow!("invalid runtime catalog entry"))?;
        guarded(deadline, cancel, async {
            let transport = ReqwestTransport {
                client: &self.client,
            };
            fetch_entry_with(
                &transport,
                &initial,
                entry.size(),
                entry.sha256(),
                writer,
                deadline,
            )
            .await
        })
        .await
    }
}

async fn guarded<T>(
    deadline: Instant,
    cancel: &CancellationToken,
    future: impl Future<Output = Result<T>>,
) -> Result<T> {
    if cancel.is_cancelled() {
        bail!("runtime download cancelled");
    }
    if Instant::now() >= deadline {
        bail!("runtime download deadline exceeded");
    }
    tokio::select! {
        biased;
        _ = cancel.cancelled() => bail!("runtime download cancelled"),
        result = timeout_at(deadline, future) => result.map_err(|_| anyhow!("runtime download deadline exceeded"))?,
    }
}

trait HttpBody {
    async fn next_chunk(&mut self) -> std::result::Result<Option<Vec<u8>>, ()>;
}

trait HttpTransport {
    type Body: HttpBody;
    async fn send(
        &self,
        url: Url,
        timeout: Duration,
    ) -> std::result::Result<WireResponse<Self::Body>, ()>;
}

struct WireResponse<B> {
    status: u16,
    location: Option<String>,
    content_length: Option<u64>,
    invalid_content_length: bool,
    invalid_content_encoding: bool,
    body: B,
}

struct ReqwestTransport<'a> {
    client: &'a Client,
}

impl HttpTransport for ReqwestTransport<'_> {
    type Body = ReqwestBody;

    async fn send(
        &self,
        url: Url,
        timeout: Duration,
    ) -> std::result::Result<WireResponse<Self::Body>, ()> {
        let response = self
            .client
            .get(url)
            .header(ACCEPT_ENCODING, "identity")
            .timeout(timeout)
            .send()
            .await
            .map_err(|_| ())?;
        let headers = response.headers();
        let lengths = headers.get_all(CONTENT_LENGTH);
        let raw_header = headers.get(CONTENT_LENGTH);
        let raw_length = raw_header.and_then(|v| v.to_str().ok());
        Ok(WireResponse {
            status: response.status().as_u16(),
            location: headers
                .get(LOCATION)
                .and_then(|v| v.to_str().ok())
                .map(str::to_owned),
            content_length: raw_length.and_then(|v| v.parse().ok()),
            invalid_content_length: lengths.iter().count() > 1
                || raw_header.is_some() && raw_length.is_none()
                || raw_length.is_some_and(|v| v.parse::<u64>().is_err()),
            invalid_content_encoding: !valid_content_encoding(headers),
            body: ReqwestBody(response),
        })
    }
}

struct ReqwestBody(reqwest::Response);

impl HttpBody for ReqwestBody {
    async fn next_chunk(&mut self) -> std::result::Result<Option<Vec<u8>>, ()> {
        self.0
            .chunk()
            .await
            .map(|chunk| chunk.map(|bytes| bytes.to_vec()))
            .map_err(|_| ())
    }
}

async fn get_final<T: HttpTransport>(
    transport: &T,
    initial: &str,
    deadline: Instant,
) -> Result<(WireResponse<T::Body>, Url)> {
    let mut current = validated_url(initial)?;
    for redirects in 0..=MAX_REDIRECTS {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|d| !d.is_zero())
            .ok_or_else(|| anyhow!("runtime download deadline exceeded"))?;
        let response = transport
            .send(current.clone(), remaining)
            .await
            .map_err(|_| anyhow!("runtime download request failed"))?;
        if is_redirect(response.status) {
            if redirects == MAX_REDIRECTS {
                bail!("runtime download redirect limit exceeded");
            }
            let location = response
                .location
                .as_deref()
                .ok_or_else(|| anyhow!("invalid runtime download redirect"))?;
            current = current
                .join(location)
                .map_err(|_| anyhow!("invalid runtime download redirect"))?;
            current = validated_url(current.as_str())?;
            continue;
        }
        return Ok((response, current));
    }
    bail!("runtime download redirect limit exceeded")
}

async fn fetch_catalog_with<T: HttpTransport>(
    transport: &T,
    initial: &str,
    version: &str,
    source_sha: &str,
    deadline: Instant,
) -> Result<CatalogDownload> {
    let (mut response, _) = get_final(transport, initial, deadline).await?;
    validate_body_response(&response, None)?;
    if response
        .content_length
        .is_some_and(|length| length > MAX_CATALOG_BYTES as u64)
    {
        bail!("runtime catalog exceeds size limit");
    }
    let mut bytes = Vec::with_capacity(MAX_CATALOG_BYTES.min(4096));
    while let Some(chunk) = response
        .body
        .next_chunk()
        .await
        .map_err(|_| anyhow!("runtime catalog transfer failed"))?
    {
        if bytes
            .len()
            .checked_add(chunk.len())
            .is_none_or(|len| len > MAX_CATALOG_BYTES)
        {
            bail!("runtime catalog exceeds size limit");
        }
        bytes.extend_from_slice(&chunk);
    }
    let catalog = RuntimeCatalog::parse(&bytes, version, source_sha)
        .map_err(|_| anyhow!("runtime catalog validation failed"))?;
    let receipt = CatalogReceipt {
        sha256: digest(&bytes),
        bytes,
    };
    Ok(CatalogDownload { catalog, receipt })
}

async fn fetch_entry_with<T: HttpTransport, W: AsyncWrite + Unpin>(
    transport: &T,
    initial: &str,
    expected_size: u64,
    expected_sha: &str,
    writer: &mut W,
    deadline: Instant,
) -> Result<DownloadReceipt> {
    let (mut response, final_url) = get_final(transport, initial, deadline).await?;
    validate_body_response(&response, Some(expected_size))?;
    let mut hasher = Sha256::new();
    let mut size = 0u64;
    while let Some(chunk) = response
        .body
        .next_chunk()
        .await
        .map_err(|_| anyhow!("runtime artifact transfer failed"))?
    {
        let next = size
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| anyhow!("runtime artifact size exceeded"))?;
        if next > expected_size {
            bail!("runtime artifact size exceeded");
        }
        writer
            .write_all(&chunk)
            .await
            .map_err(|_| anyhow!("runtime artifact writer failed"))?;
        hasher.update(&chunk);
        size = next;
    }
    if size != expected_size {
        bail!("runtime artifact size mismatch");
    }
    let actual_hash = hex_digest(hasher.finalize());
    if actual_hash != expected_sha {
        bail!("runtime artifact digest mismatch");
    }
    writer
        .flush()
        .await
        .map_err(|_| anyhow!("runtime artifact writer failed"))?;
    Ok(DownloadReceipt {
        size,
        sha256: actual_hash,
        final_host: final_url.host_str().unwrap_or_default().to_owned(),
    })
}

fn validated_url(raw: &str) -> Result<Url> {
    let url = Url::parse(raw).map_err(|_| anyhow!("invalid runtime download URL"))?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.port().is_some_and(|port| port != 443)
        || !url
            .host_str()
            .is_some_and(|host| ALLOWED_HOSTS.contains(&host))
    {
        bail!("runtime download URL is not allowed");
    }
    Ok(url)
}

fn validate_body_response<B>(
    response: &WireResponse<B>,
    expected_length: Option<u64>,
) -> Result<()> {
    if response.status != 200 {
        bail!("runtime download returned an unexpected status");
    }
    if response.invalid_content_encoding {
        bail!("runtime download encoding is not allowed");
    }
    if response.invalid_content_length {
        bail!("runtime download content length is invalid");
    }
    if let (Some(actual), Some(expected)) = (response.content_length, expected_length)
        && actual != expected
    {
        bail!("runtime artifact content length mismatch");
    }
    Ok(())
}

fn valid_content_encoding(headers: &HeaderMap) -> bool {
    let mut values = headers.get_all(CONTENT_ENCODING).iter();
    match (values.next(), values.next()) {
        (None, None) => true,
        (Some(value), None) => value
            .to_str()
            .is_ok_and(|encoding| encoding.eq_ignore_ascii_case("identity")),
        (Some(_), Some(_)) => false,
        (None, Some(_)) => false,
    }
}

fn is_redirect(status: u16) -> bool {
    matches!(status, 301 | 302 | 303 | 307 | 308)
}

fn digest(bytes: &[u8]) -> String {
    hex_digest(Sha256::digest(bytes))
}

fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let bytes = bytes.as_ref();
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(HEX[(byte >> 4) as usize] as char);
        result.push(HEX[(byte & 0x0f) as usize] as char);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_allowlist_rejects_unsafe_url_components() {
        for url in [
            "http://github.com/a",
            "https://user@github.com/a",
            "https://github.com:444/a",
            "https://github.com/a#fragment",
            "https://evil.example/a",
        ] {
            assert!(validated_url(url).is_err(), "accepted {url}");
        }
        assert!(validated_url("https://github.com/a").is_ok());
    }

    #[test]
    fn body_validation_rejects_nonidentity_encoding_and_wrong_length() {
        let response = WireResponse {
            status: 200,
            location: None,
            content_length: Some(3),
            invalid_content_length: false,
            invalid_content_encoding: true,
            body: (),
        };
        assert!(validate_body_response(&response, Some(3)).is_err());
        let response = WireResponse {
            invalid_content_encoding: false,
            ..response
        };
        assert!(validate_body_response(&response, Some(4)).is_err());
        assert!(validate_body_response(&response, Some(3)).is_ok());
    }

    #[test]
    fn content_encoding_headers_are_validated_without_lossy_projection() {
        let mut headers = reqwest::header::HeaderMap::new();
        headers.append(CONTENT_ENCODING, "identity".parse().unwrap());
        headers.append(CONTENT_ENCODING, "gzip".parse().unwrap());
        assert!(!valid_content_encoding(&headers));

        let mut headers = reqwest::header::HeaderMap::new();
        headers.insert(
            CONTENT_ENCODING,
            reqwest::header::HeaderValue::from_bytes(&[0xff]).unwrap(),
        );
        assert!(!valid_content_encoding(&headers));

        let mut headers = reqwest::header::HeaderMap::new();
        assert!(valid_content_encoding(&headers));
        headers.insert(CONTENT_ENCODING, "IdEnTiTy".parse().unwrap());
        assert!(valid_content_encoding(&headers));
        headers.insert(CONTENT_ENCODING, "identity, identity".parse().unwrap());
        assert!(!valid_content_encoding(&headers));
    }

    use std::{
        collections::VecDeque,
        pin::Pin,
        sync::Mutex,
        task::{Context, Poll},
    };
    use tokio::{io::AsyncWrite, time::sleep};

    struct Chunks {
        chunks: VecDeque<Vec<u8>>,
        delay: Duration,
    }
    impl HttpBody for Chunks {
        async fn next_chunk(&mut self) -> std::result::Result<Option<Vec<u8>>, ()> {
            if self.delay != Duration::ZERO {
                sleep(self.delay).await;
            }
            Ok(self.chunks.pop_front())
        }
    }
    struct Scripted(Mutex<VecDeque<WireResponse<Chunks>>>);
    impl HttpTransport for Scripted {
        type Body = Chunks;
        async fn send(
            &self,
            _url: Url,
            _timeout: Duration,
        ) -> std::result::Result<WireResponse<Chunks>, ()> {
            self.0.lock().unwrap().pop_front().ok_or(())
        }
    }
    fn reply(
        status: u16,
        location: Option<&str>,
        bytes: &[u8],
        length: Option<u64>,
    ) -> WireResponse<Chunks> {
        WireResponse {
            status,
            location: location.map(str::to_owned),
            content_length: length,
            invalid_content_length: false,
            invalid_content_encoding: false,
            body: Chunks {
                chunks: bytes.chunks(2).map(<[u8]>::to_vec).collect(),
                delay: Duration::ZERO,
            },
        }
    }

    const TEST_VERSION: &str = "1.2.3";
    const TEST_SOURCE: &str = "0123456789abcdef0123456789abcdef01234567";
    fn catalog_bytes() -> Vec<u8> {
        let entry = |name: String, platform: &str| serde_json::json!({ "name": name, "platform": platform, "source_sha": TEST_SOURCE, "size": 1, "sha256": "a".repeat(64) });
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 1, "version": TEST_VERSION, "release_tag": format!("v{TEST_VERSION}"), "source_sha": TEST_SOURCE,
            "artifacts": {
                "guest": entry(format!("ashnazg-microvm-v{TEST_VERSION}-linux-aarch64.tar.xz"), "linux-aarch64"),
                "helper": entry(format!("ashnazg-vm-v{TEST_VERSION}-macos-aarch64"), "macos-aarch64"),
                "agent": entry(format!("ashnazg-agent-v{TEST_VERSION}.apk"), "android"),
                "fixture": entry(format!("ashnazg-fixture-v{TEST_VERSION}.apk"), "android"),
            }, "source_archives": []
        })).unwrap()
    }

    #[tokio::test]
    async fn fetches_and_validates_bounded_catalog_bytes() {
        let bytes = catalog_bytes();
        let transport = Scripted(Mutex::new(VecDeque::from([reply(200, None, &bytes, None)])));
        let fetched = fetch_catalog_with(
            &transport,
            "https://github.com/release/catalog.json",
            TEST_VERSION,
            TEST_SOURCE,
            Instant::now() + Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(fetched.catalog.version(), TEST_VERSION);
        assert_eq!(fetched.receipt.bytes, bytes);
        assert_eq!(fetched.receipt.sha256, digest(&bytes));

        let too_large = vec![b'x'; MAX_CATALOG_BYTES + 1];
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            200, None, &too_large, None,
        )])));
        assert!(
            fetch_catalog_with(
                &transport,
                "https://github.com/release/catalog.json",
                TEST_VERSION,
                TEST_SOURCE,
                Instant::now() + Duration::from_secs(1)
            )
            .await
            .is_err()
        );
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            200,
            None,
            b"{}",
            Some((MAX_CATALOG_BYTES + 1) as u64),
        )])));
        assert!(
            fetch_catalog_with(
                &transport,
                "https://github.com/release/catalog.json",
                TEST_VERSION,
                TEST_SOURCE,
                Instant::now() + Duration::from_secs(1)
            )
            .await
            .is_err()
        );
        let transport = Scripted(Mutex::new(VecDeque::from([reply(200, None, b"{}", None)])));
        let error = fetch_catalog_with(
            &transport,
            "https://github.com/release/catalog.json",
            TEST_VERSION,
            TEST_SOURCE,
            Instant::now() + Duration::from_secs(1),
        )
        .await
        .unwrap_err();
        assert_eq!(error.to_string(), "runtime catalog validation failed");
    }

    #[tokio::test]
    async fn follows_relative_redirect_and_returns_only_final_hostname() {
        let transport = Scripted(Mutex::new(VecDeque::from([
            reply(302, Some("/asset?signature=do-not-return"), b"", None),
            reply(200, None, b"abc", Some(3)),
        ])));
        let mut output = Vec::new();
        let receipt = fetch_entry_with(
            &transport,
            "https://github.com/releases/file",
            3,
            &digest(b"abc"),
            &mut output,
            Instant::now() + Duration::from_secs(1),
        )
        .await
        .unwrap();
        assert_eq!(output, b"abc");
        assert_eq!(receipt.final_host, "github.com");
        assert!(!format!("{receipt:?}").contains("signature"));
    }

    #[tokio::test]
    async fn rejects_untrusted_redirect_without_exposing_location() {
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            302,
            Some("https://evil.example/secret?token=hidden"),
            b"",
            None,
        )])));
        let error = match get_final(
            &transport,
            "https://github.com/releases/file",
            Instant::now() + Duration::from_secs(1),
        )
        .await
        {
            Ok(_) => panic!("unsafe redirect accepted"),
            Err(error) => error,
        };
        assert!(!error.to_string().contains("evil.example"));
        assert!(!format!("{error:?}").contains("hidden"));
    }

    #[tokio::test]
    async fn rejects_sixth_redirect() {
        let redirects = (0..=MAX_REDIRECTS)
            .map(|_| reply(302, Some("/next"), b"", None))
            .collect();
        let transport = Scripted(Mutex::new(redirects));
        assert!(
            get_final(
                &transport,
                "https://github.com/releases/file",
                Instant::now() + Duration::from_secs(1)
            )
            .await
            .is_err()
        );
    }

    #[tokio::test]
    async fn verifies_fragmented_body_and_rejects_short_long_or_wrong_digest() {
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            200,
            None,
            b"abc",
            Some(3),
        )])));
        let mut output = Vec::new();
        assert!(
            fetch_entry_with(
                &transport,
                "https://github.com/a",
                3,
                &digest(b"abc"),
                &mut output,
                Instant::now() + Duration::from_secs(1)
            )
            .await
            .is_ok()
        );
        for (payload, length, expected) in [
            (b"ab".as_slice(), None, digest(b"ab")),
            (b"abcd".as_slice(), None, digest(b"abcd")),
            (b"abc".as_slice(), Some(3), digest(b"xyz")),
        ] {
            let transport = Scripted(Mutex::new(VecDeque::from([reply(
                200, None, payload, length,
            )])));
            let mut output = Vec::new();
            assert!(
                fetch_entry_with(
                    &transport,
                    "https://github.com/a",
                    3,
                    &expected,
                    &mut output,
                    Instant::now() + Duration::from_secs(1)
                )
                .await
                .is_err()
            );
        }
    }

    #[tokio::test]
    async fn one_deadline_covers_slow_chunks_and_cancellation() {
        let mut slow = reply(200, None, b"abc", Some(3));
        slow.body.delay = Duration::from_millis(40);
        let transport = Scripted(Mutex::new(VecDeque::from([slow])));
        let mut output = Vec::new();
        let token = CancellationToken::new();
        let result = guarded(
            Instant::now() + Duration::from_millis(10),
            &token,
            fetch_entry_with(
                &transport,
                "https://github.com/a",
                3,
                &digest(b"abc"),
                &mut output,
                Instant::now() + Duration::from_secs(1),
            ),
        )
        .await;
        assert!(result.is_err());
        let token = CancellationToken::new();
        token.cancel();
        assert!(
            guarded(Instant::now() + Duration::from_secs(1), &token, async {
                Ok(())
            })
            .await
            .is_err()
        );
    }

    struct NeverFlush;
    impl AsyncWrite for NeverFlush {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            bytes: &[u8],
        ) -> Poll<std::io::Result<usize>> {
            Poll::Ready(Ok(bytes.len()))
        }
        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            Poll::Pending
        }
        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn one_deadline_covers_writer_flush() {
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            200,
            None,
            b"abc",
            Some(3),
        )])));
        let token = CancellationToken::new();
        let result = guarded(
            Instant::now() + Duration::from_millis(10),
            &token,
            fetch_entry_with(
                &transport,
                "https://github.com/a",
                3,
                &digest(b"abc"),
                &mut NeverFlush,
                Instant::now() + Duration::from_secs(1),
            ),
        )
        .await;
        assert!(result.is_err());
    }

    struct NeverWrite;
    impl AsyncWrite for NeverWrite {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            _bytes: &[u8],
        ) -> Poll<std::io::Result<usize>> {
            Poll::Pending
        }
        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }
        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn one_deadline_covers_writer_writes() {
        let transport = Scripted(Mutex::new(VecDeque::from([reply(
            200,
            None,
            b"abc",
            Some(3),
        )])));
        let token = CancellationToken::new();
        let result = guarded(
            Instant::now() + Duration::from_millis(10),
            &token,
            fetch_entry_with(
                &transport,
                "https://github.com/a",
                3,
                &digest(b"abc"),
                &mut NeverWrite,
                Instant::now() + Duration::from_secs(1),
            ),
        )
        .await;
        assert!(result.is_err());
    }
}
