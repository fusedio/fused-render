use std::env;
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopPaths {
    pub root: PathBuf,
    pub state: PathBuf,
    pub cache: PathBuf,
    pub runtime: PathBuf,
    pub temp: PathBuf,
    pub logs: PathBuf,
}

impl DesktopPaths {
    pub fn discover() -> io::Result<Self> {
        let local_app_data = env::var_os("LOCALAPPDATA")
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "LOCALAPPDATA is not set"))?;
        Ok(Self::under(
            Path::new(&local_app_data)
                .join("FusedRender")
                .join("Desktop"),
        ))
    }

    pub fn under(root: PathBuf) -> Self {
        Self {
            state: root.join("state"),
            cache: root.join("cache"),
            runtime: root.join("runtime"),
            temp: root.join("temp"),
            logs: root.join("logs"),
            root,
        }
    }

    pub fn create(&self) -> io::Result<()> {
        for path in [
            &self.state,
            &self.cache,
            &self.runtime,
            &self.temp,
            &self.logs,
        ] {
            std::fs::create_dir_all(path)?;
        }
        Ok(())
    }

    /// Best-effort append to `logs/supervisor.log`, shared by main.rs's fatal-
    /// error handler and by subsystems (tray) that must warn without ever
    /// treating the warning as fatal to the Job-owned Python server.
    pub fn log(&self, message: &str) {
        if std::fs::create_dir_all(&self.logs).is_err() {
            return;
        }
        let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.logs.join("supervisor.log"))
        else {
            return;
        };
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |duration| duration.as_secs());
        let _ = io::Write::write_all(&mut file, format!("{timestamp}: {message}\n").as_bytes());
    }

    pub fn child_environment(
        &self,
        instance_id: &str,
        token: &str,
        tools_dir: &Path,
    ) -> Vec<(OsString, OsString)> {
        let openfused = self.state.join("openfused");
        let rclone = self.state.join("rclone");
        let mut environment: Vec<_> = [
            ("FUSED_RENDER_HOME", self.state.clone()),
            ("FUSED_RENDER_CACHE_DIR", self.cache.clone()),
            ("FUSED_RENDER_RUNTIME_DIR", self.runtime.clone()),
            ("FUSED_RENDER_TEMP_DIR", self.temp.clone()),
            ("FUSED_RENDER_LOG_DIR", self.logs.clone()),
            (
                "FUSED_RENDER_DESKTOP_INSTANCE_ID",
                PathBuf::from(instance_id),
            ),
            ("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN", PathBuf::from(token)),
            ("OPENFUSED_ENVS_FILE", openfused.join("envs.json")),
            (
                "OPENFUSED_FUSED_CLOUD_CREDENTIALS",
                openfused.join("fused-cloud-credentials.json"),
            ),
            ("OPENFUSED_SECRETS_FILE", openfused.join("secrets.json")),
            ("OPENFUSED_WORKSPACES_DIR", openfused.join("workspaces")),
            ("RCLONE_CONFIG", rclone.join("rclone.conf")),
            ("RCLONE_CACHE_DIR", self.cache.join("rclone")),
            ("UV_CACHE_DIR", self.cache.join("uv")),
            ("FUSED_RENDER_CLAUDE_DIR", self.state.join("claude")),
            ("CLAUDE_CONFIG_DIR", self.state.join("claude")),
            (
                "FUSED_RENDER_DUCKDB_EXTENSION_DIR",
                self.cache.join("duckdb").join("extensions"),
            ),
            (
                "FUSED_RENDER_DUCKDB_TEMP_DIR",
                self.cache.join("duckdb").join("temp"),
            ),
            ("TEMP", self.temp.clone()),
            ("TMP", self.temp.clone()),
        ]
        .into_iter()
        .map(|(name, value)| (OsString::from(name), value.into_os_string()))
        .collect();
        let mut path = tools_dir.as_os_str().to_os_string();
        if let Some(current) = env::var_os("PATH").filter(|value| !value.is_empty()) {
            path.push(";");
            path.push(current);
        }
        environment.push((OsString::from("PATH"), path));
        environment
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn desktop_roots_are_separate_from_wheel_state() {
        let paths = DesktopPaths::under(PathBuf::from(
            r"C:\Users\test\AppData\Local\FusedRender\Desktop",
        ));
        assert_eq!(paths.state, paths.root.join("state"));
        assert_eq!(paths.cache, paths.root.join("cache"));
        assert_eq!(paths.runtime, paths.root.join("runtime"));
        assert!(!paths.root.to_string_lossy().contains(".fused-render"));
    }

    #[test]
    fn child_environment_is_fully_scoped() {
        let paths = DesktopPaths::under(PathBuf::from(r"C:\desktop"));
        let tools = Path::new(r"C:\app\python");
        let environment = paths.child_environment("desktop", "launch-token", tools);
        let value = |name: &str| {
            environment
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
                .unwrap()
        };

        assert_eq!(value("FUSED_RENDER_HOME"), paths.state.as_os_str());
        assert_eq!(value("FUSED_RENDER_CACHE_DIR"), paths.cache.as_os_str());
        assert_eq!(value("FUSED_RENDER_RUNTIME_DIR"), paths.runtime.as_os_str());
        assert_eq!(value("FUSED_RENDER_DESKTOP_INSTANCE_TOKEN"), "launch-token");
        assert_eq!(
            value("RCLONE_CONFIG"),
            paths.state.join("rclone").join("rclone.conf")
        );
        assert_eq!(value("UV_CACHE_DIR"), paths.cache.join("uv"));
        assert!(
            value("PATH")
                .to_string_lossy()
                .starts_with(tools.to_string_lossy().as_ref())
        );
        assert_eq!(value("CLAUDE_CONFIG_DIR"), paths.state.join("claude"));
        assert_eq!(
            value("FUSED_RENDER_DUCKDB_EXTENSION_DIR"),
            paths.cache.join("duckdb").join("extensions")
        );
        assert_eq!(
            value("FUSED_RENDER_DUCKDB_TEMP_DIR"),
            paths.cache.join("duckdb").join("temp")
        );
    }
}
