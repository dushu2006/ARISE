#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::env;
use std::io::{BufRead, BufReader};
use std::net::{SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tauri::{Manager, State};

const BACKEND_SHUTDOWN_GRACE: Duration = Duration::from_secs(3);
const BACKEND_MONITOR_INTERVAL: Duration = Duration::from_millis(150);
const BACKEND_RESTART_LIMIT: u32 = 3;

struct BackendProcess {
    api_token: String,
    stopping: Arc<AtomicBool>,
    monitor: Mutex<Option<JoinHandle<()>>>,
}

fn shutdown_backend(child: &mut Child, grace_period: Duration) {
    // The supervised Python process watches stdin for EOF so it can stop Uvicorn
    // and release its database/process lock before the desktop shell exits.
    drop(child.stdin.take());
    let deadline = Instant::now() + grace_period;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) | Err(_) => break,
        }
    }
    // A wedged backend must not keep desktop shutdown blocked indefinitely.
    let _ = child.kill();
    let _ = child.wait();
}

fn wait_or_stopping(stopping: &AtomicBool, delay: Duration) -> bool {
    let deadline = Instant::now() + delay;
    loop {
        if stopping.load(Ordering::Acquire) {
            return true;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return false;
        }
        thread::sleep(remaining.min(Duration::from_millis(50)));
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        if let Ok(monitor) = self.monitor.get_mut() {
            if let Some(handle) = monitor.take() {
                let _ = handle.join();
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

fn generate_api_token() -> Result<String, String> {
    // Tauri owns one fresh token per desktop launch. The same in-memory token is
    // passed to backend restarts and returned only through the scoped IPC command.
    let mut bytes = [0_u8; 48];
    getrandom::fill(&mut bytes)
        .map_err(|_| "ARISE could not generate a secure local API credential.".to_string())?;
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut token = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        token.push(HEX[(byte >> 4) as usize] as char);
        token.push(HEX[(byte & 0x0f) as usize] as char);
    }
    validate_api_token(token)
}

fn find_bundled_sidecar() -> Option<std::path::PathBuf> {
    let exe_dir = env::current_exe().ok()?.parent()?.to_path_buf();
    let candidates = [
        "arise-backend-x86_64-pc-windows-msvc.exe",
        "arise-backend.exe",
        "arise-backend",
    ];
    for name in candidates {
        let direct = exe_dir.join(name);
        if direct.is_file() {
            return Some(direct);
        }
        let sub = exe_dir.join("binaries").join(name);
        if sub.is_file() {
            return Some(sub);
        }
    }
    None
}

fn spawn_backend(api_token: &str) -> Result<Child, String> {
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
    } else if let Some(sidecar) = find_bundled_sidecar() {
        Command::new(sidecar)
    } else {
        let python = env::var_os("ARISE_PYTHON").unwrap_or_else(|| "python".into());
        let mut command = Command::new(python);
        command.args(["-m", "arise.server"]);
        command
    };
    command
        .env("ARISE__API__HOST", "127.0.0.1")
        .env("ARISE__API__PORT", "8765")
        .env("ARISE__API__AUTH_TOKEN", api_token)
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

fn supervise_backend(child: Child, api_token: String, stopping: Arc<AtomicBool>) {
    supervise_backend_with(child, api_token, stopping, spawn_backend);
}

fn supervise_backend_with<F>(
    mut child: Child,
    api_token: String,
    stopping: Arc<AtomicBool>,
    mut spawn: F,
) where
    F: FnMut(&str) -> Result<Child, String>,
{
    let mut restart_attempts = 0_u32;
    loop {
        if stopping.load(Ordering::Acquire) {
            shutdown_backend(&mut child, BACKEND_SHUTDOWN_GRACE);
            return;
        }
        match child.try_wait() {
            Ok(None) => {
                thread::sleep(BACKEND_MONITOR_INTERVAL);
                continue;
            }
            Ok(Some(_)) => {}
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        if stopping.load(Ordering::Acquire) || restart_attempts >= BACKEND_RESTART_LIMIT {
            return;
        }
        let backoff_ms = 500_u64.saturating_mul(1_u64 << restart_attempts.min(4));
        if wait_or_stopping(&stopping, Duration::from_millis(backoff_ms)) {
            return;
        }
        restart_attempts += 1;
        match spawn(&api_token) {
            Ok(mut restarted) if stopping.load(Ordering::Acquire) => {
                shutdown_backend(&mut restarted, BACKEND_SHUTDOWN_GRACE);
                return;
            }
            Ok(restarted) => child = restarted,
            // Keep the dead child handle and try again after a bounded backoff.
            // Every attempt is limited by BACKEND_RESTART_LIMIT.
            Err(_) => {}
        }
    }
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
fn get_api_token(state: State<'_, BackendProcess>) -> Result<String, String> {
    validate_api_token(state.api_token.clone())
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![get_api_token])
        .setup(|app| {
            let api_token = generate_api_token().map_err(std::io::Error::other)?;
            let child = spawn_backend(&api_token).map_err(std::io::Error::other)?;
            let stopping = Arc::new(AtomicBool::new(false));
            let monitor_stopping = Arc::clone(&stopping);
            let monitor_token = api_token.clone();
            let monitor = thread::spawn(move || {
                supervise_backend(child, monitor_token, monitor_stopping);
            });
            app.manage(BackendProcess {
                api_token,
                stopping,
                monitor: Mutex::new(Some(monitor)),
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("ARISE desktop shell failed to start");
}

#[cfg(test)]
mod tests {
    use super::generate_api_token;

    #[test]
    fn generates_a_unique_valid_token_for_each_launch() {
        let first = generate_api_token().expect("OS CSPRNG token");
        let second = generate_api_token().expect("second OS CSPRNG token");
        assert_eq!(first.len(), 96);
        assert_ne!(first, second);
    }

    #[test]
    fn restarts_a_crashed_child_and_reuses_the_launch_token() {
        use super::supervise_backend_with;
        use std::process::{Command, Stdio};
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        use std::sync::Arc;
        use std::thread;
        use std::time::{Duration, Instant};

        let mut crashed = Command::new("python")
            .args(["-c", "raise SystemExit(17)"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn process that exits unexpectedly");
        // Ensure the supervisor receives a real exited process handle.
        let _ = crashed.wait();

        let stopping = Arc::new(AtomicBool::new(false));
        let monitor_stopping = Arc::clone(&stopping);
        let starts = Arc::new(AtomicUsize::new(0));
        let spawn_starts = Arc::clone(&starts);
        let monitor = thread::spawn(move || {
            supervise_backend_with(
                crashed,
                "launch-token".to_string(),
                monitor_stopping,
                move |token| {
                    if token != "launch-token" {
                        return Err("launch token changed during restart".to_string());
                    }
                    spawn_starts.fetch_add(1, Ordering::SeqCst);
                    Command::new("python")
                        .args(["-c", "import sys; sys.stdin.buffer.read()"])
                        .stdin(Stdio::piped())
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .spawn()
                        .map_err(|_| "restart test child could not start".to_string())
                },
            );
        });

        let deadline = Instant::now() + Duration::from_secs(8);
        while starts.load(Ordering::SeqCst) == 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(20));
        }
        let observed_restarts = starts.load(Ordering::SeqCst);
        stopping.store(true, Ordering::Release);
        monitor.join().expect("supervisor monitor exits on shutdown");
        assert_eq!(observed_restarts, 1);
    }

    #[cfg(unix)]
    use super::shutdown_backend;
    #[cfg(unix)]
    use std::process::{Command, Stdio};
    #[cfg(unix)]
    use std::time::Duration;

    #[cfg(unix)]
    #[test]
    fn shutdown_closes_supervision_pipe_and_waits_for_graceful_exit() {
        let mut child = Command::new("sh")
            .args(["-c", "cat >/dev/null"])
            .stdin(Stdio::piped())
            .spawn()
            .expect("spawn pipe-reading child");

        shutdown_backend(&mut child, Duration::from_secs(1));

        assert!(child.try_wait().expect("wait for child").is_some());
    }

    #[cfg(unix)]
    #[test]
    fn shutdown_kills_a_child_that_ignores_pipe_eof_after_deadline() {
        let mut child = Command::new("sh")
            .args(["-c", "exec sleep 10"])
            .stdin(Stdio::piped())
            .spawn()
            .expect("spawn non-cooperative child");

        shutdown_backend(&mut child, Duration::from_millis(25));

        assert!(child.try_wait().expect("wait for child").is_some());
    }
}
