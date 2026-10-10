import argparse
import importlib.metadata
import json
import multiprocessing
import os
import sys
from pathlib import Path


def _square(x):
    return x * x


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exit", type=int, default=0)
    parser.add_argument("--mp", action="store_true", help="exercise multiprocessing (spawn)")
    parser.add_argument("rest", nargs="*")
    args = parser.parse_args()

    from markupsafe import _speedups

    info = {
        "prog": parser.prog,
        "argv0": sys.argv[0],
        "rest": args.rest,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "isolated": sys.flags.isolated,
        "cwd_in_path": os.getcwd() in sys.path or "" in sys.path,
        "native_ext": Path(_speedups.__file__).name,
        "pygments": importlib.metadata.version("pygments"),
        "scripts": sorted(ep.name for ep in importlib.metadata.entry_points(group="console_scripts")),
        "version": "%d.%d.%d" % sys.version_info[:3],
    }
    if args.mp:
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(2) as pool:
            info["mp"] = pool.map(_square, [1, 2, 3])
    print(json.dumps(info))
    return args.exit


def gui():
    return main()
