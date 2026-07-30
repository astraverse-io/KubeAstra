#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, WebviewWindow};

#[allow(dead_code)]
struct Backend(Arc<Mutex<Option<Child>>>);

/// Locate the frozen backend.
///
/// The backend is a PyInstaller **onedir** build: a directory containing the
/// launcher plus an `_internal/` tree it cannot start without. That shape is
/// why this is shipped through `bundle.resources` rather than `externalBin`.
///
/// `externalBin` expects one self-contained executable and flattens whatever
/// it is given into `Contents/MacOS/`. Pointing it at a onedir tree produced a
/// bundle that built cleanly, exited 0, and then failed at launch with
/// `Failed to load Python shared library .../Contents/Frameworks/Python`,
/// because `_internal/` no longer existed. Resources preserve the directory.
///
/// Every candidate is checked with `is_file()`, never `exists()` — a directory
/// satisfies `exists()`, which is precisely how the broken layout slipped
/// through before.
fn resolve_backend_executable(window: &WebviewWindow) -> (String, Vec<String>) {
    let exe_name = if cfg!(windows) {
        "kubeastra-backend.exe"
    } else {
        "kubeastra-backend"
    };

    if let Ok(path) = std::env::var("KUBEASTRA_BACKEND_BIN") {
        if Path::new(&path).is_file() {
            return (path, vec![]);
        }
        eprintln!("[tauri-error] KUBEASTRA_BACKEND_BIN is not a file: {path}");
    }

    // Packaged. The glob form of bundle.resources preserves each file's path
    // relative to the crate root, so `binaries/…` is part of the layout:
    //   KubeAstra.app/Contents/Resources/binaries/kubeastra-backend/…
    match window.path().resource_dir() {
        Ok(resources) => {
            let candidate = resources
                .join("binaries")
                .join("kubeastra-backend")
                .join(exe_name);
            if candidate.is_file() {
                return (candidate.to_string_lossy().to_string(), vec![]);
            }
            eprintln!("[tauri] No packaged backend at {}", candidate.display());
        }
        Err(error) => eprintln!("[tauri] Could not resolve resource dir: {error}"),
    }

    // `cargo tauri dev`: the staged tree next to the crate.
    let staged = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("binaries")
        .join("kubeastra-backend")
        .join(exe_name);
    if staged.is_file() {
        return (staged.to_string_lossy().to_string(), vec![]);
    }

    // Last resort: run from source against a local virtualenv.
    let python = std::env::var("KUBEASTRA_PYTHON")
        .unwrap_or_else(|_| "../ui/backend/venv/bin/python".to_string());
    let entry = std::env::var("KUBEASTRA_ENTRY")
        .unwrap_or_else(|_| "../ui/backend/desktop_main.py".to_string());
    (python, vec![entry])
}

