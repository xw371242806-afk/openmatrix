import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

import main
import library
import apikeys
import audit

_SQLITE_SUFFIXES = (".sqlite3", ".db", ".sqlite")


def collect_data_paths() -> list[Path]:
    """Absolute paths of all persisted data locations (existing only)."""
    paths = [
        main.matrix_db_path(),
        library.library_root(),
        apikeys.keys_db_path(),
        audit.audit_db_path(),
        os.environ.get("OPENMATRIX_EXPORT_ROOT", str(Path.cwd() / "exports")),
    ]
    return [Path(p).resolve() for p in paths if Path(p).exists()]


def _data_files() -> list[Path]:
    """All files under the data paths (existing, non-symlink only).

    Symlinks are skipped: rglob("*") would otherwise follow a link that points
    outside the data roots and include arbitrary external files.
    """
    files = []
    for root in collect_data_paths():
        if root.is_file():
            if not root.is_symlink():
                files.append(root)
        else:
            files.extend(f for f in root.rglob("*") if f.is_file() and not f.is_symlink())
    return files


def _is_sqlite(path: Path) -> bool:
    """True if the file is a real SQLite database (magic header).

    Checks the content header directly rather than relying on filename suffixes,
    so configurable/extensionless DB paths are handled safely via SQLite's
    online backup API rather than missing their WAL sidecars.
    """
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _safe_copy(src: Path, dst: Path) -> None:
    """Copy a file; for real SQLite DBs use the online backup API so a consistent
    snapshot is captured even if the DB is in WAL mode or mid-transaction.
    Non-SQLite files (even with a .db suffix) are copied byte-for-byte."""
    if _is_sqlite(src):
        conn_src = sqlite3.connect(str(src))
        conn_dst = sqlite3.connect(str(dst))
        try:
            conn_src.backup(conn_dst)
        finally:
            conn_dst.close()
            conn_src.close()
    else:
        dst.write_bytes(src.read_bytes())


def create_backup(target_dir: str | None = None) -> str:
    """Write a timestamped zip (with a manifest) of all data; return its path."""
    target = Path(target_dir or Path.cwd() / "backups")
    target.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = target / f"openmatrix-backup-{ts}.zip"
    manifest = {}
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                for i, f in enumerate(_data_files()):
                    if str(f.resolve()) == str(zip_path.resolve()):
                        continue
                    arcname = f"data/{i:06d}"
                    staged = tmp / f"{i:06d}"
                    _safe_copy(f, staged)
                    zf.write(staged, arcname=arcname)
                    manifest[arcname] = str(f.resolve())
            zf.writestr("manifest.json", json.dumps(manifest))
        return str(zip_path)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Backup failed: {e}") from e


def _allowed_restore_targets() -> list[tuple[Path, bool]]:
    """Resolve the (absolute path, is_directory) pairs restore may write.

    Types are inferred from configuration (DB paths are files; library/export
    roots are directories), not from the current filesystem state, so restore
    is correct even when a target does not exist yet or was removed.
    """
    db_file_paths = [
        main.matrix_db_path(),
        apikeys.keys_db_path(),
        audit.audit_db_path(),
    ]
    dir_paths = [
        library.library_root(),
        os.environ.get("OPENMATRIX_EXPORT_ROOT", str(Path.cwd() / "exports")),
    ]
    allowed = [(Path(p).resolve(), False) for p in db_file_paths]
    allowed += [(Path(p).resolve(), True) for p in dir_paths]
    return allowed


def restore_backup(zip_path: str, dry_run: bool = True) -> list[str]:
    """List (or perform) restore of a backup; entries validated against data paths.

    Reads the embedded manifest to map each archive entry back to its original
    absolute location, then refuses any target outside the known data paths
    (path-traversal safe even against a tampered archive).
    """
    zp = Path(zip_path)
    if not zp.is_file():
        raise ValueError(f"Zip file not found: {zp}")
    allowed = _allowed_restore_targets()
    try:
        with zipfile.ZipFile(zp, "r") as zf:
            if "manifest.json" not in zf.namelist():
                raise ValueError("not an OpenMatrix backup (missing manifest.json)")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            targets = []
            for arcname, absolute in manifest.items():
                dest = Path(absolute).resolve()
                # A file root must match exactly; a directory root allows children.
                ok = False
                for a, is_dir in allowed:
                    if is_dir:
                        if dest == a or dest.is_relative_to(a):
                            ok = True
                            break
                    else:  # file root: exact match only
                        if dest == a:
                            ok = True
                            break
                if not ok:
                    raise ValueError(f"refusing to restore outside data paths: {arcname} -> {absolute}")
                targets.append((arcname, dest))
            if not dry_run:
                for arcname, dest in targets:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    # Use the online backup API for SQLite files (by header inspection)
                    # or for known DB targets (so an initial restore into a new path
                    # also uses sqlite3 when the source archive entry is a DB).
                    is_db = False
                    try:
                        with zf.open(arcname) as probe_src:
                            is_db = probe_src.read(16) == b"SQLite format 3\x00"
                    except Exception:
                        is_db = False

                    if is_db or _is_sqlite(dest):
                        # Restore a live SQLite DB via the online backup API so an
                        # open/WAL database is replaced safely (not a raw file clobber).
                        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
                            tmp_restore = Path(tf.name)
                        try:
                            with zf.open(arcname) as src, open(tmp_restore, "wb") as out:
                                out.write(src.read())
                            conn_src = sqlite3.connect(str(tmp_restore))
                            conn_dst = sqlite3.connect(str(dest))
                            try:
                                conn_src.backup(conn_dst)
                            finally:
                                conn_dst.close()
                                conn_src.close()
                        finally:
                            tmp_restore.unlink(missing_ok=True)
                    else:
                        with zf.open(arcname) as src, open(dest, "wb") as out:
                            out.write(src.read())
            return [str(d) for _, d in targets]
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Restore failed: {e}") from e
