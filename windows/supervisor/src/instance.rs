use std::ffi::{OsStr, c_void};
use std::io;
use std::mem::size_of;
use std::os::windows::ffi::OsStrExt;
use std::ptr::null_mut;
use std::sync::mpsc::{self, Sender, SyncSender};
use std::thread;
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{
    CloseHandle, ERROR_ALREADY_EXISTS, ERROR_NO_DATA, ERROR_PIPE_CONNECTED, GetLastError, HANDLE,
    INVALID_HANDLE_VALUE, LocalFree, WAIT_ABANDONED, WAIT_OBJECT_0,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER,
    TokenUser,
};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_FLAG_FIRST_PIPE_INSTANCE, PIPE_ACCESS_DUPLEX, ReadFile, WriteFile,
};
use windows_sys::Win32::System::Pipes::{
    CallNamedPipeW, ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_NOWAIT,
    PIPE_READMODE_MESSAGE, PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_MESSAGE, PIPE_WAIT,
    SetNamedPipeHandleState,
};
use windows_sys::Win32::System::Threading::{
    CreateMutexW, GetCurrentProcess, OpenMutexW, OpenProcessToken, ReleaseMutex,
    WaitForSingleObject,
};

use crate::protocol::Command;

const PIPE_BUFFER_SIZE: usize = 65_548;
const SYNCHRONIZE_ACCESS: u32 = 0x0010_0000;

#[derive(Clone)]
pub struct InstanceNames {
    pub mutex: String,
    pub pipe: String,
    sid: String,
}

pub enum Instance {
    Primary(PrimaryInstance),
    Secondary(SecondaryInstance),
}

pub struct PrimaryInstance {
    mutex: OwnedHandle,
    names: InstanceNames,
}

pub struct SecondaryInstance {
    names: InstanceNames,
}

pub struct Request {
    pub command: Command,
    pub response: SyncSender<u32>,
}

impl InstanceNames {
    pub fn current_user() -> io::Result<Self> {
        Self::with_suffix("v1")
    }

    fn with_suffix(suffix: &str) -> io::Result<Self> {
        let sid = current_user_sid()?;
        Ok(Self {
            mutex: format!(r"Local\FusedRender.Supervisor.{suffix}.{sid}"),
            pipe: format!(r"\\.\pipe\FusedRender.Supervisor.{suffix}.{sid}"),
            sid,
        })
    }
}

impl Instance {
    pub fn acquire(names: InstanceNames) -> io::Result<Self> {
        let security = SecurityDescriptor::for_sid(&names.sid)?;
        let mutex_name = wide_null(OsStr::new(&names.mutex));
        let mutex = unsafe { CreateMutexW(security.attributes(), 1, mutex_name.as_ptr()) };
        if mutex.is_null() {
            return Err(io::Error::last_os_error());
        }
        let already_exists = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
        let mutex = OwnedHandle(mutex);
        if already_exists {
            Ok(Self::Secondary(SecondaryInstance { names }))
        } else {
            Ok(Self::Primary(PrimaryInstance { mutex, names }))
        }
    }
}

impl PrimaryInstance {
    pub fn serve(&self, sender: Sender<Request>) -> thread::JoinHandle<io::Result<()>> {
        let pipe_name = self.names.pipe.clone();
        let sid = self.names.sid.clone();
        thread::spawn(move || serve_pipe(&pipe_name, &sid, sender))
    }

    pub fn client(&self) -> SecondaryInstance {
        SecondaryInstance {
            names: self.names.clone(),
        }
    }
}

impl Drop for PrimaryInstance {
    fn drop(&mut self) {
        unsafe { ReleaseMutex(self.mutex.0) };
    }
}

