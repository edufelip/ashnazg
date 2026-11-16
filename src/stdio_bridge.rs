//! Isolates potentially blocking inherited stdio from the async session owner.
#![cfg(unix)]

use std::ffi::OsString;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use tokio::net::UnixStream;
use tokio::process::Command as TokioCommand;
use tokio::task::JoinHandle;
use tokio::time::timeout;
use tokio_util::sync::CancellationToken;

const CHILD_MODE: &str = "--__ashnazg-stdio-bridge";
const CHILD_FD_FLAG: &str = "--fd";
const CLEANUP_BOUND: Duration = Duration::from_secs(2);
const COPY_BUFFER: usize = 16 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExitReceipt {
    NormalEof,
    RequestedStop,
}

/// Owns one hidden transport process. Dropping it requests detached cleanup;
/// explicit shutdown waits for the child to be reaped.
pub struct OwnedStdioBridge {
    io: Option<UnixStream>,
    control: CancellationToken,
    lifecycle: Option<JoinHandle<Result<ExitReceipt>>>,
    #[cfg(debug_assertions)]
    test_child_pid: Option<u32>,
}

impl OwnedStdioBridge {
    pub fn spawn(
        executable: &Path,
        input: BorrowedFd<'_>,
        output: BorrowedFd<'_>,
        caller_cancel: CancellationToken,
    ) -> Result<Self> {
        tokio::runtime::Handle::try_current().context("stdio bridge requires an active runtime")?;
        validate_endpoint(input, true)?;
        validate_endpoint(output, false)?;

        let input_source_raw = input.as_raw_fd();
        let output_source_raw = output.as_raw_fd();
        let input = input
            .try_clone_to_owned()
            .context("cannot own bridge input")?;
        let output = output
            .try_clone_to_owned()
            .context("cannot own bridge output")?;
        let (parent_socket, child_socket) = socket_pair()?;
        set_cloexec(parent_socket.as_raw_fd(), true)?;
        set_cloexec(child_socket.as_raw_fd(), true)?;

        let parent_std = std::os::unix::net::UnixStream::from(parent_socket);
        parent_std
            .set_nonblocking(true)
            .context("cannot configure bridge IPC")?;
        let monitor_std = parent_std
            .try_clone()
            .context("cannot monitor bridge IPC")?;
        let std_stream = UnixStream::from_std(parent_std).context("cannot register bridge IPC")?;
        let monitor =
            UnixStream::from_std(monitor_std).context("cannot register bridge monitor")?;

        let child_raw = child_socket.as_raw_fd();
        let mut command = TokioCommand::new(executable);
        command
            .arg(CHILD_MODE)
            .arg(CHILD_FD_FLAG)
            .arg(child_raw.to_string())
            .env_clear()
            .kill_on_drop(true)
            .stdin(Stdio::from(File::from(input)))
            .stdout(Stdio::from(File::from(output)))
            .stderr(Stdio::null());
        // SAFETY: this child-only hook changes CLOEXEC on the one socket endpoint
        // intentionally passed to the bridge. It does not touch global state.
        unsafe {
            command.as_std_mut().pre_exec(move || {
                let flags = libc::fcntl(child_raw, libc::F_GETFD);
                if flags == -1
                    || libc::fcntl(child_raw, libc::F_SETFD, flags & !libc::FD_CLOEXEC) == -1
                {
                    return Err(io::Error::last_os_error());
                }
                // The duplicated stdio descriptors are already installed by
                // Command. Close only the borrowed source aliases in the child
                // so they cannot hold caller pipes open across exec.
                for source in [input_source_raw, output_source_raw] {
                    if source > libc::STDERR_FILENO && source != child_raw {
                        libc::close(source);
                    }
                }
                Ok(())
            });
        }

        let child = command.spawn().context("cannot start stdio bridge")?;
        #[cfg(debug_assertions)]
        let test_child_pid = child.id();
        drop(child_socket);
        let control = CancellationToken::new();
        let lifecycle_control = control.clone();
        let lifecycle = tokio::spawn(async move {
            own_child(child, monitor, caller_cancel, lifecycle_control).await
        });

        Ok(Self {
            io: Some(std_stream),
            control,
            lifecycle: Some(lifecycle),
            #[cfg(debug_assertions)]
            test_child_pid,
        })
    }

    pub fn take_io(
        &mut self,
    ) -> Result<(
        tokio::net::unix::OwnedReadHalf,
        tokio::net::unix::OwnedWriteHalf,
    )> {
        self.io
            .take()
            .map(UnixStream::into_split)
            .context("bridge IO was already taken")
    }

    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn test_child_pid(&self) -> Option<u32> {
        self.test_child_pid
    }

