//! Auto-update, driven entirely from Rust.
//!
//! The usual Tauri pattern is to call the updater's JavaScript API from the
//! web app. That is unavailable here: the UI is served over http from the
//! Python backend rather than through Tauri's protocol, so the webview has no
//! IPC bridge (see the note on `dispatch` in main.rs). Everything below —
//! checking, prompting, downloading, restarting — therefore happens on the
//! Rust side, and the only user-facing surface is a native dialog and a tray
//! item.
//!
//! Design notes:
//!
//! * The check on launch is **silent when there is nothing to install**. An
//!   app that interrupts you at startup to say "you are up to date" trains
//!   people to dismiss its dialogs without reading them, which is exactly the
//!   habit you do not want when one of them eventually says something that
//!   matters.
//! * A failed check is **not** shown to the user. Being offline, or behind a
//!   proxy, or on an airline network is normal and is not a problem the
//!   operator needs to act on. It is logged and dropped.
//! * The tray item is explicit: when *you* ask, you get an answer either way,
//!   including "already current".

use std::sync::atomic::{AtomicBool, Ordering};

/// One update check at a time. Without this, a launch check and an impatient
/// tray click race each other into two downloads of the same DMG.
static CHECKING: AtomicBool = AtomicBool::new(false);

/// What a check found. Separated from the acting on it so the decision logic
/// is testable without a network or a running Tauri app.
#[derive(Debug, PartialEq, Eq)]
pub enum CheckOutcome {
    /// A newer version is available.
    Available { version: String },
    /// Already on the newest version.
    UpToDate,
    /// The check itself failed — offline, DNS, endpoint down.
    Failed { reason: String },
}

/// Whether an outcome is worth interrupting the user for, given how the check
/// was started.
///
/// The rule: an automatic check may only ever interrupt to offer a real
/// update. A manual one answers whatever it found, because the user asked.
pub fn should_notify(outcome: &CheckOutcome, manual: bool) -> bool {
    match outcome {
        CheckOutcome::Available { .. } => true,
        CheckOutcome::UpToDate | CheckOutcome::Failed { .. } => manual,
    }
}

/// Human-readable text for an outcome. Kept out of the async path so the
/// wording is covered by tests rather than only by reading it.
pub fn describe(outcome: &CheckOutcome, current: &str) -> String {
    match outcome {
        CheckOutcome::Available { version } => format!(
            "KubeAstra {version} is available. You are on {current}.\n\n\
             The update downloads in the background and applies when you \
             restart the app."
        ),
        CheckOutcome::UpToDate => {
            format!("KubeAstra {current} is the latest version.")
        }
        CheckOutcome::Failed { reason } => format!(
            "Could not check for updates.\n\n{reason}\n\n\
             This is usually a network problem rather than something wrong \
             with the app."
        ),
    }
}

#[cfg(desktop)]
mod imp {
    use super::*;
    use tauri::AppHandle;
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
    use tauri_plugin_updater::UpdaterExt;

    /// Check, and act on the result. `manual` is true when the user asked via
    /// the tray, which makes the "nothing to do" cases visible.
    pub async fn check(app: AppHandle, manual: bool) {
        if CHECKING.swap(true, Ordering::SeqCst) {
            log_line("update check already running; ignoring");
            return;
        }
        let _guard = Guard;

        let current = app.package_info().version.to_string();
        let outcome = match app.updater() {
            Ok(updater) => match updater.check().await {
                Ok(Some(update)) => CheckOutcome::Available {
                    version: update.version.clone(),
                },
                Ok(None) => CheckOutcome::UpToDate,
                Err(error) => CheckOutcome::Failed {
                    reason: error.to_string(),
                },
            },
            Err(error) => CheckOutcome::Failed {
                reason: error.to_string(),
            },
        };

        log_line(&format!("update check ({}): {outcome:?}", if manual { "manual" } else { "startup" }));

        if !should_notify(&outcome, manual) {
            return;
        }

        match outcome {
            CheckOutcome::Available { .. } => {
                let text = describe(&outcome, &current);
                if confirm(&app, "Update available", &text) {
                    install(app).await;
                }
            }
            other => {
                notify(&app, "KubeAstra", &describe(&other, &current));
            }
        }
    }

