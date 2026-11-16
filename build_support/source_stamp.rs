use std::ffi::OsStr;

pub const INVALID_SOURCE_STAMP: &str =
    "ASHNAZG_BUILD_SOURCE_SHA must be exactly 40 lowercase hexadecimal characters";

pub fn validate_source_stamp(value: Option<&OsStr>) -> Result<Option<&str>, &'static str> {
    let Some(value) = value else {
        return Ok(None);
    };
    let Some(value) = value.to_str() else {
        return Err(INVALID_SOURCE_STAMP);
    };
    if value.len() == 40
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(Some(value))
    } else {
        Err(INVALID_SOURCE_STAMP)
    }
}