    pub async fn shutdown(mut self) -> Result<ExitReceipt> {
        self.control.cancel();
        if let Some(io) = self.io.take() {
            shutdown_ipc(&io);
        }
        let lifecycle = self
            .lifecycle
            .take()
            .context("bridge lifecycle is missing")?;
        timeout(CLEANUP_BOUND, lifecycle)
            .await
            .context("timed out joining stdio bridge cleanup")?
            .context("stdio bridge lifecycle task failed")?
    }
}

impl Drop for OwnedStdioBridge {
    fn drop(&mut self) {
        self.control.cancel();
        if let Some(io) = self.io.take() {
            shutdown_ipc(&io);
        }
    }
}

async fn own_child(
    mut child: tokio::process::Child,
    monitor: UnixStream,
    caller_cancel: CancellationToken,
    control: CancellationToken,
) -> Result<ExitReceipt> {
    tokio::select! {
        biased;
        status = child.wait() => {
            shutdown_ipc(&monitor);
            let status = status.context("cannot wait for stdio bridge")?;
            if status.success() {
                Ok(ExitReceipt::NormalEof)
            } else {
                bail!("stdio bridge exited unexpectedly")
            }
        }
        _ = caller_cancel.cancelled() => stop_child(&mut child, monitor).await,
        _ = control.cancelled() => stop_child(&mut child, monitor).await,
    }
}

async fn stop_child(child: &mut tokio::process::Child, monitor: UnixStream) -> Result<ExitReceipt> {
    shutdown_ipc(&monitor);
    let kill_error = child.start_kill().err();
    let _status = timeout(CLEANUP_BOUND, child.wait())
        .await
        .context("timed out reaping stdio bridge")?
        .context("cannot reap stdio bridge")?;
    if kill_error.is_some() {
        bail!("cannot stop stdio bridge");
    }
    Ok(ExitReceipt::RequestedStop)
}

fn shutdown_ipc(stream: &UnixStream) {
    // SAFETY: this operates only on our owned local socket endpoint.
    unsafe { libc::shutdown(stream.as_raw_fd(), libc::SHUT_RDWR) };
}

fn socket_pair() -> Result<(OwnedFd, OwnedFd)> {
    let mut fds = [-1; 2];
    // SAFETY: fds points to storage for the two returned descriptors.
    if unsafe { libc::socketpair(libc::AF_UNIX, libc::SOCK_STREAM, 0, fds.as_mut_ptr()) } != 0 {
        bail!("cannot create bridge IPC socket");
    }
    // SAFETY: successful socketpair returned two new descriptors.
    Ok(unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) })
}

fn set_cloexec(fd: RawFd, enabled: bool) -> Result<()> {
    // SAFETY: fcntl operates on the caller-owned descriptor.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags == -1 {
        bail!("invalid bridge descriptor");
    }
    let updated = if enabled {
        flags | libc::FD_CLOEXEC
    } else {
        flags & !libc::FD_CLOEXEC
    };
    // SAFETY: fcntl updates descriptor-local flags only.
    if unsafe { libc::fcntl(fd, libc::F_SETFD, updated) } == -1 {
        bail!("cannot configure bridge descriptor");
    }
    Ok(())
}

fn validate_endpoint(fd: BorrowedFd<'_>, input: bool) -> Result<()> {
    let raw = fd.as_raw_fd();
    // SAFETY: fcntl and fstat inspect this valid borrowed descriptor.
    let flags = unsafe { libc::fcntl(raw, libc::F_GETFL) };
    let mut stat = unsafe { std::mem::zeroed::<libc::stat>() };
    if flags == -1 || unsafe { libc::fstat(raw, &mut stat) } == -1 {
        bail!("unsupported stdio descriptor");
    }
    let mode = flags & libc::O_ACCMODE;
    if (input && mode == libc::O_WRONLY) || (!input && mode == libc::O_RDONLY) {
        bail!("stdio descriptor has incompatible access");
    }
    let kind = stat.st_mode & libc::S_IFMT;
    if kind == libc::S_IFIFO {
        return Ok(());
    }
    if kind == libc::S_IFSOCK && is_local_stream_socket(raw) {
        return Ok(());
    }
    if kind == libc::S_IFCHR {
        // SAFETY: isatty only queries this borrowed descriptor.
        if unsafe { libc::isatty(raw) } == 1 {
            return Ok(());
        }
    }
    bail!("unsupported stdio descriptor")
}

fn is_local_stream_socket(fd: RawFd) -> bool {
    let mut socket_type = 0;
    let mut type_length = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    let mut address = unsafe { std::mem::zeroed::<libc::sockaddr_storage>() };
    let mut address_length = std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
    // SAFETY: output pointers reference writable storage; fd is borrowed and
    // has already been verified as a socket by fstat.
    unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            &mut socket_type as *mut _ as *mut _,
            &mut type_length,
        ) == 0
            && socket_type == libc::SOCK_STREAM
            && libc::getsockname(fd, &mut address as *mut _ as *mut _, &mut address_length) == 0
            && address.ss_family as libc::c_int == libc::AF_UNIX
    }
}

