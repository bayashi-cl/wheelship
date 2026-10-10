//! wheelship launcher.
//!
//! Lives at the root of a bundle next to `wheelship.json`, or carries the whole
//! bundle as an appended zip payload (single-file executable, extracted once
//! into a cache; see [`payload`]). It selects the entry point by its own file
//! stem (busybox style), then runs Python:
//!
//! ```text
//! <interpreter> <entry point args...> <path of this executable> <user args...>
//! ```
//!
//! The interpreter is either bundled (python-build-standalone inside the
//! bundle) or a system one found on `PATH` (`WHEELSHIP_PYTHON` overrides).
//!
//! On Unix the launcher `exec`s the interpreter (same PID, natural signals).
//! On Windows it spawns the interpreter, ignores Ctrl-C itself (the child gets
//! it from the shared console), ties the child's lifetime to its own through a
//! job object, and exits with the child's exit code.
//!
//! Environment variables (never command-line flags, to avoid clashing with the
//! application's own arguments):
//! * `WHEELSHIP_DEBUG=1`     print what is being executed to stderr
//! * `WHEELSHIP_BOOT=python` run the interpreter with the given arguments
//! * `WHEELSHIP_PYTHON`      interpreter to use for system-interpreter bundles
//! * `WHEELSHIP_CACHE_DIR`   where single-file executables are extracted

use std::ffi::OsString;
use std::fs::File;
use std::path::{Path, PathBuf};

mod manifest;
pub mod payload;
pub use manifest::{Candidate, EntryPoint, Interpreter, Manifest, Mode};

pub const MANIFEST_NAME: &str = "wheelship.json";
const ROOT_PREFIX: &str = "{root}/";

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

/// Process environment as seen by [`plan`]; injected for testability.
pub struct Env<'a> {
    pub boot: Option<OsString>,
    pub python: Option<OsString>,
    /// Resolve a program name on `PATH`.
    pub which: &'a dyn Fn(&str) -> Option<PathBuf>,
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

fn debug_enabled() -> bool {
    std::env::var_os("WHEELSHIP_DEBUG").is_some_and(|v| !v.is_empty() && v != "0")
}

fn launch() -> Result<i32, String> {
    let exe = current_exe()?;
    let root = bundle_root(&exe)?;
    let manifest = Manifest::load(&root.join(MANIFEST_NAME))?;
    let user_args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let env = Env {
        boot: std::env::var_os("WHEELSHIP_BOOT"),
        python: std::env::var_os("WHEELSHIP_PYTHON"),
        which: &which,
    };
    let invocation = plan(&manifest, &root, &exe, user_args, &env)?;
    if debug_enabled() {
        eprintln!("wheelship-launcher: bundle root: {}", root.display());
        eprintln!(
            "wheelship-launcher: exec {:?} {:?}",
            invocation.program, invocation.args
        );
    }
    platform::run(invocation)
}

/// The directory holding `wheelship.json`: next to the launcher, or the
/// extracted payload of a single-file executable.
fn bundle_root(exe: &Path) -> Result<PathBuf, String> {
    let mut file = File::open(exe).map_err(|e| format!("cannot open {}: {e}", exe.display()))?;
    match payload::find(&mut file).map_err(|e| format!("{}: {e}", exe.display()))? {
        Some(trailer) => payload::ensure_extracted(exe, file, &trailer),
        None => exe
            .parent()
            .map(Path::to_path_buf)
            .ok_or_else(|| format!("cannot determine the directory of {}", exe.display())),
    }
}