impl SecondaryInstance {
    pub fn send(&self, command: &Command, timeout: Duration) -> io::Result<()> {
        let pipe_name = wide_null(OsStr::new(&self.names.pipe));
        let frame = command.encode();
        let deadline = Instant::now() + timeout;
        loop {
            let mut status = 0u32;
            let mut read = 0u32;
            let ok = unsafe {
                CallNamedPipeW(
                    pipe_name.as_ptr(),
                    frame.as_ptr() as *const c_void,
                    frame.len() as u32,
                    &mut status as *mut _ as *mut c_void,
                    size_of::<u32>() as u32,
                    &mut read,
                    250,
                )
            };
            if ok != 0 && read == size_of::<u32>() as u32 {
                return if status == 0 {
                    Ok(())
                } else {
                    Err(io::Error::other("supervisor rejected the command"))
                };
            }
            if Instant::now() >= deadline {
                return Err(io::Error::last_os_error());
            }
            thread::sleep(Duration::from_millis(50));
        }
    }

    pub fn wait_for_exit(&self, timeout: Duration) -> io::Result<()> {
        let mutex_name = wide_null(OsStr::new(&self.names.mutex));
        let mutex = unsafe { OpenMutexW(SYNCHRONIZE_ACCESS, 0, mutex_name.as_ptr()) };
        if mutex.is_null() {
            return Ok(());
        }
        let mutex = OwnedHandle(mutex);
        let timeout = u32::try_from(timeout.as_millis()).unwrap_or(u32::MAX);
        match unsafe { WaitForSingleObject(mutex.0, timeout) } {
            WAIT_OBJECT_0 | WAIT_ABANDONED => {
                unsafe { ReleaseMutex(mutex.0) };
                Ok(())
            }
            _ => Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "supervisor did not exit",
            )),
        }
    }
}

fn serve_pipe(pipe_name: &str, sid: &str, sender: Sender<Request>) -> io::Result<()> {
    let security = SecurityDescriptor::for_sid(sid)?;
    let pipe_name = wide_null(OsStr::new(pipe_name));
    loop {
        let handle = unsafe {
            CreateNamedPipeW(
                pipe_name.as_ptr(),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                size_of::<u32>() as u32,
                PIPE_BUFFER_SIZE as u32,
                0,
                security.attributes(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let handle = OwnedHandle(handle);
        let connected = unsafe { ConnectNamedPipe(handle.0, null_mut()) };
        if connected == 0 && unsafe { GetLastError() } != ERROR_PIPE_CONNECTED {
            return Err(io::Error::last_os_error());
        }

        let mode = PIPE_READMODE_MESSAGE | PIPE_NOWAIT;
        check(unsafe { SetNamedPipeHandleState(handle.0, &mode, null_mut(), null_mut()) })?;
        let mut frame = vec![0u8; PIPE_BUFFER_SIZE];
        let mut read = 0u32;
        let deadline = Instant::now() + Duration::from_secs(5);
        let read_ok = loop {
            let ok = unsafe {
                ReadFile(
                    handle.0,
                    frame.as_mut_ptr(),
                    frame.len() as u32,
                    &mut read,
                    null_mut(),
                )
            };
            if ok != 0 || unsafe { GetLastError() } != ERROR_NO_DATA {
                break ok;
            }
            if Instant::now() >= deadline {
                break 0;
            }
            thread::sleep(Duration::from_millis(25));
        };
        let command = if read_ok != 0 {
            frame.truncate(read as usize);
            Command::decode(&frame).ok()
        } else {
            None
        };
        let should_stop = command == Some(Command::ShutdownForUpgrade);
        let status = if let Some(command) = command {
            let (response, result) = mpsc::sync_channel(1);
            if sender.send(Request { command, response }).is_ok() {
                result.recv_timeout(Duration::from_secs(20)).unwrap_or(1)
            } else {
                1
            }
        } else {
            1
        };
        let mut written = 0u32;
        unsafe {
            WriteFile(
                handle.0,
                &status as *const _ as *const u8,
                size_of::<u32>() as u32,
                &mut written,
                null_mut(),
            );
            DisconnectNamedPipe(handle.0);
        }
        if should_stop {
            return Ok(());
        }
    }
}

fn current_user_sid() -> io::Result<String> {
    let mut token = null_mut();
    check(unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) })?;
    let token = OwnedHandle(token);
    let mut needed = 0u32;
    unsafe { GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(io::Error::last_os_error());
    }
    let words = (needed as usize).div_ceil(size_of::<usize>());
    let mut buffer = vec![0usize; words];
    check(unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr() as *mut c_void,
            needed,
            &mut needed,
        )
    })?;
    let user = unsafe { &*(buffer.as_ptr() as *const TOKEN_USER) };
    let mut string_sid = null_mut();
    check(unsafe { ConvertSidToStringSidW(user.User.Sid, &mut string_sid) })?;
    let len = (0..)
        .take_while(|&i| unsafe { *string_sid.add(i) } != 0)
        .count();
    let value = String::from_utf16(unsafe { std::slice::from_raw_parts(string_sid, len) })
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid user SID"));
    unsafe { LocalFree(string_sid as *mut c_void) };
    value
}

