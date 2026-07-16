use std::io;
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

use tray_icon::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIconBuilder, TrayIconEvent};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    DispatchMessageW, MSG, PM_REMOVE, PeekMessageW, TranslateMessage,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayAction {
    Open,
    OpenFile,
    OpenLogs,
    DefaultApps,
    ToggleLogin,
    Exit,
}

pub fn start(port: u16, login_enabled: bool) -> io::Result<Receiver<TrayAction>> {
    let (actions, receiver) = mpsc::channel();
    let (ready, started) = mpsc::sync_channel(1);
    thread::spawn(move || {
        if let Err(error) = run(port, login_enabled, actions, &ready) {
            let _ = ready.send(Err(error.to_string()));
        }
    });
    match started.recv_timeout(Duration::from_secs(5)) {
        Ok(Ok(())) => Ok(receiver),
        Ok(Err(error)) => Err(io::Error::other(error)),
        Err(_) => Err(io::Error::other("tray did not start")),
    }
}

fn run(
    port: u16,
    login_enabled: bool,
    actions: mpsc::Sender<TrayAction>,
    ready: &mpsc::SyncSender<Result<(), String>>,
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
    let _ = ready.send(Ok(()));

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
                Some(TrayAction::ToggleLogin)
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
