#![cfg_attr(windows, windows_subsystem = "windows")]

fn main() {
    pybox_launcher::main(pybox_launcher::Subsystem::Gui)
}
