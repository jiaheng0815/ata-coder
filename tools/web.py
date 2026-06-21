"""Web search, web fetch, and HTML text extraction — mixin for ToolExecutor."""
import asyncio
import html
import html.parser
import logging
import re
import threading

import httpx

from .result import ToolResult  # noqa: E402 — circular-safe, ToolResult is defined before mixin

logger = logging.getLogger(__name__)

# ── SSRF protection ──────────────────────────────────────────────────────────

# IPv4 private / internal ranges (CIDR notation)
_INTERNAL_IPV4_RANGES: list[tuple[int, int]] = [
    (0x0A000000, 0x0AFFFFFF),     # 10.0.0.0/8
    (0x7F000000, 0x7FFFFFFF),     # 127.0.0.0/8 (loopback)
    (0xA9FE0000, 0xA9FEFFFF),     # 169.254.0.0/16 (link-local, cloud metadata)
    (0xAC100000, 0xAC1FFFFF),     # 172.16.0.0/12
    (0xC0A80000, 0xC0A8FFFF),     # 192.168.0.0/16
    (0x64400000, 0x647FFFFF),     # 100.64.0.0/10 (CGNAT)
    (0xC0000000, 0xC00000FF),     # 192.0.0.0/24 (IETF protocol assignments)
    (0xC0000200, 0xC00002FF),     # 192.0.2.0/24 (TEST-NET-1)
    (0xC6120000, 0xC613FFFF),     # 198.18.0.0/15 (benchmarking)
    (0xC6336400, 0xC63364FF),     # 198.51.100.0/24 (TEST-NET-2)
    (0xCB007100, 0xCB0071FF),     # 203.0.113.0/24 (TEST-NET-3)
    (0xE0000000, 0xEFFFFFFF),     # 224.0.0.0/4 (multicast)
    (0xF0000000, 0xFFFFFFFF),     # 240.0.0.0/4 (reserved)
]

# IPv6 private / internal ranges (as (first_hextet, last_hextet) 128-bit integer tuples)
_INTERNAL_IPV6_RANGES: list[tuple[int, int]] = [
    # ::1/128 — loopback
    (0x00000000000000000000000000000001, 0x00000000000000000000000000000001),
    # ::/128 — unspecified
    (0x00000000000000000000000000000000, 0x00000000000000000000000000000000),
    # ::ffff:0:0/96 — IPv4-mapped (already covered by IPv4 ranges, but check anyway)
    (0x00000000000000000000FFFF00000000, 0x00000000000000000000FFFFFFFFFFFFFF),
    # fe80::/10 — link-local
    (0xFE800000000000000000000000000000, 0xFEBFFFFFFFFFFFFFFFFFFFFFFFFFFFFF),
    # fc00::/7 — unique local (ULA)
    (0xFC000000000000000000000000000000, 0xFDFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF),
    # ff00::/8 — multicast (includes ff02::1, ff05::, etc. for internal services)
    (0xFF000000000000000000000000000000, 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF),
    # 2001:db8::/32 — documentation prefix (not internal but non-routable example traffic)
    (0x20010DB8000000000000000000000000, 0x20010DB8FFFFFFFFFFFFFFFFFFFFFFFF),
    # 2001::/32 — Teredo tunneling (can tunnel internal traffic)
    (0x20010000000000000000000000000000, 0x2001FFFFFFFFFFFFFFFFFFFFFFFFFFFF),
    # 64:ff9b::/96 — IPv4/IPv6 translation (NAT64 well-known prefix)
    (0x0064FF9B000000000000000000000000, 0x0064FF9B00000000FFFFFFFFFFFFFFFF),
    # 100::/64 — discard-only prefix
    (0x01000000000000000000000000000000, 0x0100000000000000FFFFFFFFFFFFFFFF),
    # 2002::/16 — 6to4 (can tunnel to internal IPv4)
    (0x20020000000000000000000000000000, 0x2002FFFFFFFFFFFFFFFFFFFFFFFFFFFF),
]

def _ip_to_int(ip: str) -> int:
    """Convert an IPv4 dotted string to a 32-bit integer."""
    parts = ip.split(".")
    return (int(parts[0]) << 24) | (int(parts[1]) << 16) | (int(parts[2]) << 8) | int(parts[3])


def _ipv6_to_int(ip: str) -> int:
    """Convert an IPv6 address string (with possible %scope_id) to a 128-bit integer."""
    import ipaddress
    # Strip zone ID (e.g. %eth0 on link-local addresses)
    if "%" in ip:
        ip = ip.split("%", 1)[0]
    return int(ipaddress.IPv6Address(ip))


