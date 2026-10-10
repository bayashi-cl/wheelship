//! Single-file executables: a zip payload appended to the launcher.
//!
//! ```text
//! [launcher][payload.zip][meta.json][MAGIC][payload_offset][payload_len][meta_len]
//!                                    8 B    u64 LE          u64 LE       u64 LE
//! [optional Authenticode certificate table, appended by code signing]
//! ```
//!
//! The payload is extracted once into `<cache>/apps/<name>/<sha256[..16]>/`:
//! into a temporary sibling first, then renamed, so a directory that exists
//! is always complete and concurrent first runs are safe without file locks.

use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::Deserialize;

/// `WHSHPAY1`, stored masked: the launcher binary must not contain the
/// marker verbatim, or searching its own file would find this constant.
const MAGIC_MASKED: [u8; 8] = [0xA8, 0xB7, 0xAC, 0xB7, 0xAF, 0xBE, 0xA6, 0xCE];

pub fn magic() -> [u8; 8] {
    MAGIC_MASKED.map(|b| !b)
}
const TRAILER_LEN: u64 = 8 + 3 * 8;
/// How far from the end to look for the trailer (room for a signature).
const SEARCH_WINDOW: u64 = 1 << 20;
/// Previously used versions of an application kept besides the current one.
const KEEP_OTHER_VERSIONS: usize = 2;
const MARKER: &str = ".wheelship-last-used";

#[derive(Debug, Deserialize, PartialEq, Eq)]
pub struct Meta {
    pub name: String,
    pub sha256: String,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Trailer {
    pub payload_offset: u64,
    pub payload_len: u64,
    pub meta: Meta,
}

/// Find the payload trailer in `file`, if any.
pub fn find(file: &mut File) -> Result<Option<Trailer>, String> {
    let len = file.metadata().map_err(|e| e.to_string())?.len();
    let window = len.min(SEARCH_WINDOW);
    let start = len - window;
    let mut buf = vec![0u8; window as usize];
    file.seek(SeekFrom::Start(start))
        .and_then(|_| file.read_exact(&mut buf))
        .map_err(|e| e.to_string())?;
    parse(&buf, start)
}

/// Parse the trailer from the last bytes of a file (`buf` starts at file offset `start`).
///
/// Candidates are tried from the end; ones that do not validate are skipped
/// (the marker could appear by chance in the data). If a candidate is found
/// but none validates, the last validation error is reported.
fn parse(buf: &[u8], start: u64) -> Result<Option<Trailer>, String> {
    let magic = magic();
    let mut end = buf.len();
    let mut error = None;
    while let Some(pos) = buf[..end].windows(magic.len()).rposition(|w| w == magic) {
        end = pos + magic.len() - 1;
        if (pos as u64) + TRAILER_LEN > buf.len() as u64 {
            continue;
        }
        match parse_at(buf, start, pos) {
            Ok(trailer) => return Ok(Some(trailer)),
            Err(e) => error = Some(e),
        }
    }
    match error {
        Some(e) => Err(e),
        None => Ok(None),
    }
}

fn parse_at(buf: &[u8], start: u64, pos: usize) -> Result<Trailer, String> {
    let field = |i: usize| {
        let at = pos + 8 + 8 * i;
        u64::from_le_bytes(buf[at..at + 8].try_into().expect("8 bytes"))
    };
    let (payload_offset, payload_len, meta_len) = (field(0), field(1), field(2));
    let magic_offset = start + pos as u64;
    let bad = || "corrupt payload trailer".to_string();
    let meta_offset = magic_offset.checked_sub(meta_len).ok_or_else(bad)?;
    if payload_offset.checked_add(payload_len) != Some(meta_offset) || meta_offset < start {
        return Err(bad());
    }
    let meta: Meta = serde_json::from_slice(&buf[(meta_offset - start) as usize..pos])
        .map_err(|e| format!("corrupt payload metadata: {e}"))?;
    if meta.sha256.len() < 16 || !meta.sha256.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(bad());
    }
    Ok(Trailer {
        payload_offset,
        payload_len,
        meta,
    })
}

pub fn cache_root() -> PathBuf {
    if let Some(dir) = std::env::var_os("WHEELSHIP_CACHE_DIR").filter(|v| !v.is_empty()) {
        return PathBuf::from(dir).join("apps");
    }
    if cfg!(windows) {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home().join("AppData").join("Local"));
        return base.join("wheelship").join("cache").join("apps");
    }
    let base = std::env::var_os("XDG_CACHE_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home().join(".cache"));
    base.join("wheelship").join("apps")
}

fn home() -> PathBuf {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
}

fn sanitize(name: &str) -> String {
    let s: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "._-".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    if s.is_empty() || s.starts_with('.') {
        format!("app{s}")
    } else {
        s
    }
}

