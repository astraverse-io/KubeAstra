#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};


/// Auto-update. Rust-driven, because the webview has no IPC bridge.
mod updater;
use tauri::{Manager, WebviewWindow};

/// Shared state for the shell.
///
/// `base_url` is set once the backend answers /health, and is what the tray,
/// the global shortcut and deep links steer the webview with. Anything that
/// arrives before then goes into `pending` and is replayed on ready — a deep
/// link is the usual way the app gets launched cold.
#[derive(Clone, Default)]
struct Shell {
    backend: Arc<Mutex<Option<Child>>>,
    base_url: Arc<Mutex<Option<String>>>,
    pending: Arc<Mutex<Option<String>>>,
    /// The per-launch token, for the shell's own API calls. The webview gets
    /// a cookie via /auth; a background poller has no cookie jar, so it sends
    /// `Authorization: Bearer` instead (desktop_security.extract_token
    /// accepts either).
    token: Arc<Mutex<Option<String>>>,
}

/// Bring the window to the foreground from wherever it was.
fn front(window: &WebviewWindow) {
    let _ = window.unminimize();
    let _ = window.show();
    let _ = window.set_focus();
}

/// Ask the running app to do something, via the URL fragment.
///
/// Deliberately not Tauri IPC. The app is served from http://127.0.0.1:<port>
/// — a remote origin, which Tauri v2 only exposes IPC to after explicitly
/// opting that domain in. A fragment needs no such grant, and a fragment-only
/// change does not reload the page, so the hotkey cannot throw away an
/// in-flight investigation.
///
/// `nonce` matters: pressing the shortcut twice must fire `hashchange` twice,
/// and an identical fragment would not.
fn dispatch(window: &WebviewWindow, shell: &Shell, action: &str, params: &[(&str, &str)]) {
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);

    let mut fragment = format!("#kubeastra={}", encode_fragment(action));
    for (key, value) in params {
        fragment.push_str(&format!(
            "&{}={}",
            encode_fragment(key),
            encode_fragment(value)
        ));
    }
    fragment.push_str(&format!("&n={nonce}"));

    let base = shell.base_url.lock().unwrap().clone();
    let Some(base) = base else {
        // Backend still starting. Hold it; start_backend replays on ready.
        *shell.pending.lock().unwrap() = Some(fragment);
        println!("[tauri] Backend not ready; queued {action}");
        return;
    };

    // Steer the page the user is already on, so the fragment does not reload
    // it. Falling back to the chat route covers a cold start.
    let current = window
        .url()
        .ok()
        .map(|u| u.to_string())
        .filter(|u| u.starts_with(&base))
        .unwrap_or_else(|| format!("{base}/chat/"));

    let target = format!("{}{}", current.split('#').next().unwrap_or(&current), fragment);
    match target.parse() {
        Ok(url) => {
            if let Err(error) = window.navigate(url) {
                eprintln!("[tauri-error] Could not dispatch {action}: {error}");
            }
        }
        Err(error) => eprintln!("[tauri-error] Bad dispatch URL {target}: {error}"),
    }
}

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

