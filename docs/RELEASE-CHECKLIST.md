# Windows release checklist

This checklist records a manual acceptance run; it is not a claim that the run has already happened.

1. In a fresh Windows Sandbox or user profile with Python 3.12, extract the ZIP from `scripts/make_release.py` into a new directory. Do not copy an existing `venv`, `.openmatrix`, `data`, database, log, API key, or source PDF.
2. Double-click `start.bat`; confirm it creates `data`, installs dependencies only when the requirements hash changes, waits for `/api/v1/health` HTTP 200, and opens `/ui/` within five minutes.
3. Confirm the UI displays `Backend live` and an empty first-run state.
4. Start again and record elapsed time; the second start should skip dependency installation and become available within 15 seconds on the target machine.
5. If `ui/dist/index.html` is absent, verify Node.js 24 or newer is detected and `npm ci` followed by `npm run build` succeeds.
6. Record OS, Python, Node, elapsed times, and warnings in the release evidence.
