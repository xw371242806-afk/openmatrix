from fastapi.testclient import TestClient
import main
from core.access_auth import get_master_key

def test_public_bind_generates_key_and_protects_legacy(monkeypatch,tmp_path,capsys):
    monkeypatch.setenv("OPENMATRIX_HOST","0.0.0.0")
    monkeypatch.delenv("OPENMATRIX_ENABLE_AUTH",raising=False)
    monkeypatch.delenv("OPENMATRIX_MASTER_KEY",raising=False)
    monkeypatch.setenv("OPENMATRIX_DATA_DIR",str(tmp_path))
    client=TestClient(main.app)
    assert client.delete("/api/v1/projects/x").status_code==401
    assert client.post("/api/analyze").status_code==401
    assert client.get("/api/models").status_code==401
    assert client.get("/api/v1/health").status_code==200
    key=(tmp_path/"master_key.txt").read_text().strip()
    assert len(key)>=40 and key not in capsys.readouterr().out
    assert client.get("/api/v1/health/detailed",headers={"Authorization":"Bearer "+key}).status_code==200

def test_loopback_auto_keeps_local_access(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_HOST","127.0.0.1")
    monkeypatch.delenv("OPENMATRIX_ENABLE_AUTH",raising=False)
    assert not main._auth_enabled()
    assert TestClient(main.app).get("/api/v1/health/detailed").status_code==200
