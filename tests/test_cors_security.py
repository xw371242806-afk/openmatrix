from fastapi.testclient import TestClient
import main

def test_cors_rejects_foreign_origin(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_ENABLE_AUTH","off")
    client=TestClient(main.app)
    foreign={"Origin":"https://evil.example"}
    response=client.get("/api/v1/health",headers=foreign)
    assert "access-control-allow-origin" not in response.headers
    response=client.get("/api/v1/health",headers={"Origin":"http://localhost:8000"})
    assert response.headers["access-control-allow-origin"]=="http://localhost:8000"
    assert "access-control-allow-credentials" not in response.headers
    assert client.delete("/api/v1/projects/x",headers=foreign).status_code==403
    assert client.post("/api/analyze-batch",headers=foreign,files={"files":("a.pdf",b"PDF")}).status_code==403

def test_explicit_origin_configuration(monkeypatch):
    monkeypatch.setenv("OPENMATRIX_CORS_ORIGINS","https://research.example, https://desk.example")
    assert main._cors_origins()==["https://research.example","https://desk.example"]

def test_authorized_origin_preflight_without_bearer(monkeypatch,tmp_path):
    monkeypatch.setenv("OPENMATRIX_ENABLE_AUTH","on")
    monkeypatch.setenv("OPENMATRIX_DATA_DIR",str(tmp_path))
    response=TestClient(main.app).options("/api/v1/projects",headers={
        "Origin":"http://localhost:5173",
        "Access-Control-Request-Method":"POST",
        "Access-Control-Request-Headers":"authorization,content-type",
    })
    assert response.status_code==200
    assert response.headers["access-control-allow-origin"]=="http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers
