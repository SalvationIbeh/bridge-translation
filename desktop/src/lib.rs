//! Vocivio desktop client core.
//!
//! Modules are UI-agnostic so both the headless probe (`main.rs`) and the future Tauri
//! UI (P1b) drive the same code path.

pub mod audio;
pub mod protocol;
pub mod pump;
pub mod ws;