    async fn install(app: AppHandle) {
        let updater = match app.updater() {
            Ok(u) => u,
            Err(error) => {
                log_line(&format!("updater unavailable at install: {error}"));
                return;
            }
        };
        let update = match updater.check().await {
            Ok(Some(update)) => update,
            Ok(None) => return,
            Err(error) => {
                log_line(&format!("update vanished between check and install: {error}"));
                return;
            }
        };

        let mut downloaded = 0usize;
        let result = update
            .download_and_install(
                |chunk, total| {
                    downloaded += chunk;
                    if let Some(total) = total {
                        log_line(&format!("update: {downloaded}/{total} bytes"));
                    }
                },
                || log_line("update: download complete, applying"),
            )
            .await;

        match result {
            Ok(()) => {
                log_line("update installed; restarting");
                app.restart();
            }
            Err(error) => {
                // Worth showing: the user opted in and is now waiting.
                log_line(&format!("update failed: {error}"));
                notify(
                    &app,
                    "Update failed",
                    &format!(
                        "KubeAstra could not install the update.\n\n{error}\n\n\
                         The installed version is unchanged and still works."
                    ),
                );
            }
        }
    }

    /// Blocking confirm. Safe here because `check` runs in a spawned task,
    /// never on the main thread — `blocking_show` would deadlock there.
    fn confirm(app: &AppHandle, title: &str, body: &str) -> bool {
        app.dialog()
            .message(body)
            .title(title)
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Install and restart".into(),
                "Not now".into(),
            ))
            .blocking_show()
    }

    fn notify(app: &AppHandle, title: &str, body: &str) {
        app.dialog().message(body).title(title).blocking_show();
    }

    fn log_line(message: &str) {
        println!("[tauri] {message}");
    }

    /// Clears the in-flight flag however `check` returns.
    struct Guard;
    impl Drop for Guard {
        fn drop(&mut self) {
            CHECKING.store(false, Ordering::SeqCst);
        }
    }
}

#[cfg(desktop)]
pub use imp::check;

/// Spawn a check without blocking the caller. Used for the launch check and
/// the tray item alike.
#[cfg(desktop)]
pub fn spawn_check(app: tauri::AppHandle, manual: bool) {
    tauri::async_runtime::spawn(async move {
        check(app, manual).await;
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_startup_check_only_interrupts_for_a_real_update() {
        let available = CheckOutcome::Available { version: "1.1.0".into() };
        assert!(should_notify(&available, false));
    }

    #[test]
    fn a_startup_check_stays_silent_when_current() {
        // Interrupting launch to say "nothing to do" teaches people to
        // dismiss dialogs unread.
        assert!(!should_notify(&CheckOutcome::UpToDate, false));
    }

    #[test]
    fn a_startup_check_stays_silent_when_offline() {
        let failed = CheckOutcome::Failed { reason: "dns error".into() };
        assert!(!should_notify(&failed, false));
    }

    #[test]
    fn a_manual_check_always_answers() {
        assert!(should_notify(&CheckOutcome::UpToDate, true));
        assert!(should_notify(&CheckOutcome::Failed { reason: "x".into() }, true));
        assert!(should_notify(&CheckOutcome::Available { version: "2".into() }, true));
    }

    #[test]
    fn the_available_message_names_both_versions() {
        let text = describe(&CheckOutcome::Available { version: "1.2.0".into() }, "1.0.0");
        assert!(text.contains("1.2.0"), "should name the new version: {text}");
        assert!(text.contains("1.0.0"), "should name the current version: {text}");
    }

    #[test]
    fn a_failed_check_says_it_is_probably_the_network() {
        let text = describe(&CheckOutcome::Failed { reason: "timed out".into() }, "1.0.0");
        assert!(text.contains("timed out"));
        assert!(text.contains("network"), "should not read as app breakage: {text}");
    }

    #[test]
    fn the_up_to_date_message_names_the_version_in_use() {
        let text = describe(&CheckOutcome::UpToDate, "1.0.0");
        assert!(text.contains("1.0.0"));
    }
}
