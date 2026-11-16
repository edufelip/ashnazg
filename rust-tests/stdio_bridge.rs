#![cfg(unix)]

use std::io::{Read, Seek, Write};
use std::os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd};
use std::time::Duration;

use ashnazg::stdio_bridge::OwnedStdioBridge;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio_util::sync::CancellationToken;

fn pipe() -> (OwnedFd, OwnedFd) {
    let mut fds = [-1; 2];
    // SAFETY: fds points to two writable descriptors.
    let result = unsafe { libc::pipe(fds.as_mut_ptr()) };
    assert_eq!(result, 0, "pipe: {}", std::io::Error::last_os_error());
    // SAFETY: pipe returned two new owned descriptors.
    let (read, write) = unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
    for fd in [read.as_raw_fd(), write.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert_ne!(flags, -1);
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) },
            0
        );
    }
    (read, write)
}

fn unix_socket_pair(socket_type: libc::c_int) -> (OwnedFd, OwnedFd) {
    let mut fds = [-1; 2];
    assert_eq!(
        unsafe { libc::socketpair(libc::AF_UNIX, socket_type, 0, fds.as_mut_ptr()) },
        0
    );
    let (left, right) = unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
    for fd in [left.as_raw_fd(), right.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) },
            0
        );
    }
    (left, right)
}

fn child_executable() -> &'static std::path::Path {
    std::path::Path::new(env!("CARGO_BIN_EXE_ashnazg"))
}

fn child_pid_for_fixture(bridge: &OwnedStdioBridge) -> Option<u32> {
    #[cfg(debug_assertions)]
    {
        bridge.test_child_pid()
    }
    #[cfg(not(debug_assertions))]
    {
        let _ = bridge;
        None
    }
}

#[tokio::test]
async fn idle_input_cancellation_reaps_child_without_cancelling_caller_token() {
    let (input_reader, _input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    let caller_cancel = CancellationToken::new();
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        caller_cancel.clone(),
    )
    .expect("bridge should spawn");
    let (_reader, _writer) = bridge.take_io().expect("IO may be taken once");
    let child_pid = child_pid_for_fixture(&bridge);
    tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
        .await
        .expect("shutdown must join the owned child within two seconds")
        .expect("shutdown must reap the child");
    if let Some(pid) = child_pid {
        assert_process_reaped(pid).await;
    }
    assert!(
        !caller_cancel.is_cancelled(),
        "bridge cleanup must preserve caller token"
    );
}

#[tokio::test]
async fn partial_input_eof_preserves_order_and_output_can_still_drain() {
    let (input_reader, input_writer) = pipe();
    let (output_reader, output_writer) = pipe();
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .expect("bridge should spawn");
    let (mut reader, mut writer) = bridge.take_io().unwrap();
    let expected = b"partial input before eof";
    std::fs::File::from(input_writer.try_clone().unwrap())
        .write_all(expected)
        .unwrap();
    drop(input_writer);
    let mut got = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), reader.read_to_end(&mut got))
        .await
        .unwrap_or_else(|_| panic!("input EOF timed out after {} bytes", got.len()))
        .unwrap();
    assert_eq!(got, expected);

    let response = b"response after input eof";
    let expected_response = response.to_vec();
    let mut output_reader = std::fs::File::from(output_reader);
    let output_task = tokio::task::spawn_blocking(move || {
        let mut got = vec![0; expected_response.len()];
        output_reader.read_exact(&mut got).unwrap();
        got
    });
    writer.write_all(response).await.unwrap();
    assert_eq!(output_task.await.unwrap(), response);
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
            .await
            .unwrap()
            .unwrap(),
        ashnazg::stdio_bridge::ExitReceipt::RequestedStop
    );
}

#[tokio::test]
async fn ipc_output_eof_ends_child_without_joining_blocked_stdin_thread() {
    let (input_reader, _input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    let (mut reader, mut writer) = bridge.take_io().unwrap();
    writer.shutdown().await.unwrap();
    let mut response = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), reader.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    assert!(response.is_empty());
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
            .await
            .unwrap()
            .unwrap(),
        ashnazg::stdio_bridge::ExitReceipt::NormalEof
    );
}

