use std::env;
use std::ffi::OsStr;
use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::ffi::OsStringExt;
use std::path::{Path, PathBuf};
use std::ptr::null_mut;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Security::Cryptography::{
    BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
};
use windows_sys::Win32::UI::Controls::Dialogs::{
    GetOpenFileNameW, OFN_FILEMUSTEXIST, OFN_PATHMUSTEXIST, OPENFILENAMEW,
};
use windows_sys::Win32::UI::Shell::ShellExecuteW;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    IDYES, MB_ICONQUESTION, MB_YESNO, MessageBoxW, SW_SHOWNORMAL,
};

use crate::instance::{Instance, InstanceNames, PrimaryInstance, Request};
use crate::job::{Job, SupervisedProcess};
use crate::paths::DesktopPaths;
use crate::protocol::Command;
use crate::startup;
use crate::tray::{self, TrayAction};

const INSTANCE_ID: &str = "desktop-v1";

pub fn run(initial: Command) -> io::Result<()> {
    let initial = absolute_command(initial)?;
    match Instance::acquire(InstanceNames::current_user()?)? {
        Instance::Secondary(instance) => {
            instance.send(&initial, Duration::from_secs(75))?;
            if initial == Command::ShutdownForUpgrade {
                instance.wait_for_exit(Duration::from_secs(20))?;
            }
            return Ok(());
        }
        Instance::Primary(instance) => {
            if initial == Command::ShutdownForUpgrade {
                return Ok(());
            }

            let paths = DesktopPaths::discover()?;
            paths.create()?;
            let token = launch_token()?;
            let (job, process, port) = start_ready_server(&paths, &token)?;
            // The server is up; a failed initial open (missing file, browser
            // launch error) must not tear it down — the tray stays available,
            // matching how tray and pipe-forwarded opens treat the same error.
            if let Err(error) = open_command(port, initial) {
                paths.log(&format!("initial open failed: {error}"));
            }
            let login_enabled = startup::enabled().unwrap_or_else(|error| {
                paths.log(&format!(
                    "could not read sign-in setting, defaulting to off: {error}"
                ));
                false
            });
            let tray = tray::start(port, login_enabled, paths.clone());
            let (sender, receiver) = mpsc::channel();
            let mut pipe = Some(instance.serve(sender));
            let mut stop_pipe_locally = false;
            let mut shutdown_response = None;

            'running: loop {
                while let Ok(action) = tray.try_recv() {
                    match action {
                        TrayAction::Open => {
                            let _ = open_command(port, Command::OpenHome);
                        }
                        TrayAction::OpenFile => {
                            if let Some(path) = choose_file()? {
                                let _ = open_command(port, Command::Open(path));
                            }
                        }
                        TrayAction::OpenLogs => {
                            let _ = open_path(&paths.logs);
                        }
                        TrayAction::DefaultApps => {
                            let _ = open_uri("ms-settings:defaultapps");
                        }
                        TrayAction::Exit if confirm_exit() => {
                            let _ = graceful_shutdown(port, &token);
                            stop_pipe_locally = true;
                            break 'running;
                        }
                        TrayAction::Exit => {}
                    }
                }
                match receiver.recv_timeout(Duration::from_millis(250)) {
                    Ok(request) if request.command == Command::ShutdownForUpgrade => {
                        let _ = graceful_shutdown(port, &token);
                        shutdown_response = Some(request.response);
                        break;
                    }
                    Ok(request) => {
                        let status = u32::from(open_command(port, request.command).is_err());
                        let _ = request.response.send(status);
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) if process.wait(0) => {
                        stop_pipe(&instance, &receiver, pipe.take().unwrap())?;
                        return Err(io::Error::other("Python server exited unexpectedly"));
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        pipe.take()
                            .unwrap()
                            .join()
                            .map_err(|_| io::Error::other("supervisor command pipe panicked"))??;
                        return Err(io::Error::other("supervisor command pipe stopped"));
                    }
                }
            }
            if stop_pipe_locally {
                stop_pipe(&instance, &receiver, pipe.take().unwrap())?;
            }

            let teardown = if !process.wait(5_000) {
                drop(job);
                if !process.wait(5_000) {
                    Err(io::Error::other("Python process tree did not stop"))
                } else {
                    Ok(())
                }
            } else {
                drop(job);
                Ok(())
            };
            if let Some(response) = shutdown_response {
                let _ = response.send(u32::from(teardown.is_err()));
                pipe.take()
                    .unwrap()
                    .join()
                    .map_err(|_| io::Error::other("supervisor command pipe panicked"))??;
            }
            teardown?;
        }
    }
    Ok(())
}

