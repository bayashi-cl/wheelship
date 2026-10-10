//! pybox launcher.
//!
//! Lives at the root of a bundle next to `pybox.json`. It selects the entry
//! point by its own file stem (busybox style), then runs the bundled Python:
//!
//! ```text
//! <interpreter> <entry point args...> <path of this executable> <user args...>
//! ```
//!
//! On Unix the launcher `exec`s the interpreter (same PID, natural signals).
//! On Windows it spawns the interpreter, ignores Ctrl-C itself (the child gets
//! it from the shared console), ties the child's lifetime to its own through a
//! job object, and exits with the child's exit code.
//!
//! Environment variables (never command-line flags, to avoid clashing with the
//! application's own arguments):
//! * `PYBOX_DEBUG=1`    print what is being executed to stderr
//! * `PYBOX_BOOT=python` run the bundled interpreter with the given arguments

use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};

mod manifest;
pub use manifest::{EntryPoint, Interpreter, Manifest};

pub const MANIFEST_NAME: &str = "pybox.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Subsystem {
    Console,
    Gui,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Invocation {
    pub program: PathBuf,
    pub args: Vec<OsString>,
}

/// Entry point of both launcher binaries.
pub fn main(subsystem: Subsystem) -> ! {
    let code = match launch() {
        Ok(code) => code,
        Err(message) => {
            report_error(subsystem, &message);
            1
        }
    };
    std::process::exit(code)
}

fn launch() -> Result<i32, String> {
    let exe = current_exe()?;
    let root = exe
        .parent()
        .ok_or_else(|| format!("cannot determine the directory of {}", exe.display()))?;
    let manifest = Manifest::load(&root.join(MANIFEST_NAME))?;
    let user_args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let boot = std::env::var_os("PYBOX_BOOT");
    let invocation = plan(&manifest, root, &exe, user_args, boot.as_deref())?;
    if std::env::var_os("PYBOX_DEBUG").is_some_and(|v| !v.is_empty() && v != "0") {
        eprintln!("pybox-launcher: bundle root: {}", root.display());
        eprintln!(
            "pybox-launcher: exec {:?} {:?}",
            invocation.program, invocation.args
        );
    }
    platform::run(invocation)
}

/// Decide what to execute. Pure function: unit-testable without a filesystem.
pub fn plan(
    manifest: &Manifest,
    root: &Path,
    exe: &Path,
    user_args: Vec<OsString>,
    boot: Option<&OsStr>,
) -> Result<Invocation, String> {
    if let Some(boot) = boot.filter(|b| !b.is_empty()) {
        if boot != "python" {
            return Err(format!(
                "unsupported PYBOX_BOOT value {boot:?} (expected 'python')"
            ));
        }
        return Ok(Invocation {
            program: join_relative(root, &manifest.interpreter.path),
            args: user_args,
        });
    }

    let name = exe
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let entry_point = manifest
        .entry_points
        .get(&name)
        .or_else(|| {
            manifest
                .default_entry_point
                .as_ref()
                .and_then(|d| manifest.entry_points.get(d))
        })
        .ok_or_else(|| {
            let known: Vec<&str> = manifest.entry_points.keys().map(String::as_str).collect();
            format!(
                "no entry point named {name:?} in {MANIFEST_NAME} (available: {})",
                known.join(", ")
            )
        })?;

    let interpreter = match (&manifest.interpreter.gui_path, entry_point.gui) {
        (Some(gui), true) => gui,
        _ => &manifest.interpreter.path,
    };
    let mut args: Vec<OsString> = entry_point.args.iter().map(OsString::from).collect();
    args.push(exe.as_os_str().to_owned());
    args.extend(user_args);
    Ok(Invocation {
        program: join_relative(root, interpreter),
        args,
    })
}

/// Join a `/`-separated manifest path onto `root` using native separators.
pub fn join_relative(root: &Path, relative: &str) -> PathBuf {
    let mut path = root.to_path_buf();
    path.extend(relative.split('/').filter(|c| !c.is_empty() && *c != "."));
    path
}

fn current_exe() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| format!("cannot locate the launcher: {e}"))?;
    // Resolve symlinks so that a launcher symlinked into e.g. ~/.local/bin
    // still finds its bundle.
    let exe = exe
        .canonicalize()
        .map_err(|e| format!("cannot resolve {}: {e}", exe.display()))?;
    Ok(strip_verbatim(exe))
}

/// `canonicalize` on Windows returns `\\?\C:\...`; hand Python a normal path.
fn strip_verbatim(path: PathBuf) -> PathBuf {
    if cfg!(windows) {
        let s = path.as_os_str().to_string_lossy();
        if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
            return PathBuf::from(format!(r"\\{rest}"));
        }
        if let Some(rest) = s.strip_prefix(r"\\?\")
            && rest.as_bytes().get(1) == Some(&b':')
        {
            return PathBuf::from(rest);
        }
    }
    path
}

fn report_error(subsystem: Subsystem, message: &str) {
    let text = format!("pybox-launcher: {message}");
    if subsystem == Subsystem::Gui {
        platform::show_error(&text);
    } else {
        eprintln!("{text}");
    }
}

#[cfg(unix)]
mod platform {
    use super::Invocation;
    use std::os::unix::process::CommandExt;
    use std::process::Command;

    pub fn run(invocation: Invocation) -> Result<i32, String> {
        let err = Command::new(&invocation.program)
            .args(&invocation.args)
            .exec();
        Err(format!(
            "cannot execute {}: {err}",
            invocation.program.display()
        ))
    }