fn start_backend(window: WebviewWindow, shell: Shell) {
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

    *shell.backend.lock().unwrap() = Some(child);

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
                *shell.base_url.lock().unwrap() = Some(format!("http://127.0.0.1:{port}"));
                *shell.token.lock().unwrap() = Some(token.clone());

                let url = format!("http://127.0.0.1:{port}/auth?token={token}");
                match url.parse() {
                    Ok(parsed) => match window.navigate(parsed) {
                        Ok(()) => {
                            println!("[tauri] Webview successfully navigated to /auth");
                            tail.clear();
                            replay_pending(&window, &shell, port);
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

/// Deliver a request that arrived before the backend was up.
///
/// Opening a `kubeastra://` link with the app closed is the ordinary case:
/// the deep link lands during startup, when there is nowhere to send it yet.
///
/// Waits for /auth to have set its cookie and redirected before applying the
/// fragment — arriving mid-redirect would lose it.
fn replay_pending(window: &WebviewWindow, shell: &Shell, port: u16) {
    let Some(fragment) = shell.pending.lock().unwrap().take() else {
        return;
    };
    println!("[tauri] Replaying queued request: {fragment}");

    let target = format!("http://127.0.0.1:{port}/chat/{fragment}");
    let window = window.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(600));
        match target.parse() {
            Ok(url) => {
                if let Err(error) = window.navigate(url) {
                    eprintln!("[tauri-error] Could not replay queued request: {error}");
                }
            }
            Err(error) => eprintln!("[tauri-error] Bad replay URL {target}: {error}"),
        }
    });
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

/// One HTTP GET against the local backend, authenticated with the launch
/// token. Deliberately hand-rolled: pulling in a full HTTP client for two
/// loopback GETs against a server we started ourselves is not worth the
/// dependency, and `wait_for_health` already speaks enough HTTP.
fn backend_get(base: &str, path: &str, token: Option<&str>, timeout: Duration) -> Option<String> {
    let host = base.strip_prefix("http://")?;
    let mut stream = std::net::TcpStream::connect(host).ok()?;
    stream.set_read_timeout(Some(timeout)).ok()?;
    stream.set_write_timeout(Some(timeout)).ok()?;

    use std::io::{Read, Write};
    let auth = match token {
        Some(value) => format!("Authorization: Bearer {value}\r\n"),
        None => String::new(),
    };
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: {host}\r\n{auth}Connection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;

    let mut response = String::new();
    stream.read_to_string(&mut response).ok()?;
    if !response.starts_with("HTTP/1.1 200") {
        return None;
    }
    response.split_once("\r\n\r\n").map(|(_, body)| body.to_string())
}

/// Raise an OS notification per newly-firing alert.
///
/// The backend polls Alertmanager and decides what is new; this only asks
/// "anything for me?" and shows it. Draining is destructive on the server
/// side, so one poll here equals one notification per alert.
///
/// Clicking a notification activates the app, which is the OS default. Tauri
/// v2's notification plugin exposes click actions on mobile only, so a click
/// cannot yet open the specific investigation — the plan's "click opens an
/// investigation" is not achievable with the plugin as it stands. The
/// namespace and pod are carried in the payload, ready for when it is.
fn start_notifier(app: tauri::AppHandle, shell: Shell) {
    use tauri_plugin_notification::NotificationExt;

    std::thread::spawn(move || {
        // macOS and Windows both gate notifications on a user grant. Ask once
        // at startup rather than at the moment the first alert fires, so the
        // permission prompt is not the thing competing with the alert.
        match app.notification().permission_state() {
            Ok(tauri_plugin_notification::PermissionState::Granted) => {}
            Ok(_) => match app.notification().request_permission() {
                Ok(state) => println!("[tauri] Notification permission: {state:?}"),
                Err(error) => {
                    eprintln!("[tauri] Could not request notification permission: {error}")
                }
            },
            Err(error) => eprintln!("[tauri] Notification permission unknown: {error}"),
        }

        loop {
            std::thread::sleep(Duration::from_secs(15));

            let (Some(base), token) = (
                shell.base_url.lock().unwrap().clone(),
                shell.token.lock().unwrap().clone(),
            ) else {
                continue;
            };

            let Some(body) = backend_get(
                &base,
                "/api/desktop/notifications",
                token.as_deref(),
                Duration::from_secs(10),
            ) else {
                continue;
            };

            let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) else {
                continue;
            };
            let Some(alerts) = parsed.get("alerts").and_then(|v| v.as_array()) else {
                continue;
            };

            for item in alerts {
                let name = item.get("name").and_then(|v| v.as_str()).unwrap_or("Alert");
                let severity = item.get("severity").and_then(|v| v.as_str()).unwrap_or("");
                let namespace = item.get("namespace").and_then(|v| v.as_str()).unwrap_or("");
                let pod = item.get("pod").and_then(|v| v.as_str()).unwrap_or("");
                let summary = item.get("summary").and_then(|v| v.as_str()).unwrap_or("");

                let title = if severity.is_empty() {
                    name.to_string()
                } else {
                    format!("{name} ({severity})")
                };
                let mut detail = String::new();
                if !namespace.is_empty() {
                    detail.push_str(namespace);
                    if !pod.is_empty() {
                        detail.push('/');
                        detail.push_str(pod);
                    }
                }
                if !summary.is_empty() {
                    if !detail.is_empty() {
                        detail.push_str(" — ");
                    }
                    detail.push_str(summary);
                }

                if let Err(error) = app
                    .notification()
                    .builder()
                    .title(&title)
                    .body(if detail.is_empty() { "Firing" } else { &detail })
                    .show()
                {
                    eprintln!("[tauri-error] Could not raise notification: {error}");
                }
            }
        }
    });
}

/// Read the connected context from /health for the tray's cluster line.
fn poll_cluster_label(base: &str) -> Option<String> {
    let body = backend_get(base, "/health", None, Duration::from_secs(5))?;
    let parsed: serde_json::Value = serde_json::from_str(&body).ok()?;
    match parsed.get("kubectl_context").and_then(|v| v.as_str()) {
        Some(context) => Some(format!("Cluster: {context}")),
        None => Some("Cluster: not connected".to_string()),
    }
}

/// Menu-bar presence: which cluster is attached, and a way in.
///
/// Left-clicking the icon is not wired to the menu on macOS — there, a click
/// opens the menu, which is the platform convention. `show_menu_on_left_click`
/// keeps that behaviour explicit rather than inherited.
fn build_tray(app: &tauri::AppHandle, shell: Shell) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
    use tauri::tray::TrayIconBuilder;

    let cluster = MenuItem::with_id(app, "cluster", "Connecting…", false, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open KubeAstra", true, None::<&str>)?;
    let investigate =
        MenuItem::with_id(app, "investigate", "New investigation…", true, Some("CmdOrCtrl+N"))?;
    let update = MenuItem::with_id(app, "update", "Check for Updates…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit KubeAstra", true, Some("CmdOrCtrl+Q"))?;

    let menu = Menu::with_items(
        app,
        &[
            &cluster,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &investigate,
            &PredefinedMenuItem::separator(app)?,
            &update,
            &quit,
        ],
    )?;

    let handler_shell = shell.clone();
    TrayIconBuilder::with_id("main")
        // A dedicated monochrome mark, not the app icon. macOS template icons
        // are rendered from the alpha channel alone, and icons/icon.png is
        // 100% opaque — using it produced a solid black square in the menu
        // bar. icons/tray.png carries its shape in alpha.
        .icon(tauri::include_image!("icons/tray.png"))
        .icon_as_template(true)
        .tooltip("KubeAstra")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(move |app, event| {
            let Some(window) = app.get_webview_window("main") else {
                return;
            };
            match event.id().as_ref() {
                "open" => front(&window),
                "investigate" => {
                    front(&window);
                    dispatch(&window, &handler_shell, "focus", &[]);
                }
                // Manual check: answers either way, including "already current".
                "update" => updater::spawn_check(app.clone(), true),
                "quit" => app.exit(0),
                _ => {}
            }
        })
        .build(app)?;

    // Keep the cluster line current. 30s is slow enough to be free and fast
    // enough that switching kubectl context shows up without a restart.
    std::thread::spawn(move || {
        let mut last = String::new();
        loop {
            std::thread::sleep(Duration::from_secs(30));
            let base = shell.base_url.lock().unwrap().clone();
            let Some(base) = base else { continue };
            if let Some(label) = poll_cluster_label(&base) {
                if label != last {
                    let _ = cluster.set_text(&label);
                    last = label;
                }
            }
        }
    });

    Ok(())
}

/// Turn `kubeastra://investigate?ns=…&pod=…` into an in-app request.
///
/// Only the recognised keys are forwarded, and `dispatch` percent-encodes
/// every value — the URL comes from outside the app (a browser, the VS Code
/// extension, anything registered to open the scheme) and is untrusted.
/// Pure half of deep-link handling, so it can be tested without a window.
///
/// Returns the namespace and pod a link asks for, or None if the URL is not
/// something this app should act on.
fn parse_deep_link(raw: &str) -> Option<(String, String)> {
    let rest = raw.strip_prefix("kubeastra://")?;
    let (action, query) = match rest.split_once('?') {
        Some((action, query)) => (action.trim_end_matches('/'), query),
        None => (rest.trim_end_matches('/'), ""),
    };
    if action != "investigate" {
        return None;
    }

    let mut namespace = String::new();
    let mut pod = String::new();
    for pair in query.split('&') {
        match pair.split_once('=') {
            Some(("ns", value)) => namespace = decode_component(value),
            Some(("pod", value)) => pod = decode_component(value),
            _ => {}
        }
    }
    Some((namespace, pod))
}

fn handle_deep_link(window: &WebviewWindow, shell: &Shell, raw: &str) {
    println!("[tauri] Deep link: {raw}");

    let Some((namespace, pod)) = parse_deep_link(raw) else {
        eprintln!("[tauri-error] Ignoring unusable deep link: {raw}");
        return;
    };

    let mut params: Vec<(&str, &str)> = Vec::new();
    if !namespace.is_empty() {
        params.push(("ns", namespace.as_str()));
    }
    if !pod.is_empty() {
        params.push(("pod", pod.as_str()));
    }

    front(window);
    dispatch(window, shell, "investigate", &params);
}

/// Percent-decode a query value. Invalid escapes are kept verbatim rather
/// than dropped, so a malformed link degrades to visible text.
fn decode_component(value: &str) -> String {
    let bytes = value.replace('+', " ").into_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
            if let Ok(byte) = u8::from_str_radix(hex, 16) {
                out.push(byte);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Summon the app from anywhere with Cmd/Ctrl+Shift+K.
///
/// Registration is best-effort: the combination may already be taken by
/// another app, and on Linux it depends on the compositor. A failure logs and
/// the app runs on — the tray and the window still work.
fn register_shortcut(app: &tauri::AppHandle, shell: Shell) {
    use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

    let accelerator = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::KeyK);
    let handle = app.clone();

    let result = app.global_shortcut().on_shortcut(accelerator, move |_app, _shortcut, event| {
        // Fire once per press, not again on release.
        if event.state != ShortcutState::Pressed {
            return;
        }
        let Some(window) = handle.get_webview_window("main") else {
            return;
        };
        front(&window);
        dispatch(&window, &shell, "focus", &[]);
    });

    match result {
        Ok(()) => println!("[tauri] Global shortcut registered: Cmd/Ctrl+Shift+K"),
        Err(error) => eprintln!("[tauri] Global shortcut unavailable (likely already taken): {error}"),
    }
}

/// Register the `kubeastra://` scheme and handle links while running.
///
/// The scheme is declared in tauri.conf.json so the installer registers it
/// with the OS. `register_all` additionally claims it at runtime, which is
/// what makes deep links work in a dev build that was never installed.
fn register_deep_links(app: &tauri::App, shell: Shell) {
    use tauri_plugin_deep_link::DeepLinkExt;

    #[cfg(any(target_os = "linux", all(debug_assertions, windows)))]
    if let Err(error) = app.deep_link().register_all() {
        eprintln!("[tauri] Could not register the kubeastra:// scheme: {error}");
    }

    let handle = app.handle().clone();
    app.deep_link().on_open_url(move |event| {
        let Some(window) = handle.get_webview_window("main") else {
            return;
        };
        for url in event.urls() {
            handle_deep_link(&window, &shell, url.as_str());
        }
    });
}

fn main() {
    let shell = Shell::default();
    let for_exit = Arc::clone(&shell.backend);

    let single_instance_shell = shell.clone();
    let setup_shell = shell.clone();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(move |app, argv, _cwd| {
            let Some(window) = app.get_webview_window("main") else {
                return;
            };
            front(&window);
            // On Windows and Linux a deep link reaches an already-running app
            // as argv on the second instance, not through the plugin event.
            if let Some(url) = argv.iter().find(|a| a.starts_with("kubeastra://")) {
                handle_deep_link(&window, &single_instance_shell, url);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .setup(move |app| {
            let window = app
                .get_webview_window("main")
                .expect("Main window missing from tauri.conf.json");

            build_tray(app.handle(), setup_shell.clone())?;
            register_shortcut(app.handle(), setup_shell.clone());
            register_deep_links(app, setup_shell.clone());
            start_notifier(app.handle().clone(), setup_shell.clone());

            let backend_window = window.clone();
            let backend_shell = setup_shell.clone();
            std::thread::spawn(move || start_backend(backend_window, backend_shell));

            // Silent unless there is genuinely something to install — see the
            // note at the top of updater.rs. Spawned last so a slow or
            // unreachable endpoint cannot hold up the window.
            updater::spawn_check(app.handle().clone(), false);
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

#[cfg(test)]
mod tests {
    use super::*;

    // ── encode_fragment ───────────────────────────────────────────────────
    // Values reach the fragment from outside the app (a deep link, a pod
    // name). If any of them could emit a bare & or #, they would forge extra
    // parameters or truncate the fragment.

    #[test]
    fn encode_fragment_passes_unreserved_characters_through() {
        assert_eq!(encode_fragment("api-gateway_0.v1~x"), "api-gateway_0.v1~x");
    }

    #[test]
    fn encode_fragment_neutralises_separators() {
        assert_eq!(encode_fragment("a&b=c#d"), "a%26b%3Dc%23d");
        assert_eq!(encode_fragment("a b"), "a%20b");
    }

    #[test]
    fn encode_fragment_handles_non_ascii() {
        assert_eq!(encode_fragment("café"), "caf%C3%A9");
    }

    // ── decode_component ──────────────────────────────────────────────────

    #[test]
    fn decode_component_reverses_percent_encoding() {
        assert_eq!(decode_component("kube%2Dsystem"), "kube-system");
        assert_eq!(decode_component("a%20b"), "a b");
        assert_eq!(decode_component("caf%C3%A9"), "café");
    }

    #[test]
    fn decode_component_treats_plus_as_space() {
        assert_eq!(decode_component("a+b"), "a b");
    }

    #[test]
    fn decode_component_keeps_malformed_escapes_verbatim() {
        // Degrade to visible text rather than silently dropping characters.
        assert_eq!(decode_component("100%"), "100%");
        assert_eq!(decode_component("%zz"), "%zz");
    }

    // ── parse_deep_link ───────────────────────────────────────────────────

    #[test]
    fn parse_deep_link_reads_namespace_and_pod() {
        assert_eq!(
            parse_deep_link("kubeastra://investigate?ns=prod&pod=api-0"),
            Some(("prod".into(), "api-0".into()))
        );
    }

    #[test]
    fn parse_deep_link_tolerates_a_trailing_slash_and_reordering() {
        assert_eq!(
            parse_deep_link("kubeastra://investigate/?pod=api-0&ns=prod"),
            Some(("prod".into(), "api-0".into()))
        );
    }

    #[test]
    fn parse_deep_link_allows_either_parameter_alone() {
        assert_eq!(
            parse_deep_link("kubeastra://investigate?ns=prod"),
            Some(("prod".into(), String::new()))
        );
        assert_eq!(
            parse_deep_link("kubeastra://investigate"),
            Some((String::new(), String::new()))
        );
    }

    #[test]
    fn parse_deep_link_ignores_unknown_parameters() {
        assert_eq!(
            parse_deep_link("kubeastra://investigate?ns=prod&exec=rm+-rf&pod=api-0"),
            Some(("prod".into(), "api-0".into()))
        );
    }

    #[test]
    fn parse_deep_link_rejects_foreign_schemes_and_actions() {
        // The scheme is registered with the OS, so anything on the machine can
        // invoke it. Only `investigate` is acted on.
        assert_eq!(parse_deep_link("https://evil.example/investigate?ns=x"), None);
        assert_eq!(parse_deep_link("kubeastra://shell?cmd=whoami"), None);
        assert_eq!(parse_deep_link("kubeastra://"), None);
    }

    #[test]
    fn parse_deep_link_cannot_forge_extra_fragment_parameters() {
        // A pod name carrying separators must stay one value once encoded.
        let (namespace, pod) =
            parse_deep_link("kubeastra://investigate?ns=prod&pod=x%26kubeastra%3Dfocus").unwrap();
        assert_eq!(namespace, "prod");
        assert_eq!(pod, "x&kubeastra=focus");
        assert_eq!(encode_fragment(&pod), "x%26kubeastra%3Dfocus");
    }
}
