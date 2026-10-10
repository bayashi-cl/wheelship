//! `wheelship.json`: the only wheelship-specific file format the launcher reads.
//!
//! Unknown fields are ignored so that newer wheelship versions can add metadata
//! without breaking older launchers; incompatible changes bump `schema`.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;

pub const SCHEMA: u32 = 1;

#[derive(Debug, Deserialize)]
pub struct Manifest {
    pub schema: u32,
    pub interpreter: Interpreter,
    pub entry_points: BTreeMap<String, EntryPoint>,
    /// Used when the executable was renamed and its stem matches no entry point.
    #[serde(default)]
    pub default_entry_point: Option<String>,
}

#[derive(Debug, Default, Deserialize, PartialEq, Eq, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    /// python-build-standalone shipped inside the bundle.
    #[default]
    Bundled,
    /// An interpreter installed on the target machine.
    System,
}

#[derive(Debug, Deserialize)]
pub struct Interpreter {
    #[serde(default)]
    pub mode: Mode,
    /// Bundled: path relative to the bundle root, `/`-separated.
    #[serde(default)]
    pub path: Option<String>,
    /// Bundled: Windows `pythonw.exe` for gui entry points.
    #[serde(default)]
    pub gui_path: Option<String>,
    /// System: programs to look up on `PATH`, in order.
    #[serde(default)]
    pub candidates: Vec<Candidate>,
}

#[derive(Debug, Deserialize)]
pub struct Candidate {
    /// Program name (looked up on `PATH`), e.g. `python3.12` or `py`.
    pub program: String,
    /// Used instead of `program` for gui entry points (e.g. `pyw`).
    #[serde(default)]
    pub gui_program: Option<String>,
    /// Arguments selecting the version, e.g. `["-3.12"]` for the `py` launcher.
    #[serde(default)]
    pub args: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct EntryPoint {
    /// Interpreter arguments placed before the executable path and user arguments.
    /// An argument starting with `{root}/` is a path relative to the bundle root.
    pub args: Vec<String>,
    #[serde(default)]
    pub gui: bool,
}

impl Manifest {
    pub fn parse(text: &str) -> Result<Self, String> {
        let m: Manifest =
            serde_json::from_str(text).map_err(|e| format!("invalid manifest: {e}"))?;
        if m.schema != SCHEMA {
            return Err(format!(
                "manifest schema {} is not supported by this launcher (expected {SCHEMA}); \
                 rebuild the bundle with a matching wheelship",
                m.schema
            ));
        }
        Ok(m)
    }

    pub fn load(path: &Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        Self::parse(&text).map_err(|e| format!("{}: {e}", path.display()))
    }
}
