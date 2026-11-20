"""OpenMatrix MCP server (stdio).

Exposes OpenMatrix analysis capabilities as Model Context Protocol tools so
external LLM clients (Claude Code, Codex, OpenCode, ...) can call them through
the standard MCP protocol.

Built on the official ``mcp`` Python SDK (MIT, modelcontextprotocol/python-sdk).

Current tools:
- export_matrix_report(run_id, format="csv"): export a matrix run as CSV or
  Markdown, reusing the same ``load_matrix_run`` + ``safe_cell`` logic as the
  existing REST endpoints (/api/export-csv, /api/export-xlsx).
- retrieve_context(question, pages, mode="bm25", limit_per_question=2): select
  the most relevant document chunks for a question using the existing chunked
  retrieval pipeline (BM25 sparse / optional BGE-M3 dense / fusion, with
  optional BGE Reranker v2-m3 reranking), mirroring the ISD-style
  "select only what needs full attention" philosophy.
- fetch_context(url, question, mode="bm25", limit_per_question=2): fetch a URL
  over HTTP(S), extract readable text, and select the most relevant chunks for
  a question via the same retrieval pipeline — closing the "URL → analysis"
  data-source abstraction loop (P1). Pure Python stdlib, no external deps.
- summarize_context(pages, question, mode, limit_per_question): select the most
  relevant chunks via the retrieval pipeline and produce an extractive summary
  (no LLM) — sentences scored by question-keyword overlap + position. P1.
- verify_quote(quote, pages, mode="exact", fuzzy_threshold=0.85): layered citation
  verification — exact substring, fuzzy (difflib similarity), or semantic (token
  Jaccard + char similarity) matching. P0 differentiation.
- export_report(run_id, format="pdf", output_dir=None): write a matrix run as a
  PDF (reportlab) or XLSX (openpyxl) file on disk and return the artifact path.
  Closes product priority ① export beyond CSV/Markdown text.
- export_slides(run_id, output_dir=None, template_id=None): Matrix -> Slides —
  render a matrix run as a citation-footnoted PowerPoint deck, one slide per
  question, with a footnote on every content slide carrying citation_status +
  page + the verbatim source quote (python-pptx, lazy import). Structured
  deck-as-deliverable over existing matrix data (no grid/cell generation).
- analyze_documents(documents, question, mode, limit_per_question): multi-document
  orchestration — per-document independent retrieval via the existing pipeline,
  returns a structured per-document report. "Document list + independent Q&A"
  pattern (no grid-cell construction; engineering-structure observation only,
  not a patent non-infringement or Freedom-to-Operate legal opinion). P1/P3.
- compute_financials(values, operation): deterministic currency/ratio/unit
  conversion on supplied numeric values (no LLM, no retrieval). P2.
- extract_kpis(pages, kpi_names, mode, limit_per_question): for each KPI name,
  select the most relevant chunks via the existing retrieval pipeline and extract
  the nearby numeric value + unit with page attribution. P2.
"""

# (find_contradictions / verify_claim_chain were deferred out of P2: experimental
#  semantic heuristics requiring deeper NLU; see docs/MCP-ROADMAP.md.)

from __future__ import annotations

import csv
import difflib
import html.parser
import http.client
import io
import ipaddress
import logging
import json
import math
import os
import re
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from main import (
    build_page_chunks,
    get_vector_store_adapter,
    load_matrix_run,
    safe_cell,
    select_retrieved_context,
    verify_matrix_citations,
)

from agent.research_run import ResearchRunManager, research_run
from agent.scheduler import ScheduleManager, default_manager
from agent.tasks import TaskManager, default_task_manager

from library import default_library_store

from apikeys import default_key_store, require_master_key
from audit import default_audit_store
mcp = FastMCP("openmatrix")


def feature_flag(name: str, default: bool = False) -> bool:
    """Read an ``OPENMATRIX_ENABLE_<NAME>`` environment feature flag (H6).

    Name matching is case-insensitive. Semantics:

    - unset -> ``default``
    - ``1``/``true``/``yes``/``on`` -> enabled (``True``)
    - ``0``/``false``/``no``/``off`` -> disabled (``False``)
    - any other value -> ``default``

    Provides per-integration feature switches while keeping current
    behavior when the flag is unset.
    """
    target = f"OPENMATRIX_ENABLE_{name.upper()}"
    value = None
    for key in os.environ:
        if key.upper() == target:
            value = os.environ[key]
            break
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return default


@mcp.tool()
def export_matrix_report(run_id: str, format: str = "csv") -> str:
    """Export a completed matrix run as CSV or Markdown.

    Args:
        run_id: The matrix run id (as returned by /api/v1/runs).
        format: "csv" (default) or "markdown".

    Returns:
        The report text, or an error string starting with "Error:" when the
        run id does not exist or the format is unsupported.
    """
    run = load_matrix_run(run_id)
    if run is None:
        return f"Error: matrix run {run_id!r} not found"

    rows = run.get("cells", [])
    headers = ["document_name", "question", "answer", "quote", "citation_status", "page"]

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        for cell in rows:
            writer.writerow([
                safe_cell(cell.get("document_name", "")),
                safe_cell(cell.get("question", "")),
                safe_cell(cell.get("answer", "")),
                safe_cell(cell.get("quote", "")),
                safe_cell(cell.get("citation_status", "")),
                safe_cell(cell.get("page", "")),
            ])
        return buffer.getvalue()

    if format == "markdown":
        lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
        for cell in rows:
            lines.append("| " + " | ".join(
                safe_cell(cell.get(h, "")) for h in headers
            ) + " |")
        return "\n".join(lines)

    return f"Error: unsupported format {format!r} (use 'csv' or 'markdown')"


@mcp.tool()
def retrieve_context(
    question: str,
    pages: list[str],
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Select the most relevant document chunks for a question.

    Uses the same chunked retrieval pipeline as the analysis path (BM25 sparse,
    optional BGE-M3 dense, or reciprocal-rank fusion; BM25 candidates may be
    reranked by an optional BGE Reranker v2-m3). Returns the selected chunks as
    JSON with chunk id, page, and text, plus a ``full_document`` flag when the
    retrieval fell back to the entire document.

    Args:
        question: The question to retrieve context for.
        pages: List of document page texts (one entry per page).
        mode: "bm25" (default), "dense", or "fusion". Invalid modes fall back to bm25.
        limit_per_question: Max chunks to select per question.

    Returns:
        JSON string with keys ``chunks`` (list of {chunk_id, page, text}) and
        ``full_document`` (bool), or an error string starting with "Error:".
    """
    if not question or not question.strip():
        return "Error: question must be non-empty"
    if not pages:
        return "Error: pages must be non-empty"

    context_text, used_full_document = select_retrieved_context(
        [question],
        pages,
        limit_per_question=limit_per_question,
        mode=mode,
    )

    if used_full_document:
        return json.dumps({"chunks": [], "full_document": True}, ensure_ascii=False)

    return json.dumps({"chunks": _parse_context_blocks(context_text), "full_document": False}, ensure_ascii=False)


@mcp.tool()
def verify_quote(
    quote: str,
    pages: list[str],
    mode: str = "exact",
    fuzzy_threshold: float = 0.85,
    semantic_threshold: float = 0.55,
) -> str:
    """Verify a quote against the document pages using layered citation verification.

    Three verification layers (product P0 "citation verification layering"):

    - ``exact`` (default): verbatim substring match via ``verify_matrix_citations``.
      Returns ``verified_exact`` with the page number, ``not_found`` when absent,
      or ``not_applicable`` when the quote is empty. **Byte-identical output** to
      the previous single-layer behavior.
    - ``fuzzy``: how much of the quote appears contiguously in a page — the longest
      matching block's coverage of the quote (``difflib.SequenceMatcher``), compared
      against ``fuzzy_threshold``. Yields ``fuzzy_match`` with ``similarity`` and the
      best-matching page. Catches OCR/punctuation noise.
    - ``semantic``: lightweight lexical approximation combining token overlap
      coefficient (|quote∩page|/|quote|, not diluted by long pages) with
      contiguous character coverage (no LLM). Compared against
      ``semantic_threshold``. Yields ``semantic_match`` with ``similarity`` and page.
      Catches light paraphrases / word-order drift.

    Args:
        quote: The text string to verify.
        pages: List of document page texts (one entry per page).
        mode: "exact" (default), "fuzzy", or "semantic". Invalid modes fall back to exact.
        fuzzy_threshold: Coverage threshold (0..1) for fuzzy mode.
        semantic_threshold: Combined-score threshold (0..1) for semantic mode.

    Returns:
        JSON string with keys ``citation_status`` (one of "verified_exact",
        "fuzzy_match", "semantic_match", "not_found", "not_applicable"), ``page``
        (int or null) and, for fuzzy/semantic modes, ``similarity`` (float or
        null). Or an error string starting with "Error:".
    """
    if not pages:
        return "Error: pages must be non-empty"

    quote_str = quote or ""
    if not quote_str.strip():
        return json.dumps({
            "citation_status": "not_applicable",
            "page": None,
        }, ensure_ascii=False)

    if mode == "fuzzy":
        best_similarity = 0.0
        best_page = None
        for page_number, page_text in enumerate(pages, start=1):
            matcher = difflib.SequenceMatcher(None, quote_str, page_text, autojunk=False)
            longest_block = max(matcher.get_matching_blocks(), key=lambda b: b.size)
            coverage = longest_block.size / len(quote_str) if quote_str else 0.0
            if coverage > best_similarity:
                best_similarity = coverage
                best_page = page_number
        if best_similarity >= fuzzy_threshold:
            return json.dumps({
                "citation_status": "fuzzy_match",
                "page": best_page,
                "similarity": round(best_similarity, 4),
            }, ensure_ascii=False)
        return json.dumps({
            "citation_status": "not_found",
            "page": None,
            "similarity": round(best_similarity, 4),
        }, ensure_ascii=False)

    if mode == "semantic":
        quote_tokens = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", quote_str.lower()))
        best_similarity = 0.0
        best_page = None
        for page_number, page_text in enumerate(pages, start=1):
            page_tokens = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", page_text.lower()))
            # Overlap coefficient: |quote ∩ page| / |quote| — unlike Jaccard's
            # union denominator, this is not diluted by long pages.
            overlap = 0.0
            if quote_tokens:
                overlap = len(quote_tokens & page_tokens) / len(quote_tokens)
            matcher = difflib.SequenceMatcher(None, quote_str, page_text, autojunk=False)
            longest_block = max(matcher.get_matching_blocks(), key=lambda b: b.size)
            char_coverage = longest_block.size / len(quote_str) if quote_str else 0.0
            combined = 0.5 * overlap + 0.5 * char_coverage
            if combined > best_similarity:
                best_similarity = combined
                best_page = page_number
        if best_similarity >= semantic_threshold:
            return json.dumps({
                "citation_status": "semantic_match",
                "page": best_page,
                "similarity": round(best_similarity, 4),
            }, ensure_ascii=False)
        return json.dumps({
            "citation_status": "not_found",
            "page": None,
            "similarity": round(best_similarity, 4),
        }, ensure_ascii=False)

    rows = [{"quote": quote_str}]
    verified_rows = verify_matrix_citations(rows, pages, exact_only=True)
    result = verified_rows[0]

    return json.dumps({
        "citation_status": result.get("citation_status"),
        "page": result.get("page"),
    }, ensure_ascii=False)


class _TextExtractor(html.parser.HTMLParser):
    """Minimal stdlib HTML→text extractor.

    Collects text from block-level content elements, inserting newlines to
    preserve readable paragraph structure. Ignores script/style/nav boilerplate
    by skipping the ``script``, ``style``, ``noscript`` and ``svg`` subtrees.
    """

    _SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._block = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag in ("p", "li", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in ("p", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.split("\n")]
        return "\n".join(ln for ln in lines if ln)


def _extract_html_text(html_body: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_body)
    return parser.text().strip()



def _ssrf_mode() -> str:
    mode = os.getenv("OPENMATRIX_SSRF_MODE", "strict").strip().lower()
    if mode not in {"strict", "off"}:
        raise ValueError("OPENMATRIX_SSRF_MODE must be strict or off")
    if mode == "off":
        logging.getLogger(__name__).warning(
            "SSRF protection is disabled (OPENMATRIX_SSRF_MODE=off)"
        )
    return mode


def _ssrf_allow_hosts() -> set[str]:
    raw = os.getenv(
        "OPENMATRIX_SSRF_ALLOW_HOSTS",
        "www.sec.gov,data.sec.gov,api.stlouisfed.org",
    )
    return {x.strip().lower().rstrip(".") for x in raw.split(",") if x.strip()}


def _ssrf_allow_cidrs() -> tuple[ipaddress._BaseNetwork, ...]:
    raw = os.getenv("OPENMATRIX_SSRF_ALLOW_CIDRS", "")
    return tuple(
        ipaddress.ip_network(x.strip(), strict=False)
        for x in raw.split(",") if x.strip()
    )


def _ssrf_proxy_enabled() -> bool:
    return os.getenv("OPENMATRIX_USE_SYSTEM_PROXY", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _ssrf_host_allowed(host: str) -> bool:
    return host.lower().rstrip(".") in _ssrf_allow_hosts()


def _ssrf_ip_allowed(host: str, ip: str, *, literal: bool = False) -> bool:
    addr = ipaddress.ip_address(ip)
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    if str(addr) in _SSRF_ALWAYS_BLOCKED:
        return False
    if (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified):
        # Existing local HTTP fixtures patch _is_blocked_ip(False); preserve
        # that seam only for the conventional localhost test hostname.
        if host.lower().rstrip(".") not in {"localhost", "localhost.localdomain"}:
            return False
        if _is_blocked_ip(ip):
            return False
    # Literal loopback/private/link-local/metadata (including IPv4-mapped IPv6)
    # is always denied. This remains true in off mode and with proxy enabled.
    if literal:
        return not (
            str(addr) in _SSRF_ALWAYS_BLOCKED
            or addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_unspecified or addr.is_multicast or not addr.is_global
        )
    if str(addr) in _SSRF_ALWAYS_BLOCKED:
        return False
    if _ssrf_mode() == "off":
        return True
    # 198.18/15 is reserved for deterministic fake DNS in tests. Official
    # allowlisted hosts may use it, but no arbitrary host may.
    if addr in ipaddress.ip_network("198.18.0.0/15") and _ssrf_host_allowed(host):
        return True
    if any(addr in net for net in _ssrf_allow_cidrs()):
        return True
    if _is_blocked_ip(ip):
        return False
    return True


class _PolicyProxyHandler(urllib.request.ProxyHandler):
    """Use environment proxy only on an explicitly allowlisted hostname."""

    def proxy_open(self, req, proxy, type):
        if (not getattr(req, "_openmatrix_proxy_allowed", False)
                or urllib.request.proxy_bypass(req.host or "")):
            return None
        setattr(req, "_openmatrix_using_proxy", True)
        return super().proxy_open(req, proxy, type)


def _ssrf_opener(parsed):
    if _ssrf_proxy_enabled():
        proxy_handler = _PolicyProxyHandler()
    else:
        # Explicit empty mapping prevents urllib's ambient proxy discovery.
        proxy_handler = _PolicyProxyHandler({})
    return urllib.request.build_opener(
        proxy_handler, _PinnedHttpHandler(), _PinnedHttpsHandler(),
        _SsrfsafeRedirectHandler(),
    )


def _resolve_fetch_target(url: str):
    """Validate URL; return (parsed, None) only for an eligible system proxy."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http(s) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL userinfo is not allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("URL host is required")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ssrf_ip_allowed(host, str(literal), literal=True):
            raise ValueError(f"URL host {host!r} resolves to blocked address {literal}")
    proxy_enabled = _ssrf_proxy_enabled()
    proxy = urllib.request.getproxies().get(parsed.scheme)
    proxy_allowed = (
        proxy_enabled and _ssrf_host_allowed(host)
        and proxy and not urllib.request.proxy_bypass(host)
    )
    if proxy_allowed:
        return parsed, None
    return _resolve_public_ips(url)

_SSRF_MAX_REDIRECTS = 5
_SSRF_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
# Cloud metadata endpoints are blocked unconditionally (never exempted).
_SSRF_ALWAYS_BLOCKED = {"169.254.169.254", "169.254.170.2"}


def _is_blocked_ip(ip: str) -> bool:
    """Return True if the IP is loopback / private / link-local / multicast / reserved.

    SSRF guard: block cloud metadata, localhost, RFC1918, link-local, multicast,
    reserved, and unspecified targets. The connection is pinned to the validated
    IP (see _PinnedHttpConnection) to close the DNS-rebinding TOCTOU window.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if str(addr) in _SSRF_ALWAYS_BLOCKED:
        return True
    if addr.is_loopback:
        return True
    if addr.is_private:
        return True
    if addr.is_link_local:
        return True
    if addr.is_multicast:
        return True
    if addr.is_reserved:
        return True
    if addr.is_unspecified:
        return True
    return False


def _resolve_public_ips(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http(s) URLs are allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("URL host is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ssrf_ip_allowed(host, str(literal), literal=True):
            raise ValueError(f"URL host {host!r} resolves to blocked address {literal}")
        return parsed, str(literal)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve URL host {host!r}: {exc}") from exc
    seen = set()
    for info in infos:
        ip = info[4][0]
        if ip in seen:
            continue
        seen.add(ip)
        if ip in _SSRF_ALWAYS_BLOCKED:
            raise ValueError(f"URL host {host!r} resolves to blocked address {ip}")
        if _ssrf_ip_allowed(host, ip):
            return parsed, ip
        if ipaddress.ip_address(ip) in ipaddress.ip_network("198.18.0.0/15"):
            raise ValueError(f"URL host {host!r} resolves to fake-IP {ip}; VPN/proxy may use fake-IP mode; configure OPENMATRIX_SSRF_ALLOW_HOSTS or switch to redir-host mode")
        raise ValueError(f"URL host {host!r} resolves to blocked address {ip}")
    raise ValueError(f"URL host {host!r} did not resolve to a usable address")

class _PinnedHttpConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated IP instead of re-resolving.

    Pinning closes the DNS-rebinding TOCTOU: the socket goes to the validated
    address while the Host header keeps the original hostname.
    """

    def __init__(self, host, port=None, **kwargs):
        # Keep hostname for Host header; store resolved ip to connect to.
        self._pinned_ip = kwargs.pop("pinned_ip", None)
        super().__init__(host, port, **kwargs)

    def connect(self):
        ip = self._pinned_ip
        if not ip:
            return super().connect()
        # Honor existing timeout; connect to the pinned IP directly.
        if self.timeout is not None:
            timeout = self.timeout
        else:
            timeout = socket.getdefaulttimeout()
        conn = socket.create_connection((ip, self.port), timeout, self.source_address)
        if getattr(self, "_tunnel_host", None):
            self.sock = conn
            self._tunnel()
        else:
            self.sock = conn


class _PinnedHttpsConnection(_PinnedHttpConnection):
    """HTTPSConnection pinned to a validated IP, with hostname TLS verification."""

    default_port = 443

    def __init__(self, host, port=None, **kwargs):
        # Let HTTPConnection resolve the port (defaulting to 443 for https) and
        # keep the original hostname for SNI; only the socket is pinned to the IP.
        super().__init__(host, port, **kwargs)
        if self.port is None:
            self.port = self.default_port

    def connect(self):
        super().connect()
        context = ssl.create_default_context()
        server_hostname = self.host
        self.sock = context.wrap_socket(self.sock, server_hostname=server_hostname)


class _PinnedHttpHandler(urllib.request.HTTPHandler):
    """HTTP handler that uses pinned connections via the validated IP."""

    def http_open(self, req):
        if getattr(req, "_openmatrix_using_proxy", False):
            return super().http_open(req)
        pinned = getattr(req, "_openmatrix_pinned_ip", None)
        if not pinned:
            raise ValueError("request has no validated pinned IP")
        return self.do_open(_PinnedHttpConnection, req, pinned_ip=pinned)


class _PinnedHttpsHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that uses pinned connections via the validated IP."""

    def https_open(self, req):
        if getattr(req, "_openmatrix_using_proxy", False):
            return super().https_open(req)
        pinned = getattr(req, "_openmatrix_pinned_ip", None)
        if not pinned:
            raise ValueError("request has no validated pinned IP")
        return self.do_open(_PinnedHttpsConnection, req, pinned_ip=pinned)


class _SsrfsafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-validates and re-pins every hop, caps redirect count."""

    def __init__(self) -> None:
        super().__init__()
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._hops >= _SSRF_MAX_REDIRECTS:
            raise ValueError(f"too many redirects (max {_SSRF_MAX_REDIRECTS})")
        parsed, ip = _resolve_fetch_target(newurl)
        # The parent builds a NEW Request for the redirect target. Attach the pin
        # to that new request so the redirected connection stays pinned and cannot
        # fall back to a second, unvalidated DNS resolution.
        new_req = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )
        if new_req is not None:
            if hasattr(new_req, "_openmatrix_using_proxy"):
                delattr(new_req, "_openmatrix_using_proxy")
            if hasattr(new_req, "_openmatrix_proxy_allowed"):
                delattr(new_req, "_openmatrix_proxy_allowed")
            setattr(new_req, "_openmatrix_pinned_ip", ip)
            if _ssrf_proxy_enabled() and _ssrf_host_allowed(parsed.hostname or ""):
                setattr(new_req, "_openmatrix_proxy_allowed", True)
        self._hops += 1
        return new_req


def _fetch_url(url: str, timeout: int = 20, user_agent: str | None = None) -> str:
    """Fetch a URL and return extracted plain text. Raises on failure.

    SSRF-hardened: resolves and pins the validated IP before connecting (closing
    the DNS-rebinding window), re-validates and re-pins every redirect, caps
    redirect count and response size, and never forwards caller credentials.
    ``user_agent`` overrides the default UA when a provider requires one.
    """
    if not url or not url.strip():
        raise ValueError("url must be non-empty")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    parsed, ip = _resolve_fetch_target(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent or "openmatrix-mcp/1.0 (+MCP data-source abstraction)"},
    )
    setattr(req, "_openmatrix_pinned_ip", ip)
    if _ssrf_proxy_enabled() and _ssrf_host_allowed(parsed.hostname or ""):
        setattr(req, "_openmatrix_proxy_allowed", True)
    opener = _ssrf_opener(parsed)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(_SSRF_MAX_BYTES + 1)
    if len(raw) > _SSRF_MAX_BYTES:
        raise ValueError(f"response exceeds {_SSRF_MAX_BYTES} byte limit")
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")
    return _extract_html_text(body)


def _fetch_bytes(url: str, timeout: int = 30, user_agent: str | None = None) -> bytes:
    """Fetch a URL and return raw bytes, reusing the SSRF-hardened connection.

    Raises ValueError on blocked targets, oversized responses, or HTTP errors.
    ``user_agent`` overrides the default UA (SEC requires a descriptive UA with
    contact info).
    """
    if not url or not url.strip():
        raise ValueError("url must be non-empty")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    parsed, ip = _resolve_fetch_target(url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent or "openmatrix-mcp/1.0 (+MCP data-source abstraction)"},
    )
    setattr(req, "_openmatrix_pinned_ip", ip)
    if _ssrf_proxy_enabled() and _ssrf_host_allowed(parsed.hostname or ""):
        setattr(req, "_openmatrix_proxy_allowed", True)
    opener = _ssrf_opener(parsed)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(_SSRF_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} from {url!r}")
    except urllib.error.URLError as exc:
        raise ValueError(f"URL error from {url!r}: {exc}")
    if len(raw) > _SSRF_MAX_BYTES:
        raise ValueError(f"response exceeds {_SSRF_MAX_BYTES} byte limit")
    return raw


def _sec_user_agent() -> str:
    """Use SEC contact identification and warn clearly when unconfigured."""
    value = os.environ.get("OPENMATRIX_SEC_USER_AGENT", "").strip()
    if not value:
        import logging
        logging.getLogger(__name__).warning(
            "Set OPENMATRIX_SEC_USER_AGENT to an application name and real contact email before accessing SEC data.")
        return "OpenMatrix-MCP/1.0 (contact not configured)"
    return value


def _sec_archives_base() -> str:
    return os.environ.get("OPENMATRIX_SEC_ARCHIVES_BASE", "https://www.sec.gov").rstrip("/")


def _sec_base() -> str:
    """SEC API base (env-overridable so tests can point at a local mock)."""
    return os.environ.get("OPENMATRIX_SEC_BASE", "https://data.sec.gov").rstrip("/")


def _sec_cik(ticker: str) -> str:
    """Resolve a ticker to a zero-padded CIK via the SEC submissions index.

    Raises ValueError if the ticker is unknown. Uses the official company_tickers
    JSON (data.sec.gov/submissions cik lookup is via the tickers index).
    """
    import json as _json

    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("ticker must be non-empty")
    tickers_url = os.environ.get("OPENMATRIX_SEC_TICKERS_URL", "https://www.sec.gov/files/company_tickers.json")
    raw = _fetch_bytes(tickers_url, user_agent=_sec_user_agent())
    try:
        data = _json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("could not parse SEC company tickers index")
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker:
            cik = str(entry["cik_str"])
            return cik.zfill(10)
    raise ValueError(f"ticker {ticker!r} not found in SEC index")


@mcp.tool()
def sec_search(ticker: str, form_type: str | None = None) -> str:
    """Return recent SEC filings for a ticker.

    Uses the official data.sec.gov submissions API (key-free). Env-overridable
    User-Agent via OPENMATRIX_SEC_USER_AGENT (SEC requires a contact address).

    Args:
        ticker: Company ticker (e.g. "AAPL").
        form_type: Optional form type filter (e.g. "10-K", "8-K").

    Returns:
        JSON string with keys ``ticker``, ``cik``, ``filings`` (list of
        {form, filing_date, accession, primary_document}), or an error string
        starting with "Error:".
    """
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json

    try:
        cik = _sec_cik(ticker)
        raw = _fetch_bytes(f"{_sec_base()}/submissions/CIK{cik}.json", user_agent=_sec_user_agent())
        data = _json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: failed to fetch SEC submissions for {ticker!r}: {exc}"

    filings = []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    for i in range(len(forms)):
        form = forms[i]
        if form_type and form.upper() != form_type.upper():
            continue
        accession = accessions[i] if i < len(accessions) else None
        primary_document = docs[i] if i < len(docs) else None
        accession_nodash = accession.replace("-", "") if accession else None
        filing_url = None
        if cik and accession_nodash and primary_document:
            filing_url = (
                f"{_sec_archives_base()}/Archives/edgar/data/{int(cik)}/{accession_nodash}/{primary_document}"
            )
        filings.append({
            "form": form,
            "filing_date": dates[i] if i < len(dates) else None,
            "accession": accession,
            "primary_document": primary_document,
            "filing_url": filing_url,
        })
        if len(filings) >= 20:
            break

    return _json.dumps({"ticker": ticker, "cik": cik, "filings": filings}, ensure_ascii=False)


_SEC_PAGE_CHARS = 12000


def _chunk_text_to_pages(text: str, page_chars: int = _SEC_PAGE_CHARS) -> list[str]:
    """Split long text into page-sized chunks, character-lossless.

    ``''.join(pages) == text`` always holds: no stripping, no skipping. Cuts
    prefer a space boundary when one exists near the limit, but every character
    (including boundary whitespace) is preserved exactly once.
    """
    if not text:
        return []
    if len(text) <= page_chars:
        return [text]
    pages = []
    start = 0
    while start < len(text):
        end = min(start + page_chars, len(text))
        if end < len(text):
            # back off to a space boundary only if it keeps the page non-trivial
            cut = text.rfind(" ", start, end)
            if cut >= start:
                end = cut + 1  # include the space so no char is dropped
        pages.append(text[start:end])
        start = end
    return pages


@mcp.tool()
def sec_get_filing(filing_url: str) -> str:
    """Fetch a SEC filing document and return character sections, not physical pages.

    The URL must be an http(s) SEC document URL. SSRF-hardened; content is
    normalized into page-sized chunks so it can feed the retrieval pipeline.

    Args:
        filing_url: http(s) URL of the SEC document (e.g. an EDGAR filing HTML).

    Returns:
        JSON string with keys ``url``, ``page_count``, ``pages`` (list of
        {page, text}), or an error string starting with "Error:".
    """
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json

    try:
        text = _fetch_url(filing_url, user_agent=_sec_user_agent())
    except (ValueError, OSError, urllib.error.URLError) as exc:
        return f"Error: failed to fetch filing: {exc}"
    if not text:
        return f"Error: no readable text extracted from {filing_url!r}"
    pages = _chunk_text_to_pages(text)
    return _json.dumps({
        "url": filing_url,
        "page_count": len(pages),
        "unit": "char_chunk",
        "page_is_physical": False,
        "section_count": len(pages),
        "pages": [{"section": i + 1, "page": i + 1, "unit": "char_chunk", "page_is_physical": False, "text": p} for i, p in enumerate(pages)],
    }, ensure_ascii=False)


def _sec_fact_is_annual(entry: dict) -> bool:
    """Return whether an XBRL fact represents a fiscal-year value."""
    fp = str(entry.get("fp") or "").upper()
    form = str(entry.get("form") or "").upper()
    annual_forms = {"10-K", "10-K/A", "20-F", "40-F"}
    if fp != "FY" or form not in annual_forms:
        return False
    start, end = entry.get("start"), entry.get("end")
    if not start:
        return bool(end)
    if not end:
        return False
    try:
        from datetime import date
        days = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except (TypeError, ValueError):
        return False
    return 330 <= days <= 380


def _sec_fact_is_quarter(entry: dict) -> bool:
    """Return whether an XBRL fact represents a quarterly duration value."""
    fp = str(entry.get("fp") or "").upper()
    if not fp.startswith("Q"):
        return False
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return False
    try:
        from datetime import date
        days = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except (TypeError, ValueError):
        return False
    return 70 <= days <= 110


def _sec_fact_latest_per_period(entries: list[dict]) -> list[dict]:
    """Deduplicate facts by period, retaining the latest filed restatement."""
    chosen: dict[tuple[object, object], dict] = {}
    for entry in entries:
        key = (entry.get("start"), entry.get("end"))
        previous = chosen.get(key)
        if previous is None or str(entry.get("filed") or "") >= str(previous.get("filed") or ""):
            chosen[key] = entry
    return sorted(
        chosen.values(),
        key=lambda item: (str(item.get("end") or ""), str(item.get("filed") or "")),
        reverse=True,
    )


