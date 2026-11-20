#!/usr/bin/env python3
from __future__ import annotations
import argparse, fnmatch, re, shutil, subprocess, tempfile, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
EXCLUDES = ROOT / "scripts" / "release_exclude.txt"
SECRET_RE = re.compile(rb"(?<![A-Za-z0-9_])" + b"".join((b"s", b"k", b"-", rb"[A-Za-z0-9_-]{8,}")))
DRIVE_RE = re.compile(rb"(?<![A-Za-z0-9_])[A-Za-z]:" + re.escape(bytes((92,))) + rb"[^\x00\r\n\"']{3,}")
def patterns():
    return [s.strip().replace("\\", "/") for s in EXCLUDES.read_text(encoding="utf-8").splitlines() if s.strip() and not s.startswith("#")]
def excluded(rel, pats):
    rel = rel.replace("\\", "/")
    if rel.startswith("./"): rel = rel[2:]
    if rel in {"scripts/make_release.py", "scripts/wait_for_backends.py", "scripts/release_exclude.txt"}: return False
    return any(fnmatch.fnmatch(rel, p) or rel.startswith(p.rstrip("/") + "/") for p in pats)
def scan(root):
    bad = []
    for path in root.rglob("*"):
        if not path.is_file(): continue
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, p) for p in ("*.db", "*.sqlite3", "*.env")): bad.append("forbidden file: " + rel)
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"}:
            continue
        data = path.read_bytes()
        if SECRET_RE.search(data): bad.append("possible API key: " + rel)
        if DRIVE_RE.search(data): bad.append("absolute Windows path: " + rel)
    if bad: raise SystemExit("Release scan failed:\n" + "\n".join(bad))
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", type=Path, required=True); ap.add_argument("--version"); args = ap.parse_args()
    ui_dist = ROOT / "ui" / "dist"
    if not (ui_dist / "index.html").is_file(): raise SystemExit("ui/dist/index.html is required; build the UI before releasing")
    version = args.version or subprocess.check_output(["git", "describe", "--tags", "--always"], cwd=ROOT, text=True).strip()
    version = "".join(c if c.isalnum() or c in ".-_" else "-" for c in version); args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openmatrix-release-") as td:
        stage = Path(td) / "openmatrix"; stage.mkdir(); archive = Path(td) / "source.tar"
        with archive.open("wb") as fp: subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, stdout=fp, check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(stage)], check=True)
        pats = patterns()
        for path in stage.rglob("*"):
            if not path.resolve().is_relative_to(stage.resolve()):
                raise SystemExit("Archive contains a path outside the staging directory")
            if path.is_file() and excluded(path.relative_to(stage).as_posix(), pats):
                path.unlink()
        for path in sorted(stage.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        target = stage / "ui" / "dist"; target.mkdir(parents=True, exist_ok=True); shutil.copytree(ui_dist, target, dirs_exist_ok=True); scan(stage)
        output = args.out / f"openmatrix-{version}.zip"
        if output.exists(): output.unlink()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in stage.rglob("*"):
                if path.is_file(): zf.write(path, path.relative_to(stage).as_posix())
    print(output)
if __name__ == "__main__": main()
