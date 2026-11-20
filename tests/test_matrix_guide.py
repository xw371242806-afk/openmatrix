from fastapi.testclient import TestClient
import main
def test_matrix_setup_guide_is_available():
    r=TestClient(main.app).get('/docs/MATRIX-DEEP-DIVE-SETUP.md')
    assert r.status_code == 200
    assert 'OPENMATRIX_XLAT_PATH' in r.text
    assert 'license' in r.text