def _is_internal_ip(host: str) -> bool:
    """Check if *host* resolves to an internal/private IP address (IPv4 + IPv6)."""
    import socket
    # Block bare IPv4 internal addresses
    try:
        if all(p.isdigit() for p in host.split(".")):
            ip_int = _ip_to_int(host)
            for lo, hi in _INTERNAL_IPV4_RANGES:
                if lo <= ip_int <= hi:
                    return True
    except (ValueError, IndexError):
        pass

    # Check for bare IPv6 internal addresses (e.g. "::1", "fe80::1")
    try:
        if ":" in host:
            ip_int = _ipv6_to_int(host)
            for lo, hi in _INTERNAL_IPV6_RANGES:
                if lo <= ip_int <= hi:
                    return True
    except (ValueError, ImportError):
        pass

    # Resolve hostname and check every returned IP (both IPv4 and IPv6)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            for info in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM):
                ip_str = info[4][0]
                if family == socket.AF_INET:
                    ip_int = _ip_to_int(ip_str)
                    for lo, hi in _INTERNAL_IPV4_RANGES:
                        if lo <= ip_int <= hi:
                            return True
                elif family == socket.AF_INET6:
                    ip_int = _ipv6_to_int(ip_str)
                    for lo, hi in _INTERNAL_IPV6_RANGES:
                        if lo <= ip_int <= hi:
                            return True
        except (socket.gaierror, OSError, ValueError):
            pass

    return False