fn choose_file() -> io::Result<Option<PathBuf>> {
    let mut file = vec![0u16; 32_768];
    let filter: Vec<u16> = "All files\0*.*\0\0".encode_utf16().collect();
    let mut dialog = OPENFILENAMEW {
        lStructSize: std::mem::size_of::<OPENFILENAMEW>() as u32,
        lpstrFilter: filter.as_ptr(),
        lpstrFile: file.as_mut_ptr(),
        nMaxFile: file.len() as u32,
        Flags: OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST,
        ..Default::default()
    };
    if unsafe { GetOpenFileNameW(&mut dialog) } == 0 {
        return Ok(None);
    }
    let len = file
        .iter()
        .position(|unit| *unit == 0)
        .unwrap_or(file.len());
    Ok(Some(PathBuf::from(std::ffi::OsString::from_wide(
        &file[..len],
    ))))
}

fn confirm_exit() -> bool {
    let message = wide_null(OsStr::new(
        "Stop FusedRender and all running render processes?",
    ));
    let title = wide_null(OsStr::new("Exit FusedRender"));
    unsafe {
        MessageBoxW(
            null_mut(),
            message.as_ptr(),
            title.as_ptr(),
            MB_YESNO | MB_ICONQUESTION,
        ) == IDYES
    }
}

fn open_path(path: &Path) -> io::Result<()> {
    let operation = wide_null(OsStr::new("open"));
    let path = wide_null(path.as_os_str());
    let result = unsafe {
        ShellExecuteW(
            null_mut(),
            operation.as_ptr(),
            path.as_ptr(),
            null_mut(),
            null_mut(),
            SW_SHOWNORMAL,
        )
    } as isize;
    if result <= 32 {
        Err(io::Error::other("could not open path"))
    } else {
        Ok(())
    }
}

fn open_uri(uri: &str) -> io::Result<()> {
    open_path(Path::new(uri))
}

fn stop_pipe(
    instance: &PrimaryInstance,
    receiver: &mpsc::Receiver<Request>,
    pipe: thread::JoinHandle<io::Result<()>>,
) -> io::Result<()> {
    let client = instance.client();
    let stop =
        thread::spawn(move || client.send(&Command::ShutdownForUpgrade, Duration::from_secs(5)));
    loop {
        let request = receiver
            .recv_timeout(Duration::from_secs(5))
            .map_err(|_| io::Error::other("could not stop supervisor command pipe"))?;
        let shutdown = request.command == Command::ShutdownForUpgrade;
        let _ = request.response.send(u32::from(!shutdown));
        if shutdown {
            break;
        }
    }
    stop.join()
        .map_err(|_| io::Error::other("command pipe stop client panicked"))??;
    pipe.join()
        .map_err(|_| io::Error::other("supervisor command pipe panicked"))??;
    Ok(())
}

fn start_ready_server(
    paths: &DesktopPaths,
    token: &str,
) -> io::Result<(Job, SupervisedProcess, u16)> {
    let mut last_error = None;
    for _ in 0..3 {
        let port = available_port()?;
        let job = Job::new()?;
        let process = start_server(&job, paths, port, token)?;
        match wait_until_ready(&process, port, token, Duration::from_secs(20)) {
            Ok(()) => return Ok((job, process, port)),
            Err(error) => {
                drop(job);
                let _ = process.wait(5_000);
                last_error = Some(error);
            }
        }
    }
    Err(last_error.unwrap_or_else(|| io::Error::other("Python server failed to start")))
}

fn start_server(
    job: &Job,
    paths: &DesktopPaths,
    port: u16,
    token: &str,
) -> io::Result<SupervisedProcess> {
    let python_dir = current_install_dir()?.join("python");
    let python = python_dir.join("pythonw.exe");
    if !python.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("private Python runtime not found: {}", python.display()),
        ));
    }
    let arguments = [
        "-I".into(),
        "-m".into(),
        "fused_render.cli".into(),
        "serve".into(),
        "--no-browser".into(),
        "--port".into(),
        port.to_string().into(),
    ];
    job.spawn(
        &python,
        &arguments,
        &paths.child_environment(INSTANCE_ID, token, &python_dir),
        Some(&paths.logs.join("server-console.log")),
    )
}

