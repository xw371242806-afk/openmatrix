# Contributing to OpenMatrix

Thank you for contributing to **OpenMatrix**! Please read [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) and the engineering standards below before opening a Pull Request.

## Development Workflow

1. Check existing [Issues](https://github.com/xw371242806-afk/openmatrix/issues) or open a new Issue describing the bug or enhancement.
2. Create an isolated branch from `main` (e.g., `feat/issue-42-cache` or `fix/sec-edge-case`).
3. Write focused tests alongside your implementation; keep changes small, well-documented, and reversible.
4. Run the full verification suite locally before submitting:
   ```bash
   python -m pytest -q
   cd ui && npm test && npm run lint && npm run build
   git diff --check
   ```
5. Open a Pull Request referencing the related Issue.

## Engineering Rules

- **Secrets & privacy:** Never commit API keys, `.env` files, local databases (`*.db` / `*.sqlite3`), PDFs, or machine-specific local paths.
- **Dependencies & licenses:** Avoid introducing AGPL, non-commercial (`CC-BY-NC`), or proprietary dependencies. Update [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) when adding any new permissive dependency.
- **Compliance guardrails:** Follow the architectural guardrails in [PATENT_COMPLIANCE.md](PATENT_COMPLIANCE.md) (no attention-score highlighting, keep `analyze_documents` grid-free, no chat-over-grid-cell subqueries).

## Developer Certificate of Origin (DCO)

Every commit must carry a `Signed-off-by` trailer, e.g.:

```text
Signed-off-by: Jane Doe <jane@example.com>
```

Use `git commit -s` to append it. By adding the `Signed-off-by` line you certify the Developer's Certificate of Origin, version 1.1 (<https://developercertificate.org/>).