def _check_internal_url(url: str) -> str:
    """Return an error string if *url* targets an internal address, else ""."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname
    if not host:
        return "Could not determine host from URL"
    if _is_internal_ip(host):
        return f"URL resolves to internal/private IP: {host}"
    return ""


class WebToolsMixin:
    """Web search and fetch capabilities for ToolExecutor."""

    # Internal HTTP client (lazy-init, shared across web tools)
    _http: httpx.Client | None = None

    # ── Web tools ──────────────────────────────────────────────────────────

    async def _run_in_thread(self, func, *args, **kwargs):
        """Run a sync function in a thread pool to avoid blocking the event loop."""
        from functools import partial
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    @property
    def http(self) -> httpx.Client:
        """Thread-safe httpx.Client: one client per thread."""
        if self._http is None:
            self._http = threading.local()
        client = getattr(self._http, 'client', None)
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(30.0),
                follow_redirects=False,  # disable auto-redirect; we handle redirects manually for SSRF safety
                headers={
                    "User-Agent": (
                        "ATA-Coder/2.0 (AI Coding Assistant; "
                        "+https://github.com/ata-coder/ata-coder)"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*",
                    "Accept-Language": "en-US,zh-CN;q=0.9",
                },
            )
            self._http.client = client
        return client

    async def _tool_web_search(
        self,
        query: str,
        max_results: int = 10,
    ) -> ToolResult:
        """Search the web with tiered fallback: Bing → Baidu → Google.

        All three use web scraping (no API key required).
        Set ATA_CODER_SEARCH_BACKEND to force a single backend:
          "bing" / "baidu" / "google" / "duckduckgo"
        """
        import os
        max_results = min(max(max_results, 1), 20)
        forced = os.environ.get("ATA_CODER_SEARCH_BACKEND", "")

        # Whitelist valid backend names
        _VALID_BACKENDS = {"bing", "baidu", "google", "duckduckgo"}
        if forced and forced.lower() not in _VALID_BACKENDS:
            logger.warning("Unknown ATA_CODER_SEARCH_BACKEND=%r — ignoring, using fallback chain", forced)
            forced = ""

        errors: list[str] = []

        # Build fallback chain: respect forced backend, otherwise tiered
        if forced:
            chain = [(forced, getattr(self, f"_search_{forced}", None))]
        else:
            chain = [
                ("Bing",   self._search_bing),
                ("Baidu",  self._search_baidu),
                ("Google", self._search_google),
            ]

        for name, searcher in chain:
            if searcher is None:
                errors.append(f"{name}: unsupported backend")
                continue
            # Real-time progress: tell the user which backend we're trying
            if self._stream_cb:
                self._stream_cb("web_search", f"🔍 Searching {name}...\n")
            try:
                # Run sync search in thread pool to avoid blocking event loop
                results = await self._run_in_thread(searcher, query)
                if results:
                    if self._stream_cb:
                        self._stream_cb("web_search", f"✓ {name}: {len(results)} results\n")
                    return self._format_search_results(query, results, max_results, name)
                if self._stream_cb:
                    self._stream_cb("web_search", f"✗ {name}: no results\n")
                errors.append(f"{name} returned no results")
            except httpx.TimeoutException:
                if self._stream_cb:
                    self._stream_cb("web_search", f"✗ {name}: timed out\n")
                errors.append(f"{name} timed out")
            except httpx.HTTPStatusError as e:
                if self._stream_cb:
                    self._stream_cb("web_search", f"✗ {name}: HTTP {e.response.status_code}\n")
                errors.append(f"{name} HTTP {e.response.status_code}")
            except Exception as e:
                if self._stream_cb:
                    self._stream_cb("web_search", f"✗ {name}: {e}\n")
                errors.append(f"{name}: {e}")

        return ToolResult(
            success=False, output="",
            error=f"Search failed: {'; '.join(errors)}"
        )

    def _search_bing(self, query: str) -> list[dict[str, str]]:
        """Search Bing (web scraping, no API key)."""
        import urllib.parse
        url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=en"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = self.http.get(url, headers=headers)
        resp.raise_for_status()

        results: list[dict[str, str]] = []
        # Bing results are in <li class="b_algo"> blocks
        blocks = re.findall(
            r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
            resp.text, re.DOTALL | re.IGNORECASE,
        )
        for block in blocks:
            # Title + link in <h2><a href="...">title</a></h2>
            m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not m:
                continue
            href = html.unescape(m.group(1).strip())
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not title or not href.startswith("http"):
                continue
            # Snippet in <p> or <div class="b_caption">
            snippet = ""
            sm = re.search(
                r'<(?:p|div)[^>]*class="[^"]*(?:b_caption|b_lineclamp)[^"]*"[^>]*>(.*?)</(?:p|div)>',
                block, re.DOTALL | re.IGNORECASE,
            )
            if sm:
                snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            snippet = html.unescape(snippet)
            results.append({"title": title, "url": href, "snippet": snippet})

        return results

    def _search_baidu(self, query: str) -> list[dict[str, str]]:
        """Search Baidu (web scraping, no API key)."""
        import urllib.parse
        url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&ie=utf-8"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = self.http.get(url, headers=headers)
        resp.raise_for_status()

        results: list[dict[str, str]] = []
        # Baidu results: <div class="result c-container"> or <div class="c-container">
        blocks = re.findall(
            r'<div[^>]*class="[^"]*(?:result|c-container)[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]*class="[^"]*(?:result|c-container)|$)',
            resp.text, re.DOTALL | re.IGNORECASE,
        )
        if not blocks:
            # Fallback: match h3 titles with links
            blocks = re.findall(
                r'<div[^>]*class="[^"]*c-container[^"]*"[^>]*>(.*?)</div>',
                resp.text, re.DOTALL | re.IGNORECASE,
            )

        for block in blocks:
            m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not m:
                continue
            href = html.unescape(m.group(1).strip())
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if not title or not href.startswith("http"):
                continue
            snippet = ""
            sm = re.search(
                r'<(?:span|div|p)[^>]*class="[^"]*(?:content-right_[^"]*|c-abstract|content)[^"]*"[^>]*>(.*?)</(?:span|div|p)>',
                block, re.DOTALL | re.IGNORECASE,
            )
            if sm:
                snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            snippet = html.unescape(snippet)
            results.append({"title": title, "url": href, "snippet": snippet})

        return results

    def _search_google(self, query: str) -> list[dict[str, str]]:
        """Search Google (web scraping, no API key)."""
        import urllib.parse
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}&hl=en"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = self.http.get(url, headers=headers)
        resp.raise_for_status()

        results: list[dict[str, str]] = []
        # Google results are in <div class="g"> or <div data-sokoban-container>
        blocks = re.findall(
            r'<(?:div|li)[^>]*\b(?:class="g\b|data-sokoban-container)[^>]*>(.*?)</(?:div|li)>',
            resp.text, re.DOTALL | re.IGNORECASE,
        )
        for block in blocks:
            # Title + link: <h3>...<a href="...">title</a></h3>
            m = re.search(r'<a[^>]*href="(/url\?q=|)([^"&]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not m:
                continue
            href = html.unescape(m.group(2).strip())
            if not href.startswith("http"):
                href = "https://www.google.com" + m.group(1) + m.group(2)
            title = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if not title:
                continue
            # Snippet: <span class="aCOpRe"> or various other classes
            snippet = ""
            sm = re.search(
                r'<(?:span|div)[^>]*\b(?:class="[^"]*(?:\baCOpRe\b|st\b)[^"]*")[^>]*>(.*?)</(?:span|div)>',
                block, re.DOTALL | re.IGNORECASE,
            )
            if sm:
                snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            snippet = html.unescape(snippet)
            results.append({"title": title, "url": href, "snippet": snippet})

        return results

    @staticmethod
    def _format_search_results(
        query: str, results: list[dict[str, str]], max_results: int, source: str
    ) -> ToolResult:
        out = [f"Search results for: {query}  (via {source})\n"]
        for i, r in enumerate(results[:max_results], 1):
            out.append(f"{i}. **{html.unescape(r['title'])}**")
            out.append(f"   {r['url']}")
            if r.get("snippet"):
                out.append(f"   {html.unescape(r['snippet'])}")
            out.append("")
        return ToolResult(success=True, output="\n".join(out))

    @staticmethod
    def _parse_ddg_lite(html_text: str) -> list[dict[str, str]]:
        """Extract search results from DuckDuckGo Lite HTML."""
        results: list[dict[str, str]] = []

        # DDG Lite: results are in <a> tags with class="result-link"
        # and snippets in <td class="result-snippet">
        link_pattern = re.compile(
            r'<a[^>]*href="([^"]*)"[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        snippet_pattern = re.compile(
            r'<td[^>]*class="[^"]*result-snippet[^"]*"[^>]*>(.*?)</td>',
            re.DOTALL | re.IGNORECASE,
        )

        links = link_pattern.findall(html_text)
        snippets = snippet_pattern.findall(html_text)

        for i, (href, title) in enumerate(links):
            href = html.unescape(href.strip())
            title = re.sub(r'<[^>]+>', '', title).strip()
            if not title:
                continue

            # Pick corresponding snippet
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i])
                snippet = html.unescape(snippet.strip())

            results.append({
                "title": title,
                "url": href,
                "snippet": snippet[:300],
            })

        return results

    def _get_safe(self, url: str, max_redirects: int = 5):
        """Perform a GET request with manual redirect following,
        checking SSRF safety on every hop."""
        for _ in range(max_redirects):
            blocked = _check_internal_url(url)
            if blocked:
                raise ValueError(f"SSRF blocked: {blocked}")
            resp = self.http.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                url = resp.headers.get("location", "")
                if not url:
                    break
                # Resolve relative redirects
                if not url.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    url = urljoin(resp.url, url) if hasattr(resp, 'url') else urljoin(url, url)
                continue
            return resp
        raise httpx.TooManyRedirects("Too many redirects")

    async def _tool_web_fetch(self, url: str) -> ToolResult:
        """Fetch a URL and extract its text content."""
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                success=False, output="",
                error="Invalid URL: must start with http:// or https://"
            )

        if self._stream_cb:
            self._stream_cb("web_fetch", f"🌐 Fetching {url}...\n")

        def _do_fetch():
            return self._get_safe(url)

        try:
            resp = await self._run_in_thread(_do_fetch)
            resp.raise_for_status()
        except httpx.TimeoutException:
            if self._stream_cb:
                self._stream_cb("web_fetch", f"✗ Timeout: {url}\n")
            return ToolResult(
                success=False, output="",
                error=f"Request timed out: {url}"
            )
        except httpx.HTTPStatusError as e:
            if self._stream_cb:
                self._stream_cb("web_fetch", f"✗ HTTP {e.response.status_code}: {url}\n")
            return ToolResult(
                success=False, output="",
                error=f"HTTP {e.response.status_code} for {url}"
            )
        except Exception as e:
            if self._stream_cb:
                self._stream_cb("web_fetch", f"✗ Failed: {url} — {e}\n")
            return ToolResult(
                success=False, output="",
                error=f"Fetch failed: {e}"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ToolResult(
                success=False, output="",
                error=f"Cannot process content type: {content_type}. Only text/html and text/plain are supported."
            )

        if self._stream_cb:
            size_kb = len(resp.text) // 1024
            self._stream_cb("web_fetch", f"✓ Downloaded {size_kb}KB, extracting text...\n")

        text = self._extract_text(resp.text, url)

        if self._stream_cb:
            self._stream_cb("web_fetch", f"✓ Extracted {len(text):,} chars\n")

        # Truncate
        MAX_CHARS = 15_000
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + (
                f"\n\n... [truncated {len(text) - MAX_CHARS:,} "
                f"chars from {url}]"
            )

        return ToolResult(
            success=True,
            output=f"Content from: {url}\n\n{text}",
        )


