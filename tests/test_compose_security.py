from pathlib import Path
import re

def test_compose_binds_localhost():
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        text=Path(name).read_text(encoding="utf-8")
        blocks=re.findall(r"(?m)^[ ]+ports:[ ]*\n((?:[ ]+- [^\n]+\n)+)",text)
        for block in blocks:
            for line in block.splitlines():
                value=line.strip().removeprefix("- ").strip('\"')
                assert value.startswith("127.0.0.1:"), value
    text=Path("docker-compose.yml").read_text(encoding="utf-8")+Path("docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "openmatrix-secret" not in text
    assert "POSTGRES_PASSWORD: openmatrix" not in text
    assert "${POSTGRES_PASSWORD:?set in .env}" in text
    assert "${MINIO_ROOT_PASSWORD:?set in .env}" in text