#[tokio::test]
async fn test_owned_unix_stream_stdio_is_supported_by_the_isolated_child() {
    let (stdio_socket, peer_socket) = unix_socket_pair(libc::SOCK_STREAM);
    let mut peer = std::os::unix::net::UnixStream::from(peer_socket);
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        stdio_socket.as_fd(),
        stdio_socket.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    let (mut reader, mut writer) = bridge.take_io().unwrap();
    peer.write_all(b"socket input").unwrap();
    peer.shutdown(std::net::Shutdown::Write).unwrap();
    let mut received = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), reader.read_to_end(&mut received))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(received, b"socket input");

    writer.write_all(b"socket output").await.unwrap();
    let mut response = [0; b"socket output".len()];
    peer.read_exact(&mut response).unwrap();
    assert_eq!(&response, b"socket output");
    bridge.shutdown().await.unwrap();
}

#[tokio::test]
async fn large_input_burst_is_ordered_across_paused_reader_backpressure() {
    let (input_reader, input_writer) = pipe();
    let (output_reader, output_writer) = pipe();
    let before = unsafe { libc::fcntl(input_reader.as_fd().as_raw_fd(), libc::F_GETFL) };
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .expect("bridge should spawn");
    let (mut reader, _writer) = bridge.take_io().unwrap();
    let payload: Vec<u8> = (0..(256 * 1024)).map(|n| (n % 251) as u8).collect();
    let expected = payload.clone();
    let input_task = std::thread::spawn(move || {
        let mut file = std::fs::File::from(input_writer);
        file.write_all(&payload).unwrap();
    });
    // Intentionally leave the async reader paused while the actual child fills
    // the bounded IPC path and blocks on backpressure.
    tokio::task::yield_now().await;
    let mut got = Vec::new();
    tokio::time::timeout(Duration::from_secs(5), reader.read_to_end(&mut got))
        .await
        .unwrap()
        .unwrap();
    input_task.join().unwrap();
    assert_eq!(got, expected);
    let after = unsafe { libc::fcntl(input_reader.as_fd().as_raw_fd(), libc::F_GETFL) };
    assert_eq!(before & !libc::O_NONBLOCK, after & !libc::O_NONBLOCK);
    drop(output_reader);
    bridge.shutdown().await.unwrap();
}

#[tokio::test]
async fn competing_reader_consuming_pipe_data_does_not_block_cancellation() {
    let (input_reader, input_writer) = pipe();
    let alias = input_reader.try_clone().unwrap();
    let mut input_file = std::fs::File::from(input_writer);
    input_file
        .write_all(b"readiness consumed by alias")
        .unwrap();
    let mut alias_file = std::fs::File::from(alias);
    let mut stolen = vec![0; b"readiness consumed by alias".len()];
    alias_file.read_exact(&mut stolen).unwrap();
    assert_eq!(stolen, b"readiness consumed by alias");
    // Keep a writer open so the child's input read must wait rather than
    // immediately observing EOF after the alias consumed the available data.

    let (_output_reader, output_writer) = pipe();
    let bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
        .await
        .unwrap()
        .unwrap();
    drop(input_file);
}

