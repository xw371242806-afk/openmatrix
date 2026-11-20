"""Network-aware access authentication and local master-key persistence."""
import os
import secrets
from pathlib import Path

def auth_enabled():
    mode=os.environ.get("OPENMATRIX_ENABLE_AUTH","auto").strip().lower()
    if mode in {"off","false","0","no"}: return False
    if mode in {"on","true","1","yes"}: return True
    if mode not in {"auto",""}: raise ValueError("OPENMATRIX_ENABLE_AUTH must be on, off or auto")
    host=os.environ.get("OPENMATRIX_HOST","127.0.0.1").strip().lower().strip("[]")
    return host not in {"127.0.0.1","localhost","::1"}

def master_key_path():
    return Path(os.environ.get("OPENMATRIX_DATA_DIR",str(Path(__file__).resolve().parents[1]/".openmatrix"))) / "master_key.txt"

def get_master_key(create=False):
    configured=os.environ.get("OPENMATRIX_MASTER_KEY","").strip()
    if configured: return configured
    path=master_key_path()
    if path.is_file():
        value=path.read_text(encoding="utf-8").strip()
        if not value: raise ValueError(f"Master-key file is empty: {path}")
        return value
    if not create: return None
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        return get_master_key()
    key=secrets.token_urlsafe(32)
    with os.fdopen(fd,"w",encoding="utf-8") as handle: handle.write(key+"\n")
    print(f"Access master key saved to: {path}")
    return key
