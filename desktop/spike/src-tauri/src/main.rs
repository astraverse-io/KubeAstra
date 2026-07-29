// Tauri sidecar spike — see ../../README.md
//
// Proves four things with a running binary:
//   1. spawn the Python backend as a child and read its stdout
//   2. parse the PORT=/URL= handshake desktop_main.py prints
//   3. single-instance (a second launch must focus, not spawn a 2nd backend —
//      qdrant local mode holds an exclusive lock)
//   4. navigate the webview to /auth?token=… so the cookie is established
//
// The spike spawns via std::process::Command against the dev venv. Phase 2
// will use Tauri's `externalBin` sidecar against a PyInstaller bundle; the
// mechanism under test (spawn, read stdout, parse, navigate) is identical —
// only the path to the executable changes.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewWindow};

/// Holds the backend child so it can be killed when the app exits. Without
/// this the Python process outlives the window and keeps the port and the
/// vector-store lock.
struct Backend(Arc<Mutex<Option<Child>>>);

fn python_executable() -> String {
    std::env::var("KUBEASTRA_SPIKE_PYTHON")
        .unwrap_or_else(|_| "../../../ui/backend/venv/bin/python".to_string())
}

fn backend_entry() -> String {
    std::env::var("KUBEASTRA_SPIKE_BACKEND")
        .unwrap_or_else(|_| "../../../ui/backend/desktop_main.py".to_string())
}

/// Block until the backend answers /health, or give up.
fn wait_for_health(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let address = format!("127.0.0.1:{port}");
    while Instant::now() < deadline {
        // A TCP connect is enough for the spike: it proves the socket the
        // backend announced is the one actually listening.
        if std::net::TcpStream::connect(&address).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

fn start_backend(window: WebviewWindow, slot: Arc<Mutex<Option<Child>>>) {
    let mut child = match Command::new(python_executable())
        .arg(backend_entry())
        .env("KUBEASTRA_MODE", "desktop")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(child) => child,
        Err(error) => {
            eprintln!("SPIKE-FAIL: could not spawn backend: {error}");
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            eprintln!("SPIKE-FAIL: no stdout pipe");
            return;
        }
    };

    *slot.lock().unwrap() = Some(child);

    let mut port: Option<u16> = None;
    let mut token: Option<String> = None;
    let mut launched = false;

    // Keep draining stdout for the whole life of the process. Stopping at the
    // handshake drops the BufReader, which closes the read end of the pipe —
    // the backend's next print then raises BrokenPipeError and it dies. (The
    // spike hit exactly that on `print("READY")`.) A bounded buffer would
    // cause the same failure later under log volume, so the drain has to be
    // continuous, not just long enough.
    for line in BufReader::new(stdout).lines() {
        let line = match line {
            Ok(line) => line,
            Err(error) => {
                eprintln!("SPIKE-FAIL: stdout read error: {error}");
                return;
            }
        };
        println!("[backend] {line}");

        if let Some(value) = line.strip_prefix("PORT=") {
            port = value.trim().parse::<u16>().ok();
            println!("SPIKE-OK: parsed port {:?}", port);
        } else if let Some(rest) = line.strip_prefix("URL=") {
            if let Some(index) = rest.find("token=") {
                token = Some(rest[index + 6..].trim().to_string());
                println!(
                    "SPIKE-OK: parsed token ({} chars)",
                    token.as_ref().map(|t| t.len()).unwrap_or(0)
                );
            }
        }

        // Fire once, then fall through and keep reading.
        if !launched {
            if let (Some(port), Some(token)) = (port, token.as_ref()) {
                launched = true;
                if !wait_for_health(port, Duration::from_secs(60)) {
                    eprintln!("SPIKE-FAIL: backend never accepted connections on {port}");
                    continue;
                }
                println!("SPIKE-OK: backend healthy on {port}");

                let url = format!("http://127.0.0.1:{port}/auth?token={token}");
                match url.parse() {
                    Ok(parsed) => match window.navigate(parsed) {
                        Ok(()) => println!("SPIKE-OK: navigated webview to /auth"),
                        Err(error) => eprintln!("SPIKE-FAIL: navigate: {error}"),
                    },
                    Err(error) => eprintln!("SPIKE-FAIL: bad url: {error}"),
                }
            }
        }
    }

    if !launched {
        eprintln!("SPIKE-FAIL: stdout closed before a complete handshake");
    } else {
        println!("SPIKE-OK: backend stdout closed (process exited)");
    }
}

fn main() {
    let backend: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let for_exit = Arc::clone(&backend);

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // Second launch: focus the existing window instead of starting a
            // second backend, which would fail on the vector-store lock.
            println!("SPIKE-OK: single-instance callback fired");
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(Backend(Arc::clone(&backend)))
        .setup(move |app| {
            let window = app
                .get_webview_window("main")
                .expect("main window missing from tauri.conf.json");
            let slot = Arc::clone(&backend);
            std::thread::spawn(move || start_backend(window, slot));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build app")
        .run(move |_app, event| {
            // Both variants: ExitRequested fires on a graceful window close,
            // Exit on teardown. Neither fires when the process is signalled
            // or force-quit — the spike proved a backend outliving its parent
            // that way — so desktop_main.py also runs a parent-death
            // watchdog. Belt and braces, because an orphan holds the vector
            // store's exclusive lock and blocks the next launch.
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                if let Some(mut child) = for_exit.lock().unwrap().take() {
                    println!("SPIKE-OK: killing backend on exit");
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
