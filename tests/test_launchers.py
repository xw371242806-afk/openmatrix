from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]

def test_launchers_use_ui_and_portable_paths():
    for path in ROOT.glob('*.bat'):
        text = path.read_text(encoding='utf-8', errors='replace')
        assert '8787' not in text
        assert '/static/index.html' not in text
        assert not re.search(r'(?i)D:[\\/]', text)
    start = (ROOT / 'start.bat').read_text(encoding='utf-8')
    assert 'OPENMATRIX_PORT=8000' in start or 'setup_windows.ps1' in start
    assert '/ui/' in start or 'setup_windows.ps1' in start

def test_main_startup_message_points_to_react_ui():
    text = (ROOT / 'main.py').read_text(encoding='utf-8')
    assert '/ui/' in text
    assert '/static/index.html' not in text

def test_readme_uses_the_same_default_entrypoint():
    text = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert '8000' in text and '/ui/' in text
