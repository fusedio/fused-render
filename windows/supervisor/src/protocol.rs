use std::ffi::{OsStr, OsString};
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::PathBuf;

const MAGIC: u32 = 0x3153_5246;
const VERSION: u16 = 1;
const HEADER_LEN: usize = 12;
const MAX_PATH_UNITS: usize = 32_767;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Open(PathBuf),
    OpenHome,
    StartInBackground,
    ShutdownForUpgrade,
}

impl Command {
    pub fn encode(&self) -> Vec<u8> {
        let (opcode, payload): (u16, Vec<u16>) = match self {
            Self::Open(path) => (1, path.as_os_str().encode_wide().collect()),
            Self::OpenHome => (2, Vec::new()),
            Self::ShutdownForUpgrade => (3, Vec::new()),
            Self::StartInBackground => (4, Vec::new()),
        };
        let mut frame = Vec::with_capacity(HEADER_LEN + payload.len() * 2);
        frame.extend_from_slice(&MAGIC.to_le_bytes());
        frame.extend_from_slice(&VERSION.to_le_bytes());
        frame.extend_from_slice(&opcode.to_le_bytes());
        frame.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        for unit in payload {
            frame.extend_from_slice(&unit.to_le_bytes());
        }
        frame
    }

    pub fn decode(frame: &[u8]) -> io::Result<Self> {
        if frame.len() < HEADER_LEN {
            return Err(invalid("truncated command header"));
        }
        let magic = u32::from_le_bytes(frame[0..4].try_into().unwrap());
        let version = u16::from_le_bytes(frame[4..6].try_into().unwrap());
        let opcode = u16::from_le_bytes(frame[6..8].try_into().unwrap());
        let units = u32::from_le_bytes(frame[8..12].try_into().unwrap()) as usize;
        if magic != MAGIC || version != VERSION {
            return Err(invalid("unsupported command protocol"));
        }
        if units > MAX_PATH_UNITS || frame.len() != HEADER_LEN + units * 2 {
            return Err(invalid("invalid command payload length"));
        }

        let payload: Vec<u16> = frame[HEADER_LEN..]
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect();
        match opcode {
            1 if !payload.is_empty() => {
                Ok(Self::Open(PathBuf::from(OsString::from_wide(&payload))))
            }
            2 if payload.is_empty() => Ok(Self::OpenHome),
            3 if payload.is_empty() => Ok(Self::ShutdownForUpgrade),
            4 if payload.is_empty() => Ok(Self::StartInBackground),
            _ => Err(invalid("invalid command opcode or payload")),
        }
    }
}

pub fn parse_args<I, S>(args: I) -> io::Result<Command>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let mut args = args.into_iter();
    let Some(first) = args.next() else {
        return Ok(Command::OpenHome);
    };
    if args.next().is_some() {
        return Err(invalid(
            "expected one file path, --startup, or --shutdown-for-upgrade",
        ));
    }
    match first.as_ref().to_str() {
        Some("--startup") => Ok(Command::StartInBackground),
        Some("--shutdown-for-upgrade") => Ok(Command::ShutdownForUpgrade),
        _ => Ok(Command::Open(PathBuf::from(first.as_ref()))),
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_unicode_and_unc_paths() {
        for command in [
            Command::Open(PathBuf::from(r"C:\data\résumé.xlsx")),
            Command::Open(PathBuf::from(r"\\server\share\report.pdf")),
            Command::OpenHome,
            Command::StartInBackground,
            Command::ShutdownForUpgrade,
        ] {
            assert_eq!(Command::decode(&command.encode()).unwrap(), command);
        }
    }

    #[test]
    fn rejects_malformed_frames() {
        assert!(Command::decode(&[]).is_err());
        let mut frame = Command::OpenHome.encode();
        frame[0] = 0;
        assert!(Command::decode(&frame).is_err());
        let mut frame = Command::OpenHome.encode();
        frame.extend_from_slice(&[0, 0]);
        assert!(Command::decode(&frame).is_err());
    }

    #[test]
    fn parses_launcher_commands() {
        assert_eq!(parse_args::<[&str; 0], _>([]).unwrap(), Command::OpenHome);
        assert_eq!(
            parse_args(["--startup"]).unwrap(),
            Command::StartInBackground
        );
        assert_eq!(
            parse_args(["--shutdown-for-upgrade"]).unwrap(),
            Command::ShutdownForUpgrade
        );
        assert_eq!(
            parse_args([r"C:\data\report.pdf"]).unwrap(),
            Command::Open(PathBuf::from(r"C:\data\report.pdf"))
        );
        assert!(parse_args(["one", "two"]).is_err());
    }
}
