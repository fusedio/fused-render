use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

use tray_icon::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIconBuilder, TrayIconEvent};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, MSG, PM_REMOVE, PeekMessageW, TranslateMessage,
};

use crate::paths::DesktopPaths;
use crate::startup;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayAction {
    Open,
    OpenFile,
    OpenLogs,
    DefaultApps,
    Exit,
}

const RETRY_START: Duration = Duration::from_millis(500);
const RETRY_CAP: Duration = Duration::from_secs(30);
const LOG_AFTER_ATTEMPTS: u32 = 10;

/// Spawns the tray on its own thread and returns immediately — this cannot
/// fail from the caller's side, by construction: the Job/Python lifecycle
/// must never depend on tray success. If the shell's notification area isn't
/// ready yet (the real case: launched from the sign-in Run key before
/// Explorer's tray infrastructure is up), the thread retries with backoff
/// until it succeeds — the icon shows up late, never "not at all," and no
/// restart or re-login is ever required. A panic inside a single attempt is
/// also just another attempt to retry, not a reason for the icon to vanish
/// for the rest of the session.
pub fn start(port: u16, login_enabled: bool, paths: DesktopPaths) -> Receiver<TrayAction> {
    let (actions, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut delay = RETRY_START;
        for attempt in 1u32.. {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                run(port, login_enabled, actions.clone(), &paths)
            }));
            match outcome {
                Ok(Ok(())) => return, // consumer hung up: supervisor is shutting down
                Ok(Err(error)) if attempt == LOG_AFTER_ATTEMPTS => paths.log(&format!(
                    "tray icon still not up after {attempt} attempts, retrying: {error}"
                )),
                Ok(Err(_)) => {}
                Err(_) => {
                    paths.log(&format!(
                        "tray thread panicked on attempt {attempt}, retrying"
                    ));
                }
            }
            thread::sleep(delay);
            delay = (delay * 2).min(RETRY_CAP);
        }
    });
    receiver
}

fn run(
    port: u16,
    login_enabled: bool,
    actions: mpsc::Sender<TrayAction>,
    paths: &DesktopPaths,
) -> Result<(), Box<dyn std::error::Error>> {
    let menu = Menu::new();
    let open = MenuItem::new("Open FusedRender", true, None);
    let open_file = MenuItem::new("Open file...", true, None);
    let status = MenuItem::new(format!("Running on port {port}"), false, None);
    let logs = MenuItem::new("Open logs", true, None);
    let default_apps = MenuItem::new("Default apps...", true, None);
    let login = CheckMenuItem::new("Start at sign in", true, login_enabled, None);
    let exit = MenuItem::new("Exit", true, None);
    menu.append_items(&[
        &open,
        &open_file,
        &PredefinedMenuItem::separator(),
        &status,
        &logs,
        &default_apps,
        &login,
        &PredefinedMenuItem::separator(),
        &exit,
    ])?;

    let image = image::load_from_memory_with_format(
        include_bytes!("../../../fused_render/assets/fused-render.ico"),
        image::ImageFormat::Ico,
    )?
    .into_rgba8();
    let (width, height) = image.dimensions();
    let icon = Icon::from_rgba(image.into_raw(), width, height)?;
    let _tray = TrayIconBuilder::new()
        .with_tooltip(format!("FusedRender (port {port})"))
        .with_menu(Box::new(menu))
        .with_icon(icon)
        .build()?;

    loop {
        let mut message = MSG::default();
        while unsafe { PeekMessageW(&mut message, std::ptr::null_mut(), 0, 0, PM_REMOVE) } != 0 {
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
        while let Ok(event) = MenuEvent::receiver().try_recv() {
            let action = if event.id == open.id() {
                Some(TrayAction::Open)
            } else if event.id == open_file.id() {
                Some(TrayAction::OpenFile)
            } else if event.id == logs.id() {
                Some(TrayAction::OpenLogs)
            } else if event.id == default_apps.id() {
                Some(TrayAction::DefaultApps)
            } else if event.id == login.id() {
                let checked = login.is_checked();
                if let Err(error) = startup::set_enabled(checked) {
                    paths.log(&format!("could not update sign-in setting: {error}"));
                    login.set_checked(!checked);
                }
                None
            } else if event.id == exit.id() {
                Some(TrayAction::Exit)
            } else {
                None
            };
            if let Some(action) = action
                && actions.send(action).is_err()
            {
                return Ok(());
            }
        }
        while let Ok(event) = TrayIconEvent::receiver().try_recv() {
            if matches!(event, TrayIconEvent::DoubleClick { .. })
                && actions.send(TrayAction::Open).is_err()
            {
                return Ok(());
            }
        }
        thread::sleep(Duration::from_millis(25));
    }
}
