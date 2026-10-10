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

#[derive(Debug, Deserialize)]
pub struct Interpreter {
    /// Path relative to the bundle root, `/`-separated.
    pub path: String,
    /// Windows `pythonw.exe` for gui entry points.
    #[serde(default)]
    pub gui_path: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct EntryPoint {
    /// Interpreter arguments placed before the executable path and user arguments.
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
