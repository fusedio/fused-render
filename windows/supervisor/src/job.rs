use std::collections::BTreeMap;
use std::env;
use std::ffi::{OsStr, OsString, c_void};
use std::fs::{File, OpenOptions};
use std::io;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::AsRawHandle;
use std::path::Path;
use std::ptr::null;

use windows_sys::Win32::Foundation::{
    CloseHandle, HANDLE, HANDLE_FLAG_INHERIT, SetHandleInformation, WAIT_OBJECT_0,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
    SetInformationJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT, CreateProcessW,
    PROCESS_INFORMATION, ResumeThread, STARTF_USESHOWWINDOW, STARTF_USESTDHANDLES, STARTUPINFOW,
    TerminateProcess, WaitForSingleObject,
};
use windows_sys::Win32::UI::WindowsAndMessaging::SW_HIDE;

pub struct Job {
    handle: OwnedHandle,
}

pub struct SupervisedProcess {
    handle: OwnedHandle,
    pub id: u32,
}

impl Job {
    pub fn new() -> io::Result<Self> {
        let handle = unsafe { CreateJobObjectW(null(), null()) };
        let handle = OwnedHandle::new(handle)?;
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        check(unsafe {
            SetInformationJobObject(
                handle.raw(),
                JobObjectExtendedLimitInformation,
                &limits as *const _ as *const c_void,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        })?;
        Ok(Self { handle })
    }

    pub fn spawn(
        &self,
        application: &Path,
        arguments: &[OsString],
        environment: &[(OsString, OsString)],
        output: Option<&Path>,
    ) -> io::Result<SupervisedProcess> {
        let application_wide = wide_null(application.as_os_str());
        let mut command_line = command_line(application.as_os_str(), arguments);
        let environment = environment_block(environment);
        let mut startup: STARTUPINFOW = unsafe { zeroed() };
        startup.cb = size_of::<STARTUPINFOW>() as u32;
        startup.dwFlags = STARTF_USESHOWWINDOW;
        startup.wShowWindow = SW_HIDE as u16;
        let stdio = output.map(open_stdio).transpose()?;
        if let Some((input, output)) = &stdio {
            startup.dwFlags |= STARTF_USESTDHANDLES;
            startup.hStdInput = input.as_raw_handle() as HANDLE;
            startup.hStdOutput = output.as_raw_handle() as HANDLE;
            startup.hStdError = output.as_raw_handle() as HANDLE;
        }
        let mut info: PROCESS_INFORMATION = unsafe { zeroed() };

        check(unsafe {
            CreateProcessW(
                application_wide.as_ptr(),
                command_line.as_mut_ptr(),
                null(),
                null(),
                i32::from(stdio.is_some()),
                CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                environment.as_ptr() as *const c_void,
                null(),
                &startup,
                &mut info,
            )
        })?;

        let process = OwnedHandle::new(info.hProcess)?;
        let thread = OwnedHandle::new(info.hThread)?;
        if unsafe { AssignProcessToJobObject(self.handle.raw(), process.raw()) } == 0 {
            unsafe { TerminateProcess(process.raw(), 1) };
            return Err(io::Error::last_os_error());
        }
        if unsafe { ResumeThread(thread.raw()) } == u32::MAX {
            unsafe { TerminateProcess(process.raw(), 1) };
            return Err(io::Error::last_os_error());
        }

        Ok(SupervisedProcess {
            handle: process,
            id: info.dwProcessId,
        })
    }

    pub fn contains(&self, process: &SupervisedProcess) -> io::Result<bool> {
        let mut result = 0;
        check(unsafe { IsProcessInJob(process.handle.raw(), self.handle.raw(), &mut result) })?;
        Ok(result != 0)
    }
}

fn open_stdio(output: &Path) -> io::Result<(File, File)> {
    let input = File::open("NUL")?;
    let output = OpenOptions::new().create(true).append(true).open(output)?;
    for file in [&input, &output] {
        check(unsafe {
            SetHandleInformation(
                file.as_raw_handle() as HANDLE,
                HANDLE_FLAG_INHERIT,
                HANDLE_FLAG_INHERIT,
            )
        })?;
    }
    Ok((input, output))
}

impl SupervisedProcess {
    pub fn wait(&self, timeout_ms: u32) -> bool {
        unsafe { WaitForSingleObject(self.handle.raw(), timeout_ms) == WAIT_OBJECT_0 }
    }

