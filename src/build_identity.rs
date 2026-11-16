//! Explicit compiler-supplied runtime source identity.

/// Returns the source revision explicitly supplied and validated at build time.
///
/// Unstamped development builds return `None`.
pub fn runtime_source_sha() -> Option<&'static str> {
    let stamp = env!("ASHNAZG_RUNTIME_SOURCE_SHA");
    (!stamp.is_empty()).then_some(stamp)
}
