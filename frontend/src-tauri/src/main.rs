#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::fs;
use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::thread;
use std::time::Duration;
use tauri::{Manager, State};

struct BackendProcess(Mutex<Option<Child>>);

impl Drop for BackendProcess {
    fn drop(&mut self) {
        if let Ok(slot) = self.0.get_mut() {
            if let Some(child) = slot.as_mut() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn backend_address() -> SocketAddr {
    "127.0.0.1:8765"
        .parse()
        .expect("loopback backend address is a compile-time constant")
}

fn backend_is_running(address: SocketAddr) -> bool {
    TcpStream::connect_timeout(&address, Duration::from_millis(180)).is_ok()
}

fn spawn_backend() -> Result<Child, String> {
    let address = backend_address();
    if backend_is_running(address) {
        // Do not send the local API credential to an unverified process on loopback.
        return Err(
            "The local API port is already in use. Stop the existing process before launching ARISE; the desktop shell will not reuse an unverified backend."
                .to_string(),
        );
    }

    let mut command = if let Some(executable) = env::var_os("ARISE_BACKEND_EXECUTABLE") {
        Command::new(executable)
    } else {
        let python = env::var_os("ARISE_PYTHON").unwrap_or_else(|| "python".into());
        let mut command = Command::new(python);
        command.args(["-m", "arise.server"]);
        command
    };
    command
        .env("ARISE__API__HOST", "127.0.0.1")
        .env("ARISE__API__PORT", "8765")
        .env("ARISE__SECURITY__ENVIRONMENT", "production")
        .env("ARISE_BACKEND_READY_SIGNAL", "1")
        .env("ARISE_BACKEND_SUPERVISED", "1")
        // EOF on this parent-owned pipe is the crash/exit signal consumed by the
        // Python backend. Child::drop alone does not terminate a process on Windows.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());

    let mut child = command.spawn().map_err(|_| {
        "ARISE could not start its local Python backend. Install the backend or set ARISE_BACKEND_EXECUTABLE."
            .to_string()
    })?;

    let Some(stdout) = child.stdout.take() else {
        let _ = child.kill();
        let _ = child.wait();
        return Err("ARISE could not monitor local backend startup.".to_string());
    };
    let (ready_sender, ready_receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut readiness_notified = false;
        for line in BufReader::new(stdout).lines() {
            match line {
                Ok(line) if line.trim() == "ARISE_BACKEND_READY" && !readiness_notified => {
                    let _ = ready_sender.send(());
                    readiness_notified = true;
                }
                Ok(_) => {}
                Err(_) => return,
            }
        }
    });

    for _ in 0..60 {
        if ready_receiver.try_recv().is_ok() {
            return Ok(child);
        }
        match child.try_wait() {
            Ok(Some(_)) => {
                return Err("The local backend exited before reporting ready.".to_string());
            }
            Ok(None) => {}
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("ARISE could not check the local backend process.".to_string());
            }
        }
        thread::sleep(Duration::from_millis(150));
    }
    let _ = child.kill();
    let _ = child.wait();
    Err("The local backend did not become ready before the startup deadline.".to_string())
}

fn api_token_path() -> Result<PathBuf, String> {
    if let Some(data_dir) = env::var_os("ARISE__DATA_DIR") {
        return Ok(PathBuf::from(data_dir).join("api.token"));
    }
    let local_app_data = env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or_else(|| "Windows local application data directory is unavailable.".to_string())?;
    Ok(local_app_data.join("ARISE").join("api.token"))
}

fn validate_api_token(token: String) -> Result<String, String> {
    let length = token.chars().count();
    if !(32..=512).contains(&length)
        || !token.is_ascii()
        || token.chars().any(|character| !character.is_ascii_graphic())
    {
        return Err("The local ARISE API credential is invalid.".to_string());
    }
    Ok(token)
}

#[tauri::command]
fn get_api_token() -> Result<String, String> {
    if let Some(configured) = env::var_os("ARISE__API__AUTH_TOKEN") {
        let token = configured
            .into_string()
            .map_err(|_| "The configured ARISE API credential is invalid.".to_string())?;
        return validate_api_token(token);
    }
    let path = api_token_path()?;
    let value = fs::read_to_string(path).map_err(|_| {
        "The ARISE API credential is not available. Start the local backend once and retry."
            .to_string()
    })?;
    let token = value
        .strip_suffix("\r\n")
        .or_else(|| value.strip_suffix('\n'))
        .unwrap_or(&value)
        .to_string();
    validate_api_token(token)
}

fn main() {
    tauri::Builder::default()
        .manage(BackendProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![get_api_token])
        .setup(|app| {
            let child = spawn_backend().map_err(std::io::Error::other)?;
            let state: State<BackendProcess> = app.state();
            *state
                .0
                .lock()
                .map_err(|_| std::io::Error::other("backend process state is unavailable"))? =
                Some(child);
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("ARISE desktop shell failed to start");
}