/// Percent-encode for a URL fragment. Only unreserved characters survive, so
/// the JSON payload cannot terminate the fragment or smuggle a separator.
fn encode_fragment(value: &str) -> String {
    let mut out = String::with_capacity(value.len() * 2);
    for byte in value.as_bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(*byte as char)
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Show a startup failure in the window instead of leaving it blank.
///
/// Startup failures used to reach stderr and nowhere else, so a broken
/// sidecar was indistinguishable from a slow one: an empty window, forever.
/// That is how a bundle whose backend could not start reached a .dmg.
///
/// Delivered by navigating the splash page to `#fail=<json>` rather than by
/// injecting script. `default-src 'self'` with no `script-src` blocks inline
/// script, and whether a host-side eval is exempt varies by platform webview
/// — a fragment change is navigation, and splash/boot.js is same-origin, so
/// neither can be refused by the policy.
fn show_fatal(window: &WebviewWindow, splash_url: &Option<String>, headline: &str, detail: &str) {
    eprintln!("[tauri-fatal] {headline}: {detail}");

    let Some(base) = splash_url else {
        eprintln!("[tauri-error] No splash URL captured; failure shown on stderr only");
        return;
    };

    let payload = serde_json::json!({ "headline": headline, "detail": detail }).to_string();
    let target = format!(
        "{}#fail={}",
        base.split('#').next().unwrap_or(base),
        encode_fragment(&payload)
    );

    match target.parse() {
        Ok(url) => {
            if let Err(error) = window.navigate(url) {
                eprintln!("[tauri-error] Could not show the failure notice: {error}");
            }
        }
        Err(error) => eprintln!("[tauri-error] Bad splash URL {target}: {error}"),
    }
}

fn wait_for_health(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    let address = format!("127.0.0.1:{port}");
    while Instant::now() < deadline {
        if let Ok(mut stream) = std::net::TcpStream::connect(&address) {
            use std::io::{Read, Write};
            // desktop_main binds and listens *before* uvicorn starts, so the
            // socket accepts connections that nothing is serving yet. Without
            // a read timeout, read_to_string would block past the deadline.
            let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
            let request = format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
            if stream.write_all(request.as_bytes()).is_ok() {
                let mut response = String::new();
                let _ = stream.read_to_string(&mut response);
                if response.contains("200 OK") || response.contains("200 ok") {
                    return true;
                }
            }
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

fn start_backend(window: WebviewWindow, slot: Arc<Mutex<Option<Child>>>) {
    // Captured before anything can navigate away — this is the only handle on
    // the splash document, and failures need somewhere to render.
    let splash_url = window.url().ok().map(|u| u.to_string());
    let (cmd, args) = resolve_backend_executable(&window);
    println!("[tauri] Spawning backend sidecar: {cmd} {args:?}");

    let mut command = Command::new(&cmd);
    if !args.is_empty() {
        command.args(args);
    }

    let mut child = match command
        .env("KUBEASTRA_MODE", "desktop")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(child) => child,
        Err(error) => {
            show_fatal(
                &window,
                &splash_url,
                "Backend failed to launch",
                &format!("Could not run {cmd}\n\n{error}"),
            );
            return;
        }
    };

    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            show_fatal(&window, &splash_url, "Backend failed to launch", "No stdout pipe on the child process.");
            return;
        }
    };

    *slot.lock().unwrap() = Some(child);

    let mut port: Option<u16> = None;
    let mut token: Option<String> = None;
    let mut launched = false;
    // Kept only until navigation succeeds, so a failure can show the user the
    // backend's own last words instead of an empty window.
    let mut tail: Vec<String> = Vec::new();

    // Continuously drain stdout lines to keep the backend write pipe open
    for line in BufReader::new(stdout).lines() {
        let line = match line {
            Ok(line) => line,
            Err(error) => {
                if !launched {
                    show_fatal(&window, &splash_url, "Backend stopped responding", &format!("{error}"));
                }
                return;
            }
        };
        println!("[backend] {line}");
        if !launched {
            tail.push(line.clone());
            if tail.len() > 25 {
                tail.remove(0);
            }
        }

        if let Some(value) = line.strip_prefix("PORT=") {
            port = value.trim().parse::<u16>().ok();
        } else if let Some(rest) = line.strip_prefix("URL=") {
            if let Some(index) = rest.find("token=") {
                token = Some(rest[index + 6..].trim().to_string());
            }
        }

        if !launched {
            if let (Some(port), Some(token)) = (port, token.as_ref()) {
                launched = true;
                if !wait_for_health(port, Duration::from_secs(60)) {
                    show_fatal(
                        &window,
                        &splash_url,
                        "Backend did not become ready",
                        &format!(
                            "No 200 from http://127.0.0.1:{port}/health within 60s.\n\n{}",
                            tail.join("\n")
                        ),
                    );
                    continue;
                }
                println!("[tauri] Backend healthy on port {port}");

                let url = format!("http://127.0.0.1:{port}/auth?token={token}");
                match url.parse() {
                    Ok(parsed) => match window.navigate(parsed) {
                        Ok(()) => {
                            println!("[tauri] Webview successfully navigated to /auth");
                            tail.clear();
                        }
                        Err(error) => {
                            show_fatal(&window, &splash_url, "Could not open the app", &format!("{error}"))
                        }
                    },
                    Err(error) => show_fatal(&window, &splash_url, "Could not open the app", &format!("{error}")),
                }
            }
        }
    }

    if !launched {
        show_fatal(
            &window,
            &splash_url,
            "Backend exited during startup",
            if tail.is_empty() {
                "It produced no output before closing.".to_string()
            } else {
                tail.join("\n")
            }
            .as_str(),
        );
    }
}

fn stop_backend(child_opt: &mut Option<Child>) {
    if let Some(mut child) = child_opt.take() {
        let pid = child.id();
        println!("[tauri] Initiating graceful termination for backend (PID: {pid})...");

        #[cfg(unix)]
        {
            unsafe {
                libc::kill(pid as i32, libc::SIGINT);
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut exited = false;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    exited = true;
                    println!("[tauri] Backend process (PID: {pid}) terminated gracefully.");
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            if !exited {
                println!("[tauri] Backend (PID: {pid}) did not exit within timeout; sending SIGKILL.");
                let _ = child.kill();
                let _ = child.wait();
            }
        }

        #[cfg(not(unix))]
        {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn main() {
    let backend: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let for_exit = Arc::clone(&backend);

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(Backend(Arc::clone(&backend)))
        .setup(move |app| {
            let window = app
                .get_webview_window("main")
                .expect("Main window missing from tauri.conf.json");
            let slot = Arc::clone(&backend);
            std::thread::spawn(move || start_backend(window, slot));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Failed to build Tauri app")
        .run(move |_app, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                let mut guard = for_exit.lock().unwrap();
                stop_backend(&mut guard);
            }
        });
}