/// Make sure the payload of `exe` is extracted; returns the bundle root.
pub fn ensure_extracted(exe: &Path, file: File, trailer: &Trailer) -> Result<PathBuf, String> {
    let app_dir = cache_root().join(sanitize(&trailer.meta.name));
    let dest = app_dir.join(&trailer.meta.sha256[..16]);
    if !dest.join(crate::MANIFEST_NAME).is_file() {
        fs::create_dir_all(&app_dir)
            .map_err(|e| format!("cannot create {}: {e}", app_dir.display()))?;
        let tmp = app_dir.join(format!(
            ".tmp-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let result = extract(file, trailer, &tmp).map_err(|e| {
            format!(
                "cannot extract {} into {}: {e}",
                exe.display(),
                tmp.display()
            )
        });
        if let Err(e) = result {
            let _ = fs::remove_dir_all(&tmp);
            return Err(e);
        }
        if let Err(e) = fs::rename(&tmp, &dest) {
            let _ = fs::remove_dir_all(&tmp);
            // Another process won the race: its complete copy is just as good.
            if !dest.join(crate::MANIFEST_NAME).is_file() {
                return Err(format!("cannot move bundle into {}: {e}", dest.display()));
            }
        }
    }
    let _ = File::create(dest.join(MARKER)); // last-used timestamp for cleanup
    collect_garbage(&app_dir, &dest);
    Ok(dest)
}

/// A window `[offset, offset + len)` of a file, for reading the embedded zip.
struct Section {
    file: File,
    offset: u64,
    len: u64,
    pos: u64,
}

impl Read for Section {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let remaining = self.len.saturating_sub(self.pos);
        let n = (buf.len() as u64).min(remaining) as usize;
        if n == 0 {
            return Ok(0);
        }
        self.file.seek(SeekFrom::Start(self.offset + self.pos))?;
        let read = self.file.read(&mut buf[..n])?;
        self.pos += read as u64;
        Ok(read)
    }
}

impl Seek for Section {
    fn seek(&mut self, to: SeekFrom) -> io::Result<u64> {
        let new = match to {
            SeekFrom::Start(p) => Some(p),
            SeekFrom::End(d) => self.len.checked_add_signed(d),
            SeekFrom::Current(d) => self.pos.checked_add_signed(d),
        };
        match new {
            Some(p) => {
                self.pos = p;
                Ok(p)
            }
            None => Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "seek out of range",
            )),
        }
    }
}

fn extract(file: File, trailer: &Trailer, dest: &Path) -> Result<(), String> {
    let section = io::BufReader::with_capacity(
        1 << 16,
        Section {
            file,
            offset: trailer.payload_offset,
            len: trailer.payload_len,
            pos: 0,
        },
    );
    let mut archive = zip::ZipArchive::new(section).map_err(|e| e.to_string())?;
    fs::create_dir_all(dest).map_err(|e| e.to_string())?;
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| e.to_string())?;
        let rel = entry
            .enclosed_name()
            .ok_or_else(|| format!("unsafe path in payload: {:?}", entry.name()))?;
        let out = dest.join(&rel);
        if entry.is_dir() {
            fs::create_dir_all(&out).map_err(|e| e.to_string())?;
            continue;
        }
        if let Some(parent) = out.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mode = entry.unix_mode();
        if mode.is_some_and(|m| m & 0o170000 == 0o120000) {
            let mut target = String::new();
            entry
                .read_to_string(&mut target)
                .map_err(|e| e.to_string())?;
            if symlink_escapes(&rel, &target) {
                return Err(format!(
                    "unsafe symlink in payload: {} -> {target}",
                    rel.display()
                ));
            }
            make_symlink(&target, &out)?;
            continue;
        }
        let mut f = File::create(&out).map_err(|e| format!("{}: {e}", out.display()))?;
        io::copy(&mut entry, &mut f).map_err(|e| format!("{}: {e}", out.display()))?;
        #[cfg(unix)]
        if let Some(m) = mode {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&out, fs::Permissions::from_mode(m & 0o777))
                .map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Whether a symlink at `link` (relative to the bundle root) pointing to
/// `target` would resolve outside the bundle.
fn symlink_escapes(link: &Path, target: &str) -> bool {
    if target.starts_with('/') || target.starts_with('\\') || target.contains(':') {
        return true;
    }
    let mut depth = link.components().count() as i64 - 1; // directory holding the link
    for part in target.split('/') {
        match part {
            "" | "." => {}
            ".." => depth -= 1,
            _ => depth += 1,
        }
        if depth < 0 {
            return true;
        }
    }
    false
}

