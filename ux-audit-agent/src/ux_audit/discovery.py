"""Phase 3: page discovery (spec §6.1).

Pure Python — no model in the loop. It's a URL filter, so it's a `@node` function
rather than an agent (spec §4.4).

The `discovery_log.jsonl` reason trail is mandatory, not optional: the path
heuristic *will* be wrong on some site, and the log is how that gets diagnosed in
a minute instead of an hour (spec §6.1, §16).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx

from .config import Config
from .models import DiscoveryDecision, PageTarget

TRACKING_PARAM_RE = re.compile(r"^(utm_.*|ref|referrer|fbclid|gclid|mc_cid|mc_eid)$", re.I)
NON_HTML_EXT_RE = re.compile(
    r"\.(pdf|zip|png|jpe?g|gif|svg|webp|ico|css|js|json|xml|mp4|mp3|woff2?|ttf|eot|dmg|exe)$", re.I
)
LOGIN_WALL_RE = re.compile(r"/(login|signin|sign-in|auth)(/|$|\?)", re.I)


class _LinkExtractor(HTMLParser):
    """Stdlib link scraping — avoids a bs4 dependency for what is one attribute."""

    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def normalize(url: str) -> str:
    """Strip fragment + tracking params, drop trailing slash, lowercase host."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAM_RE.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc.lower()) == (pb.scheme, pb.netloc.lower())


def coerce_scheme(url: str, root: str) -> str:
    """Adopt the root's scheme when only the scheme differs.

    Real sitemaps routinely list `http://` URLs for sites served over HTTPS.
    Without this, strict same-origin comparison drops the entire sitemap as
    "different origin" — which is exactly what the discovery log caught on the
    first real run.
    """
    p, r = urlparse(url), urlparse(root)
    if p.netloc.lower() == r.netloc.lower() and p.scheme != r.scheme and p.scheme in ("http", "https"):
        return urlunparse((r.scheme, p.netloc, p.path, p.params, p.query, p.fragment))
    return url


def priority_of(url: str, prioritise: list[str]) -> int:
    """Lower sorts first. Exact-path matches beat prefix matches."""
    path = urlparse(url).path.rstrip("/") or "/"
    for i, pref in enumerate(prioritise):
        want = pref.rstrip("/") or "/"
        if path == want:
            return i
    for i, pref in enumerate(prioritise):
        want = pref.rstrip("/") or "/"
        if want != "/" and path.startswith(want):
            return i + len(prioritise)
    return 999


def _excluded_reason(url: str, cfg: Config) -> str | None:
    path = urlparse(url).path
    for pat in cfg.discovery.exclude_patterns:
        if pat in path:
            return f"matches exclude pattern {pat!r} (app surface, not marketing)"
    if NON_HTML_EXT_RE.search(path):
        return "non-HTML content type by extension"
    return None


async def _fetch_text(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    """Returns (text, error). Never raises — discovery must not kill the run."""
    try:
        r = await client.get(url, follow_redirects=True, timeout=15)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            return None, f"content-type {ctype!r} is not HTML/XML"
        return r.text, None
    except Exception as e:  # noqa: BLE001 - network is allowed to fail here
        return None, f"{type(e).__name__}: {e}"


async def _load_robots(client: httpx.AsyncClient, root: str) -> RobotFileParser | None:
    p = urlparse(root)
    robots_url = urlunparse((p.scheme, p.netloc, "/robots.txt", "", "", ""))
    text, _ = await _fetch_text(client, robots_url)
    if text is None:
        return None
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp


async def _from_sitemap(client: httpx.AsyncClient, root: str, depth: int = 0) -> list[str]:
    """Read sitemap.xml, following one level of sitemap-index nesting."""
    p = urlparse(root)
    sm_url = urlunparse((p.scheme, p.netloc, "/sitemap.xml", "", "", ""))
    text, _ = await _fetch_text(client, sm_url)
    if not text:
        return []

    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, re.I)
    if "<sitemapindex" in text.lower() and depth == 0:
        nested: list[str] = []
        for child in locs[:5]:  # bound the fan-out; a big index isn't worth exhausting
            ctext, _ = await _fetch_text(client, child)
            if ctext:
                nested.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", ctext, re.I))
        return nested
    return locs


