use std::ffi::{OsStr, OsString};

#[path = "../build_support/source_stamp.rs"]
mod source_stamp;

use source_stamp::validate_source_stamp;

#[test]
fn source_stamp_accepts_absent_or_exact_lowercase_sha() {
    assert_eq!(validate_source_stamp(None), Ok(None));
    let sha = "0123456789abcdef0123456789abcdef01234567";
    assert_eq!(validate_source_stamp(Some(OsStr::new(sha))), Ok(Some(sha)));
}

#[test]
fn source_stamp_rejects_uppercase_nonhex_and_wrong_lengths() {
    for value in [
        "0123456789ABCDEF0123456789abcdef01234567",
        "0123456789abcdef0123456789abcdef0123456g",
        "0123456789abcdef0123456789abcdef0123456",
        "0123456789abcdef0123456789abcdef012345678",
    ] {
        assert_eq!(
            validate_source_stamp(Some(OsStr::new(value))),
            Err(source_stamp::INVALID_SOURCE_STAMP)
        );
    }
}

#[cfg(unix)]
#[test]
fn source_stamp_rejects_non_unicode_without_exposing_it() {
    use std::os::unix::ffi::OsStringExt;

    let invalid = OsString::from_vec(vec![0xff, 0xfe]);
    let error = validate_source_stamp(Some(&invalid));
    assert_eq!(error, Err(source_stamp::INVALID_SOURCE_STAMP));
    assert_eq!(
        format!("{error:?}"),
        format!("Err({:?})", source_stamp::INVALID_SOURCE_STAMP)
    );
}

#[test]
fn runtime_identity_matches_the_value_emitted_by_the_build_script() {
    let compiler_value = env!("ASHNAZG_RUNTIME_SOURCE_SHA");
    let expected = std::env::var("ASHNAZG_TEST_EXPECTED_SOURCE_SHA")
        .unwrap_or_else(|_| compiler_value.to_owned());
    assert_eq!(compiler_value, expected);
    assert_eq!(
        ashnazg::build_identity::runtime_source_sha(),
        (!compiler_value.is_empty()).then_some(compiler_value)
    );
}
