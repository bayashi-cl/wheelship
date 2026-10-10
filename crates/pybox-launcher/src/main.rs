//! pybox launcher (Phase 1).
//!
//! Reads `pybox.json` next to the executable, selects the entry point by the
//! executable's file stem, and runs the bundled/system Python with it.

use std::process::ExitCode;

fn main() -> ExitCode {
    eprintln!("pybox-launcher: not implemented yet (Phase 1)");
    ExitCode::from(2)
}