/// Decide what to execute. No side effects: unit-testable.
pub fn plan(
    manifest: &Manifest,
    root: &Path,
    exe: &Path,
    user_args: Vec<OsString>,
    env: &Env,
) -> Result<Invocation, String> {
    let boot = env.boot.as_deref().filter(|b| !b.is_empty());
    if let Some(boot) = boot {
        if boot != "python" {
            return Err(format!(
                "unsupported WHEELSHIP_BOOT value {boot:?} (expected 'python')"
            ));
        }
        let (program, mut args) = interpreter(manifest, root, false, env)?;
        args.extend(user_args);
        return Ok(Invocation { program, args });
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

    let (program, mut args) = interpreter(manifest, root, entry_point.gui, env)?;
    args.extend(
        entry_point
            .args
            .iter()
            .map(|a| match a.strip_prefix(ROOT_PREFIX) {
                Some(rel) => join_relative(root, rel).into_os_string(),
                None => OsString::from(a),
            }),
    );
    args.push(exe.as_os_str().to_owned());
    args.extend(user_args);
    Ok(Invocation { program, args })
}

/// The interpreter program and the arguments that must precede everything else.
fn interpreter(
    manifest: &Manifest,
    root: &Path,
    gui: bool,
    env: &Env,
) -> Result<(PathBuf, Vec<OsString>), String> {
    let interp = &manifest.interpreter;
    match interp.mode {
        Mode::Bundled => {
            let path = match (&interp.gui_path, gui) {
                (Some(gui_path), true) => gui_path,
                _ => interp
                    .path
                    .as_ref()
                    .ok_or_else(|| format!("{MANIFEST_NAME}: interpreter.path is missing"))?,
            };
            Ok((join_relative(root, path), Vec::new()))
        }
        Mode::System => {
            if let Some(python) = env.python.as_ref().filter(|p| !p.is_empty()) {
                let as_path = PathBuf::from(python);
                let program = match as_path.to_str() {
                    Some(name) if as_path.components().count() == 1 => {
                        (env.which)(name).unwrap_or(as_path)
                    }
                    _ => as_path,
                };
                return Ok((program, Vec::new()));
            }
            for candidate in &interp.candidates {
                let name = match (&candidate.gui_program, gui) {
                    (Some(g), true) => g,
                    _ => &candidate.program,
                };
                if let Some(program) = (env.which)(name) {
                    let args = candidate.args.iter().map(OsString::from).collect();
                    return Ok((program, args));
                }
            }
            let tried: Vec<String> = interp
                .candidates
                .iter()
                .map(|c| {
                    std::iter::once(c.program.as_str())
                        .chain(c.args.iter().map(String::as_str))
                        .collect::<Vec<_>>()
                        .join(" ")
                })
                .collect();
            Err(format!(
                "no suitable Python interpreter found on PATH (tried: {}); \
                 install it or set WHEELSHIP_PYTHON",
                tried.join(", ")
            ))
        }
    }
}

/// Look up `name` on `PATH` (with `PATHEXT` on Windows).
pub fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let exts: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".EXE;.COM;.BAT;.CMD".into())
            .split(';')
            .filter(|e| !e.is_empty())
            .map(|e| e.to_ascii_lowercase())
            .collect()
    } else {
        Vec::new()
    };
    for dir in std::env::split_paths(&path) {
        let base = dir.join(name);
        if cfg!(windows) {
            if Path::new(name).extension().is_some() && base.is_file() {
                return Some(base);
            }
            for ext in &exts {
                let candidate = dir.join(format!("{name}{ext}"));
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        } else if is_executable(&base) {
            return Some(base);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
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
    let text = format!("wheelship-launcher: {message}");
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
        let (text, caption) = (wide(text), wide("wheelship"));
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

    fn no_path(_: &str) -> Option<PathBuf> {
        None
    }

    fn env(boot: Option<&str>) -> Env<'static> {
        Env {
            boot: boot.map(OsString::from),
            python: None,
            which: &no_path,
        }
    }

    #[test]
    fn selects_entry_point_by_stem() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let exe = root.join("demo");
        let inv = plan(&m, root, &exe, os(&["--flag", "x"]), &env(None)).unwrap();
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
        let inv = plan(&m, root, &root.join("demo-gui.exe"), vec![], &env(None)).unwrap();
        assert_eq!(inv.program, root.join("python").join("pythonw.exe"));
        assert_eq!(inv.args[2], OsString::from("gui"));
    }

    #[test]
    fn renamed_executable_falls_back_to_default() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let inv = plan(&m, root, &root.join("renamed"), vec![], &env(None)).unwrap();
        assert_eq!(inv.args[2], OsString::from("code"));
    }

    #[test]
    fn unknown_entry_point_without_default_fails() {
        let text = MANIFEST.replace(r#""default_entry_point": "demo""#, r#""x": null"#);
        let m = Manifest::parse(&text).unwrap();
        let err = plan(
            &m,
            Path::new("/r"),
            Path::new("/r/other"),
            vec![],
            &env(None),
        )
        .unwrap_err();
        assert!(err.contains("available: demo, demo-gui"), "{err}");
    }

    #[test]
    fn boot_python_runs_interpreter_directly() {
        let m = Manifest::parse(MANIFEST).unwrap();
        let root = Path::new("/opt/demo");
        let inv = plan(
            &m,
            root,
            &root.join("demo"),
            os(&["-V"]),
            &env(Some("python")),
        )
        .unwrap();
        assert_eq!(inv.program, root.join("python/bin/python3.12"));
        assert_eq!(inv.args, os(&["-V"]));
        assert!(plan(&m, root, &root.join("demo"), vec![], &env(Some("shell"))).is_err());
    }

    const SYSTEM: &str = r#"{
        "schema": 1,
        "interpreter": {"mode": "system", "candidates": [
            {"program": "py", "gui_program": "pyw", "args": ["-3.12"]},
            {"program": "python3.12"}
        ]},
        "entry_points": {
            "tool": {"args": ["-I", "{root}/_wheelship_boot.py", "tool"]},
            "tool-gui": {"args": ["-I", "{root}/_wheelship_boot.py", "tool-gui"], "gui": true}
        }
    }"#;

    #[test]
    fn system_interpreter_from_candidates() {
        let m = Manifest::parse(SYSTEM).unwrap();
        let root = Path::new("/opt/tool");
        let only_python312 =
            |name: &str| (name == "python3.12").then(|| PathBuf::from("/usr/bin/python3.12"));
        let e = Env {
            boot: None,
            python: None,
            which: &only_python312,
        };
        let inv = plan(&m, root, &root.join("tool"), os(&["x"]), &e).unwrap();
        assert_eq!(inv.program, Path::new("/usr/bin/python3.12"));
        let boot_script = root.join("_wheelship_boot.py").into_os_string();
        assert_eq!(
            inv.args,
            vec![
                "-I".into(),
                boot_script,
                "tool".into(),
                "/opt/tool/tool".into(),
                "x".into()
            ]
        );
    }

    #[test]
    fn system_interpreter_py_launcher_and_gui() {
        let m = Manifest::parse(SYSTEM).unwrap();
        let root = Path::new("/opt/tool");
        let py = |name: &str| {
            ["py", "pyw"]
                .contains(&name)
                .then(|| PathBuf::from(format!("C:/W/{name}.exe")))
        };
        let e = Env {
            boot: None,
            python: None,
            which: &py,
        };
        let inv = plan(&m, root, &root.join("tool"), vec![], &e).unwrap();
        assert_eq!(inv.program, Path::new("C:/W/py.exe"));
        assert_eq!(inv.args[0], OsString::from("-3.12"));
        let gui = plan(&m, root, &root.join("tool-gui"), vec![], &e).unwrap();
        assert_eq!(gui.program, Path::new("C:/W/pyw.exe"));
    }

    #[test]
    fn system_interpreter_override_and_missing() {
        let m = Manifest::parse(SYSTEM).unwrap();
        let root = Path::new("/opt/tool");
        let e = Env {
            boot: None,
            python: Some("/x/python".into()),
            which: &no_path,
        };
        let inv = plan(&m, root, &root.join("tool"), vec![], &e).unwrap();
        assert_eq!(inv.program, Path::new("/x/python"));
        assert_eq!(inv.args[0], OsString::from("-I")); // no candidate args with an override
        let err = plan(&m, root, &root.join("tool"), vec![], &env(None)).unwrap_err();
        assert!(err.contains("tried: py -3.12, python3.12"), "{err}");
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
