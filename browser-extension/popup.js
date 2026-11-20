const status = document.getElementById('status');
const baseUrl = document.getElementById('baseUrl');
const projectId = document.getElementById('projectId');
const token = document.getElementById('token');
const capture = document.getElementById('capture');

chrome.storage.session.get(['baseUrl', 'projectId']).then((saved) => {
  if (saved.baseUrl) baseUrl.value = saved.baseUrl;
  if (saved.projectId) projectId.value = saved.projectId;
});

function setStatus(text, error = false) {
  status.textContent = text;
  status.style.color = error ? '#b42318' : '#1a7f37';
}

function captureReadablePage() {
  const maxContentChars = 500000;
  const maxSections = 500;
  const maxSectionChars = 20000;
  const source = document.querySelector('article') || document.querySelector('main') || document.body;
  const clone = source.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,nav,footer,aside,form,input,textarea,select,button').forEach((node) => node.remove());
  const text = clone.innerText.replace(/\n{3,}/g, '\n\n').trim();
  return {
    title: document.title || location.hostname,
    source_url: location.href,
    canonical_url: document.querySelector('link[rel="canonical"]')?.href || null,
    captured_at: new Date().toISOString(),
    content: text.slice(0, maxContentChars),
    sections: text.split(/\n{2,}/).map((item) => item.trim()).filter(Boolean)
      .slice(0, maxSections).map((item) => item.slice(0, maxSectionChars))
  };
}

capture.addEventListener('click', async () => {
  const project = projectId.value.trim();
  const endpoint = baseUrl.value.trim().replace(/\/$/, '');
  if (!project || !endpoint) {
    setStatus('OpenMatrix URL and project ID are required.', true);
    return;
  }
  capture.disabled = true;
  setStatus('Reading current page…');
  try {
    // The token stays only in this popup DOM; it is never persisted by the extension.
    await chrome.storage.session.set({ baseUrl: endpoint, projectId: project });
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url?.startsWith('http')) throw new Error('Open an http(s) page first.');
    const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: captureReadablePage });
    if (!result?.result?.content) throw new Error('No readable text found on this page.');
    const headers = { 'Content-Type': 'application/json' };
    if (token.value.trim()) headers.Authorization = `Bearer ${token.value.trim()}`;
    const response = await fetch(`${endpoint}/api/v1/projects/${encodeURIComponent(project)}/evidence-snapshots`, {
      method: 'POST', headers, body: JSON.stringify({ ...result.result, indexed: true })
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.error || `Capture failed (${response.status}).`);
    setStatus(`Captured ${data.evidence_snapshot.section_count} sections.`);
  } catch (error) {
    setStatus(error.message || 'Capture failed.', true);
  } finally {
    capture.disabled = false;
  }
});
