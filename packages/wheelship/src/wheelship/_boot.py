"""wheelship bootstrap for bundles that run on a system interpreter.

Copied verbatim into bundles: as ``_wheelship_boot.py`` next to the launcher
(``dir``/``archive``/``exe``), or as ``__main__.py`` of a zipapp (``pyz``).
Standard library only, and kept compatible with old Python versions so that
it can report a version mismatch instead of failing with a syntax error.

* checks that the interpreter matches the ABI the bundle was built for;
* keeps the interpreter's own site-packages out of ``sys.path``;
* zipapp only: extracts ``lib/`` once into the cache (native extensions
  cannot be imported from a zip), like a single-file executable does;
* adds ``lib/`` with :func:`site.addsitedir` (``.pth`` files are honored);
* calls the selected entry point with ``sys.argv[0]`` set to the program path.

Directory mode is invoked by the launcher as
``python -I _wheelship_boot.py <entry point> <launcher path> <args...>``.
"""

# mypy: ignore-errors
# (deliberately untyped and old-style: it must run on any target Python)

import json
import os
import sys

MANIFEST = "wheelship.json"
MARKER = ".wheelship-last-used"


def _fail(message):
    sys.stderr.write("wheelship: %s\n" % message)
    sys.exit(1)


def _check_interpreter(manifest):
    import sysconfig

    want = manifest["interpreter"]
    have = "%d.%d" % sys.version_info[:2]
    ext_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if (
        sys.implementation.name != "cpython"
        or have != want["python"]
        or ext_suffix != want["ext_suffix"]
    ):
        _fail(
            "%s needs CPython %s (extension suffix %s), but %s is %s %s (%s); "
            "set WHEELSHIP_PYTHON to a matching interpreter"
            % (
                manifest["app"]["name"],
                want["python"],
                want["ext_suffix"],
                sys.executable,
                sys.implementation.name,
                have,
                ext_suffix,
            )
        )


def _cache_root():
    env = os.environ.get("WHEELSHIP_CACHE_DIR")
    if env:
        return os.path.join(env, "apps")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
        return os.path.join(base, "wheelship", "cache", "apps")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "wheelship", "apps")


def _extract(archive, manifest):
    """Extract ``lib/`` of a zipapp atomically into the cache; returns the root."""
    import shutil
    import time
    import zipfile

    name = manifest["app"]["name"]
    app_dir = os.path.join(_cache_root(), name)
    dest = os.path.join(app_dir, manifest["payload"]["sha256"][:16])
    if not os.path.isfile(os.path.join(dest, MANIFEST)):
        if not os.path.isdir(app_dir):
            os.makedirs(app_dir, exist_ok=True)
        tmp = os.path.join(app_dir, ".tmp-%d-%d" % (os.getpid(), time.time_ns()))
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [n for n in zf.namelist() if n.startswith("lib/")]
                zf.extractall(tmp, members + [MANIFEST])
            os.rename(tmp, dest)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.isfile(os.path.join(dest, MANIFEST)):
                raise  # not a lost race with a concurrent first run
    try:
        open(os.path.join(dest, MARKER), "w").close()
    except OSError:
        pass
    return dest


def _drop_interpreter_site():
    """Remove the interpreter's site-packages from sys.path.

    The launcher passes -S for that; a zipapp cannot, so undo what site did.
    """
    if sys.flags.no_site:
        return
    import site

    dirs = list(getattr(site, "getsitepackages", lambda: [])())
    if getattr(site, "ENABLE_USER_SITE", False):
        dirs.append(site.getusersitepackages())
    drop = set(os.path.normcase(os.path.abspath(d)) for d in dirs)
    sys.path[:] = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) not in drop]


def _select(manifest, program):
    stem = os.path.basename(program)
    for ext in (".exe", ".pyz", ".pyzw"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
    entry_points = manifest["entry_points"]
    if stem in entry_points:
        return stem
    default = manifest.get("default_entry_point")
    if default in entry_points:
        return default
    _fail("no entry point named %r (available: %s)" % (stem, ", ".join(sorted(entry_points))))


def _call(value):
    import importlib

    module, _, attr = value.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(here):  # running from a zipapp: ``here`` is the archive
        import zipfile

        with zipfile.ZipFile(here) as zf:
            manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        _check_interpreter(manifest)
        root = _extract(here, manifest)
        name = _select(manifest, here)
        argv = [here] + sys.argv[1:]
    else:
        with open(os.path.join(here, MANIFEST), encoding="utf-8") as f:
            manifest = json.load(f)
        _check_interpreter(manifest)
        root = here
        if len(sys.argv) < 3:
            _fail("start this application through its launcher")
        name = sys.argv[1]
        argv = sys.argv[2:]

    _drop_interpreter_site()
    import site

    site.addsitedir(os.path.join(root, "lib"))
    sys.argv = argv
    sys.exit(_call(manifest["entry_points"][name]["value"]))


if __name__ == "__main__":
    main()