@mcp.tool()
def sec_get_xbrl_facts(
    ticker: str,
    fact_names: list[str],
    period: str = "latest_annual",
) -> str:
    """Return structured XBRL financial facts for a ticker.

    Uses data.sec.gov/api/xbrl/companyfacts (key-free). Only standard facts in
    the us-gaap, dei, or ifrs-full taxonomies are returned. By default the
    latest annual value is selected; ``latest_quarter`` selects the latest
    quarterly duration and ``all`` returns every period after restatement
    deduplication.

    Args:
        ticker: Company ticker (e.g. "AAPL").
        fact_names: List of fact names (e.g. ["Revenues", "Assets"]).
        period: ``latest_annual`` (default), ``latest_quarter``, or ``all``.

    Returns:
        JSON string with keys ``ticker``, ``facts`` (dict name -> value or list
        of values), or an error string starting with "Error:".
    """
    if period not in {"latest_annual", "latest_quarter", "all"}:
        return "Error: period must be one of latest_annual, latest_quarter, all"
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json

    try:
        cik = _sec_cik(ticker)
        raw = _fetch_bytes(f"{_sec_base()}/api/xbrl/companyfacts/CIK{cik}.json", user_agent=_sec_user_agent())
        data = _json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: failed to fetch XBRL facts for {ticker!r}: {exc}"

    result = {}
    revenue_aliases = {
        "revenue": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ],
        "revenues": [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
        ],
    }
    for fact in fact_names or []:
        name = (fact or "").strip()
        if not name:
            continue
        concepts = revenue_aliases.get(name.lower(), [name])
        found_entries = None
        resolved_concept = None
        for concept in concepts:
            for taxonomy in ("us-gaap", "dei", "ifrs-full"):
                node = data.get("facts", {}).get(taxonomy, {}).get(concept)
                if not node:
                    continue
                units = node.get("units", {})
                for unit, series in units.items():
                    if series:
                        found_entries = [
                            {"unit": unit, **entry}
                            for entry in series
                            if isinstance(entry, dict) and "val" in entry
                        ]
                        resolved_concept = concept
                        break
                if found_entries is not None:
                    break
            if found_entries is not None:
                break
        if found_entries is None:
            result[name] = {"error": "fact not found"}
            continue

        period_entries = [
            entry for entry in found_entries
            if entry.get("end") and entry.get("fp") and entry.get("form")
        ]
        if not period_entries:
            result[name] = {
                "error": "fact is missing SEC period metadata (end, fp, form)",
                "resolved_concept": resolved_concept,
            }
            continue
        if period == "all":
            selected = _sec_fact_latest_per_period(period_entries)
        elif period == "latest_annual":
            annual_entries = [entry for entry in period_entries if _sec_fact_is_annual(entry)]
            selected = _sec_fact_latest_per_period(annual_entries)
        else:
            quarter_entries = [entry for entry in period_entries if _sec_fact_is_quarter(entry)]
            selected = _sec_fact_latest_per_period(quarter_entries)
        if period != "all":
            if not selected:
                result[name] = {
                    "error": f"no fact found for period {period}",
                    "resolved_concept": resolved_concept,
                }
                continue
            selected = [selected[0]]

        def public_entry(entry: dict) -> dict:
            fields = {
                "value": entry.get("val"),
                "unit": entry.get("unit"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "fy": entry.get("fy"),
                "fp": entry.get("fp"),
                "form": entry.get("form"),
                "filed": entry.get("filed"),
                "accn": entry.get("accn"),
                "frame": entry.get("frame"),
            }
            if resolved_concept and len(concepts) > 1:
                fields["resolved_concept"] = resolved_concept
            return fields

        values = [public_entry(entry) for entry in selected]
        result[name] = values if period == "all" else values[0]

    return _json.dumps({"ticker": ticker, "cik": cik, "facts": result}, ensure_ascii=False)


@mcp.tool()
def fetch_context(
    url: str,
    question: str,
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Fetch a URL and retrieve the most relevant chunks for a question.

    Fetches the URL over HTTP(S) using the Python standard library, extracts
    readable text, and runs it through the same chunked retrieval pipeline as
    ``retrieve_context`` (BM25 sparse / optional BGE-M3 dense / fusion, with
    optional BGE Reranker v2-m3 reranking). Closes the
    "URL → analysis" data-source abstraction loop without external dependencies.

    Args:
        url: The http(s) URL to fetch.
        question: The question to retrieve context for.
        mode: "bm25" (default), "dense", or "fusion". Invalid modes fall back to bm25.
        limit_per_question: Max chunks to select per question.

    Returns:
        JSON string with keys ``url``, ``chunks`` (list of {chunk_id, page,
        text}), ``full_document`` (bool) and ``extracted_chars`` (int), or an
        error string starting with "Error:".
    """
    if not question or not question.strip():
        return "Error: question must be non-empty"

    try:
        page_text = _fetch_url(url)
    except (ValueError, urllib.error.URLError, OSError) as exc:
        return f"Error: failed to fetch {url!r}: {exc}"

    if not page_text:
        return "Error: no readable text extracted from {!r}".format(url)

    pages = [page_text]
    context_text, used_full_document = select_retrieved_context(
        [question],
        pages,
        limit_per_question=limit_per_question,
        mode=mode,
    )

    if used_full_document:
        return json.dumps({
            "url": url,
            "chunks": [],
            "full_document": True,
            "extracted_chars": len(page_text),
        }, ensure_ascii=False)

    return json.dumps({
        "url": url,
        "chunks": _parse_context_blocks(context_text),
        "full_document": False,
        "extracted_chars": len(page_text),
    }, ensure_ascii=False)


_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")


def _split_sentences(text: str) -> list[str]:
    """Split text into non-empty sentences using stdlib regex."""
    parts = [p.strip() for p in _SENTENCE_RE.split(text)]
    return [p for p in parts if p]


def _question_keywords(question: str) -> set[str]:
    """Extract candidate keywords from the question (lowercased, alnum tokens)."""
    return {
        tok.lower()
        for tok in re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", question)
        if len(tok) >= 2
    }


def _score_sentence(sentence: str, keywords: set[str]) -> float:
    """Score a sentence by question-keyword overlap (0..1)."""
    if not keywords:
        return 0.0
    sents = re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", sentence.lower())
    if not sents:
        return 0.0
    hits = sum(1 for tok in sents if tok in keywords)
    return hits / max(1, len(sents))


@mcp.tool()
def summarize_context(
    pages: list[str],
    question: str,
    mode: str = "bm25",
    limit_per_question: int = 3,
    max_sentences_per_chunk: int = 2,
) -> str:
    """Produce an extractive summary of a document grounded in a question.

    Selects the most relevant chunks via the same retrieval pipeline as
    ``retrieve_context`` (BM25 / dense / fusion), then scores the sentences
    inside each selected chunk by overlap with question keywords plus position
    (earlier sentences rank higher on ties). The top sentences are joined into a
    summary paragraph. No LLM is called — every sentence comes verbatim from the
    page text, so the summary is auditable and hallucination-free.

    Args:
        pages: List of document page texts (one entry per page).
        question: The question/topic to ground the summary on.
        mode: "bm25" (default), "dense", or "fusion". Invalid modes fall back to bm25.
        limit_per_question: Max chunks to select per question.
        max_sentences_per_chunk: Max sentences to take from each selected chunk.

    Returns:
        JSON string with keys ``summary`` (str), ``source_chunks`` (list of
        {chunk_id, page}), ``mode`` (str) and ``full_document`` (bool), or an
        error string starting with "Error:".
    """
    if not question or not question.strip():
        return "Error: question must be non-empty"
    if not pages:
        return "Error: pages must be non-empty"

    context_text, used_full_document = select_retrieved_context(
        [question],
        pages,
        limit_per_question=limit_per_question,
        mode=mode,
    )

    if used_full_document:
        return json.dumps({
            "summary": "",
            "source_chunks": [],
            "mode": mode,
            "full_document": True,
        }, ensure_ascii=False)

    keywords = _question_keywords(question)
    selected_sentences: list[tuple[float, int, str]] = []
    source_chunks: list[dict] = []
    index = 0

    for block in context_text.split("\n\n"):
        if not block.strip():
            continue
        first_line = block.splitlines()[0]
        body = "\n".join(block.splitlines()[1:])
        if body.strip():
            chunk_text = body.strip()
        else:
            chunk_text = first_line

        page = None
        chunk_id = None
        if first_line.startswith("[Page "):
            header = first_line[6:].rstrip("]")
            page_part, _, id_part = header.partition("|")
            try:
                page = int(page_part.strip())
            except ValueError:
                page = None
            chunk_id = id_part.strip() or None

        source_chunks.append({"chunk_id": chunk_id, "page": page})

        sentences = _split_sentences(chunk_text)
        ranked = sorted(
            enumerate(sentences),
            key=lambda pair: (_score_sentence(pair[1], keywords), -pair[0]),
            reverse=True,
        )
        for _, sentence in ranked[:max_sentences_per_chunk]:
            selected_sentences.append((0.0, index, sentence))
            index += 1

    selected_sentences.sort(key=lambda item: item[1])
    summary = " ".join(item[2] for item in selected_sentences)

    return json.dumps({
        "summary": summary,
        "source_chunks": source_chunks,
        "mode": mode,
        "full_document": False,
    }, ensure_ascii=False)


def _export_root() -> Path:
    """Server-fixed export root (env-overridable). All artifacts stay inside it."""
    root = os.environ.get("OPENMATRIX_EXPORT_ROOT")
    if root:
        return Path(root).resolve()
    base = Path(tempfile.gettempdir()) / "openmatrix-exports"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()




@mcp.tool()
def locate_quote_bboxes(pdf_path: str, quote: str, page: int | None = None) -> str:
    """Find quote coordinates within the server-configured PDF root."""
    root = Path(os.environ.get("OPENMATRIX_PDF_ROOT", str(_export_root()))).resolve()
    return _locate_quote_bboxes(pdf_path, quote, page=page, root=root)


def _locate_quote_bboxes(pdf_path: str, quote: str, page: int | None = None, *, root: str | Path) -> str:
    """Locate a quote in a PDF under the approved root using pdfplumber.

    This coordinate helper is intentionally confined to OPENMATRIX_PDF_ROOT
    (default: export root), rejects traversal/oversized input, and uses the
    approved pdfplumber adapter rather than blocked PyMuPDF/fitz.
    """
    if not isinstance(pdf_path, str) or not pdf_path.strip():
        return "Error: pdf_path must be a non-empty string"
    if not isinstance(quote, str) or not quote.strip():
        return "Error: quote must be a non-empty string"
    if len(quote) > 1000:
        return "Error: quote is too long (max 1000 characters)"
    if page is not None and (not isinstance(page, int) or page < 1):
        return "Error: page must be a positive integer when provided"

    root = Path(root).resolve()
    path = Path(pdf_path)
    if path.is_absolute():
        return "Error: pdf_path must be relative to the approved PDF root"
    if ".." in path.parts:
        return "Error: pdf_path must not contain '..' (path traversal rejected)"
    path = (root / path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "Error: pdf_path escapes the approved PDF root"
    if not path.exists() or not path.is_file():
        return f"Error: pdf_path {pdf_path!r} not found"
    if path.stat().st_size > 50 * 1024 * 1024:
        return "Error: PDF is too large for bbox lookup (max 50MB)"

    try:
        import pdfplumber  # type: ignore[import-not-found]
    except ImportError:
        return "Error: pdfplumber is not installed (required for PDF bounding boxes)"

    def _norm_tokens(text: str) -> list[str]:
        return [token.strip(".,;:!?()[]{}\"'“”‘’").casefold() for token in text.split() if token.strip(".,;:!?()[]{}\"'“”‘’")]

    target = _norm_tokens(quote)
    if not target:
        return "Error: quote must contain searchable text"

    def _without_raised_markers(words: list[dict]) -> list[dict]:
        body = []
        for word in words:
            previous = body[-1] if body else None
            # A small, raised digit immediately attached to an alphabetic
            # word can be a footnote marker. Baseline numbers are retained.
            is_marker = (
                previous is not None
                and str(word.get("text", "")).isdigit()
                and len(str(word["text"])) <= 3
                and any(c.isalpha() for c in str(previous.get("text", "")))
                and float(word["size"]) <= 0.8 * float(previous["size"])
                and -0.1 * float(previous["size"]) <= float(word["x0"]) - float(previous["x1"]) <= 0.4 * float(previous["size"])
                and float(previous["bottom"]) - float(word["bottom"]) >= 0.2 * float(previous["size"])
                and float(previous["top"]) - 0.5 * float(previous["size"]) <= float(word["top"]) < float(previous["bottom"])
            )
            if not is_marker:
                body.append(word)
        return body

    matches = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            if len(pdf.pages) > 500:
                return "Error: PDF has too many pages for bbox lookup (max 500)"
            page_indexes = [page - 1] if isinstance(page, int) else range(len(pdf.pages))
            for page_index in page_indexes:
                if page_index < 0 or page_index >= len(pdf.pages):
                    continue
                # Font-aware segmentation keeps raised footnote markers from
                # being merged into a body word (e.g. "sales1"). Try the
                # original word stream first; only raised footnote markers are optional.
                font_words = []
                for segmentation in ("original", "font", "body"):
                    before = len(matches)
                    if segmentation == "original":
                        words = pdf.pages[page_index].extract_words() or []
                    elif segmentation == "font":
                        font_words = pdf.pages[page_index].extract_words(extra_attrs=["size"]) or []
                        words = font_words
                    else:
                        words = _without_raised_markers(font_words)
                    normalized_words = [_norm_tokens(str(word.get("text", "")))[0] if _norm_tokens(str(word.get("text", ""))) else "" for word in words]
                    for start in range(0, max(0, len(normalized_words) - len(target) + 1)):
                        if normalized_words[start:start + len(target)] != target:
                            continue
                        span = words[start:start + len(target)]
                        bbox = [
                            float(min(float(w["x0"]) for w in span)),
                            float(min(float(w["top"]) for w in span)),
                            float(max(float(w["x1"]) for w in span)),
                            float(max(float(w["bottom"]) for w in span)),
                        ]
                        matches.append({"page": page_index + 1, "bbox": bbox, "quote": quote})
                        if len(matches) >= 50:
                            return json.dumps({"matches": matches, "count": len(matches), "truncated": True}, ensure_ascii=False)
                    if len(matches) > before:
                        break
    except Exception:
        return "Error: failed to locate quote bboxes"

    return json.dumps({"matches": matches, "count": len(matches), "truncated": False}, ensure_ascii=False)


def _confine_export_dir(output_dir: str | None) -> Path:
    """Resolve a logical relative output_dir to a path inside the export root.

    Raises ValueError for absolute paths or path traversal. The returned path is
    always inside the server-fixed export root.
    """
    root = _export_root()
    if output_dir is None:
        out = root
    else:
        p = Path(output_dir)
        if p.is_absolute():
            raise ValueError("output_dir must be a relative sub-directory, not an absolute path")
        if ".." in p.parts:
            raise ValueError("output_dir must not contain '..' (path traversal rejected)")
        out = root / p
    out = out.resolve()
    try:
        out.relative_to(root)
    except ValueError:
        raise ValueError(f"resolved output_dir escapes the export root ({root})")
    return out


@mcp.tool()
def export_report(run_id: str, format: str = "pdf", output_dir: str | None = None) -> str:
    """Write a matrix run report to disk as PDF, XLSX, PPTX, or DOCX and return the file path.

    Reuses the same ``load_matrix_run`` + ``safe_cell`` data path as
    ``export_matrix_report``. Writes an actual artifact file so LLM clients can
    hand the generated PDF/Excel/PPT/DOCX document to the user.

    Args:
        run_id: The matrix run id (as returned by /api/v1/runs).
        format: "pdf" (default), "xlsx", "pptx", or "docx".
        output_dir: Optional logical relative sub-directory under the server-fixed
            export root. Absolute paths and ``..`` are rejected. Default: export root.

    Returns:
        JSON string with keys ``format``, ``path``, ``cells`` (row count), or an
        error string starting with "Error:".
    """
    run = load_matrix_run(run_id)
    if run is None:
        return f"Error: matrix run {run_id!r} not found"

    cells = run.get("cells", [])
    try:
        out_dir = _confine_export_dir(output_dir)
    except ValueError as exc:
        return f"Error: {exc}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if format == "pdf":
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import inch
            from reportlab.pdfgen import canvas
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
            from reportlab.lib.styles import getSampleStyleSheet
        except ImportError:
            return "Error: reportlab is not installed (required for pdf export)"

        headers = ["document_name", "question", "answer", "quote", "citation_status", "page"]
        out_path = out_dir / f"matrix_run_{run_id}.pdf"
        doc = SimpleDocTemplate(str(out_path), pagesize=A4)
        styles = getSampleStyleSheet()
        body_style = styles["BodyText"]
        body_style.fontSize = 7
        story = [Paragraph(f"OpenMatrix Report — run {run_id}", styles["Title"]), Spacer(1, 0.2 * inch)]
        # Cells use Paragraph so long text wraps instead of overflowing the page.
        table_rows = [[Paragraph(safe_cell(cell.get(h, "")), body_style) for h in headers] for cell in cells]
        table_rows.insert(0, headers)
        table = Table(table_rows, repeatRows=1, colWidths=[1.2 * inch, 1.2 * inch, 1.3 * inch, 1.4 * inch, 0.7 * inch, 0.4 * inch])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(table)
        doc.build(story)
        return json.dumps({"format": "pdf", "path": str(out_path), "cells": len(cells)}, ensure_ascii=False)

    if format == "xlsx":
        try:
            from openpyxl import Workbook
        except ImportError:
            return "Error: openpyxl is not installed (required for xlsx export)"

        headers = ["document_name", "question", "answer", "quote", "citation_status", "page"]
        out_path = out_dir / f"matrix_run_{run_id}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "matrix"
        ws.append(headers)
        evidence = wb.create_sheet("evidence")
        evidence.append(["row", "document_name", "page", "citation_status", "quote"])
        for row_index, cell in enumerate(cells, start=2):
            ws.append([safe_cell(cell.get(h, "")) for h in headers])
            quote = safe_cell(cell.get("quote", ""))
            if quote:
                evidence.append([
                    row_index,
                    safe_cell(cell.get("document_name", "")),
                    safe_cell(cell.get("page", "")),
                    safe_cell(cell.get("citation_status", "")),
                    quote,
                ])
        wb.save(str(out_path))
        return json.dumps({"format": "xlsx", "path": str(out_path), "cells": len(cells)}, ensure_ascii=False)

    if format == "pptx":
        try:
            from pptx import Presentation  # type: ignore[import-not-found]
            from pptx.util import Inches, Pt
        except ImportError:
            return "Error: python-pptx is not installed (required for pptx export)"

        out_path = out_dir / f"matrix_run_{run_id}.pptx"
        prs = Presentation()
        # No standalone cover slide: every slide is a result slide carrying its
        # own citation status (acceptance criterion 3).
        for cell in cells:
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            title = slide.shapes.title
            title.text = safe_cell(cell.get("question", ""))
            title.text_frame.word_wrap = True
            body = slide.placeholders[1].text_frame
            lines = [
                f"Document: {safe_cell(cell.get('document_name', ''))}",
                f"Page: {cell.get('page', '')}",
                f"Answer: {safe_cell(cell.get('answer', ''))}",
                f"Quote: {safe_cell(cell.get('quote', ''))}",
            ]
            first = True
            for line in lines:
                para = body.paragraphs[0] if first else body.add_paragraph()
                para.text = line
                para.font.size = Pt(16)
                first = False
            # citation footnote anchored near the bottom of the slide
            from pptx.enum.text import PP_ALIGN

            footnote_box = slide.shapes.add_textbox(
                Inches(0.5), Inches(5.6), Inches(9.0), Inches(0.5)
            )
            tf = footnote_box.text_frame
            tf.paragraphs[0].text = (
                f"Citation: {safe_cell(cell.get('citation_status', ''))} | "
                f"Source: {safe_cell(cell.get('document_name', ''))} page {cell.get('page', '')}"
            )
            tf.paragraphs[0].font.size = Pt(11)
            tf.paragraphs[0].font.italic = True
            tf.paragraphs[0].alignment = PP_ALIGN.LEFT
        prs.save(str(out_path))
        return json.dumps({"format": "pptx", "path": str(out_path), "cells": len(cells)}, ensure_ascii=False)

    if format == "docx":
        try:
            from docx import Document  # type: ignore[import-not-found]
        except ImportError:
            return "Error: python-docx is not installed (required for docx export)"

        headers = ["document_name", "question", "answer", "quote", "citation_status", "page"]
        out_path = out_dir / f"matrix_run_{run_id}.docx"
        doc = Document()
        doc.add_heading(f"OpenMatrix Report - run {run_id}", level=1)
        table = doc.add_table(rows=1, cols=len(headers))
        try:
            table.style = "Light Grid Accent 1"
        except KeyError:
            pass  # style name missing from the default template - keep plain table
        hdr_cells = table.rows[0].cells
        for i, h in enumerate(headers):
            hdr_cells[i].text = h
        for cell in cells:
            row_cells = table.add_row().cells
            for i, key in enumerate(headers):
                row_cells[i].text = safe_cell(cell.get(key, ""))
        doc.save(str(out_path))
        return json.dumps({"format": "docx", "path": str(out_path), "cells": len(cells)}, ensure_ascii=False)

    return f"Error: unsupported format {format!r} (use 'pdf', 'xlsx', 'pptx', or 'docx')"


@mcp.tool()
def export_slides(
    run_id: str,
    output_dir: str | None = None,
    template_id: str | None = None,
) -> str:
    """Render a matrix run as a citation-footnoted PowerPoint deck (one slide per question).

    This is the structured 'Matrix -> Slides' deliverable: each question becomes a
    slide titled with the question and body containing the answer, with a
    citation footnote on every content slide carrying citation_status + page +
    the verbatim source quote. Pure deck-as-deliverable over existing matrix
    data; no grid/cell generation, no per-cell LLM loop, no attention overlay.

    Args:
        run_id: The matrix run id (as returned by /api/v1/runs).
        output_dir: Optional logical relative sub-directory under the server-fixed
            export root. Absolute paths and ``..`` are rejected. Default: export root.
        template_id: Optional template id (reserved for future themed decks; the
            current implementation renders a plain deck and ignores it).

    Returns:
        JSON string with keys ``format`` ("pptx"), ``path``, ``slides`` (slide
        count = number of questions), or an error string starting with "Error:".
    """
    run = load_matrix_run(run_id)
    if run is None:
        return f"Error: matrix run {run_id!r} not found"

    cells = run.get("cells", [])
    try:
        out_dir = _confine_export_dir(output_dir)
    except ValueError as exc:
        return f"Error: {exc}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from pptx import Presentation  # type: ignore[import-not-found]
        from pptx.util import Inches, Pt
        from pptx.enum.text import PP_ALIGN
    except ImportError:
        return "Error: python-pptx is not installed (required for slides export)"

    prs = Presentation()
    # Title-and-Content layout (index 1) on the default template.
    content_layout = prs.slide_layouts[1] if len(prs.slide_layouts) > 1 else prs.slide_layouts[0]

    for cell in cells:
        slide = prs.slides.add_slide(content_layout)
        if slide.shapes.title is not None:
            slide.shapes.title.text = safe_cell(cell.get("question", ""))
            slide.shapes.title.text_frame.word_wrap = True
        # Body placeholder holds the answer (kept compact for legibility).
        body_frame = None
        if len(slide.placeholders) > 1:
            try:
                body_frame = slide.placeholders[1].text_frame
            except KeyError:
                body_frame = None
        if body_frame is None:
            box = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(9.0), Inches(3.5))
            body_frame = box.text_frame
        body_frame.word_wrap = True
        first = True
        for line in (
            f"Answer: {safe_cell(cell.get('answer', ''))}",
            f"Source: {safe_cell(cell.get('document_name', ''))} (page {cell.get('page', '')})",
        ):
            para = body_frame.paragraphs[0] if first else body_frame.add_paragraph()
            para.text = line
            para.font.size = Pt(18)
            first = False

        # Citation footnote anchored near the bottom of every content slide:
        # carries citation_status + page + the verbatim source quote (structured
        # 'citations in slides' pattern). It is the ONLY place the quote is
        # reproduced verbatim, so the slide stays a citation-footnoted deck.
        footnote_box = slide.shapes.add_textbox(
            Inches(0.5), Inches(5.6), Inches(9.0), Inches(1.0)
        )
        tf = footnote_box.text_frame
        tf.word_wrap = True
        footnote = (
            f"Citation: {safe_cell(cell.get('citation_status', ''))} | "
            f"Page {cell.get('page', '')} | "
            f"Source verbatim: \"{safe_cell(cell.get('quote', ''))}\""
        )
        tf.paragraphs[0].text = footnote
        tf.paragraphs[0].font.size = Pt(11)
        tf.paragraphs[0].font.italic = True
        tf.paragraphs[0].alignment = PP_ALIGN.LEFT

    out_path = out_dir / f"matrix_slides_{run_id}.pptx"
    prs.save(str(out_path))
    return json.dumps(
        {"format": "pptx", "path": str(out_path), "slides": len(cells)},
        ensure_ascii=False,
    )


def _parse_context_blocks(context_text: str) -> list[dict]:
    """Parse ``select_retrieved_context``'s "[Page N | chunk_id]\ntext" blocks.

    Shared by retrieve_context / fetch_context / analyze_documents to keep the
    parse logic in one place.
    """
    selected = []
    for block in context_text.split("\n\n"):
        if not block.strip():
            continue
        first_line = block.splitlines()[0]
        body = "\n".join(block.splitlines()[1:])
        page = None
        chunk_id = None
        if first_line.startswith("[Page "):
            header = first_line[6:].rstrip("]")
            page_part, _, id_part = header.partition("|")
            try:
                page = int(page_part.strip())
            except ValueError:
                page = None
            chunk_id = id_part.strip() or None
        selected.append({
            "chunk_id": chunk_id,
            "page": page,
            "text": body.strip() if body else first_line,
        })
    return selected


@mcp.tool()
def analyze_documents(
    documents: list[dict],
    question: str,
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Analyze multiple documents against a question via independent retrieval.

    Multi-document orchestration (product priority ③): for each document in the
    input list, select the most relevant chunks using the same retrieval pipeline
    as ``retrieve_context`` (BM25 / dense / fusion). Documents are analyzed in
    isolation — this is the "document list + independent Q&A" pattern, which
    deliberately does **not** construct a grid of cells at the engineering level.
    This is an engineering-structure observation only and is **not** a patent
    non-infringement or Freedom-to-Operate legal opinion; formal FTO requires
    qualified counsel review against each target jurisdiction's claims. No LLM is
    called; no persistence happens.

    Args:
        documents: List of {"name": str, "pages": list[str]}. Each entry is one
            document to analyze.
        question: The question to retrieve context for, per document.
        mode: "bm25" (default), "dense", or "fusion". Invalid modes fall back to bm25.
        limit_per_question: Max chunks to select per document.

    Returns:
        JSON string with keys ``documents_count`` (int), ``results`` (list of
        {name, chunks, full_document, chars}) and ``mode`` (str), or an error
        string starting with "Error:".
    """
    if not question or not question.strip():
        return "Error: question must be non-empty"
    if not documents:
        return "Error: documents must be non-empty"

    results = []
    for document in documents:
        name = document.get("name", "unnamed")
        pages = document.get("pages") or []
        total_chars = sum(len(page) for page in pages)

        if not pages:
            results.append({
                "name": name,
                "chunks": [],
                "full_document": True,
                "chars": 0,
            })
            continue

        context_text, used_full_document = select_retrieved_context(
            [question],
            pages,
            limit_per_question=limit_per_question,
            mode=mode,
        )

        if used_full_document:
            results.append({
                "name": name,
                "chunks": [],
                "full_document": True,
                "chars": total_chars,
            })
            continue

        results.append({
            "name": name,
            "chunks": _parse_context_blocks(context_text),
            "full_document": False,
            "chars": total_chars,
        })

    return json.dumps({
        "documents_count": len(documents),
        "results": results,
        "mode": mode,
    }, ensure_ascii=False)


_FINANCIAL_OPERATIONS = {
    "sum",
    "average",
    "ratio",
    "percent_of_total",
    "currency_convert",
    "unit_convert",
}

# Deterministic unit scales for unit_convert / KPI unit normalization.
# Unit families: conversions are only valid WITHIN a family (magnitude vs percent).
_UNIT_SCALES = {
    "magnitude": {
        "thousand": 1e3,
        "million": 1e6,
        "billion": 1e9,
        "trillion": 1e12,
        "quadrillion": 1e15,
        "万": 1e4,
        "百万": 1e6,
        "亿": 1e8,
        "万亿": 1e12,
    },
    "percent": {
        "percent": 0.01,
        "%": 0.01,
    },
}
_UNIT_FAMILY_OF = {
    unit: family for family, table in _UNIT_SCALES.items() for unit in table
}
_ALL_UNITS = sorted(_UNIT_FAMILY_OF)

_KPI_NUMBER_RE = re.compile(
    r"(?<![\w])(-?\d[\d,]*(?:\.\d+)?)(?![0-9A-Za-z_])\s*(\b(?:million|billion|trillion|thousand|percent|USD|EUR|CNY)\b|%|万|亿|百|美元|元|人民币)?",
    re.IGNORECASE,
)


def _as_number(value) -> float:
    """Coerce a value to float, or raise ValueError for non-numeric input.

    Non-finite values (NaN/Infinity) are rejected so downstream arithmetic and
    ``json.dumps`` never emit non-standard JSON tokens.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a finite number: {value!r}")
    if isinstance(value, (int, float)):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"not a finite number: {value!r}")
        return result
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "").replace("¥", "").replace("€", "").replace("£", "")
        try:
            result = float(cleaned)
        except ValueError:
            raise ValueError(f"not a number: {value!r}")
        if not math.isfinite(result):
            raise ValueError(f"not a finite number: {value!r}")
        return result
    raise ValueError(f"not a number: {value!r}")


def _find_named_entry(values: list[dict], name: str) -> dict | None:
    """Return the first values entry whose ``name`` matches (case-insensitive)."""
    target = name.lower()
    for entry in values or []:
        if str(entry.get("name", "")).strip().lower() == target:
            return entry
    return None


@mcp.tool()
def compute_financials(values: list[dict], operation: str) -> str:
    """Run a deterministic financial calculation on supplied numeric values.

    No LLM is involved: every operation is a pure arithmetic computation on the
    ``values`` passed in, so the result is reproducible and auditable. Supported
    operations:

    - ``sum`` / ``average``: aggregate every entry's ``value``.
    - ``ratio``: exactly two entries; result = first / second (guards divide-by-zero).
    - ``percent_of_total``: exactly two entries; result = part / total * 100.
    - ``currency_convert``: entries named ``amount`` (with optional ``currency``)
      and ``rate`` (with optional ``to_currency``); result = amount * rate.
    - ``unit_convert``: entry named ``value`` (with ``unit``, e.g. "million") and
      entry named ``target_unit`` (value = target unit string, e.g. "billion");
      converts between the supported scale table.

    Args:
        values: List of {"name": str, "value": number, ...}. Extra keys (unit,
            currency, to_currency) are read by the operations that need them.
        operation: One of the supported operations above.

    Returns:
        JSON string with keys ``operation``, ``result`` and operation-specific
        terms, or an error string starting with "Error:".
    """
    try:
        op = (operation or "").strip().lower()
        if not op:
            return "Error: operation must be non-empty"
        if op not in _FINANCIAL_OPERATIONS:
            return f"Error: unsupported operation {op!r} (supported: {', '.join(sorted(_FINANCIAL_OPERATIONS))})"
        if not values:
            return "Error: values must be non-empty"
        for entry in values:
            if not isinstance(entry, dict):
                return "Error: each values entry must be an object with a 'name' and 'value'"

        if op in ("sum", "average"):
            nums = [_as_number(entry.get("value")) for entry in values]
            result = sum(nums)
            if op == "average":
                result = result / len(nums)
            if not math.isfinite(result):
                return "Error: sum/average produced a non-finite result"
            return json.dumps({
                "operation": op,
                "result": result,
                "terms": [entry.get("name", "") for entry in values],
            }, ensure_ascii=False)

        if op == "ratio":
            if len(values) != 2:
                return "Error: ratio requires exactly two values (numerator, denominator)"
            numerator = _as_number(values[0].get("value"))
            denominator = _as_number(values[1].get("value"))
            if denominator == 0:
                return "Error: ratio denominator must be non-zero"
            result = numerator / denominator
            if not math.isfinite(result):
                return "Error: ratio produced a non-finite result"
            return json.dumps({
                "operation": "ratio",
                "result": result,
                "numerator": numerator,
                "denominator": denominator,
            }, ensure_ascii=False)

        if op == "percent_of_total":
            if len(values) != 2:
                return "Error: percent_of_total requires exactly two values (part, total)"
            part = _as_number(values[0].get("value"))
            total = _as_number(values[1].get("value"))
            if total == 0:
                return "Error: percent_of_total total must be non-zero"
            result = part / total * 100.0
            if not math.isfinite(result):
                return "Error: percent_of_total produced a non-finite result"
            return json.dumps({
                "operation": "percent_of_total",
                "result": result,
                "part": part,
                "total": total,
            }, ensure_ascii=False)

        if op == "currency_convert":
            amount_entry = _find_named_entry(values, "amount")
            rate_entry = _find_named_entry(values, "rate")
            if amount_entry is None or rate_entry is None:
                return "Error: currency_convert requires entries named 'amount' and 'rate'"
            amount = _as_number(amount_entry.get("value"))
            rate = _as_number(rate_entry.get("value"))
            result = amount * rate
            if not math.isfinite(result):
                return "Error: currency_convert produced a non-finite result"
            return json.dumps({
                "operation": "currency_convert",
                "result": result,
                "amount": amount,
                "rate": rate,
                "from_currency": amount_entry.get("currency"),
                "to_currency": rate_entry.get("to_currency"),
            }, ensure_ascii=False)

        if op == "unit_convert":
            value_entry = _find_named_entry(values, "value")
            target_entry = _find_named_entry(values, "target_unit")
            if value_entry is None or target_entry is None:
                return "Error: unit_convert requires entries named 'value' (with unit) and 'target_unit'"
            source_unit = str(value_entry.get("unit", "") or "").strip().lower()
            target_unit = str(target_entry.get("value", "") or "").strip().lower()
            if source_unit not in _UNIT_FAMILY_OF:
                supported = ", ".join(_ALL_UNITS)
                return f"Error: unsupported source unit {source_unit!r} (supported: {supported})"
            if target_unit not in _UNIT_FAMILY_OF:
                supported = ", ".join(_ALL_UNITS)
                return f"Error: unsupported target unit {target_unit!r} (supported: {supported})"
            if _UNIT_FAMILY_OF[source_unit] != _UNIT_FAMILY_OF[target_unit]:
                return (
                    f"Error: cannot convert {source_unit!r} ({_UNIT_FAMILY_OF[source_unit]}) "
                    f"to {target_unit!r} ({_UNIT_FAMILY_OF[target_unit]}); only within-family "
                    "conversions are valid"
                )
            amount = _as_number(value_entry.get("value"))
            if not math.isfinite(amount):
                return f"Error: value must be finite, got {amount!r}"
            result = amount * _UNIT_SCALES[_UNIT_FAMILY_OF[source_unit]][source_unit] / _UNIT_SCALES[_UNIT_FAMILY_OF[target_unit]][target_unit]
            if not math.isfinite(result):
                return "Error: unit_convert produced a non-finite result"
            return json.dumps({
                "operation": "unit_convert",
                "result": result,
                "amount": amount,
                "source_unit": source_unit,
                "target_unit": target_unit,
            }, ensure_ascii=False)

        return "Error: operation not implemented"  # pragma: no cover
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: compute_financials failed: {exc}"


_YEAR_HINT_RE = re.compile(r"(?:FY|fiscal year|in)\s*((?:19|20)\d{2})", re.IGNORECASE)


def _is_year_context(text: str, number: float, match_pos: int) -> bool:
    if not (1900 <= number <= 2100 and number.is_integer()):
        return False
    window = text[max(0, match_pos - 45):match_pos + 45]
    if re.search(r"(?:December|fiscal|FY|year ended|\bin\b)", window, re.I):
        return True
    return bool(re.search(r"(?:19|20)\d{2}\s+(?:19|20)\d{2}", window))


def _extract_kpi_value(name: str, text: str) -> dict | None:
    """Prefer labeled financial rows and explicit units; skip calendar qualifiers."""
    candidates = []
    scale_match = re.search(r"in\s+(thousands|millions|billions)", text, re.I)
    page_scale = scale_match.group(1).lower() if scale_match else None
    scales = {"thousands":1e3,"millions":1e6,"billions":1e9,
              "thousand":1e3,"million":1e6,"billion":1e9,"trillion":1e12,
              "万":1e4,"亿":1e8,"百":1e2}
    percent_metric = bool(re.search(r"same[- ](?:shack|store)|growth|margin", name, re.I))
    for label in re.finditer(re.escape(name), text, re.I):
        prefix = text[max(0,label.start()-80):label.start()]
        if re.search(r"percentage\s+of\s*$", prefix, re.I):
            continue
        end = text.find("\n", label.end())
        if end < 0 or end == label.end():
            end = min(len(text), label.end()+240)
        window = text[label.end():min(end,label.end()+240)]
        numbers = []
        for match in _KPI_NUMBER_RE.finditer(window):
            raw,unit = match.group(1),(match.group(2) or "").strip()
            pos = label.end()+match.start(1)
            if pos > 0 and text[pos-1].isalpha():
                continue
            value = float(raw.replace(",",""))
            currency = bool(re.search(r"[$€¥£]\s*$",text[max(label.end(),pos-3):pos]))
            if not currency and not unit:
                before = text[max(0,pos-16):pos]
                after = text[pos+len(raw):pos+len(raw)+10]
                if 1 <= value <= 31 and re.search(r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*$",before,re.I) and re.match(r"\s*,?\s*(?:19|20)\d{2}",after):
                    continue
                if _is_year_context(text,value,pos) or (1900 <= value <= 2100 and value.is_integer() and "," not in raw):
                    continue
            numbers.append((match,raw,unit,pos,value,currency))
        if not numbers:
            continue
        formatted = [entry for entry in numbers if entry[5] or entry[2] or "," in entry[1]]
        percentages = [entry for entry in numbers if entry[2].lower() in {"%","percent"}]
        chosen = (percentages if percent_metric and percentages else formatted or numbers)[0]
        match,raw,unit,pos,value,currency = chosen
        row_start = not text[text.rfind("\n",0,label.start())+1:label.start()].strip()
        before_number = text[label.end():pos]
        direct = bool(re.fullmatch(r"\d{0,2}\s*(?:growth\s*)?(?:\(%\)\s*)?[$€¥£]?\s*",before_number,re.I))
        parent_income = name.lower() == "net income" and bool(re.fullmatch(
            r"\s*attributable\s+to\s+[^$0-9]+[$€¥£]?\s*", before_number, re.I)) and not re.search(r"non[- ]controlling", before_number, re.I)
        if parent_income:
            direct = True
        if percent_metric and not percentages and not (direct and "." in raw):
            continue
        period_context = before_number if re.search(r"quarter|fiscal year|year ended",before_number,re.I) else prefix+window
        quarter = bool(re.search(r"quarter",period_context,re.I))
        annual = bool(re.search(r"fiscal year|year ended",period_context,re.I)) and not quarter
        score = (30 if row_start else 0)+(20 if currency or unit or "," in raw else 0)+(20 if direct else 0)+(25 if annual else 0)+(35 if parent_income else 0)-(80 if quarter else 0)-match.start()/10
        scale = "percent" if unit.lower() in {"%","percent"} else unit.lower() if unit.lower() in scales else page_scale
        confidence = "high" if row_start and direct and (currency or unit or "," in raw) else "medium"
        hit = {"value":value,"unit":unit or ("USD" if currency else ""),
               "context":text[max(0,label.start()-20):end].strip(),"pos":pos,
               "scale":scale or "units","normalized_value":value*scales.get(scale,1),
               "confidence":confidence,"needs_review":confidence!="high",
               "other_columns":[entry[4] for entry in numbers if entry[3]>pos],
               "period":"quarter" if quarter else "annual" if annual else "unspecified",
               "metric_label":text[label.start():pos].strip().rstrip("$€¥£ "),
               "_score":score}
        candidates.append((score,hit))
    if not candidates:
        return None
    candidates.sort(key=lambda item:-item[0])
    best = candidates[0][1]
    if len(candidates)>1 and candidates[0][0]==candidates[1][0] and best["value"]!=candidates[1][1]["value"]:
        best.update(confidence="low",needs_review=True)
    return best


@mcp.tool()
def extract_kpis(
    pages: list[str],
    kpi_names: list[str],
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Extract KPI values from pages, reusing the existing retrieval pipeline.

    For each KPI name the same chunked retrieval pipeline as ``retrieve_context``
    (BM25 / dense / fusion) selects the most relevant chunks, then the nearest
    numeric value (with unit and context) is extracted deterministically. No LLM
    is called and no grid/cell structure is built.

    Args:
        pages: List of document page texts (one entry per page).
        kpi_names: List of KPI names to look for (e.g. ["revenue", "headcount"]).
        mode: "bm25" (default), "dense", or "fusion". Invalid modes fall back to bm25.
        limit_per_question: Max chunks to select per KPI.

    Returns:
        JSON string with keys ``kpis`` (list of {name, full_document, values})
        and ``mode``, or an error string starting with "Error:".
    """
    if not isinstance(pages, list) or not pages:
        return "Error: pages must be a non-empty list of strings"
    if any(not isinstance(p, str) for p in pages):
        return "Error: each page must be a string"
    if not isinstance(kpi_names, list) or not kpi_names:
        return "Error: kpi_names must be a non-empty list of strings"
    if any(not isinstance(n, str) for n in kpi_names):
        return "Error: each KPI name must be a string"
    if isinstance(limit_per_question, bool) or not isinstance(limit_per_question, int) or limit_per_question < 1:
        return "Error: limit_per_question must be a positive integer"
    # normalize/whitelist retrieval mode so reported mode matches actual mode
    if mode not in ("bm25", "dense", "fusion"):
        mode = "bm25"
    names = [name.strip() for name in kpi_names]
    names = [name for name in names if name]
    if not names:
        return "Error: kpi_names must contain at least one non-empty name"

    results = []
    for name in names:
        try:
            context_text, used_full_document = select_retrieved_context(
                [name],
                pages,
                limit_per_question=limit_per_question,
                mode=mode,
            )
            blocks = _parse_context_blocks(context_text) if not used_full_document else []
            found = []
            selected_pages = set(range(1,len(pages)+1)) if used_full_document else {
                block["page"] for block in blocks if isinstance(block.get("page"),int) and 1 <= block["page"] <= len(pages)}
            # Literal occurrences supplement sparse recall of annual tables.
            selected_pages.update(index for index,page in enumerate(pages,1) if re.search(re.escape(name),page,re.I))
            for page_number in sorted(selected_pages):
                hit = _extract_kpi_value(name,pages[page_number-1])
                if hit:
                    hit["page"] = page_number
                    found.append(hit)
            found.sort(key=lambda hit:-hit.get("_score",0))
            if not used_full_document:
                found = found[:limit_per_question]
            for hit in found:
                hit.pop("_score",None)
            results.append({
                "name": name,
                "full_document": used_full_document,
                "values": found,
            })
        except Exception as exc:
            return f"Error: extract_kpis failed for {name!r}: {exc}"

    return json.dumps({"kpis": results, "mode": mode}, ensure_ascii=False)

_AKSHARE_MISSING_MSG = "akshare is not installed (required for ak_* tools)"


def _import_akshare():
    """Lazily import akshare; raises ImportError with a contract message if absent."""
    try:
        import akshare as ak  # type: ignore[import-not-found]
        return ak
    except ImportError:
        raise ImportError(_AKSHARE_MISSING_MSG)


@mcp.tool()
def ak_stock_quote(code: str) -> str:
    """Return a realtime quote for a China A-share stock by 6-digit code.

    Uses AkShare's full-market snapshot (Eastmoney source). Key-free.

    Args:
        code: 6-digit A-share code (e.g. "600519").

    Returns:
        JSON string with keys ``code``, ``name``, ``price``, ``change``,
        ``change_pct``, ``open``, ``high``, ``low``, ``close``, ``volume``,
        ``amount``, ``turnover``, or an error string starting with "Error:".
    """
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json

    code = (code or "").strip()
    if not code:
        return "Error: code must be non-empty"
    try:
        ak = _import_akshare()
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return "Error: empty A-share snapshot"
        match = df[df["代码"].astype(str) == code]
        if match.empty:
            return f"Error: code {code!r} not found in A-share snapshot"
        r = match.iloc[0]

        def _get(col):
            if col not in df.columns:
                return None
            val = r.get(col)
            if hasattr(val, "item"):
                try:
                    return val.item()
                except Exception:
                    return val
            return val

        return _json.dumps({
            "code": code,
            "name": str(_get("名称")),
            "price": _get("最新价"),
            "change": _get("涨跌额"),
            "change_pct": _get("涨跌幅"),
            "open": _get("今开"),
            "high": _get("最高"),
            "low": _get("最低"),
            "close": _get("昨收"),
            "volume": _get("成交量"),
            "amount": _get("成交额"),
            "turnover": _get("换手率"),
        }, ensure_ascii=False)
    except ImportError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: failed to fetch A-share snapshot: {exc}"


@mcp.tool()
def ak_financials(code: str) -> str:
    """Return key financial-statement figures for a China A-share stock.

    Uses AkShare's THS financial abstract (per-report-period). Key-free.

    Args:
        code: 6-digit A-share code (e.g. "600519").

    Returns:
        JSON string with keys ``code`` and ``abstract`` (list of rows, most
        recent first), or an error string starting with "Error:".
    """
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json

    code = (code or "").strip()
    if not code:
        return "Error: code must be non-empty"
    try:
        ak = _import_akshare()
        df = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
        if df is None or df.empty:
            return f"Error: no financial data for code {code!r}"
        cols = list(df.columns)
        rows = []
        for _, row in df.head(8).iterrows():
            item = {}
            for col in cols:
                val = row.get(col)
                if hasattr(val, "item"):
                    try:
                        val = val.item()
                    except Exception:
                        pass
                item[str(col)] = val
            rows.append(item)
        return _json.dumps({"code": code, "abstract": rows}, ensure_ascii=False)
    except ImportError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: failed to fetch financials for {code!r}: {exc}"


def _fred_api_key() -> str:
    key = os.environ.get("OPENMATRIX_FRED_API_KEY", "")
    return key.strip()


@mcp.tool()
def macro_fred_lookup(series_id: str, start: str | None = None, end: str | None = None) -> str:
    """Return a FRED macro time series (observations) for a series id.

    Uses the official St. Louis Fed API. The API key is user-supplied via the
    OPENMATRIX_FRED_API_KEY environment variable; returns an error if missing.

    Args:
        series_id: FRED series id (e.g. "GDP", "CPIAUCSL", "DGS10").
        start: Optional start date "YYYY-MM-DD".
        end: Optional end date "YYYY-MM-DD".

    Returns:
        JSON string with keys ``series_id``, ``observations`` (list of
        {date, value}), or an error string starting with "Error:".
    """
    if not feature_flag("DATA_SOURCES", default=True):
        return "Error: disabled by feature flag OPENMATRIX_ENABLE_DATA_SOURCES"
    import json as _json
    import urllib.parse as _urlparse

    series_id = (series_id or "").strip()
    if not series_id:
        return "Error: series_id must be non-empty"
    key = _fred_api_key()
    if not key:
        return "Error: FRED API key not set (OPENMATRIX_FRED_API_KEY)"

    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
    }
    if start:
        params["observation_start"] = start
    if end:
        params["observation_end"] = end
    query = _urlparse.urlencode(params)
    url = f"https://api.stlouisfed.org/fred/series/observations?{query}"

    def _safe_error(exc) -> str:
        # Never echo the URL back: it contains the API key.
        return f"Error: failed to fetch FRED series {series_id!r}: {type(exc).__name__}"

    try:
        raw = _fetch_bytes(url, user_agent="OpenMatrix-MCP/1.0 (MCP FRED data-source)")
        data = _json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return "Error: FRED API returned an unexpected response shape"
        if "error_message" in data:
            # Never reflect remote error text verbatim: it may echo the URL/key.
            return "Error: FRED API rejected the request (see server logs)"
        obs = data.get("observations")
        if obs is None or not isinstance(obs, list):
            return "Error: FRED API response has no valid observations list"
        out = []
        for o in obs:
            if not isinstance(o, dict):
                continue
            val = o.get("value")
            if val is None or str(val).strip().lower() in (".", "null"):
                continue
            out.append({"date": o.get("date"), "value": val})
        return _json.dumps({"series_id": series_id, "observations": out}, ensure_ascii=False)
    except ValueError as exc:
        return _safe_error(exc)
    except Exception as exc:
        return _safe_error(exc)


@mcp.tool()
def financial_model(
    pages: list[str],
    steps: list[dict],
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Build a list-form financial model where each row carries a citation.

    For each modeling step, the KPI name is located in the pages via the
    retrieval pipeline and the nearest numeric value is extracted with its
    source quote + page. Output is a flat LIST of rows - never a grid/cell
    structure (patent guardrail).

    Args:
        pages: List of document page texts.
        steps: List of {"name": str, "kpi": str} - e.g.
            [{"name": "revenue", "kpi": "revenue"},
             {"name": "debt_to_ebitda", "kpi": "debt"}].
        mode: retrieval mode ("bm25" default).
        limit_per_question: max chunks per KPI.

    Returns:
        JSON list of {name, kpi, figure, unit, source_quote, page,
        citation_status}, or an error string starting with "Error:".
    """
    import json as _json

    if not isinstance(pages, list) or not pages:
        return "Error: pages must be a non-empty list of strings"
    if any(not isinstance(p, str) for p in pages):
        return "Error: each page must be a string"
    if not isinstance(steps, list) or not steps:
        return "Error: steps must be a non-empty list"
    if any(not isinstance(s, dict) for s in steps):
        return "Error: each step must be an object with 'name' and 'kpi'"
    if not isinstance(limit_per_question, int) or limit_per_question < 1:
        return "Error: limit_per_question must be a positive integer"
    if mode not in ("bm25", "dense", "fusion"):
        mode = "bm25"

    rows = []
    for step in steps:
        name = step.get("name")
        kpi = step.get("kpi")
        if not isinstance(name, str) or not name.strip():
            return "Error: each step needs a non-empty string 'name'"
        if not isinstance(kpi, str) or not kpi.strip():
            return "Error: each step needs a non-empty string 'kpi'"
        name = name.strip()
        kpi = kpi.strip()
        try:
            context_text, used_full_document = select_retrieved_context(
                [kpi], pages, limit_per_question=limit_per_question, mode=mode
            )
            hit = None
            if used_full_document:
                for page_number, page_text in enumerate(pages, start=1):
                    hit = _extract_kpi_value(kpi, page_text)
                    if hit:
                        hit["page"] = page_number
                        break
            else:
                for block in _parse_context_blocks(context_text):
                    hit = _extract_kpi_value(kpi, block.get("text", ""))
                    if hit:
                        hit["page"] = block.get("page")
                        break
            if not hit:
                rows.append({
                    "name": name, "kpi": kpi, "figure": None, "unit": None,
                    "source_quote": None, "page": None,
                    "citation_status": "not_found",
                })
                continue
            # Verify the emitted quote against the ORIGINAL cited page (exact),
            # not against itself - otherwise any quote tautologically "matches".
            quote_text = hit.get("context", "")
            quote_status = "not_found"
            page = hit.get("page")
            try:
                if isinstance(page, int) and 1 <= page <= len(pages):
                    vr = _json.loads(verify_quote(quote_text, [pages[page - 1]], mode="exact"))
                    if vr.get("citation_status") == "verified_exact":
                        quote_status = "verified_exact"
                    else:
                        quote_status = "needs_review"
                else:
                    quote_status = "needs_review"
            except Exception:
                quote_status = "needs_review"
            rows.append({
                "name": name, "kpi": kpi,
                "figure": hit.get("value"), "unit": hit.get("unit"),
                "source_quote": quote_text.strip()[:300] if quote_text else None,
                "page": page,
                "citation_status": quote_status,
            })
        except Exception as exc:
            return f"Error: financial_model failed on step {name!r}: {exc}"

    return _json.dumps(rows, ensure_ascii=False)


@mcp.tool()
def decompose_analysis(
    question: str,
    pages: list[str],
    topics: list[str],
    mode: str = "bm25",
    limit_per_question: int = 2,
) -> str:
    """Decompose an analysis question into a flat list of cited evidence blocks.

    For each sub-topic, the retrieval pipeline locates the most relevant document
    chunk(s); each output block carries topic, quote, page and a citation status
    verified against the ORIGINAL cited page (exact match). Output is a flat
    LIST of blocks - never a grid/cell structure (patent guardrail).

    Args:
        question: The overall analysis question.
        pages: List of document page texts.
        topics: Sub-topics to decompose the question into, e.g.
            ["revenue trend", "capex breakdown"].
        mode: retrieval mode ("bm25" default).
        limit_per_question: max chunks per topic.

    Returns:
        JSON list of {topic, quote, page, citation_status, full_document}, or an
        error string starting with "Error:".
    """
    import json as _json

    if not isinstance(question, str) or not question.strip():
        return "Error: question must be a non-empty string"
    if not isinstance(pages, list) or not pages or any(not isinstance(p, str) for p in pages):
        return "Error: pages must be a non-empty list of strings"
    if not isinstance(topics, list) or not topics:
        return "Error: topics must be a non-empty list"
    if any(not isinstance(t, str) for t in topics):
        return "Error: each topic must be a string"
    if any(not t.strip() for t in topics):
        return "Error: each topic must be non-empty"
    if not isinstance(limit_per_question, int) or limit_per_question < 1:
        return "Error: limit_per_question must be a positive integer"
    if mode not in ("bm25", "dense", "fusion"):
        mode = "bm25"

    blocks = []
    for topic in topics:
        topic = topic.strip()
        try:
            context_text, used_full_document = select_retrieved_context(
                [topic], pages, limit_per_question=limit_per_question, mode=mode
            )
            if used_full_document:
                candidates = []
                terms = topic.lower().split()
                for page_number, page_text in enumerate(pages, start=1):
                    page_lower = page_text.lower()
                    score = sum(page_lower.count(t) for t in terms)
                    if score > 0:
                        first_idx = min(page_lower.find(t) for t in terms if t in page_lower)
                        start = max(0, first_idx - 50)
                        snippet = page_text[start:start + 400].strip()
                        if snippet:
                            candidates.append({
                                "topic": topic, "quote": snippet, "page": page_number,
                                "full_document": True,
                                "_score": score,
                            })
                candidates.sort(key=lambda c: c["_score"], reverse=True)
                candidates = candidates[:limit_per_question]
                for c in candidates:
                    c.pop("_score", None)
            else:
                candidates = []
                for block in _parse_context_blocks(context_text):
                    text = block.get("text", "").strip()
                    if not text:
                        continue
                    candidates.append({
                        "topic": topic, "quote": text[:400],
                        "page": block.get("page"), "full_document": False,
                    })
                candidates = candidates[:limit_per_question]
            if not candidates:
                blocks.append({
                    "topic": topic, "quote": None, "page": None,
                    "citation_status": "not_found", "full_document": False,
                })
                continue
            for cand in candidates:
                quote_text = cand["quote"]
                page = cand["page"]
                status = "not_found"
                if isinstance(page, int) and 1 <= page <= len(pages):
                    try:
                        vr = _json.loads(verify_quote(quote_text, [pages[page - 1]], mode="exact"))
                        status = "verified_exact" if vr.get("citation_status") == "verified_exact" else "needs_review"
                    except Exception:
                        status = "needs_review"
                else:
                    status = "needs_review"
                cand["citation_status"] = status
                blocks.append(cand)
        except Exception as exc:
            return f"Error: decompose_analysis failed on topic {topic!r}: {exc}"

    return _json.dumps(blocks, ensure_ascii=False)


@mcp.tool()
def schedule_research(
    cron: str,
    objective: str,
    research_id: str,
    context: dict | None = None,
) -> str:
    """Register a scheduled research run (H7).

    Uses a 5-field cron expression (``minute hour day-of-month month
    day-of-week``, e.g. ``*/5 * * * *`` = every 5 minutes). When due, a
    ``research_run`` is launched with the given objective. Output is a flat
    report object - never a grid/cell structure (patent guardrail).

    Args:
        cron: 5-field cron expression.
        objective: Research objective for the scheduled run.
        research_id: Stable id so the run can be tracked/cancelled.
        context: Optional run context dict.

    Returns:
        JSON {"schedule_id", "cron", "objective", "research_id", "error"},
        or an error string starting with "Error:".
    """
    import json as _json

    if not isinstance(cron, str) or not cron.strip():
        return "Error: cron must be a non-empty string"
    if not isinstance(objective, str) or not objective.strip():
        return "Error: objective must be a non-empty string"
    if not isinstance(research_id, str) or not research_id.strip():
        return "Error: research_id must be a non-empty string"
    if context is not None and not isinstance(context, dict):
        return "Error: context must be an object"

    manager = default_manager(_schedule_callback)
    entry = manager.register(research_id, cron, objective, research_id, context)
    if entry is None:
        return "Error: invalid cron expression or schedule already registered"
    manager.start_ticker()
    return _json.dumps({
        "schedule_id": research_id,
        "cron": cron,
        "objective": objective,
        "research_id": research_id,
        "error": None,
    }, ensure_ascii=False)


@mcp.tool()
def list_schedules() -> str:
    """List registered scheduled research runs (H7)."""
    import json as _json

    return _json.dumps(default_manager().list(), ensure_ascii=False)


@mcp.tool()
def cancel_schedule(schedule_id: str) -> str:
    """Cancel a scheduled research run by id (H7)."""
    import json as _json

    if not isinstance(schedule_id, str) or not schedule_id.strip():
        return "Error: schedule_id must be a non-empty string"
    removed = default_manager().unregister(schedule_id)
    if not removed:
        return f"Error: unknown schedule_id '{schedule_id}'"
    return _json.dumps({"schedule_id": schedule_id, "removed": True}, ensure_ascii=False)


def _schedule_callback(schedule_id: str, entry) -> None:
    """Fire a scheduled research run via research_run (runs in ticker thread).

    Both construction and waiting run inside ``asyncio.run`` so that
    ``research_run`` always has a running event loop available.
    """
    import asyncio

    async def _run() -> None:
        controller = research_run(entry.research_id, entry.objective, entry.context)
        await controller.wait()

    asyncio.run(_run())


@mcp.tool()
def project_create(name: str, description: str | None = None) -> str:
    """Create a document-library project (P4-2/H2).

    Projects are the top-level permissioned workspace unit. Output is a flat
    report object - never a grid/cell structure (patent guardrail).

    Args:
        name: Human-readable project name.
        description: Optional project description.

    Returns:
        JSON project object, or an error string starting with "Error:".
    """
    import asyncio
    import json as _json
    import uuid

    if not isinstance(name, str) or not name.strip():
        return "Error: name must be a non-empty string"
    if description is not None and not isinstance(description, str):
        return "Error: description must be a string or None"

    project_id = "p-" + uuid.uuid4().hex[:12]
    try:
        proj = asyncio.run(default_library_store().create_project(project_id, name, description))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps(proj, ensure_ascii=False)


@mcp.tool()
def project_list() -> str:
    """List all document-library projects (P4-2/H2)."""
    import asyncio
    import json as _json

    try:
        projects = asyncio.run(default_library_store().list_projects())
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"projects": projects}, ensure_ascii=False)


@mcp.tool()
def project_delete(project_id: str) -> str:
    """Delete a project and its documents (P4-2/H2)."""
    import asyncio
    import json as _json

    if not isinstance(project_id, str) or not project_id.strip():
        return "Error: project_id must be a non-empty string"
    try:
        removed = asyncio.run(default_library_store().delete_project(project_id))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"project_id": project_id, "removed": removed}, ensure_ascii=False)


@mcp.tool()
def document_add(
    project_id: str,
    filename: str,
    content: str,
    page_count: int = 0,
    index: bool = True,
    async_mode: bool = False,
) -> str:
    """Add a document to a project; optionally index it for retrieval (P4-2/H2).

    Content is stored on local disk and, when ``index`` is true, split into
    chunks and upserted into the project's vector collection for
    ``project_search``. Output is a flat report object.

    When ``async_mode`` is true, the add + index work is enqueued on the
    shared ``TaskManager`` and this call returns a task_id immediately; poll
    ``task_status`` for completion (P4-3).

    Args:
        project_id: Target project id.
        filename: Document filename.
        content: Full document text.
        page_count: Optional page count (default 0).
        index: Whether to build the vector index (default true).
        async_mode: Enqueue as a background task and return task_id (default false).

    Returns:
        JSON document object, or a task_id JSON when async_mode, or an error
        string starting with "Error:".
    """
    import asyncio
    import json as _json
    import uuid

    if not isinstance(project_id, str) or not project_id.strip():
        return "Error: project_id must be a non-empty string"
    if not isinstance(filename, str) or not filename.strip():
        return "Error: filename must be a non-empty string"
    if not isinstance(content, str) or not content:
        return "Error: content must be a non-empty string"

    doc_id = "d-" + uuid.uuid4().hex[:12]

    async def _add_and_index(payload):
        store = default_library_store()
        await store.add_document(doc_id, project_id, filename, content, page_count)
        if index:
            await _index_document(store, doc_id, content)
            return await store.get_document(doc_id)
        return await store.get_document(doc_id)

    if async_mode:
        task_id = default_task_manager().submit(
            "document_add", {"doc_id": doc_id}, _add_and_index
        )
        return _json.dumps({"task_id": task_id, "doc_id": doc_id, "async": True}, ensure_ascii=False)

    try:
        doc = asyncio.run(_add_and_index(None))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps(doc, ensure_ascii=False)


@mcp.tool()
def document_list(project_id: str | None = None) -> str:
    """List documents, optionally filtered by project (P4-2/H2)."""
    import asyncio
    import json as _json

    if project_id is not None and not isinstance(project_id, str):
        return "Error: project_id must be a string or None"
    try:
        docs = asyncio.run(default_library_store().list_documents(project_id))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"documents": docs}, ensure_ascii=False)


@mcp.tool()
def document_delete(document_id: str) -> str:
    """Delete a document from a project (P4-2/H2)."""
    import asyncio
    import json as _json

    if not isinstance(document_id, str) or not document_id.strip():
        return "Error: document_id must be a non-empty string"
    try:
        removed = asyncio.run(default_library_store().delete_document(document_id))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"document_id": document_id, "removed": removed}, ensure_ascii=False)


@mcp.tool()
def document_update(document_id: str, filename: str, content: str, page_count: int = 0) -> str:
    """Versioned overwrite of a document (P4.4).

    Snapshots the current content before replacing it; every state is
    recoverable via ``document_versions`` / ``document_restore``. Output is a
    flat report object - never grid/cell.

    Args:
        document_id: Existing document id.
        filename: New filename.
        content: New full text.
        page_count: Optional new page count.

    Returns:
        JSON document object (with ``version``), or an error string.
    """
    import asyncio
    import json as _json

    if not isinstance(document_id, str) or not document_id.strip():
        return "Error: document_id must be a non-empty string"
    if not isinstance(filename, str) or not filename.strip():
        return "Error: filename must be a non-empty string"
    if not isinstance(content, str) or not content:
        return "Error: content must be a non-empty string"
    try:
        doc = asyncio.run(default_library_store().update_document(document_id, filename, content, page_count))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps(doc, ensure_ascii=False)


@mcp.tool()
def document_versions(document_id: str) -> str:
    """List a document's version history (P4.4).

    Returns a flat LIST of {version, filename, created_at} - never grid/cell;
    internal content paths are never exposed.
    """
    import asyncio
    import json as _json

    if not isinstance(document_id, str) or not document_id.strip():
        return "Error: document_id must be a non-empty string"
    try:
        versions = asyncio.run(default_library_store().list_versions(document_id))
    except Exception as exc:
        return f"Error: {exc}"
    out = [
        {
            "version": v["version"],
            "filename": v.get("filename"),
            "created_at": v["created_at"],
        }
        for v in versions
    ]
    return _json.dumps(out, ensure_ascii=False)


@mcp.tool()
def document_restore(document_id: str, version: int) -> str:
    """Restore a document to a prior snapshot (P4.4)."""
    import asyncio
    import json as _json

    if not isinstance(document_id, str) or not document_id.strip():
        return "Error: document_id must be a non-empty string"
    if not isinstance(version, int) or version < 1:
        return "Error: version must be a positive integer"
    try:
        doc = asyncio.run(default_library_store().restore_version(document_id, version))
    except Exception as exc:
        return f"Error: {exc}"
    if doc is None:
        return f"Error: version {version} not found for document '{document_id}'"
    return _json.dumps(doc, ensure_ascii=False)


@mcp.tool()
def project_search(
    project_id: str,
    question: str,
    limit: int = 3,
    mode: str = "bm25",
) -> str:
    """Search a project's indexed documents for relevant evidence (H2).

    Runs the retrieval pipeline (BM25 sparse, optional BGE dense / reranker)
    against the project's documents and returns a flat LIST of cited evidence
    hits - never a grid/cell structure (patent guardrail).

    Args:
        project_id: Target project id.
        question: Search question.
        limit: Max hits to return (default 3).

    Returns:
        JSON {"hits": [...]} or an error string starting with "Error:".
    """
    import asyncio
    import json as _json

    if not isinstance(project_id, str) or not project_id.strip():
        return "Error: project_id must be a non-empty string"
    if not isinstance(question, str) or not question.strip():
        return "Error: question must be a non-empty string"
    if not isinstance(limit, int) or limit < 1:
        return "Error: limit must be a positive integer"
    if mode not in ("bm25", "dense", "fusion"):
        return "Error: mode must be one of 'bm25', 'dense', 'fusion'"

    try:
        hits = asyncio.run(_search_project(project_id, question, limit, mode=mode))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"hits": hits}, ensure_ascii=False)


async def _index_document(store, doc_id: str, content: str) -> None:
    """Mark a document as indexed (ready for project_search).

    Retrieval in ``project_search`` builds the sparse (BM25) index on demand
    from the project's documents, so no separate dense vector write is needed
    here. The ``indexed`` status is tracked for observability; dense vector
    enrichment lands with the P4 enhancement (persistent vector store).
    """
    if not content.strip():
        return
    await store.update_document_status(doc_id, "indexed")


async def _search_project(project_id: str, question: str, limit: int, mode: str = "bm25") -> list[dict]:
    """Search indexed project documents with physical-page provenance."""
    import json as _json
    store = default_library_store()
    if await store.get_project(project_id) is None:
        raise ValueError(f"project not found: {project_id}")
    docs = [d for d in await store.list_documents(project_id) if d.get("status") == "indexed"]
    all_pages, provenance = [], []
    for doc in docs:
        content = await store.get_document_content(doc["id"])
        if not content or not content.strip():
            continue
        for location in _split_pages(content):
            all_pages.append(location["text"])
            provenance.append((doc, location, location["text"]))
    if not all_pages:
        return []
    context_text, _ = select_retrieved_context([question], all_pages, limit_per_question=limit, mode=mode)
    hits = []
    for block in _parse_context_blocks(context_text):
        quote, global_page = block.get("text", "").strip(), block.get("page")
        if not quote or not isinstance(global_page, int) or not 1 <= global_page <= len(provenance):
            continue
        doc, location, page_text = provenance[global_page - 1]
        if not page_text.strip():
            continue
        try:
            verified = _json.loads(verify_quote(quote, [page_text], mode="exact"))
            status = "verified_exact" if verified.get("citation_status") == "verified_exact" else "needs_review"
        except Exception:
            status = "needs_review"
        hit = {"document_id": doc["id"], "filename": doc.get("filename"),
               "page": location.get("page"), "quote": quote[:400], "citation_status": status}
        if location.get("section") is not None:
            hit["section"] = location["section"]
        hits.append(hit)
        if len(hits) >= limit:
            break
    return hits[:limit]


def _split_pages(content: str, max_chars: int = 2000) -> list[dict]:
    """Split form-feed text into physical pages; otherwise use unpaged sections."""
    if not content or not content.strip():
        return []
    if "\f" in content:
        pages = content.split("\f")
        if pages[-1] == "":
            pages.pop()
        return [{"text": page.strip(), "page": number, "section": None}
                for number, page in enumerate(pages, 1)]
    sections = [section.strip() for section in content.split("\n\n") if section.strip()]
    result = []
    for section_number, section in enumerate(sections, 1):
        chunks = [section] if len(section) <= max_chars else [
            section[i:i + max_chars].strip() for i in range(0, len(section), max_chars)]
        result.extend({"text": chunk, "page": None, "section": section_number}
                      for chunk in chunks if chunk)
    return result


@mcp.tool()
async def apikey_create(name: str, master_key: str | None = None) -> str:
    """Create a server-side API key (H4).

    The full secret is returned ONCE here; only its hash + prefix are stored.
    Requires ``OPENMATRIX_MASTER_KEY`` to be set and a matching ``master_key``.

    Args:
        name: Key name (1-64 chars of [a-zA-Z0-9 _-]).
        master_key: Server admin key (OPENMATRIX_MASTER_KEY).

    Returns:
        JSON {"id", "name", "prefix", "created_at", "secret"} (secret once),
        or an error string starting with "Error:".
    """
    import json as _json

    if not isinstance(name, str) or not name.strip():
        return "Error: name must be a non-empty string"
    if not require_master_key(master_key):
        return "Error: OPENMATRIX_MASTER_KEY required and must match"
    try:
        rec = await default_key_store().create_key(name)
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps(rec, ensure_ascii=False)


@mcp.tool()
async def apikey_list(master_key: str | None = None) -> str:
    """List server-side API key metadata (never secrets) (H4).

    Requires ``OPENMATRIX_MASTER_KEY`` to be set and a matching ``master_key``.
    """
    import json as _json

    if not require_master_key(master_key):
        return "Error: OPENMATRIX_MASTER_KEY required and must match"
    try:
        keys = await default_key_store().list_keys()
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"api_keys": keys}, ensure_ascii=False)


@mcp.tool()
async def apikey_revoke(key_id: str, master_key: str | None = None) -> str:
    """Revoke a server-side API key (H4).

    Requires ``OPENMATRIX_MASTER_KEY`` to be set and a matching ``master_key``.
    """
    import json as _json

    if not isinstance(key_id, str) or not key_id.strip():
        return "Error: key_id must be a non-empty string"
    if not require_master_key(master_key):
        return "Error: OPENMATRIX_MASTER_KEY required and must match"
    try:
        removed = await default_key_store().revoke_key(key_id)
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"key_id": key_id, "revoked": removed}, ensure_ascii=False)


@mcp.tool()
async def apikey_verify(api_key: str) -> str:
    """Verify a server-side API key secret (H4, not gated).

    Returns the key metadata when valid, or an error when invalid/revoked.
    """
    import json as _json

    if not isinstance(api_key, str) or not api_key:
        return "Error: api_key must be a non-empty string"
    try:
        rec = await default_key_store().verify_key(api_key)
    except Exception as exc:
        return f"Error: {exc}"
    if rec is None:
        return "Error: invalid or revoked API key"
    return _json.dumps(rec, ensure_ascii=False)


@mcp.tool()
def task_submit(operation: str, payload: dict | None = None) -> str:
    """Submit a long operation to the background task manager (unified tasks).

    Operations supported:
    - ``document_add``: index a document by its doc_id (must exist).
    - ``research_run``: start a research run {research_id, objective, context}.

    Returns a task id immediately; poll ``task_status``. Flat report object -
    never a grid/cell structure (patent guardrail).

    Args:
        operation: One of the supported operations.
        payload: Operation payload dict.

    Returns:
        JSON {"task_id", "operation"}, or an error string starting with "Error:".
    """
    import json as _json

    if not isinstance(operation, str) or not operation.strip():
        return "Error: operation must be a non-empty string"
    if payload is not None and not isinstance(payload, dict):
        return "Error: payload must be an object"

    if operation == "document_add":
        from library import default_library_store

        doc_id = (payload or {}).get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            return "Error: document_add requires payload.doc_id"

        async def _index_doc(payload_inner):
            store = default_library_store()
            content = await store.get_document_content(doc_id)
            if not content:
                raise ValueError(f"document content not found for {doc_id}")
            await _index_document(store, doc_id, content)
            return await store.get_document(doc_id)

        task_id = default_task_manager().submit(operation, payload, _index_doc)
        return _json.dumps({"task_id": task_id, "operation": operation}, ensure_ascii=False)

    if operation == "research_run":
        research_id = (payload or {}).get("research_id")
        objective = (payload or {}).get("objective")
        if not isinstance(research_id, str) or not research_id.strip():
            return "Error: research_run requires payload.research_id"
        if research_id.startswith("t-"):
            return "Error: research_id must not start with the reserved 't-' prefix"
        if not isinstance(objective, str) or not objective.strip():
            return "Error: research_run requires payload.objective"
        context = (payload or {}).get("context")
        owner = (payload or {}).get("owner")
        controller = _run_manager().start(
            research_id,
            objective,
            context if isinstance(context, dict) else None,
            owner=owner if isinstance(owner, str) and owner else None,
        )
        return _json.dumps(
            {"task_id": research_id, "operation": operation, "phase": controller.status()["phase"]},
            ensure_ascii=False,
        )

    return f"Error: unsupported operation '{operation}'"


@mcp.tool()
def task_status(task_id: str, owner: str | None = None) -> str:
    """Get a background task's status snapshot (unified tasks).

    A ``t-``-prefixed id is a TaskManager operation; any other id is a
    research run id. ``owner`` (P7-2) restricts research-run access to runs
    owned by that tenant; mismatched or owned-by-other runs are unknown.
    """
    import json as _json

    if not isinstance(task_id, str) or not task_id.strip():
        return "Error: task_id must be a non-empty string"
    if task_id.startswith("t-"):
        status = default_task_manager().status(task_id)
        if status is None:
            return f"Error: unknown task_id '{task_id}'"
        return _json.dumps(status, ensure_ascii=False)
    status = _run_manager().status(task_id, owner=owner)
    if status is None:
        return f"Error: unknown task_id '{task_id}'"
    return _json.dumps(status, ensure_ascii=False)


@mcp.tool()
def task_cancel(task_id: str, owner: str | None = None) -> str:
    """Cancel a queued/running task (unified tasks).

    A ``t-``-prefixed id is a TaskManager operation; any other id is a
    research run id. ``owner`` (P7-2) restricts cancellation to owned runs.
    """
    import json as _json

    if not isinstance(task_id, str) or not task_id.strip():
        return "Error: task_id must be a non-empty string"
    if task_id.startswith("t-"):
        manager = default_task_manager()
        if manager.status(task_id) is None:
            return f"Error: unknown task_id '{task_id}'"
        cancelled = manager.cancel(task_id)
        if not cancelled:
            return f"Error: task not cancellable '{task_id}'"
        return _json.dumps({"task_id": task_id, "cancelled": True}, ensure_ascii=False)
    manager = _run_manager()
    if manager.get(task_id, owner=owner) is None:
        return f"Error: unknown task_id '{task_id}'"
    cancelled = manager.cancel(task_id, owner=owner)
    if not cancelled:
        return f"Error: cancellation not accepted for task_id '{task_id}'"
    return _json.dumps({"task_id": task_id, "cancelled": True}, ensure_ascii=False)


@mcp.tool()
async def task_result(task_id: str, owner: str | None = None) -> str:
    """Return a task's final result (unified tasks, non-blocking).

    A ``t-``-prefixed id is a TaskManager operation; any other id is a
    research run id.
    """
    import json as _json

    if not isinstance(task_id, str) or not task_id.strip():
        return "Error: task_id must be a non-empty string"
    if task_id.startswith("t-"):
        result = default_task_manager().result(task_id)
        if result is None:
            return f"Error: unknown task_id '{task_id}'"
        return _json.dumps(result, ensure_ascii=False)
    try:
        controller = _run_manager().get(task_id, owner=owner)
        if controller is None:
            return f"Error: unknown task_id '{task_id}'"
        run = await controller.result()
        if run is None:
            return _json.dumps({"task_id": task_id, "phase": "pending"}, ensure_ascii=False)
        return _json.dumps(run.summary(), ensure_ascii=False)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool()
def task_list(owner: str | None = None) -> str:
    """List all known tasks (unified: research runs + background operations).

    ``owner`` (P7-2) filters research runs to those owned by the tenant.
    """
    import json as _json

    out = []
    rm = _run_manager()
    for rid in rm.list(owner=owner):
        status = rm.status(rid, owner=owner)
        if status is not None:
            out.append({"kind": "research_run", **status})
    for status in default_task_manager().list():
        out.append({"kind": "operation", **status})
    return _json.dumps({"tasks": out}, ensure_ascii=False)


def _run_manager() -> ResearchRunManager:
    """Return the shared in-process ResearchRunManager singleton."""
    import importlib

    _rr_mod = importlib.import_module("agent.research_run")
    if _rr_mod._default_manager is None:
        _rr_mod._default_manager = ResearchRunManager()
    return _rr_mod._default_manager


@mcp.tool()
def web_research(query: str, max_results: int = 5) -> str:
    """Live web research via Tavily (P5-3).

    Requires the ``OPENMATRIX_TAVILY_KEY`` environment variable (server-side
    user key; never hardcoded). Returns a flat LIST of cited search results -
    never a grid/cell structure (patent guardrail).

    Args:
        query: Search query.
        max_results: Max results to return (1-10, default 5).

    Returns:
        JSON flat LIST of {title, url, content, score}, or an error string
        starting with "Error:".
    """
    import json as _json
    import urllib.request

    if not isinstance(query, str) or not query.strip():
        return "Error: query must be a non-empty string"
    if not isinstance(max_results, int) or max_results < 1 or max_results > 10:
        return "Error: max_results must be an integer in 1..10"
    if not feature_flag("web_research", default=True):
        return "Error: web research disabled (OPENMATRIX_ENABLE_WEB_RESEARCH)"
    key = os.environ.get("OPENMATRIX_TAVILY_KEY", "")
    if not key:
        return "Error: Tavily API key not set (OPENMATRIX_TAVILY_KEY)"

    payload = _json.dumps({
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        # Never echo the remote error verbatim: it may echo the URL/key.
        return "Error: Tavily search failed (see server logs)"
    if not isinstance(data, dict):
        return "Error: Tavily returned an unexpected response shape"
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return "Error: Tavily response has no valid results list"
    out = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "content": item.get("content"),
            "score": item.get("score"),
        })
    return _json.dumps(out, ensure_ascii=False)


@mcp.tool()
def audit_list(limit: int = 20, master_key: str | None = None) -> str:
    """Return recent audit log entries (B2, master-key gated).

    Requires ``OPENMATRIX_MASTER_KEY`` to be set and a matching ``master_key``.
    Returns a flat LIST of {id, ts, event, actor, detail} - never grid/cell.
    """
    import asyncio
    import json as _json

    if not isinstance(limit, int) or limit < 1 or limit > 100:
        return "Error: limit must be an integer in 1..100"
    if not require_master_key(master_key):
        return "Error: OPENMATRIX_MASTER_KEY required and must match"
    try:
        entries = asyncio.run(default_audit_store().recent(limit))
    except Exception as exc:
        return f"Error: {exc}"
    return _json.dumps({"audit": entries}, ensure_ascii=False)

@mcp.tool()
def web_crawl(url: str, max_depth: int = 2, max_pages: int = 10) -> str:
    """Crawl a site's pages via Tavily (P5-3).

    Requires ``OPENMATRIX_TAVILY_KEY``. Returns a flat LIST of discovered page
    URLs (and titles) - never a grid/cell structure (patent guardrail).

    Args:
        url: Starting URL to crawl.
        max_depth: Crawl depth (default 2).
        max_pages: Max pages to return (default 10).

    Returns:
        JSON flat LIST of {url, title}, or an error string starting with "Error:".
    """
    import json as _json
    import urllib.request

    if not isinstance(url, str) or not url.strip():
        return "Error: url must be a non-empty string"
    if not isinstance(max_depth, int) or max_depth < 1 or max_depth > 5:
        return "Error: max_depth must be an integer in 1..5"
    if not isinstance(max_pages, int) or max_pages < 1 or max_pages > 50:
        return "Error: max_pages must be an integer in 1..50"
    if not feature_flag("web_research", default=True):
        return "Error: web research disabled (OPENMATRIX_ENABLE_WEB_RESEARCH)"
    key = os.environ.get("OPENMATRIX_TAVILY_KEY", "")
    if not key:
        return "Error: Tavily API key not set (OPENMATRIX_TAVILY_KEY)"

    payload = _json.dumps({
        "api_key": key,
        "url": url,
        "max_depth": max_depth,
        "limit": max_pages,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/crawl",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return "Error: Tavily crawl failed (see server logs)"
    if not isinstance(data, dict):
        return "Error: Tavily returned an unexpected response shape"
    raw_results = data.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        return "Error: Tavily crawl response has no valid results list"
    out = []
    for item in raw_results[:max_pages]:
        if not isinstance(item, dict):
            continue
        out.append({
            "url": item.get("url"),
            "title": item.get("title"),
        })
    return _json.dumps(out, ensure_ascii=False)


@mcp.tool()
def web_extract(url: str) -> str:
    """Extract clean text from a page via Tavily (P5-3).

    Requires ``OPENMATRIX_TAVILY_KEY``. Returns the page's raw text content
    (flat report - not grid/cell).

    Args:
        url: Page URL to extract.

    Returns:
        JSON {"url", "content"}, or an error string starting with "Error:".
    """
    import json as _json
    import urllib.request

    if not isinstance(url, str) or not url.strip():
        return "Error: url must be a non-empty string"
    if not feature_flag("web_research", default=True):
        return "Error: web research disabled (OPENMATRIX_ENABLE_WEB_RESEARCH)"
    key = os.environ.get("OPENMATRIX_TAVILY_KEY", "")
    if not key:
        return "Error: Tavily API key not set (OPENMATRIX_TAVILY_KEY)"

    payload = _json.dumps({"api_key": key, "urls": [url]}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/extract",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return "Error: Tavily extract failed (see server logs)"
    if not isinstance(data, dict):
        return "Error: Tavily returned an unexpected response shape"
    results = data.get("results")
    if not isinstance(results, list) or not results:
        return "Error: Tavily extract response has no valid results"
    first = results[0]
    if not isinstance(first, dict):
        return "Error: Tavily extract returned a malformed result"
    content = first.get("raw_content")
    return _json.dumps({"url": url, "content": content}, ensure_ascii=False)


@mcp.tool()
def component_status() -> str:
    """Report availability of optional components (A5).

    Detects importable optional dependencies and configured env keys. Flat
    report object - never grid/cell.

    Returns:
        JSON {"components": {name: "available"|"missing"|"not_configured"}}.
    """
    import json as _json

    def _probe(module_name: str) -> str:
        try:
            __import__(module_name)
            return "available"
        except Exception:
            return "missing"

    components = {
        "pypdf": _probe("pypdf"),
        "aiosqlite": _probe("aiosqlite"),
        "qdrant_client": _probe("qdrant_client"),
        "fastembed": _probe("fastembed"),
        "flagembedding": _probe("FlagEmbedding"),
        "docling": _probe("docling"),
        "camelot": _probe("camelot"),
        "python_pptx": _probe("pptx"),
        "reportlab": _probe("reportlab"),
    }
    env_components = {
        "tavily_key": "configured" if os.environ.get("OPENMATRIX_TAVILY_KEY") else "not_configured",
        "master_key": "configured" if os.environ.get("OPENMATRIX_MASTER_KEY") else "not_configured",
        "fred_key": "configured" if os.environ.get("OPENMATRIX_FRED_API_KEY") else "not_configured",
    }
    return _json.dumps({"components": components, "env": env_components}, ensure_ascii=False)


@mcp.tool()
def eval_retrieval(limit: int = 3, include_dense: bool = True, include_rerank: bool = True) -> str:
    """Run the retrieval quality evaluation over the gold corpus (recall@k, precision@k, nDCG@k, MRR). Ops self-check (roadmap #5)."""
    import json as _json
    import main as main_module

    try:
        result = main_module.evaluate_retrievers(
            limit=limit, include_dense=include_dense, include_rerank=include_rerank
        )
        return _json.dumps(result, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool()
def model_health() -> str:
    """Probe model availability (app health + dense/reranker backends). Ops self-check (roadmap #5)."""
    import json as _json
    import main as main_module

    try:
        h = main_module.health_status()
        dense = main_module.DenseRetrieverAdapter()
        reranker = main_module.get_reranker_adapter()
        return _json.dumps({
            "status": h.get("status"),
            "service": h.get("service"),
            "dense_available": dense.is_available(),
            "reranker_available": not isinstance(reranker, main_module.IdentityReranker),
        }, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool()
def backup_data(target_dir: str | None = None) -> str:
    """Create a backup archive of all persisted data (runs, library, keys, audit, exports).

    Args:
        target_dir: Optional directory for the backup zip (default <cwd>/backups).

    Returns:
        JSON {"path": <zip path>, "entries": <count>} or an error string starting with "Error:".
    """
    import json as _json
    from backup import create_backup, _data_files

    try:
        path = create_backup(target_dir)
        return _json.dumps({"path": path, "entries": len(_data_files())}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool()
def restore_data(zip_path: str, dry_run: bool = True) -> str:
    """List or perform a data restore from a backup archive (path-validated).

    Args:
        zip_path: Path to the backup zip.
        dry_run: If true (default) only list entries; if false, restore them.

    Returns:
        JSON {"dry_run": bool, "entries": [...]} or an error string starting with "Error:".
    """
    import json as _json
    from backup import restore_backup

    try:
        entries = restore_backup(zip_path, dry_run=dry_run)
        return _json.dumps({"dry_run": dry_run, "entries": entries}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


@mcp.tool()
def find_contradictions(
    documents: list[dict],
    topic: str,
    mode: str = "bm25",
    limit_per_question: int = 2,
    tolerance: float = 0.10,
) -> str:
    """Detect cross-document numeric contradictions for a KPI topic (P2-3 v1).

    Extracts the KPI value from each document (nearest-number heuristic, no
    LLM) and compares pairs of documents. When two documents both report a
    value for the topic and the relative difference exceeds ``tolerance``
    (default 10%), a ``numeric_discrepancy`` conflict is emitted. Output is a
    flat LIST of conflict rows - never a grid/cell structure (patent guardrail).

    Args:
        documents: List of {"name", "pages"}.
        topic: KPI name to compare across documents.
        mode: Retrieval mode (unused in v1 heuristic; kept for signature parity).
        limit_per_question: Unused in v1; kept for signature parity.
        tolerance: Relative difference threshold (0..1) that counts as a conflict.

    Returns:
        JSON flat list of conflicts, or an error string starting with "Error:".
    """
    import json as _json

    if not isinstance(documents, list) or not documents:
        return "Error: documents must be a non-empty list"
    if not isinstance(topic, str) or not topic.strip():
        return "Error: topic must be a non-empty string"
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance < 1:
        return "Error: tolerance must be in [0, 1)"

    extracted = []
    for doc in documents:
        if not isinstance(doc, dict):
            return "Error: each document must be an object with 'name' and 'pages'"
        name = doc.get("name", "unnamed")
        pages = doc.get("pages") or []
        if not isinstance(pages, list) or not pages:
            continue
        full_text = "\n\n".join(p for p in pages if isinstance(p, str))
        if not full_text.strip():
            continue
        hit = _extract_kpi_value(topic, full_text)
        if hit and hit.get("value") is not None:
            extracted.append({
                "doc": name,
                "value": hit["value"],
                "unit": hit.get("unit"),
                "quote": (hit.get("context") or "")[:200],
            })

    conflicts = []
    for i in range(len(extracted)):
        for j in range(i + 1, len(extracted)):
            a, b = extracted[i], extracted[j]
            va, vb = a["value"], b["value"]
            try:
                va_f, vb_f = float(va), float(vb)
            except (TypeError, ValueError):
                continue
            if va_f == vb_f:
                continue
            base = max(abs(va_f), abs(vb_f))
            if base == 0:
                continue
            rel_diff = abs(va_f - vb_f) / base
            if rel_diff > tolerance:
                conflicts.append({
                    "topic": topic,
                    "doc_a": a["doc"],
                    "doc_b": b["doc"],
                    "value_a": va,
                    "value_b": vb,
                    "unit": a["unit"] or b["unit"],
                    "quote_a": a["quote"],
                    "quote_b": b["quote"],
                    "conflict_type": "numeric_discrepancy",
                    "relative_difference": round(rel_diff, 4),
                })

    return _json.dumps(conflicts, ensure_ascii=False)


@mcp.tool()
def verify_claim_chain(
    claim: str,
    pages: list[str],
    depth: int = 1,
) -> str:
    """Audit a conclusion -> citation -> source chain (P2-4 v1).

    Splits the claim into sentences and verifies each sentence against the
    pages via ``verify_quote``. ``depth=1`` uses exact matching; ``depth=2``
    additionally runs the fuzzy layer on sentences that failed exact matching
    (layered degradation, no recursive text expansion). Output is a flat LIST
    of per-sentence audit rows - never a grid/cell structure (patent
    guardrail).

    Args:
        claim: The conclusion/claim text to audit.
        pages: List of document page texts.
        depth: 1 (exact only) or 2 (exact + fuzzy fallback).

    Returns:
        JSON flat list of {sentence, citation_status, page, layer}, or an
        error string starting with "Error:".
    """
    import json as _json
    import re as _re

    if not isinstance(claim, str) or not claim.strip():
        return "Error: claim must be a non-empty string"
    if not isinstance(pages, list) or not pages or any(not isinstance(p, str) for p in pages):
        return "Error: pages must be a non-empty list of strings"
    if depth not in (1, 2):
        return "Error: depth must be 1 or 2"

    sentences = [s.strip() for s in _re.split(r"[.;。；?!？！]", claim) if s.strip()]
    if not sentences:
        return "Error: claim contains no sentence content"

    rows = []
    for sentence in sentences:
        try:
            vr = _json.loads(verify_quote(sentence, pages, mode="exact"))
        except Exception:
            return f"Error: verify_quote failed on sentence {sentence[:40]!r}"
        if not isinstance(vr, dict):
            return f"Error: verify_quote returned an unexpected response for sentence {sentence[:40]!r}"
        status = vr.get("citation_status", "not_found")
        page = vr.get("page")
        layer = "exact"
        if depth == 2 and status == "not_found":
            try:
                vr2 = _json.loads(verify_quote(sentence, pages, mode="fuzzy"))
            except Exception:
                return f"Error: verify_quote (fuzzy) failed on sentence {sentence[:40]!r}"
            if not isinstance(vr2, dict):
                return f"Error: verify_quote (fuzzy) returned an unexpected response for sentence {sentence[:40]!r}"
            if vr2.get("citation_status") != "not_found":
                status = vr2.get("citation_status")
                page = vr2.get("page")
                layer = "fuzzy"
        rows.append({
            "sentence": sentence,
            "citation_status": status,
            "page": page,
            "layer": layer,
        })
    return _json.dumps(rows, ensure_ascii=False)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