fn absolute_command(command: Command) -> io::Result<Command> {
    match command {
        Command::Open(path) if !path.is_absolute() => {
            Ok(Command::Open(env::current_dir()?.join(path)))
        }
        command => Ok(command),
    }
}

fn current_install_dir() -> io::Result<PathBuf> {
    let executable = env::current_exe()?;
    executable
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| io::Error::other("supervisor executable has no parent directory"))
}

fn available_port() -> io::Result<u16> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    Ok(listener.local_addr()?.port())
}

fn wait_until_ready(
    process: &SupervisedProcess,
    port: u16,
    token: &str,
    timeout: Duration,
) -> io::Result<()> {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if process.wait(0) {
            return Err(io::Error::other("Python server failed during startup"));
        }
        if matching_server(port, token) {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "Python server did not become ready",
    ))
}

fn matching_server(port: u16, token: &str) -> bool {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let request = format!(
        "GET /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Fused-Desktop-Token: {token}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200")
        && response.contains(&format!(r#""id":"{INSTANCE_ID}""#))
        && response.contains(&format!(r#""token":"{token}""#))
}

fn graceful_shutdown(port: u16, token: &str) -> io::Result<()> {
    let address = SocketAddr::from((Ipv4Addr::LOCALHOST, port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(1))?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    let request = format!(
        "POST /api/desktop/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Fused-Desktop-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    if response.starts_with("HTTP/1.1 200") {
        Ok(())
    } else {
        Err(io::Error::other("Python server rejected graceful shutdown"))
    }
}

fn open_command(port: u16, command: Command) -> io::Result<()> {
    let url = match command {
        Command::Open(path) => view_url(port, &path)?,
        Command::OpenHome => format!("http://127.0.0.1:{port}/"),
        Command::StartInBackground | Command::ShutdownForUpgrade => return Ok(()),
    };
    open_browser(&url)
}

fn view_url(port: u16, path: &Path) -> io::Result<String> {
    if !path.exists() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!("file not found: {}", path.display()),
        ));
    }
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()?.join(path)
    };
    let raw = absolute.to_string_lossy();
    let normalized = if raw.as_bytes().get(1) == Some(&b':') {
        raw.replace('\\', "/")
    } else {
        raw.into_owned()
    };
    let segments = normalized
        .trim_start_matches('/')
        .split('/')
        .filter(|segment| !segment.is_empty())
        .map(percent_encode)
        .collect::<Vec<_>>()
        .join("/");
    Ok(format!("http://127.0.0.1:{port}/view/{segments}"))
}

fn percent_encode(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(*byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn open_browser(url: &str) -> io::Result<()> {
    if env::var_os("FUSED_RENDER_SUPERVISOR_NO_BROWSER").is_some() {
        return Ok(());
    }
    let operation = wide_null(OsStr::new("open"));
    let url = wide_null(OsStr::new(url));
    let result = unsafe {
        ShellExecuteW(
            null_mut(),
            operation.as_ptr(),
            url.as_ptr(),
            null_mut(),
            null_mut(),
            SW_SHOWNORMAL,
        )
    } as isize;
    if result <= 32 {
        Err(io::Error::other(format!(
            "could not open the default browser (ShellExecuteW={result})"
        )))
    } else {
        Ok(())
    }
}

fn launch_token() -> io::Result<String> {
    let mut bytes = [0u8; 32];
    let status = unsafe {
        BCryptGenRandom(
            null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status != 0 {
        return Err(io::Error::other(format!(
            "BCryptGenRandom failed with status {status:#x}"
        )));
    }
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn wide_null(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_drive_and_unicode_paths() {
        let path = Path::new(r"C:\data\résumé.xlsx");
        let normalized = path.to_string_lossy().replace('\\', "/");
        let segments = normalized
            .split('/')
            .map(percent_encode)
            .collect::<Vec<_>>()
            .join("/");
        assert_eq!(segments, "C%3A/data/r%C3%A9sum%C3%A9.xlsx");
    }

    #[test]
    fn launch_tokens_are_random_and_256_bit() {
        let first = launch_token().unwrap();
        let second = launch_token().unwrap();
        assert_eq!(first.len(), 64);
        assert_ne!(first, second);
    }
}
