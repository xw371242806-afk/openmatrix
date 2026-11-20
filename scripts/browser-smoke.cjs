#!/usr/bin/env node
/**
 * Browser JavaScript smoke coverage for static/index.html
 *
 * Starts the FastAPI app in a subprocess with a throwaway SQLite DB, loads the
 * page in headless Chromium, and asserts:
 *   1. clean console / no page errors on load (inline script failures only;
 *      external CDN noise is reported but not fatal),
 *   2. the cross-run comparison key builder works (guards the historical
 *      `||`/`??` SyntaxError at line 394),
 *   3. the comparison render path works end-to-end and marks divergent rows.
 *
 * Requires `playwright-core` (point NODE_PATH at a dir that has it) and the
 * Playwright chromium build. Run from the repository root:
 *
 *   NODE_PATH=<dir-with-playwright-core> node scripts/browser-smoke.cjs
 *
 * Optional env overrides:
 *   OPENMATRIX_PYTHON  python interpreter used to launch the app
 *   OPENMATRIX_PORT    port for the temporary server (default 8137)
 *   OPENMATRIX_RUN_DB  throwaway DB path (default tempfile)
 */
const { chromium } = require('playwright-core');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = path.resolve(__dirname, '..');
const PORT = process.env.OPENMATRIX_PORT || 8137;
const PY = process.env.OPENMATRIX_PYTHON || 'python';
const RUN_DB = process.env.OPENMATRIX_RUN_DB || path.join(os.tmpdir(), `openmatrix_smoke_${process.pid}.sqlite3`);

const EXECUTABLE_CANDIDATES = [
  path.join(process.env.LOCALAPPDATA || '', 'ms-playwright', 'chromium_headless_shell-1234', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe'),
  path.join(process.env.LOCALAPPDATA || '', 'ms-playwright', 'chromium-1234', 'chrome-win64', 'chrome.exe'),
];

function findExecutable() {
  for (const candidate of EXECUTABLE_CANDIDATES) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return undefined; // let playwright-core resolve its own build
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

async function waitForServer(url, timeoutMs = 60000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url);
      if (res.ok) return true;
    } catch (_) { /* not up yet */ }
    await delay(300);
  }
  throw new Error('server did not become reachable');
}

async function runSmoke() {
  const env = { ...process.env, OPENMATRIX_RUN_DB: RUN_DB };
  const server = spawn(PY, ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(PORT)], {
    cwd: BASE, env, stdio: ['ignore', 'ignore', 'pipe'],
  });
  server.stderr.on('data', () => { /* keep pipe drained */ });

  const browser = await chromium.launch({ executablePath: findExecutable(), headless: true });

  try {
    await waitForServer(`http://127.0.0.1:${PORT}/static/index.html`);

    const context = await browser.newContext();
    const page = await context.newPage();

    const consoleErrors = [];
    const pageErrors = [];
    page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    page.on('pageerror', err => pageErrors.push(String(err)));

    await page.goto(`http://127.0.0.1:${PORT}/static/index.html`, { waitUntil: 'load', timeout: 30000 });
    await delay(500);

    // Inline (ours) vs external (CDN/pdf.js) console noise.
    const foreignPattern = /net::|Failed to load|unpkg\.com|cdnjs|jsdelivr|Failed to fetch|503|404/;
    const ownConsoleErrors = consoleErrors.filter(t => !foreignPattern.test(t));
    const foreign = consoleErrors.filter(t => foreignPattern.test(t));

    const compareFn = await page.evaluate(() => {
      const probe = { document_name: 'annual.pdf', document_index: 0, question_index: 2, question: 'Q2' };
      return {
        comparisonCellKey: typeof window.comparisonCellKey === 'function' ? window.comparisonCellKey(probe) : '__missing__',
        comparisonMarkup: typeof window.comparisonMarkup === 'function',
        renderComparison: typeof window.renderComparison === 'function',
        buildComparisonMap: typeof window.buildComparisonMap === 'function',
      };
    });

    const renderResult = await page.evaluate(() => {
      const dataA = { cells: [{ document_index: 0, document_name: 'annual.pdf', question_index: 0, question: 'Q1', answer: 'A', status: 'verified_exact', citation_status: 'verified_exact', page: 1 }] };
      const dataB = { cells: [{ document_index: 0, document_name: 'annual.pdf', question_index: 0, question: 'Q1', answer: 'B', status: 'verified_exact', citation_status: 'verified_exact', page: 1 }] };
      try {
        window.renderComparison(dataA, dataB);
        const table = document.getElementById('comparisonTable');
        return {
          ok: true,
          rows: table.querySelectorAll('tr').length,
          divergentRows: table.querySelectorAll('tr.divergent-row').length,
        };
      } catch (e) {
        return { ok: false, error: String((e && e.message) || e) };
      }
    });

    console.log(JSON.stringify({ ownConsoleErrors, foreignConsoleErrors: foreign.length, pageErrors, compareFn, renderResult }, null, 2));

    let failures = [];
    if (ownConsoleErrors.length) failures.push('inline console errors: ' + ownConsoleErrors.join(' | '));
    if (pageErrors.length) failures.push('page errors: ' + pageErrors.join(' | '));
    if (compareFn.comparisonCellKey !== '2|annual.pdf') failures.push('comparisonCellKey produced wrong key: ' + JSON.stringify(compareFn.comparisonCellKey));
    if (!compareFn.renderComparison) failures.push('renderComparison not defined');
    if (!compareFn.buildComparisonMap) failures.push('buildComparisonMap not defined');
    if (!renderResult.ok) failures.push('renderComparison threw: ' + renderResult.error);
    if (renderResult.divergentRows !== 1) failures.push('expected 1 divergent row, got ' + renderResult.divergentRows);

    if (failures.length) {
      console.error('SMOKE FAILURES:\n' + failures.join('\n'));
      process.exitCode = 1;
    } else {
      console.log('SMOKE PASS');
    }
  } finally {
    await browser.close();
    server.kill();
    try { fs.unlinkSync(RUN_DB); } catch (_) { /* best effort */ }
  }
}

runSmoke().catch(err => {
  console.error('SMOKE CRASH:', err);
  process.exit(1);
});