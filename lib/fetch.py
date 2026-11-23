"""fetch.py — 统一采集器。读 sources.json，按 method 分流，写 raw-data-DATE.md + search-manifest。

method:
  rss    → http_get → parse_feed → 时间窗过滤
  api    → 按 URL 派发(HF Daily Papers / OpenFDA / HN Algolia / Crossref；其余交 papers.py)
  scrape → 抓首页 → 同域文章链接通用提取
  search → 本地不抓，写入 search-manifest.json 供 Cowork 模型 WebSearch
"""
from __future__ import annotations
import os, re, json, glob
from datetime import datetime
from . import util

# 停更判定阈值: feed 最新条目距今超过 N 天视为"真停更"(需人工处理)
STALE_DAYS = 14


def _prior_health(date_str):
    """取上一份 source-health(用于"骤降归零"判定)。找不到返回 {}。"""
    d = os.path.dirname(util.path_for("prepared", "x"))
    files = sorted(f for f in glob.glob(os.path.join(d, "source-health-*.json")) if date_str not in f)
    if not files:
        return {}
    try:
        return (json.load(open(files[-1], encoding="utf-8")) or {}).get("sources", {})
    except Exception:
        return {}


def compute_alarms(health, prior):
    """纯函数(可单测):按本次健康状态分三类告警。
    fail            = 本次抓取失败(ok=False)
    sudden_drop     = 本次0条 且 上次>0(仅对无 latest_pub 的 scrape/api 源,疑似临时失败)
    persistent_zero = 见下:
      · 有 latest_pub 的源(RSS): feed 最新条目距今 > STALE_DAYS 天 → 真停更
      · 无 latest_pub 的源(scrape/api): 本次0条 且 上次也0/无记录 → 疑似死源

    (2026-11-23 修复: 原逻辑对 RSS 源用"时间窗过滤后的 count==0"判死源,导致早间触发时
     窗口空档的活源被误报成 persistent_zero。改为: RSS 源用 feed 最新条目日期判停更,
     不再用窗口内条数判死; scrape/api 源无时间窗过滤,保持原 count 判定不变。)
    """
    fail, drop, zero = [], [], []
    for name, h in health.items():
        if not h.get("ok", True):
            fail.append(name); continue
        if h.get("latest_pub"):
            try:
                d = datetime.strptime(h["latest_pub"], "%Y-%m-%d").date()
                if (datetime.now().date() - d).days > STALE_DAYS:
                    zero.append(name)
            except (ValueError, TypeError):
                pass  # 日期异常, 保守不判停更
        elif h.get("count", 0) == 0:
            pc = (prior.get(name) or {}).get("count")
            if pc and pc > 0:
                drop.append(name)
            else:
                zero.append(name)
    return {"fail": sorted(fail), "sudden_drop": sorted(drop), "persistent_zero": sorted(zero)}


_FEED_ROOTS = ("<rss", "<feed", "<rdf:RDF", "<RDF", "<?xml")


def _looks_like_feed(txt):
    """HTTP 200 不等于拿到 feed。Cloudflare 人机质询页、域名迁移落地页、编码异常
    都是 200 + 非 XML,原先会被 parse_feed 静默吃成 0 条、health 记成
    ok=True/count=0,再被 compute_alarms 误判成 persistent_zero(看着像源停更)。
    这里对返回体做一次根元素体检,不含 feed 根就判抓取失败。"""
    head = txt.lstrip()[:400]
    return any(r in head for r in _FEED_ROOTS)


def _fetch_rss(src, http):
    txt, ok = util.http_get(src["url"], http["timeout_s"], http["retries"], http["user_agent"])
    if not ok:
        return [], False, None
    if not _looks_like_feed(txt):
        return [], False, None
    all_items = util.parse_feed(txt)
    # 记录 feed 最新条目日期(窗口过滤前),供健康判定区分"活源但窗口空档" vs "真停更"
    latest = None
    for it in all_items:
        pub = util.parse_date(it.get("pub"))
        if pub and (latest is None or pub > latest):
            latest = pub
    latest_pub = latest.strftime("%Y-%m-%d") if latest else None
    items = []
    for it in all_items:
        pub = util.parse_date(it.get("pub"))
        if not util.within_window(pub, src.get("window_h")):
            continue
        items.append({"title": it["title"], "link": it["link"],
                      "content": it.get("summary", ""), "pub_date": it.get("pub", "")})
    return items, True, latest_pub


