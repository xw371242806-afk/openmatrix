# Security Policy

## Reporting a Vulnerability

OpenMatrix is a public open-source project (`v0.1.0`). Please **do not** disclose security vulnerabilities in public GitHub Issues, Discussions, or Pull Requests.

Instead, report vulnerabilities privately through **GitHub Private Vulnerability Reporting**:

- Open a private security advisory at:
  `https://github.com/xw371242806-afk/openmatrix/security/advisories/new`

Please include:
- A clear description of the issue and its potential impact.
- Minimal reproduction steps or proof of concept.
- Affected files, endpoints, MCP tools, or deployment configuration.

We aim to acknowledge reports within 72 hours and coordinate a fix before public disclosure.

## Built-in Security Hardening (`v0.1.0`)

- **Network-aware authentication (`OPENMATRIX_ENABLE_AUTH=auto`)**: Binding to non-loopback addresses (such as `0.0.0.0` in Docker) automatically enables Bearer authentication on API endpoints and generates a random master key at `${OPENMATRIX_DATA_DIR}/master_key.txt` when `OPENMATRIX_MASTER_KEY` is unset (never printed to stdout).
- **Strict CORS & origin checks**: Wildcard `allow_origins=["*"]` with credentials is disallowed; foreign-origin state-changing requests are rejected when not explicitly allowed.
- **Loopback Compose defaults**: `docker-compose.yml` and `docker-compose.prod.yml` bind published ports to `127.0.0.1` and require `.env` credentials without committed fallback passwords.
- **Credential handling & redaction**: Provider API keys (BYOK) are never persisted in browser `localStorage` or database tables, model discovery uses JSON `POST` bodies instead of URL query strings, and log/error filters redact key patterns.
- **SSRF hardening (`fetch_context`)**: Validates resolved IPs against loopback/private/link-local/metadata/reserved ranges, pins DNS-resolved sockets to prevent DNS-rebinding TOCTOU, re-validates every redirect (up to 5 hops), and enforces a 5 MB response limit, with optional explicit proxy allowlists (`OPENMATRIX_ALLOWED_PROXY_HOSTS`).
- **Export path confinement (`export_report` / `export_slides`)**: Confines generated artifacts strictly under `OPENMATRIX_EXPORT_ROOT`, rejecting absolute paths and traversal (`..`).
- **Streaming upload limits**: Uploads stream directly with enforced byte caps instead of buffering unbounded files into memory.

## Scope

This policy covers the OpenMatrix backend (`main.py`), agent layer (`agent/`), MCP server (`mcp_server.py`), React Research Desk (`ui/`), and official container/launcher scripts.
