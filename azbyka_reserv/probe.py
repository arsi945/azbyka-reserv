"""Разведка сайта перед полным обходом.

Скачивает robots.txt, карты сайта, корни разделов и несколько страниц
вглубь каждого раздела; сохраняет образцы и отчёт (Markdown + JSON),
по которым настраиваются правила обхода. Отчёт небольшой — его можно
закоммитить в репозиторий для анализа.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from . import extract
from .config import Config
from .fetcher import Bandwidth, FetchError, Fetcher, RateLimiter, maybe_gunzip
from .robots import Robots
from .urls import UrlRules, kind_by_ext, url_ext

_ENGINE_SIGNS = [
    ("WordPress", re.compile(rb"wp-content|wp-includes|<meta[^>]+generator[^>]+WordPress", re.I)),
    ("MediaWiki", re.compile(rb"mediawiki|wgPageName|/load\.php\?", re.I)),
    ("XenForo", re.compile(rb"xenforo|data-xf-init", re.I)),
    ("Bitrix", re.compile(rb"bitrix", re.I)),
    ("Next/React", re.compile(rb"__NEXT_DATA__|data-reactroot", re.I)),
    ("Vue/Nuxt", re.compile(rb"__NUXT__|data-v-[0-9a-f]{6}", re.I)),
]


def _section(url: str) -> str:
    p = urlsplit(url).path.strip("/")
    return "/" + (p.split("/", 1)[0] if p else "")


def _slug(url: str) -> str:
    s = re.sub(r"[^\w.-]+", "_", url.split("://", 1)[-1])[:150]
    return s.strip("_") or "root"


def run_probe(cfg: Config, out_dir: str, limit: int = 3) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(out_dir, stamp)
    raw_dir = os.path.join(out, "samples")
    os.makedirs(raw_dir, exist_ok=True)
    rules = UrlRules(cfg.scope_hosts, cfg.host_aliases, cfg.drop_params, cfg.https_hosts)
    limiter = RateLimiter(max(cfg.min_delay, 0.7))
    f = Fetcher(cfg.user_agent, cfg.timeout, limiter, Bandwidth(0), cfg.cookies_file)
    report: dict = {"started": stamp, "user_agent": cfg.user_agent, "pages": [], "sections": {}, "errors": []}

    def fetch(url: str, save: bool = True, max_mem: int = 15 * 1024 * 1024):
        t0 = time.monotonic()
        try:
            r = f.get(url, max_memory=max_mem)
        except FetchError as e:
            report["errors"].append({"url": url, "error": str(e)})
            print(f"  ! {url}: {e}")
            return None
        dt = time.monotonic() - t0
        h = r.headers
        info = {
            "url": url, "status": r.status, "ctype": r.ctype, "size": r.size, "seconds": round(dt, 2),
            "location": r.location, "server": h.get("Server") if h else None,
            "headers": {k: v for k, v in (h.items() if h else []) if k.lower() in (
                "server", "x-powered-by", "cf-ray", "x-cache", "via", "content-encoding", "cache-control",
                "x-frame-options", "set-cookie", "last-modified", "etag", "content-disposition", "accept-ranges",
                "x-robots-tag", "link", "x-generator", "x-ddos-protection", "x-qrator")},
        }
        if r.body is not None and save:
            body = maybe_gunzip(r.body, url, r.ctype)
            ext = ".html" if "html" in r.ctype else (".xml" if "xml" in r.ctype else ".txt")
            if "json" in r.ctype:
                ext = ".json"
            name = _slug(url) + ext
            with open(os.path.join(raw_dir, name), "wb") as fh:
                fh.write(body[:3 * 1024 * 1024])
            info["sample"] = f"samples/{name}"
        print(f"  {r.status} {r.ctype[:30]:<30} {r.size:>9} {url}")
        return r, info

    print("robots.txt и карты сайта…")
    robots_text = ""
    host = urlsplit(cfg.start_urls[0]).hostname or "azbyka.ru"
    res = fetch(f"https://{host}/robots.txt")
    if res and res[0].status == 200 and res[0].body:
        robots_text = res[0].body.decode("utf-8", "replace")
    robots = Robots(robots_text, cfg.user_agent)
    report["robots"] = {"text": robots_text[:20000], "sitemaps": robots.sitemaps, "crawl_delay": robots.crawl_delay,
                        "clean_params": robots.clean_params}
    sitemap_report = []
    for sm in list(dict.fromkeys(robots.sitemaps + cfg.sitemap_urls))[:10]:
        res = fetch(sm)
        if not res:
            continue
        r, info = res
        entry = {"url": sm, "status": r.status, "children": 0, "sample": []}
        if r.status == 200 and r.body:
            links = extract.sitemap_links(maybe_gunzip(r.body, sm, r.ctype).decode("utf-8", "replace"))
            entry["children"] = len(links)
            entry["sample"] = [lk.url for lk in links[:40]]
            entry["sections"] = Counter(_section(lk.url) for lk in links).most_common(60)
            # заглянуть в первую дочернюю карту
            sub = [lk.url for lk in links if lk.kind == "sitemap"][:3]
            for s in sub:
                rr = fetch(s)
                if rr and rr[0].status == 200 and rr[0].body:
                    sl = extract.sitemap_links(maybe_gunzip(rr[0].body, s, rr[0].ctype).decode("utf-8", "replace"))
                    entry.setdefault("sub", []).append({"url": s, "children": len(sl), "sample": [x.url for x in sl[:20]]})
        sitemap_report.append(entry)
    report["sitemaps"] = sitemap_report

    print("Корни разделов…")
    seen: set[str] = set()
    for start in cfg.start_urls:
        norm = rules.normalize(start)
        if not norm or norm[0] in seen:
            continue
        url = norm[0]
        seen.add(url)
        res = fetch(url)
        if not res:
            continue
        r, info = res
        sec = _section(url)
        srep = report["sections"].setdefault(sec, {"roots": [], "deep": []})
        page = _analyze(r, url, rules, cfg, info)
        srep["roots"].append(page)
        report["pages"].append(info)
        # вглубь: несколько ссылок того же раздела, разной «формы» пути
        cands = [u for u in page["internal_sample_all"] if _section(u) == sec and u != url]
        picked = _diverse(cands, limit)
        for u in picked:
            if u in seen:
                continue
            seen.add(u)
            res2 = fetch(u)
            if not res2:
                continue
            r2, info2 = res2
            srep["deep"].append(_analyze(r2, u, rules, cfg, info2))
            report["pages"].append(info2)

    # проверка известных API/движков
    print("API движков…")
    api_checks = []
    for sec in ["", "/audio", "/video", "/art", "/kliros", "/fiction", "/recept", "/deti", "/shemy", "/vopros", "/foto", "/news"]:
        for path in (f"{sec}/wp-json/", f"{sec}/wp-sitemap.xml", f"{sec}/sitemap.xml"):
            res = fetch(f"https://{host}{path}", save=False, max_mem=2 * 1024 * 1024)
            if res:
                api_checks.append({"url": path, "status": res[0].status, "ctype": res[0].ctype, "size": res[0].size})
    for path in ("/palomnik/api.php?action=query&meta=siteinfo&format=json", "/palomnik/index.php?title=Special:AllPages",
                 "/days/api-v2/doc", "/days/widgets/presentations.json"):
        res = fetch(f"https://{host}{path}", save=True, max_mem=2 * 1024 * 1024)
        if res:
            api_checks.append({"url": path, "status": res[0].status, "ctype": res[0].ctype, "size": res[0].size})
    report["api_checks"] = api_checks

    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(_markdown(report))
    return out


def _diverse(urls: list[str], n: int) -> list[str]:
    """Выбирает URL с разной «формой» пути (глубина, наличие query, расширение)."""
    seen_shapes: set[tuple] = set()
    out: list[str] = []
    for u in urls:
        p = urlsplit(u)
        shape = (p.path.count("/"), bool(p.query), url_ext(u), bool(re.search(r"/\d+/?$", p.path)))
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        out.append(u)
        if len(out) >= n:
            break
    return out


def _analyze(r, url: str, rules: UrlRules, cfg: Config, info: dict) -> dict:
    page: dict = {"url": url, "status": r.status, "ctype": r.ctype, "size": r.size, "location": r.location}
    if r.status != 200 or r.body is None or "html" not in r.ctype:
        page["internal_sample_all"] = []
        return page
    body = r.body
    page["engines"] = [name for name, rx in _ENGINE_SIGNS if rx.search(body)]
    page["has_login_form"] = b'type="password"' in body
    links, meta = extract.extract_links(body, r.ctype, url)
    page["title"] = meta.get("title")
    page["canonical"] = meta.get("canonical")
    kinds = Counter()
    sources = Counter()
    internal: list[str] = []
    external_hosts = Counter()
    media: list[str] = []
    embeds: list[str] = []
    q_keys = Counter()
    by_sec = defaultdict(int)
    for lk in links:
        norm = rules.normalize(lk.url, url)
        if not norm:
            continue
        u = norm[0]
        host = urlsplit(u).hostname or ""
        kinds[lk.kind] += 1
        sources[lk.source.split(":")[0]] += 1
        if rules.host_in_scope(host):
            if lk.kind in ("page", "embed"):
                internal.append(u)
                by_sec[_section(u)] += 1
            if lk.kind == "media" or kind_by_ext(u) == "media":
                media.append(u)
            q = urlsplit(u).query
            for t in q.split("&") if q else []:
                q_keys[t.split("=", 1)[0][:30]] += 1
        else:
            external_hosts[host] += 1
            if lk.kind == "embed":
                embeds.append(u)
    internal = list(dict.fromkeys(internal))
    page.update({
        "link_kinds": dict(kinds),
        "link_sources": dict(sources.most_common(15)),
        "internal_count": len(internal),
        "internal_sections": dict(sorted(by_sec.items(), key=lambda x: -x[1])[:25]),
        "internal_sample": internal[:60],
        "internal_sample_all": internal,
        "query_keys": dict(q_keys.most_common(20)),
        "media": list(dict.fromkeys(media))[:40],
        "embeds": list(dict.fromkeys(embeds))[:20],
        "external_hosts": dict(external_hosts.most_common(20)),
        "excluded_by_rules": [u for u in internal if cfg.excluded(u)][:20],
        "wp_playlist": b"wp-playlist-script" in body,
        "download_like": [u for u in internal if re.search(r"(?i)download|skachat|/get/|format=|\.epub|\.pdf|\.fb2", u)][:30],
    })
    return page


def _markdown(rep: dict) -> str:
    lines = [f"# Отчёт пробы azbyka.ru ({rep['started']})", ""]
    rb = rep.get("robots", {})
    lines += ["## robots.txt", "", "```", (rb.get("text") or "(нет)")[:6000], "```", ""]
    lines += [f"Crawl-delay: {rb.get('crawl_delay')}; Clean-param: {rb.get('clean_params')}", ""]
    lines += ["## Карты сайта", ""]
    for sm in rep.get("sitemaps", []):
        lines.append(f"- {sm['url']} — HTTP {sm['status']}, записей {sm['children']}")
        for sec, n in (sm.get("sections") or [])[:30]:
            lines.append(f"  - {sec}: {n}")
        for sub in sm.get("sub", []):
            lines.append(f"  - {sub['url']}: {sub['children']} (напр. {', '.join(sub['sample'][:3])})")
    lines += ["", "## Разделы", ""]
    for sec, data in sorted(rep.get("sections", {}).items()):
        lines.append(f"### {sec}")
        for p in data["roots"] + data["deep"]:
            lines.append(f"- **{p['url']}** — {p['status']} {p.get('ctype', '')[:25]} {p.get('size', 0)} Б"
                         + (f" → {p['location']}" if p.get("location") else ""))
            if "engines" in p:
                lines.append(f"  - движок: {', '.join(p['engines']) or '?'}; ссылок внутрь: {p['internal_count']}; "
                             f"типы: {p['link_kinds']}; playlist: {p['wp_playlist']}; форма входа: {p['has_login_form']}")
                if p.get("query_keys"):
                    lines.append(f"  - ключи query: {p['query_keys']}")
                if p.get("media"):
                    lines.append(f"  - медиа: {p['media'][:8]}")
                if p.get("download_like"):
                    lines.append(f"  - похожие на скачивание: {p['download_like'][:8]}")
                if p.get("embeds"):
                    lines.append(f"  - встраивания: {p['embeds'][:5]}")
                if p.get("external_hosts"):
                    lines.append(f"  - внешние хосты: {p['external_hosts']}")
                lines.append(f"  - разделы ссылок: {p.get('internal_sections')}")
        lines.append("")
    lines += ["## Проверка API", ""]
    for a in rep.get("api_checks", []):
        lines.append(f"- {a['url']}: {a['status']} {a['ctype']} {a['size']}")
    if rep.get("errors"):
        lines += ["", "## Ошибки", ""] + [f"- {e['url']}: {e['error']}" for e in rep["errors"]]
    return "\n".join(lines) + "\n"