/// Parse only the fixed internal child mode. A malformed attempt returns a
/// generic error and can never fall through to the public CLI parser.
pub fn hidden_child_fd(args: impl IntoIterator<Item = OsString>) -> Result<Option<RawFd>> {
    let args: Vec<OsString> = args.into_iter().collect();
    if args.get(1).and_then(|v| v.to_str()) != Some(CHILD_MODE) {
        return Ok(None);
    }
    if args.len() != 4 || args.get(2).and_then(|v| v.to_str()) != Some(CHILD_FD_FLAG) {
        bail!("invalid internal bridge invocation");
    }
    let raw: RawFd = args[3]
        .to_str()
        .and_then(|v| v.parse().ok())
        .filter(|v| *v >= 3)
        .ok_or_else(|| anyhow::anyhow!("invalid internal bridge invocation"))?;
    Ok(Some(raw))
}

/// Called by the executable before it initializes logging, signals, or Tokio.
pub fn run_hidden_child(fd: RawFd) -> Result<()> {
    validate_ipc(fd)?;
    // SAFETY: the hidden mode accepts only the descriptor deliberately inherited
    // by the parent owner after validating the socket type and family.
    let socket = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
    let input_socket = socket
        .try_clone()
        .context("invalid internal bridge invocation")?;
    std::thread::spawn(move || bridge_stdin(input_socket));
    bridge_stdout(socket)
}

fn validate_ipc(fd: RawFd) -> Result<()> {
    let mut socket_type = 0;
    let mut length = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    let mut address = unsafe { std::mem::zeroed::<libc::sockaddr_storage>() };
    let mut address_length = std::mem::size_of::<libc::sockaddr_storage>() as libc::socklen_t;
    // SAFETY: all pointers refer to initialized writable storage and fd is only
    // used after the hidden fixed-mode argument was parsed.
    let valid = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_TYPE,
            &mut socket_type as *mut _ as *mut _,
            &mut length,
        ) == 0
            && socket_type == libc::SOCK_STREAM
            && libc::getsockname(fd, &mut address as *mut _ as *mut _, &mut address_length) == 0
            && address.ss_family as libc::c_int == libc::AF_UNIX
    };
    if !valid {
        bail!("invalid internal bridge invocation");
    }
    Ok(())
}

fn bridge_stdin(mut socket: std::os::unix::net::UnixStream) {
    let mut buffer = [0u8; COPY_BUFFER];
    loop {
        match read_inherited(libc::STDIN_FILENO, &mut buffer) {
            Ok(0) => break,
            Ok(count) => {
                if socket.write_all(&buffer[..count]).is_err() {
                    break;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    let _ = socket.shutdown(std::net::Shutdown::Write);
}

fn bridge_stdout(mut socket: std::os::unix::net::UnixStream) -> Result<()> {
    let mut buffer = [0u8; COPY_BUFFER];
    loop {
        match socket.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(count) => {
                write_inherited(libc::STDOUT_FILENO, &buffer[..count])
                    .context("bridge output failed")?;
            }
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => bail!("bridge IPC failed"),
        }
    }
}

fn read_inherited(fd: RawFd, buffer: &mut [u8]) -> io::Result<usize> {
    loop {
        // SAFETY: buffer is writable for its full length and fd is the child
        // process's validated stdin descriptor.
        let result = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
        if result >= 0 {
            return Ok(result as usize);
        }
        let error = io::Error::last_os_error();
        match error.kind() {
            io::ErrorKind::Interrupted => continue,
            io::ErrorKind::WouldBlock => wait_inherited(fd, libc::POLLIN)?,
            _ => return Err(error),
        }
    }
}

fn write_inherited(fd: RawFd, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        // SAFETY: bytes points to readable storage for its full length.
        let result = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if result > 0 {
            bytes = &bytes[result as usize..];
            continue;
        }
        if result == 0 {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "bridge output failed",
            ));
        }
        let error = io::Error::last_os_error();
        match error.kind() {
            io::ErrorKind::Interrupted => continue,
            io::ErrorKind::WouldBlock => wait_inherited(fd, libc::POLLOUT)?,
            _ => return Err(error),
        }
    }
    Ok(())
}

fn wait_inherited(fd: RawFd, events: libc::c_short) -> io::Result<()> {
    let mut pollfd = libc::pollfd {
        fd,
        events,
        revents: 0,
    };
    loop {
        // SAFETY: pollfd points to one initialized poll descriptor.
        let result = unsafe { libc::poll(&mut pollfd, 1, -1) };
        if result > 0 {
            return Ok(());
        }
        if result == 0 {
            continue;
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}
