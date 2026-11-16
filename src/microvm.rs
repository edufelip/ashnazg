use std::{
    env,
    path::{Path, PathBuf},
    process::{Command, ExitStatus},
};

use anyhow::{Context, Result, bail};
use clap::Args;

#[derive(Debug, Args)]
pub struct UpArgs {
    /// Directory containing manifest.json, kernel, and rootfs.ext4.
    #[arg(long, value_name = "DIR")]
    pub assets: Option<PathBuf>,
    /// Persistent VM state directory. Defaults to the current user's Application Support folder.
    #[arg(long, value_name = "DIR")]
    pub state_dir: Option<PathBuf>,
    /// Loopback TCP port exposed for ADB.
    #[arg(long, default_value_t = 15555, value_parser = clap::value_parser!(u16).range(1024..))]
    pub adb_port: u16,
    /// Seconds allowed for the guest Android supervisor to become ready.
    #[arg(long, default_value_t = 300, value_parser = clap::value_parser!(u16).range(15..=1800))]
    pub timeout: u16,
    /// Override the sibling ashnazg-vm helper path.
    #[arg(long, value_name = "PATH")]
    pub vm_helper: Option<PathBuf>,
    /// Validate manifest, assets, and Virtualization.framework configuration without booting.
    #[arg(long)]
    pub validate_only: bool,
}

pub fn run(args: UpArgs) -> Result<ExitStatus> {
    ensure_supported(env::consts::OS, env::consts::ARCH)?;
    let assets = resolve_assets(args.assets)?;
    let state_dir = args.state_dir.unwrap_or_else(default_state_dir);
    let helper = args.vm_helper.unwrap_or(default_helper_path()?);
    if !helper.is_file() {
        bail!(
            "Virtualization helper not found at {}. Build it with `swift build -c release --package-path platform/macos`, or pass `--vm-helper PATH`.",
            helper.display()
        );
    }

    let mut command = Command::new(&helper);
    command
        .arg("--assets")
        .arg(&assets)
        .arg("--state-dir")
        .arg(&state_dir)
        .arg("--adb-port")
        .arg(args.adb_port.to_string())
        .arg("--timeout")
        .arg(args.timeout.to_string());
    if args.validate_only {
        command.arg("--validate-only");
    }
    launch_helper(&mut command)
        .with_context(|| format!("failed to start VM helper at {}", helper.display()))
}

fn launch_helper(command: &mut Command) -> Result<ExitStatus> {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Retain the CLI PID so a signal sent to it reaches the VM owner directly.
        // No parent remains that could exit and leave a helper supervising a VM.
        Err(command.exec().into())
    }
    #[cfg(not(unix))]
    Ok(command.status()?)
}

pub fn finish(status: ExitStatus) -> ! {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        std::process::exit(
            status
                .code()
                .unwrap_or_else(|| 128 + status.signal().unwrap_or(0)),
        );
    }
    #[cfg(not(unix))]
    std::process::exit(status.code().unwrap_or(1));
}

fn ensure_supported(os: &str, arch: &str) -> Result<()> {
    if os != "macos" || arch != "aarch64" {
        bail!(
            "`ashnazg up` requires macOS on Apple Silicon (macOS/aarch64); this host is {os}/{arch}"
        );
    }
    Ok(())
}

fn resolve_assets(assets: Option<PathBuf>) -> Result<PathBuf> {
    let candidate = assets
        .or_else(|| env::var_os("ASHNAZG_MICROVM_ASSETS").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("runtime/microvm"));
    let required = ["manifest.json", "kernel", "rootfs.ext4"];
    let missing: Vec<_> = required
        .iter()
        .filter(|name| !candidate.join(name).is_file())
        .copied()
        .collect();
    if !missing.is_empty() {
        bail!(
            "MicroVM assets are unavailable in {} (missing: {}). No verified bundle is installed. Build or obtain the reviewed artifact bundle, then run `ashnazg up --assets DIR`. No artifact download URL is configured.",
            candidate.display(),
            missing.join(", ")
        );
    }
    Ok(candidate)
}

fn default_state_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Application Support/ashnazg/microvm")
}

fn default_helper_path() -> Result<PathBuf> {
    let executable = env::current_exe().context("cannot determine ash nazg executable location")?;
    let parent = executable
        .parent()
        .ok_or_else(|| anyhow::anyhow!("ash nazg executable has no parent directory"))?;
    Ok(parent.join("ashnazg-vm"))
}

#[allow(dead_code)]
fn helper_path_for(executable: &Path) -> Option<PathBuf> {
    executable.parent().map(|parent| parent.join("ashnazg-vm"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_apple_silicon_macos() {
        assert!(ensure_supported("macos", "aarch64").is_ok());
        assert!(ensure_supported("linux", "aarch64").is_err());
        assert!(ensure_supported("macos", "x86_64").is_err());
    }

    #[test]
    fn missing_bundle_explains_setup_without_fake_url() {
        let err = resolve_assets(Some(PathBuf::from("/definitely/missing/ashnazg-assets")))
            .unwrap_err()
            .to_string();
        assert!(err.contains("--assets DIR"));
        assert!(err.contains("No artifact download URL is configured"));
        assert!(!err.contains("https://"));
    }

    #[test]
    fn sibling_helper_location_is_stable() {
        assert_eq!(
            helper_path_for(Path::new("/opt/ashnazg/bin/ashnazg")),
            Some(PathBuf::from("/opt/ashnazg/bin/ashnazg-vm"))
        );
    }

    #[cfg(unix)]
    #[test]
    fn exec_helper_probe() {
        if env::var_os("ASHNAZG_EXEC_PID").is_none() {
            return;
        }
        let mut command = Command::new("/bin/sh");
        command
            .arg("-c")
            .arg("printf '%s' \"$$\" > \"$ASHNAZG_EXEC_PID\"; exit 17");
        launch_helper(&mut command).expect("probe shell must replace the test process");
    }

    #[cfg(unix)]
    #[test]
    fn helper_replaces_the_cli_pid_and_preserves_its_exit_status() {
        let temporary = tempfile::tempdir().unwrap();
        let capture = temporary.path().join("pid");
        let mut child = Command::new(env::current_exe().unwrap())
            .args(["--exact", "microvm::tests::exec_helper_probe"])
            .env("ASHNAZG_EXEC_PID", &capture)
            .spawn()
            .unwrap();
        let original_pid = child.id();
        assert_eq!(child.wait().unwrap().code(), Some(17));
        assert_eq!(
            std::fs::read_to_string(capture).unwrap(),
            original_pid.to_string()
        );
    }
}