struct SecurityDescriptor {
    descriptor: PSECURITY_DESCRIPTOR,
    attributes: SECURITY_ATTRIBUTES,
}

impl SecurityDescriptor {
    fn for_sid(sid: &str) -> io::Result<Self> {
        let sddl = wide_null(OsStr::new(&format!(r"D:P(A;;GA;;;SY)(A;;GA;;;{sid})")));
        let mut descriptor = null_mut();
        check(unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                null_mut(),
            )
        })?;
        Ok(Self {
            descriptor,
            attributes: SECURITY_ATTRIBUTES {
                nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
                lpSecurityDescriptor: descriptor,
                bInheritHandle: 0,
            },
        })
    }

    fn attributes(&self) -> *const SECURITY_ATTRIBUTES {
        &self.attributes
    }
}

impl Drop for SecurityDescriptor {
    fn drop(&mut self) {
        unsafe { LocalFree(self.descriptor) };
    }
}

fn wide_null(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}

fn check(result: i32) -> io::Result<()> {
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

unsafe impl Send for OwnedHandle {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;

    #[test]
    fn second_instance_forwards_unicode_command() {
        let names = InstanceNames::with_suffix(&format!("test.{}", std::process::id())).unwrap();
        let primary = match Instance::acquire(names).unwrap() {
            Instance::Primary(primary) => primary,
            Instance::Secondary(_) => panic!("test namespace already exists"),
        };
        let (sender, receiver) = mpsc::channel();
        let server = primary.serve(sender);
        let secondary_names =
            InstanceNames::with_suffix(&format!("test.{}", std::process::id())).unwrap();
        let secondary = match Instance::acquire(secondary_names).unwrap() {
            Instance::Secondary(secondary) => secondary,
            Instance::Primary(_) => panic!("second instance became primary"),
        };
        let command = Command::Open(r"C:\data\résumé.xlsx".into());
        let client_command = command.clone();
        let client = thread::spawn(move || {
            secondary
                .send(&client_command, Duration::from_secs(5))
                .unwrap()
        });
        let request = receiver.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(request.command, command);
        request.response.send(0).unwrap();
        client.join().unwrap();

        let secondary_names =
            InstanceNames::with_suffix(&format!("test.{}", std::process::id())).unwrap();
        let secondary = match Instance::acquire(secondary_names).unwrap() {
            Instance::Secondary(secondary) => secondary,
            Instance::Primary(_) => panic!("shutdown client became primary"),
        };
        let client = thread::spawn(move || {
            secondary
                .send(&Command::ShutdownForUpgrade, Duration::from_secs(5))
                .unwrap()
        });
        let request = receiver.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!(request.command, Command::ShutdownForUpgrade);
        request.response.send(0).unwrap();
        client.join().unwrap();
        server.join().unwrap().unwrap();
    }

    #[test]
    fn secondary_can_wait_for_primary_exit() {
        let suffix = format!("test.wait.{}", std::process::id());
        let primary = match Instance::acquire(InstanceNames::with_suffix(&suffix).unwrap()).unwrap()
        {
            Instance::Primary(primary) => primary,
            Instance::Secondary(_) => panic!("test namespace already exists"),
        };
        let secondary =
            match Instance::acquire(InstanceNames::with_suffix(&suffix).unwrap()).unwrap() {
                Instance::Secondary(secondary) => secondary,
                Instance::Primary(_) => panic!("second instance became primary"),
            };
        let waiter = thread::spawn(move || secondary.wait_for_exit(Duration::from_secs(5)));
        thread::sleep(Duration::from_millis(50));
        drop(primary);
        waiter.join().unwrap().unwrap();
    }
}