    pub fn show_error(text: &str) {
        eprintln!("{text}");
    }
}

#[cfg(windows)]
mod platform {
    use super::Invocation;
    use std::os::windows::io::AsRawHandle;
    use std::process::Command;

    use windows_sys::Win32::Foundation::{FALSE, HANDLE, TRUE};
    use windows_sys::Win32::System::Console::{
        CTRL_BREAK_EVENT, CTRL_C_EVENT, SetConsoleCtrlHandler,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JobObjectExtendedLimitInformation, SetInformationJobObject,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONERROR, MB_OK, MessageBoxW};
    use windows_sys::core::BOOL;

    unsafe extern "system" fn ignore_interrupts(ctrl_type: u32) -> BOOL {
        // The child shares our console and receives the same event; we just
        // must not die before it does. Close/logoff/shutdown keep the default.
        if ctrl_type == CTRL_C_EVENT || ctrl_type == CTRL_BREAK_EVENT {
            TRUE
        } else {
            FALSE
        }
    }

    /// A job that kills its process when the launcher goes away. Only the
    /// Python process itself is assigned; with SILENT_BREAKAWAY_OK the
    /// processes it starts are not part of the job, so the application can
    /// still spawn long-lived processes.
    fn kill_on_close_job() -> Option<HANDLE> {
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags =
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK;
            let ok = SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            );
            if ok == 0 { None } else { Some(job) }
        }
    }

    pub fn run(invocation: Invocation) -> Result<i32, String> {
        unsafe {
            SetConsoleCtrlHandler(Some(ignore_interrupts), TRUE);
        }
        let job = kill_on_close_job();
        let mut child = Command::new(&invocation.program)
            .args(&invocation.args)
            .spawn()
            .map_err(|e| format!("cannot execute {}: {e}", invocation.program.display()))?;
        if let Some(job) = job {
            // Best effort: failure only loses the kill-on-close guarantee.
            unsafe {
                AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE);
            }
        }
        let status = child
            .wait()
            .map_err(|e| format!("waiting for the interpreter failed: {e}"))?;
        Ok(status.code().unwrap_or(1))
    }

    pub fn show_error(text: &str) {
        let wide = |s: &str| s.encode_utf16().chain(Some(0)).collect::<Vec<u16>>();
        let (text, caption) = (wide(text), wide("pybox"));
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                text.as_ptr(),
                caption.as_ptr(),
                MB_OK | MB_ICONERROR,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MANIFEST: &str = r#"{
        "schema": 1,
        "app": {"name": "demo", "version": "1.0"},
        "interpreter": {"path": "python/bin/python3.12", "gui_path": "python/pythonw.exe"},
        "entry_points": {
            "demo": {"args": ["-I", "-c", "code"]},
            "demo-gui": {"args": ["-I", "-c", "gui"], "gui": true}
        },
        "default_entry_point": "demo"
    }"#;

    fn os(v: &[&str]) -> Vec<OsString> {
        v.iter().map(OsString::from).collect()
    }

    #[test]
    fn selects_entry_point_by_stem() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let exe = root.join("demo");
        let inv = plan(&m, root, &exe, os(&["--flag", "x"]), None).unwrap();
        assert_eq!(
            inv.program,
            root.join("python").join("bin").join("python3.12")
        );
        assert_eq!(
            inv.args,
            os(&["-I", "-c", "code", "/opt/demo/demo", "--flag", "x"])
        );
    }

    #[test]
    fn gui_entry_point_uses_gui_interpreter() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let inv = plan(&m, root, &root.join("demo-gui.exe"), vec![], None).unwrap();
        assert_eq!(inv.program, root.join("python").join("pythonw.exe"));
        assert_eq!(inv.args[2], OsString::from("gui"));
    }

    #[test]
    fn renamed_executable_falls_back_to_default() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let inv = plan(&m, root, &root.join("renamed"), vec![], None).unwrap();
        assert_eq!(inv.args[2], OsString::from("code"));
    }

    #[test]
    fn unknown_entry_point_without_default_fails() {
        let text = MANIFEST.replace(r#""default_entry_point": "demo""#, r#""x": null"#);
        let m = Manifest::parse(&text).unwrap();
        let err = plan(&m, Path::new("/r"), Path::new("/r/other"), vec![], None).unwrap_err();
        assert!(err.contains("available: demo, demo-gui"), "{err}");
    }

    #[test]
    fn boot_python_runs_interpreter_directly() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let boot = OsString::from("python");
        let inv = plan(&m, root, &root.join("demo"), os(&["-V"]), Some(&boot)).unwrap();
        assert_eq!(inv.program, root.join("python/bin/python3.12"));
        assert_eq!(inv.args, os(&["-V"]));
        let bad = OsString::from("shell");
        assert!(plan(&m, root, &root.join("demo"), vec![], Some(&bad)).is_err());
    }

    #[test]
    fn rejects_other_schema() {
        let err =
            Manifest::parse(&MANIFEST.replace(r#""schema": 1"#, r#""schema": 2"#)).unwrap_err();
        assert!(err.contains("schema 2"), "{err}");
    }

    #[test]
    fn join_ignores_empty_and_dot_components() {
        assert_eq!(
            join_relative(Path::new("/r"), "./a//b"),
            Path::new("/r/a/b")
        );
    }
}