async def _bfs(client: httpx.AsyncClient, root: str, cfg: Config) -> list[tuple[str, int]]:
    """Same-origin breadth-first walk to max_depth. Returns (url, depth) pairs."""
    seen = {normalize(root)}
    frontier = [(root, 0)]
    found: list[tuple[str, int]] = [(root, 0)]

    while frontier:
        url, depth = frontier.pop(0)
        if depth >= cfg.run.max_depth:
            continue
        html, _ = await _fetch_text(client, url)
        if not html:
            continue
        parser = _LinkExtractor()
        parser.feed(html)
        for href in parser.links:
            absolute = urljoin(url, href)
            if not absolute.startswith(("http://", "https://")):
                continue
            if not same_origin(root, absolute):
                continue
            n = normalize(absolute)
            if n in seen:
                continue
            seen.add(n)
            found.append((n, depth + 1))
            frontier.append((n, depth + 1))
        # Enough raw candidates to fill max_pages many times over; stop walking.
        if len(found) > cfg.run.max_pages * 6:
            break
    return found


async def discover(root_url: str, cfg: Config) -> tuple[list[PageTarget], list[DiscoveryDecision]]:
    """Returns (selected targets, decision log covering EVERY url seen)."""
    decisions: list[DiscoveryDecision] = []
    root_norm = normalize(root_url)

    async with httpx.AsyncClient(headers={"User-Agent": "ux-audit-agent/1.0"}) as client:
        robots = await _load_robots(client, root_norm)

        sitemap_urls = await _from_sitemap(client, root_norm)

        # "sitemap.xml if usable" (spec §6.1) — a sitemap that yields fewer
        # candidates than max_pages isn't usable on its own, so fall through to
        # BFS and merge rather than auditing 2 pages of a 40-page site.
        if len(sitemap_urls) >= cfg.run.max_pages:
            candidates = [(root_norm, 0)] + [(u, 1) for u in sitemap_urls]
            source = "sitemap.xml"
        elif sitemap_urls:
            bfs = await _bfs(client, root_norm, cfg)
            seen_sm = {normalize(u) for u in sitemap_urls}
            candidates = (
                [(root_norm, 0)]
                + [(u, 1) for u in sitemap_urls]
                + [(u, d) for u, d in bfs if normalize(u) not in seen_sm]
            )
            source = "sitemap.xml+BFS"
        else:
            candidates = await _bfs(client, root_norm, cfg)
            source = "BFS"

    seen: set[str] = set()
    kept: list[PageTarget] = []
    blog_count = 0

    for raw_url, depth in candidates:
        url = coerce_scheme(normalize(raw_url), root_norm)

        if url in seen:
            decisions.append(DiscoveryDecision(url=url, decision="duplicate", reason="already seen after normalization"))
            continue
        seen.add(url)

        if not same_origin(root_norm, url):
            decisions.append(DiscoveryDecision(url=url, decision="excluded", reason="different origin"))
            continue

        if robots is not None and not robots.can_fetch("ux-audit-agent", url):
            decisions.append(DiscoveryDecision(url=url, decision="excluded", reason="disallowed by robots.txt"))
            continue

        if (why := _excluded_reason(url, cfg)):
            decisions.append(DiscoveryDecision(url=url, decision="excluded", reason=why))
            continue

        # Blog sampling: keep the index, sample N posts (spec §6.1 step 4).
        path = urlparse(url).path
        if "/blog/" in path and path.rstrip("/") != "/blog":
            blog_count += 1
            if blog_count > cfg.run.blog_sample:
                decisions.append(
                    DiscoveryDecision(url=url, decision="excluded", reason=f"blog post beyond blog_sample={cfg.run.blog_sample}")
                )
                continue

        kept.append(PageTarget(url=url, depth=depth, priority=priority_of(url, cfg.discovery.prioritise)))

    # Deterministic ordering: priority, then depth, then URL. This ordering is what
    # makes ADK's auto-generated execution IDs stable across resume (spec §7).
    kept.sort(key=lambda t: (t.priority, t.depth, t.url))

    selected, overflow = kept[: cfg.run.max_pages], kept[cfg.run.max_pages :]
    for t in selected:
        decisions.append(
            DiscoveryDecision(url=t.url, decision="selected", reason=f"priority={t.priority} depth={t.depth} via {source}")
        )
    for t in overflow:
        decisions.append(DiscoveryDecision(url=t.url, decision="capped", reason=f"beyond max_pages={cfg.run.max_pages}"))

    return selected, decisions