fn make_symlink(target: &str, link: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(target, link).map_err(|e| e.to_string())
    }
    #[cfg(not(unix))]
    {
        // Windows bundles are built without symlinks.
        Err(format!(
            "symlinks are not supported on this platform: {} -> {target}",
            link.display()
        ))
    }
}

/// Remove stale extractions: unfinished temporaries older than an hour, and
/// versions beyond the most recently used ones. Best effort: errors (e.g. a
/// version still running on Windows) are ignored.
fn collect_garbage(app_dir: &Path, current: &Path) {
    let Ok(entries) = fs::read_dir(app_dir) else {
        return;
    };
    let now = SystemTime::now();
    let mut versions: Vec<(SystemTime, PathBuf)> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if path == current || !path.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with(".tmp-") {
            let old = entry.metadata().and_then(|m| m.modified()).is_ok_and(|t| {
                now.duration_since(t).unwrap_or_default() > Duration::from_secs(3600)
            });
            if old {
                let _ = fs::remove_dir_all(&path);
            }
            continue;
        }
        let used = fs::metadata(path.join(MARKER))
            .and_then(|m| m.modified())
            .unwrap_or(SystemTime::UNIX_EPOCH);
        versions.push((used, path));
    }
    versions.sort_by_key(|v| std::cmp::Reverse(v.0));
    for (_, path) in versions.into_iter().skip(KEEP_OTHER_VERSIONS) {
        let _ = fs::remove_dir_all(path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn build(stub: &[u8], payload: &[u8], meta: &str, suffix: &[u8]) -> Vec<u8> {
        let mut v = stub.to_vec();
        v.extend_from_slice(payload);
        v.extend_from_slice(meta.as_bytes());
        v.extend_from_slice(&magic());
        v.extend_from_slice(&(stub.len() as u64).to_le_bytes());
        v.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        v.extend_from_slice(&(meta.len() as u64).to_le_bytes());
        v.extend_from_slice(suffix);
        v
    }

    const META: &str = r#"{"name":"demo","sha256":"0123456789abcdef0123"}"#;

    #[test]
    fn parses_trailer_even_with_trailing_signature() {
        for suffix in [&b""[..], &[0xAAu8; 5000][..]] {
            let file = build(b"STUB", b"ZIPDATA", META, suffix);
            let t = parse(&file, 0).unwrap().unwrap();
            assert_eq!(t.payload_offset, 4);
            assert_eq!(t.payload_len, 7);
            assert_eq!(t.meta.name, "demo");
        }
    }

    #[test]
    fn parses_from_a_window_of_the_file() {
        let file = build(&[0u8; 100], b"ZIPDATA", META, b"");
        let start = 90;
        let t = parse(&file[start..], start as u64).unwrap().unwrap();
        assert_eq!(t.payload_offset, 100);
    }

    #[test]
    fn marker_is_not_stored_verbatim() {
        assert_eq!(&magic(), b"WHSHPAY1");
        assert_ne!(&MAGIC_MASKED, b"WHSHPAY1");
    }

    #[test]
    fn skips_invalid_candidates_before_the_real_trailer() {
        // A stray marker inside the payload must not hide the real trailer.
        let mut payload = b"ZIP".to_vec();
        payload.extend_from_slice(&magic());
        payload.extend_from_slice(&[0u8; 40]);
        let file = build(b"STUB", &payload, META, b"");
        let t = parse(&file, 0).unwrap().unwrap();
        assert_eq!(t.payload_len, payload.len() as u64);
    }

    #[test]
    fn no_trailer_means_plain_launcher() {
        assert_eq!(parse(b"just a launcher binary", 0).unwrap(), None);
    }

    #[test]
    fn rejects_inconsistent_offsets() {
        let mut file = build(b"STUB", b"ZIPDATA", META, b"");
        let n = file.len();
        file[n - 24] ^= 1; // payload_offset
        assert!(parse(&file, 0).is_err());
    }

    #[test]
    fn symlinks_must_stay_inside() {
        let link = Path::new("python/bin/python3");
        assert!(!symlink_escapes(link, "python3.12"));
        assert!(!symlink_escapes(link, "../lib/libpython3.so"));
        assert!(!symlink_escapes(Path::new("a/b/c/l"), "../../x"));
        assert!(symlink_escapes(link, "../../../etc/passwd"));
        assert!(symlink_escapes(link, "/etc/passwd"));
        assert!(symlink_escapes(Path::new("top"), "../x"));
    }

    #[test]
    fn sanitizes_names() {
        assert_eq!(sanitize("cli-app"), "cli-app");
        assert_eq!(sanitize("../evil"), "app.._evil");
        assert_eq!(sanitize("a b/c"), "a_b_c");
    }
}
