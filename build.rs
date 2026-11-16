use std::env;

#[path = "build_support/source_stamp.rs"]
mod source_stamp;

fn main() {
    println!("cargo:rerun-if-env-changed=ASHNAZG_BUILD_SOURCE_SHA");
    let value = env::var_os("ASHNAZG_BUILD_SOURCE_SHA");
    let stamp = match source_stamp::validate_source_stamp(value.as_deref()) {
        Ok(Some(stamp)) => stamp,
        Ok(None) => "",
        Err(error) => panic!("{error}"),
    };
    println!("cargo:rustc-env=ASHNAZG_RUNTIME_SOURCE_SHA={stamp}");
}
