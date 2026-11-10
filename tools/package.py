#!/usr/bin/env python3
"""Build the Splunk app package: releases/dmx_inventory-<version>.spl

A .spl is a gzipped tar of the app folder; Splunk Cloud (UI upload or ACS)
accepts it as-is. The build is deterministic (sorted entries, fixed
timestamps, owner and permissions), so the same source always gives the same
bytes and CI can check that the committed .spl matches the source.

    python3 tools/package.py             build releases/dmx_inventory-<version>.spl
    python3 tools/package.py --check     fail if the committed .spl is out of date
    python3 tools/package.py --out-dir dist
"""

import argparse
import configparser
import gzip
import io
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = "dmx_inventory"
APP_DIR = os.path.join(ROOT, APP)
MTIME = 1735689600  # 2025-01-01T00:00:00Z, fixed for reproducible builds

# Splunk Cloud vetting rejects local/ content, bytecode and hidden files.
EXCLUDE_DIRS = {"__pycache__", "local"}
EXCLUDE_FILES = {"local.meta"}


def app_version():
    conf = configparser.ConfigParser(interpolation=None, strict=False)
    conf.read(os.path.join(APP_DIR, "default", "app.conf"))
    return conf["id"]["version"]


def iter_paths():
    for dirpath, dirnames, filenames in os.walk(APP_DIR):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        if not dirnames and not filenames:
            continue  # git doesn't keep empty folders, so neither does the package
        yield rel_dir, None
        for name in sorted(filenames):
            if name.startswith(".") or name.endswith(".pyc") or name in EXCLUDE_FILES:
                continue
            yield rel_dir + "/" + name, os.path.join(dirpath, name)


def build_bytes():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for arcname, path in iter_paths():
            info = tarfile.TarInfo(arcname)
            info.mtime, info.uid, info.gid, info.uname, info.gname = MTIME, 0, 0, "", ""
            if path is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
                continue
            with open(path, "rb") as f:
                data = f.read()
            # Executable bit for scripts in bin/, regardless of the OS it was built on.
            info.mode = 0o755 if arcname.startswith(APP + "/bin/") and arcname.endswith(".py") else 0o644
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _contents(spl_bytes):
    """Names, types, modes and file bytes: what matters, independent of zlib version."""
    entries = []
    with tarfile.open(fileobj=io.BytesIO(spl_bytes), mode="r:gz") as tar:
        for m in tar.getmembers():
            data = tar.extractfile(m).read() if m.isfile() else None
            entries.append((m.name, m.type, m.mode, data))
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=os.path.join(ROOT, "releases"))
    parser.add_argument("--check", action="store_true", help="verify the existing package is up to date")
    args = parser.parse_args()

    path = os.path.join(args.out_dir, "%s-%s.spl" % (APP, app_version()))
    data = build_bytes()
    if args.check:
        try:
            with open(path, "rb") as f:
                current = f.read()
        except FileNotFoundError:
            sys.exit("missing %s: run python3 tools/package.py" % os.path.relpath(path, ROOT))
        if current != data and _contents(current) != _contents(data):
            sys.exit("%s is out of date: run python3 tools/package.py and commit it"
                     % os.path.relpath(path, ROOT))
        print("up to date:", os.path.relpath(path, ROOT))
        return
    os.makedirs(args.out_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    print(path)


if __name__ == "__main__":
    main()
