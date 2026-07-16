#![windows_subsystem = "windows"]

use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;

use fused_render_windows_supervisor::paths::DesktopPaths;
use fused_render_windows_supervisor::protocol::parse_args;
use fused_render_windows_supervisor::supervisor;
use windows_sys::Win32::UI::Shell::SetCurrentProcessExplicitAppUserModelID;
use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONERROR, MB_OK, MessageBoxW};

fn main() {
    let result = set_app_user_model_id()
        .and_then(|()| parse_args(std::env::args_os().skip(1)))
        .and_then(supervisor::run);
    if let Err(error) = result {
        log_error(&error);
        let message = wide_null(OsStr::new(&format!(
            "FusedRender could not start:\n\n{error}"
        )));
        let title = wide_null(OsStr::new("FusedRender"));
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                message.as_ptr(),
                title.as_ptr(),
                MB_OK | MB_ICONERROR,
            )
        };
        std::process::exit(1);
    }
}

fn log_error(error: &dyn std::fmt::Display) {
    if let Ok(paths) = DesktopPaths::discover() {
        paths.log(&error.to_string());
    }
}

fn set_app_user_model_id() -> std::io::Result<()> {
    let app_id = wide_null(OsStr::new("Fused.FusedRender.Desktop"));
    let result = unsafe { SetCurrentProcessExplicitAppUserModelID(app_id.as_ptr()) };
    if result >= 0 {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "could not set application identity (HRESULT 0x{:08X})",
            result as u32
        )))
    }
}

fn wide_null(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}
