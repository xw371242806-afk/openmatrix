import subprocess, sys, zipfile
from pathlib import Path

def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def git_commit(root, message):
    """Commit with an explicit identity so CI runners without global user.* still work."""
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", message],
        cwd=root,
        check=True,
        capture_output=True,
    )

def make_fixture(tmp_path):
    root = tmp_path / 'repo'; write(root / 'main.py', 'print(1)'); write(root / 'requirements.txt', 'fastapi')
    write(root / 'ui/dist/index.html', '<!doctype html>'); write(root / 'AGENTS.md', 'internal')
    write(root / 'tests/test_internal.py', 'internal'); write(root / 'scripts/dev_probe.py', 'internal')
    write(root / '.openmatrix/old.sqlite3', 'private'); write(root / 'scripts/release_exclude.txt', '.openmatrix/\ntests/\nscripts/\nAGENTS.md\n')
    source = Path('scripts/make_release.py').read_text(encoding='utf-8'); write(root / 'scripts/make_release.py', source)
    subprocess.run(['git', 'init'], cwd=root, check=True, capture_output=True); subprocess.run(['git', 'add', '.'], cwd=root, check=True)
    git_commit(root, 'fixture'); return root

def test_release_builds_clean_archive(tmp_path):
    root = make_fixture(tmp_path); out = tmp_path / 'out'
    subprocess.run([sys.executable, str(root / 'scripts/make_release.py'), '--out', str(out), '--version', 'test'], cwd=root, check=True)
    with zipfile.ZipFile(out / 'openmatrix-test.zip') as zf: names = set(zf.namelist())
    assert {'main.py', 'requirements.txt', 'ui/dist/index.html', 'scripts/make_release.py', 'scripts/release_exclude.txt'} <= names; assert not any(name.startswith(('AGENTS.md', 'tests/', '.openmatrix/', 'scripts/dev_probe.py')) for name in names)

def test_release_scan_rejects_secret(tmp_path):
    root = make_fixture(tmp_path / 'secret'); write(root / 'bad.txt', 'sk-SECRET12345678')
    subprocess.run(['git', 'add', 'bad.txt'], cwd=root, check=True); git_commit(root, 'bad-secret')
    result = subprocess.run([sys.executable, str(root / 'scripts/make_release.py'), '--out', str(tmp_path / 'out-secret'), '--version', 'bad'], cwd=root, text=True, capture_output=True)
    assert result.returncode != 0; assert 'possible API key' in (result.stdout + result.stderr)

def test_release_scan_rejects_absolute_path(tmp_path):
    root = make_fixture(tmp_path / 'path'); write(root / 'bad_path.txt', r'C:\Users\Demo\secret')
    subprocess.run(['git', 'add', 'bad_path.txt'], cwd=root, check=True); git_commit(root, 'bad-path')
    result = subprocess.run([sys.executable, str(root / 'scripts/make_release.py'), '--out', str(tmp_path / 'out-path'), '--version', 'bad'], cwd=root, text=True, capture_output=True)
    assert result.returncode != 0; assert 'absolute Windows path' in (result.stdout + result.stderr)


def test_release_scan_ignores_task_identifier_and_binary_image(tmp_path):
    import importlib.util
    spec=importlib.util.spec_from_file_location("release_scan",Path("scripts/make_release.py"))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    (tmp_path / "tasks.py").write_text("task-background-worker",encoding="utf-8")
    (tmp_path / "trace.ts").write_text(r"frame.match(/^data:\s*(.+)$/m)",encoding="utf-8")
    (tmp_path / "design.png").write_bytes(bytes((137,80,78,71))+b"D:"+bytes((92,))+b"image-binary")
    module.scan(tmp_path)