#[tokio::test]
async fn full_output_pipe_backpressure_is_cancelled_and_reaped() {
    let (input_reader, input_writer) = pipe();
    let (output_reader, output_writer) = pipe();
    let output_fd = output_writer.as_fd().as_raw_fd();
    let old_flags = unsafe { libc::fcntl(output_fd, libc::F_GETFL) };
    assert_ne!(old_flags, -1);
    assert_eq!(
        unsafe { libc::fcntl(output_fd, libc::F_SETFL, old_flags | libc::O_NONBLOCK) },
        0
    );
    let mut fill = [0x5a; 4096];
    loop {
        let result = unsafe { libc::write(output_fd, fill.as_mut_ptr().cast(), fill.len()) };
        if result >= 0 {
            continue;
        }
        let error = std::io::Error::last_os_error();
        let code = error.raw_os_error();
        assert!(code == Some(libc::EAGAIN) || code == Some(libc::EWOULDBLOCK));
        break;
    }
    assert_eq!(
        unsafe { libc::fcntl(output_fd, libc::F_SETFL, old_flags) },
        0
    );

    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    let child_pid = child_pid_for_fixture(&bridge);
    let (mut reader, mut writer) = bridge.take_io().unwrap();
    std::fs::File::from(input_writer)
        .write_all(b"ready")
        .unwrap();
    let mut ready = [0; 5];
    tokio::time::timeout(Duration::from_secs(2), reader.read_exact(&mut ready))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        &ready, b"ready",
        "the actual child must be running before backpressure"
    );
    drive_output_until_backpressured(&mut writer).await;
    tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
        .await
        .unwrap()
        .unwrap();
    if let Some(pid) = child_pid {
        assert_process_reaped(pid).await;
    }
    drop(output_reader);
}

async fn drive_output_until_backpressured(writer: &mut tokio::net::unix::OwnedWriteHalf) {
    let chunk = [0x42; 16 * 1024];
    let mut total = 0;
    loop {
        match tokio::time::timeout(Duration::from_millis(100), writer.write(&chunk)).await {
            Ok(Ok(count)) if count > 0 => total += count,
            Err(_) => break,
            result => panic!("unexpected write result before backpressure: {result:?}"),
        }
        assert!(
            total <= 16 * 1024 * 1024,
            "IPC must not accumulate unbounded output"
        );
    }
    assert!(
        total > 0,
        "first-poll pending alone is not evidence of backpressure"
    );
}

#[tokio::test]
async fn test_owned_canonical_xoff_terminal_is_cancelled_without_input_or_output_progress() {
    let mut master = -1;
    let mut slave = -1;
    // SAFETY: openpty writes two new descriptors into these variables.
    assert_eq!(
        unsafe {
            libc::openpty(
                &mut master,
                &mut slave,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        0,
        "openpty: {}",
        std::io::Error::last_os_error()
    );
    // SAFETY: openpty returned two owned descriptors.
    let master = unsafe { OwnedFd::from_raw_fd(master) };
    // SAFETY: openpty returned two owned descriptors.
    let slave = unsafe { OwnedFd::from_raw_fd(slave) };
    for fd in [master.as_raw_fd(), slave.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) },
            0
        );
    }
    let mut settings = unsafe { std::mem::zeroed::<libc::termios>() };
    assert_eq!(
        unsafe { libc::tcgetattr(slave.as_raw_fd(), &mut settings) },
        0
    );
    settings.c_lflag |= libc::ICANON;
    settings.c_iflag |= libc::IXON;
    settings.c_cc[libc::VSTOP] = 0x13;
    settings.c_cc[libc::VSTART] = 0x11;
    assert_eq!(
        unsafe { libc::tcsetattr(slave.as_raw_fd(), libc::TCSANOW, &settings) },
        0
    );
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        slave.as_fd(),
        slave.as_fd(),
        CancellationToken::new(),
    )
    .expect("test-owned terminal descriptors should be accepted");
    let child_pid = child_pid_for_fixture(&bridge);
    let (_reader, mut writer) = bridge.take_io().unwrap();
    writer.write_all(b"ready").await.unwrap();
    let mut pollfd = libc::pollfd {
        fd: master.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    assert_eq!(
        unsafe { libc::poll(&mut pollfd, 1, 1000) },
        1,
        "the child must produce its readiness bytes"
    );
    let mut ready = [0; 5];
    assert_eq!(
        unsafe { libc::read(master.as_raw_fd(), ready.as_mut_ptr().cast(), ready.len()) },
        5
    );
    assert_eq!(&ready, b"ready");
    let xoff = [settings.c_cc[libc::VSTOP]];
    assert_eq!(
        unsafe { libc::write(master.as_raw_fd(), xoff.as_ptr().cast(), 1) },
        1
    );
    drive_output_until_backpressured(&mut writer).await;
    pollfd.revents = 0;
    assert_eq!(
        unsafe { libc::poll(&mut pollfd, 1, 0) },
        0,
        "XOFF must actually suppress terminal output"
    );
    tokio::time::timeout(Duration::from_secs(2), bridge.shutdown())
        .await
        .unwrap()
        .unwrap();
    if let Some(pid) = child_pid {
        assert_process_reaped(pid).await;
    }
}