    pub fn raw_handle(&self) -> HANDLE {
        self.handle.raw()
    }
}

fn command_line(application: &OsStr, arguments: &[OsString]) -> Vec<u16> {
    let mut command = quote_argument(application);
    for argument in arguments {
        command.push(' ');
        command.push_str(&quote_argument(argument));
    }
    wide_null(OsStr::new(&command))
}

fn quote_argument(argument: &OsStr) -> String {
    let argument = argument.to_string_lossy();
    if !argument.is_empty() && !argument.chars().any(|ch| ch.is_whitespace() || ch == '"') {
        return argument.into_owned();
    }

    let mut quoted = String::from('"');
    let mut backslashes = 0;
    for ch in argument.chars() {
        if ch == '\\' {
            backslashes += 1;
        } else if ch == '"' {
            quoted.push_str(&"\\".repeat(backslashes * 2 + 1));
            quoted.push('"');
            backslashes = 0;
        } else {
            quoted.push_str(&"\\".repeat(backslashes));
            quoted.push(ch);
            backslashes = 0;
        }
    }
    quoted.push_str(&"\\".repeat(backslashes * 2));
    quoted.push('"');
    quoted
}

fn environment_block(overrides: &[(OsString, OsString)]) -> Vec<u16> {
    let mut values: BTreeMap<String, (OsString, OsString)> = env::vars_os()
        .map(|(name, value)| (name.to_string_lossy().to_uppercase(), (name, value)))
        .collect();
    for name in [
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONINSPECT",
    ] {
        values.remove(name);
    }
    for (name, value) in overrides {
        values.insert(
            name.to_string_lossy().to_uppercase(),
            (name.clone(), value.clone()),
        );
    }

    let mut block = Vec::new();
    for (_, (name, value)) in values {
        block.extend(name.encode_wide());
        block.push('=' as u16);
        block.extend(value.encode_wide());
        block.push(0);
    }
    block.push(0);
    block
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

impl OwnedHandle {
    fn new(handle: HANDLE) -> io::Result<Self> {
        if handle.is_null() {
            Err(io::Error::last_os_error())
        } else {
            Ok(Self(handle))
        }
    }

    fn raw(&self) -> HANDLE {
        self.0
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

unsafe impl Send for OwnedHandle {}
unsafe impl Sync for OwnedHandle {}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::thread;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
    };

    #[test]
    fn quotes_windows_arguments() {
        assert_eq!(quote_argument(OsStr::new("plain")), "plain");
        assert_eq!(quote_argument(OsStr::new("two words")), "\"two words\"");
        assert_eq!(quote_argument(OsStr::new("")), "\"\"");
        assert_eq!(
            quote_argument(OsStr::new("C:\\path with space\\")),
            "\"C:\\path with space\\\\\""
        );
    }

    #[test]
    fn closing_job_kills_child_and_grandchild() {
        let marker =
            env::temp_dir().join(format!("fused-render-job-test-{}.txt", std::process::id()));
        let _ = fs::remove_file(&marker);
        let powershell = Path::new(&env::var_os("SystemRoot").unwrap())
            .join(r"System32\WindowsPowerShell\v1.0\powershell.exe");
        let marker_text = marker.to_string_lossy().replace('\'', "''");
        let script = format!(
            "$p=Start-Process -PassThru -WindowStyle Hidden powershell.exe -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 60';[IO.File]::WriteAllText('{marker_text}',$p.Id);Wait-Process -Id $p.Id"
        );

        let job = Job::new().unwrap();
        let parent = job
            .spawn(
                &powershell,
                &[
                    OsString::from("-NoProfile"),
                    OsString::from("-NonInteractive"),
                    OsString::from("-Command"),
                    OsString::from(script),
                ],
                &[],
                None,
            )
            .unwrap();
        assert!(job.contains(&parent).unwrap());

        let deadline = Instant::now() + Duration::from_secs(10);
        let grandchild_id = loop {
            if let Ok(value) = fs::read_to_string(&marker)
                && let Ok(id) = value.parse::<u32>()
            {
                break id;
            }
            assert!(Instant::now() < deadline, "grandchild did not start");
            thread::sleep(Duration::from_millis(50));
        };
        let grandchild = OwnedHandle::new(unsafe {
            OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                0,
                grandchild_id,
            )
        })
        .unwrap();
        let mut in_job = 0;
        check(unsafe { IsProcessInJob(grandchild.raw(), job.handle.raw(), &mut in_job) }).unwrap();
        assert_ne!(in_job, 0);

        drop(job);
        assert!(parent.wait(5_000));
        assert_eq!(
            unsafe { WaitForSingleObject(grandchild.raw(), 5_000) },
            WAIT_OBJECT_0
        );
        let _ = fs::remove_file(marker);
    }
}