def _fetch_scrape(src, http):
    txt, ok = util.http_get(src["url"], http["timeout_s"], http["retries"], http["user_agent"])
    if not ok:
        return [], False
    # 通用同域文章链接提取:取 <a href> + 锚文本，过滤导航/太短
    base = re.sub(r"^(https?://[^/]+).*", r"\1", src["url"])
    items, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt, re.I | re.S):
        href, anchor = m.group(1), util.strip_html(m.group(2))
        if href.startswith("/"):
            href = base + href
        if not href.startswith("http") or base not in href:
            continue
        if len(anchor) < 12 or util.normalize_url(href) in seen:
            continue
        if re.search(r"/(tag|category|author|page|about|privacy|terms|login)/", href, re.I):
            continue
        seen.add(util.normalize_url(href))
        items.append({"title": anchor[:160], "link": href, "content": "", "pub_date": ""})
        if len(items) >= 25:
            break
    return items, True


def _fetch_api(src, http):
    url = src["url"]
    txt, ok = util.http_get(url, http["timeout_s"], http["retries"], http["user_agent"])
    if not ok:
        return [], False
    try:
        data = json.loads(txt)
    except Exception:
        return [], False
    items = []
    if "huggingface.co/api/daily_papers" in url:
        for p in (data if isinstance(data, list) else [])[:15]:
            paper = p.get("paper", p)
            pid = paper.get("id", "")
            items.append({"title": paper.get("title", ""),
                          "link": f"https://arxiv.org/abs/{pid}" if pid else "",
                          "content": util.strip_html(paper.get("summary", "")), "pub_date": p.get("publishedAt", "")})
    elif "hn.algolia.com" in url:
        for h in data.get("hits", [])[:20]:
            items.append({"title": h.get("title", ""), "link": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                          "content": f"HN points={h.get('points')} comments={h.get('num_comments')}", "pub_date": h.get("created_at", "")})
    elif "api.fda.gov" in url:
        for r in data.get("results", [])[:20]:
            sub = (r.get("submissions") or [{}])[0]
            items.append({"title": f"{(r.get('openfda',{}).get('brand_name',['?']) or ['?'])[0]} — {sub.get('submission_status','')}",
                          "link": "https://www.accessdata.fda.gov/scripts/cder/daf/", "content": json.dumps(r.get("products", []), ensure_ascii=False)[:600], "pub_date": sub.get("submission_status_date", "")})
    elif "clinicaltrials.gov/api/v2" in url:
        # v2 API：studies[].protocolSection.{identification/status/sponsor/design/conditions/description}Module
        for s in data.get("studies", [])[:25]:
            ps = s.get("protocolSection", {})
            idm = ps.get("identificationModule", {})
            stm = ps.get("statusModule", {})
            spm = ps.get("sponsorCollaboratorsModule", {})
            dsm = ps.get("designModule", {})
            cdm = ps.get("conditionsModule", {})
            dscm = ps.get("descriptionModule", {})
            nct = idm.get("nctId", "")
            phases = ",".join(dsm.get("phases", []) or []) or "NA"
            conds = ", ".join((cdm.get("conditions") or [])[:5])
            sponsor = (spm.get("leadSponsor") or {}).get("name", "")
            status = stm.get("overallStatus", "")
            body = f"[{phases}] {status} | 申办方/赞助方: {sponsor} | 适应症: {conds}. {dscm.get('briefSummary', '')}"
            items.append({"title": idm.get("briefTitle", ""),
                          "link": f"https://clinicaltrials.gov/study/{nct}" if nct else url,
                          "content": util.strip_html(body)[:1200],
                          "pub_date": (stm.get("lastUpdatePostDateStruct") or {}).get("date", "")})
    elif "api.crossref.org" in url:
        # Crossref REST：按期刊 ISSN 取最新文章(ACS Synthetic Biology 换源后的通道)。
        for w in ((data.get("message") or {}).get("items") or [])[:25]:
            title = (w.get("title") or [""])[0]
            journal = (w.get("container-title") or [""])[0]
            parts = ((w.get("published") or {}).get("date-parts") or [[]])[0]
            pub = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts[:3])) if parts else ""
            doi = w.get("DOI", "")
            items.append({"title": title,
                          "link": w.get("URL") or (f"https://doi.org/{doi}" if doi else url),
                          "content": f"{journal} | DOI: {doi}",
                          "pub_date": pub})
    return items, True