#[tokio::test]
async fn closed_output_reader_causes_bounded_child_failure_not_parent_sigpipe() {
    let (input_reader, _input_writer) = pipe();
    let (output_reader, output_writer) = pipe();
    drop(output_reader);
    let mut bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .expect("bridge should spawn");
    let child_pid = child_pid_for_fixture(&bridge);
    let (mut reader, mut writer) = bridge.take_io().unwrap();
    let write_result =
        tokio::time::timeout(Duration::from_secs(2), writer.write_all(b"break stdout")).await;
    assert!(
        write_result.is_ok(),
        "the parent async write should not block indefinitely"
    );
    let mut eof = Vec::new();
    tokio::time::timeout(Duration::from_secs(2), reader.read_to_end(&mut eof))
        .await
        .unwrap()
        .unwrap();
    let result = tokio::time::timeout(Duration::from_secs(2), bridge.shutdown()).await;
    assert!(result.is_ok(), "lifecycle join must be bounded");
    assert!(
        result.unwrap().is_err(),
        "unexpected child exit must be reported"
    );
    if let Some(pid) = child_pid {
        assert_process_reaped(pid).await;
    }
}

#[tokio::test]
async fn rejects_regular_files_and_reversed_pipe_access_before_startup() {
    let mut file = tempfile::tempfile().unwrap();
    file.write_all(b"caller data must remain unchanged")
        .unwrap();
    file.rewind().unwrap();
    let (input_reader, _input_writer) = pipe();
    assert!(
        OwnedStdioBridge::spawn(
            child_executable(),
            input_reader.as_fd(),
            file.as_fd(),
            CancellationToken::new(),
        )
        .is_err()
    );
    file.rewind().unwrap();
    let mut contents = Vec::new();
    file.read_to_end(&mut contents).unwrap();
    assert_eq!(contents, b"caller data must remain unchanged");

    let (_input_reader, input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    assert!(
        OwnedStdioBridge::spawn(
            child_executable(),
            input_writer.as_fd(),
            output_writer.as_fd(),
            CancellationToken::new(),
        )
        .is_err()
    );
    let (readable_output, _output_writer) = pipe();
    let (input_reader, _input_writer) = pipe();
    assert!(
        OwnedStdioBridge::spawn(
            child_executable(),
            input_reader.as_fd(),
            readable_output.as_fd(),
            CancellationToken::new(),
        )
        .is_err()
    );

    let (datagram, _peer) = unix_socket_pair(libc::SOCK_DGRAM);
    assert!(
        OwnedStdioBridge::spawn(
            child_executable(),
            datagram.as_fd(),
            datagram.as_fd(),
            CancellationToken::new(),
        )
        .is_err()
    );
}

#[tokio::test]
async fn output_pipe_status_flags_are_unchanged_after_bridge_lifecycle() {
    let (input_reader, _input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    let fd = output_writer.as_fd().as_raw_fd();
    let before = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    let bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    bridge.shutdown().await.unwrap();
    let after = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    assert_eq!(before & !libc::O_NONBLOCK, after & !libc::O_NONBLOCK);
    assert_eq!(before & libc::O_NONBLOCK, after & libc::O_NONBLOCK);
}

#[tokio::test]
async fn caller_nonblocking_flags_survive_and_aliases_keep_the_same_status() {
    let (input_reader, _input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    let input_alias = input_reader.try_clone().unwrap();
    let output_alias = output_writer.try_clone().unwrap();
    let input_fd = input_reader.as_raw_fd();
    let output_fd = output_writer.as_raw_fd();
    let input_old = unsafe { libc::fcntl(input_fd, libc::F_GETFL) };
    let output_old = unsafe { libc::fcntl(output_fd, libc::F_GETFL) };
    assert_eq!(
        unsafe { libc::fcntl(input_fd, libc::F_SETFL, input_old | libc::O_NONBLOCK) },
        0
    );
    assert_eq!(
        unsafe { libc::fcntl(output_fd, libc::F_SETFL, output_old | libc::O_NONBLOCK) },
        0
    );

    let bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    bridge.shutdown().await.unwrap();
    for fd in [input_fd, input_alias.as_raw_fd()] {
        assert_ne!(
            unsafe { libc::fcntl(fd, libc::F_GETFL) } & libc::O_NONBLOCK,
            0
        );
    }
    for fd in [output_fd, output_alias.as_raw_fd()] {
        assert_ne!(
            unsafe { libc::fcntl(fd, libc::F_GETFL) } & libc::O_NONBLOCK,
            0
        );
    }
}

#[tokio::test]
async fn owner_drop_keeps_cleanup_running_while_runtime_is_alive() {
    let (input_reader, _input_writer) = pipe();
    let (_output_reader, output_writer) = pipe();
    let bridge = OwnedStdioBridge::spawn(
        child_executable(),
        input_reader.as_fd(),
        output_writer.as_fd(),
        CancellationToken::new(),
    )
    .unwrap();
    let child_pid = child_pid_for_fixture(&bridge);
    drop(bridge);
    if let Some(pid) = child_pid {
        assert_process_reaped(pid).await;
    }
}

async fn assert_process_reaped(pid: u32) {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            // SAFETY: signal 0 only observes this fixture's owned child.
            if unsafe { libc::kill(pid as libc::pid_t, 0) } == -1
                && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("owned child must disappear from the process table");
}

#[tokio::test]
async fn malformed_hidden_descriptor_is_rejected_by_actual_executable() {
    let output = std::process::Command::new(child_executable())
        .args(["--__ashnazg-stdio-bridge", "--fd", "0"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        output.stderr.is_empty(),
        "hidden child must not emit descriptor details"
    );
}

#[test]
fn inherited_stdin_read_error_exits_child_with_failure_instead_of_eof() {
    use std::os::unix::process::CommandExt;
    struct ReapOnDrop(std::process::Child);
    impl Drop for ReapOnDrop {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let directory = tempfile::tempdir().unwrap();
    let (parent, child_socket) = unix_socket_pair(libc::SOCK_STREAM);
    let raw = child_socket.as_raw_fd();
    let mut command = std::process::Command::new(child_executable());
    command
        .args(["--__ashnazg-stdio-bridge", "--fd", &raw.to_string()])
        .stdin(std::fs::File::open(directory.path()).unwrap())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    // This actual-child fault fixture deliberately supplies an inherited fd
    // whose read returns EISDIR. The public parent API rejects directories.
    unsafe {
        command.pre_exec(move || {
            let flags = libc::fcntl(raw, libc::F_GETFD);
            if flags < 0 || libc::fcntl(raw, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = ReapOnDrop(command.spawn().unwrap());
    drop(child_socket);
    let mut parent = std::os::unix::net::UnixStream::from(parent);
    parent
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    let mut eof = [0];
    assert_eq!(
        parent.read(&mut eof).unwrap(),
        0,
        "wait for the child to attempt the failing input read before measuring its exit"
    );
    let deadline = std::time::Instant::now() + Duration::from_millis(500);
    let status = loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            child.0.kill().unwrap();
            break child.0.wait().unwrap();
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    drop(parent);
    assert_eq!(
        status.code(),
        Some(1),
        "read errors must fail without waiting for parent output EOF"
    );
}

#[tokio::test]
async fn public_help_does_not_expose_internal_bridge_mode() {
    let output = std::process::Command::new(child_executable())
        .arg("--help")
        .output()
        .unwrap();
    assert!(output.status.success());
    let help = String::from_utf8_lossy(&output.stdout);
    assert!(help.contains("Host control CLI and MCP server"));
    assert!(!help.contains("stdio-bridge"));
}