def fetch_all(section_filter=None, date_str=None):
    """采集。section_filter: 'ai'/'vaccine'/'payment'/... 或 None(全部)。写 raw-data + search-manifest。"""
    sources, settings, _ = util.settings()
    http = settings["http"]
    date_str = date_str or util.today_str()

    by_section = {}     # section → {source_name → [items]}
    search_manifest = []
    health = {}         # source_name → {section, method, count, ok, latest_pub?}  (本地源产量体检)
    stats = {"ok": 0, "fail": 0, "skip_search": 0, "items": 0}

    for section, srclist in sources.items():
        if section.startswith("_"):
            continue
        if section_filter and section != section_filter:
            continue
        for src in srclist:
            if not src.get("enabled", True):
                continue
            method = src.get("method")
            if method in ("search", "remote"):
                # 本地不抓(国际源走本地，中国源/无feed源走 Cowork 侧)。写进 manifest:
                #   remote → Cowork web_fetch 这个 url；search → Cowork WebSearch 这些 queries
                search_manifest.append({"name": src["name"], "section": section, "method": method,
                                        "url": src.get("url"), "queries": src.get("queries", []),
                                        "dual_china": src.get("dual_china", False),
                                        "window_h": src.get("window_h"), "note": src.get("note", "")})
                stats["skip_search"] += 1
                continue
            try:
                if method == "rss":
                    items, ok, latest_pub = _fetch_rss(src, http)
                elif method == "scrape":
                    items, ok = _fetch_scrape(src, http)
                    latest_pub = None
                elif method == "api":
                    items, ok = _fetch_api(src, http)
                    latest_pub = None
                else:
                    items, ok, latest_pub = [], False, None
            except Exception as e:
                items, ok, latest_pub = [], False, None
            if ok:
                stats["ok"] += 1
                stats["items"] += len(items)
                by_section.setdefault(section, {}).setdefault(src["name"], []).extend(items)
            else:
                stats["fail"] += 1
            h = {"section": section, "method": method,
                 "count": len(items) if ok else 0, "ok": ok}
            if latest_pub:
                h["latest_pub"] = latest_pub
            health[src["name"]] = h
            print(f"  [{section}] {src['name']:32} {'OK '+str(len(items)) if ok else 'FAIL':>8}")

    # 写 raw-data(每个 domain 一个文件，沿用旧目录结构便于聚合)
    domain = section_filter or "all"
    raw_path = util.path_for("raw", f"raw-data-{domain}-{date_str}.md")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(f"# raw-data {domain} {date_str}\n\n")
        for section, srcmap in by_section.items():
            for sname, items in srcmap.items():
                f.write(f"## {sname}\n")
                for it in items:
                    f.write(f"**{it['title']}**\n- URL: {it['link']}\n- 日期: {it['pub_date']}\n- 摘要: {it['content'][:1200]}\n\n")

    man_path = util.path_for("prepared", f"search-manifest-{date_str}.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "sources": search_manifest}, f, ensure_ascii=False, indent=2)

    # 信源健康体检:对比上一份产量,分"骤降归零/抓取失败/持续归零"三类告警
    alarms = compute_alarms(health, _prior_health(date_str))
    health_path = util.path_for("prepared", f"source-health-{date_str}.json")
    with open(health_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "sources": health, "alarms": alarms}, f, ensure_ascii=False, indent=2)

    print(f"\n采集完成: ok={stats['ok']} fail={stats['fail']} search待补={stats['skip_search']} 条目={stats['items']}")
    print(f"  raw-data → {raw_path}")
    print(f"  search-manifest → {man_path}  (Cowork 侧 WebSearch 补 {len(search_manifest)} 个源)")
    if alarms["fail"] or alarms["sudden_drop"] or alarms["persistent_zero"]:
        print("\n⚠️ 信源健康告警:")
        if alarms["sudden_drop"]:
            print(f"  🔴 骤降归零(上次有本次0,疑似临时抓取失败,建议复跑/关注): {alarms['sudden_drop']}")
        if alarms["fail"]:
            print(f"  🔴 抓取失败(FAIL): {alarms['fail']}")
        if alarms["persistent_zero"]:
            print(f"  🟡 持续归零(多次0,疑似死源/选择器失效,需在 Mac 端修): {alarms['persistent_zero']}")
        print(f"  → 详见 {health_path}")
    else:
        print(f"  信源健康: 无告警 → {health_path}")
    return raw_path
