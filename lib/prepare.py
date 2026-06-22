"""prepare.py — 预处理:解析 raw → 分板块/去重/相关性降权/交叉匹配/排序/配额/预抓全文 → prepared JSON。

合并自旧 prepare_daily.py，配置驱动(板块映射/tier/priority 全来自 sources.json，关键词来自 keywords.json)。
"""
from __future__ import annotations
import os, re, json, glob
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from . import util, clean


def _source_meta():
    """从 sources.json 建 source_name → {section,tier,priority,dual_china}。"""
    sources, _, _ = util.settings()
    meta = {}
    for section, lst in sources.items():
        if section.startswith("_"):
            continue
        for s in lst:
            meta[s["name"]] = {"section": s["section"], "tier": s.get("tier", "T3"),
                               "priority": s.get("priority", 9), "dual_china": s.get("dual_china", False)}
    return meta


def parse_raw(path):
    items, src, cur = [], None, None
    if not os.path.exists(path):
        return items
    for line in open(path, encoding="utf-8"):
        s = line.rstrip("\n")
        if s.startswith("## "):
            src = s[3:].strip(); continue
        if s.startswith("**") and s.endswith("**") and src:
            if cur and cur.get("title"):
                items.append(cur)
            cur = {"source": src, "title": s.strip("*").strip(), "link": "", "content": "", "pub_date": ""}
            continue
        if cur is None:
            continue
        if s.startswith("- URL: "):
            cur["link"] = s[7:].strip()
        elif s.startswith("- 日期: "):
            cur["pub_date"] = s[6:].strip()
        elif s.startswith("- 摘要: "):
            cur["content"] = s[6:].strip()
    if cur and cur.get("title"):
        items.append(cur)
    return items


def _relevant(text, kws):
    t = text.lower()
    for kw in kws:
        if len(kw) <= 3:
            if re.search(r"\b" + re.escape(kw) + r"\b", t):
                return True
        elif kw in t:
            return True
    return False


def _load_history(date_str, lookback):
    """读近 N 天 prepared/daily 历史的链接+标题用于去重。"""
    titles, urls = set(), set()
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return titles, urls
    for d in range(1, lookback + 1):
        ds = (target - timedelta(days=d)).strftime("%Y-%m-%d")
        for p in glob.glob(util.path_for("daily", f"{ds}*.md")) + glob.glob(util.path_for("prepared", f"prepared-daily-{ds}.json")):
            try:
                if p.endswith(".json"):
                    data = json.load(open(p, encoding="utf-8"))
                    for sec in data.get("sections", {}).values():
                        for c in sec.get("candidates", []):
                            urls.add(util.normalize_url(c.get("link", "")))
                else:
                    for line in open(p, encoding="utf-8"):
                        if "🔗" in line and "http" in line:
                            u = util.extract_url(line)
                            if u:
                                urls.add(util.normalize_url(u))
                        if line.startswith("### "):
                            titles.add(re.sub(r"[🔴🟡🔵🇨🇳⚡💉🤖🧬🔗]", "", line[4:]).strip().lower())
            except Exception:
                pass
    return titles, urls


def _is_dup(title, link, htitles, hurls, batch, thresh):
    nu = util.normalize_url(link)
    if nu and (nu in hurls or nu in batch):
        return True
    tl = title.lower()
    for h in htitles:
        if SequenceMatcher(None, tl, h).ratio() >= thresh:
            return True
    return False


def _prefetch(cands, http):
    def one(c):
        if not c.get("link", "").startswith("http"):
            c["fetch_ok"] = False; return
        html, ok = util.http_get(c["link"], http["timeout_s"], 1, http["user_agent"], max_bytes=http["prefetch_max_chars"] * 3)
        body = clean.from_html(html, c["link"]) if ok else ""
        if len(body) > 200:
            c["fulltext"] = body[:http["prefetch_max_chars"]]; c["fetch_ok"] = True
        else:
            c["fetch_ok"] = False
    with ThreadPoolExecutor(max_workers=http["prefetch_concurrency"]) as ex:
        list(ex.map(one, cands))


def prepare(date_str=None, raw_paths=None):
    sources, settings, kw = util.settings()
    date_str = date_str or util.today_str()
    meta = _source_meta()
    quota = settings["quota"]; dd = settings["dedup"]; ct = settings["content"]

    raw_paths = raw_paths or sorted(glob.glob(util.path_for("raw", f"raw-data-*-{date_str}.md")))
    raw = []
    for p in raw_paths:
        raw.extend(parse_raw(p))
    if not raw:
        print("❌ 无 raw-data"); return None

    htitles, hurls = _load_history(date_str, dd["lookback_days"])
    # 存"最近已报道清单"供 Cowork 生成子代理去重(中国/交叉/支付是现抓的,本地去重覆盖不到)
    cov_path = util.path_for("prepared", f"recent-coverage-{date_str}.json")
    json.dump({"date": date_str, "lookback_days": dd["lookback_days"],
               "titles": sorted(htitles), "links": sorted(hurls)},
              open(cov_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  最近已报道清单 → {cov_path}（{len(htitles)} 标题 / {len(hurls)} 链接）")
    sections = {k: [] for k in ("ai", "vaccine", "synbio", "cross", "china", "payment")}
    batch = set()
    st = {"dup": 0, "nocontent": 0, "noise": 0}

    for i, it in enumerate(raw):
        m = meta.get(it["source"], {"section": "ai", "tier": "T3", "priority": 9, "dual_china": False})
        content = it.get("content", "")
        has_content = len(content) >= ct["min_chars"]
        if not has_content:
            st["nocontent"] += 1
        dup = _is_dup(it["title"], it.get("link", ""), htitles, hurls, batch, dd["title_similarity_threshold"])
        nu = util.normalize_url(it.get("link", ""))
        if nu:
            batch.add(nu)
        if dup:
            st["dup"] += 1
        tl = it["title"].lower()
        noise = any(t in tl for t in ("[editorial]", "[correction]", "[comment]", "[letter]", "[erratum]", "research in brief"))
        if noise:
            st["noise"] += 1
        entry = {"id": i + 1, "source": it["source"], "section": m["section"], "tier": m["tier"],
                 "priority": m["priority"], "title": it["title"], "link": it.get("link", ""),
                 "content": content[:ct["default_truncate"]], "pub_date": it.get("pub_date", ""),
                 "is_duplicate": dup, "has_content": has_content, "is_noise": noise, "word_count": len(content)}
        # dual_china 源(AI/疫苗/合成生物/交叉的中国源)→ 同时进所属板块 + 🇨🇳中国动态。
        # 支付的中文源 dual_china=false，只进支付、不进 china(避免挤占中国动态配额)。
        if m["dual_china"]:
            ce = dict(entry, section="china", content=content[:ct["china_truncate"]])
            sections["china"].append(ce)
            sections[m["section"]].append(dict(entry))
        else:
            sections[m["section"]].append(entry)

    # 交叉匹配:AI∩疫苗/生物 → cross; AI∩支付 → payment
    for e in list(sections["ai"]):
        if not e["is_duplicate"] and e["has_content"]:
            txt = e["title"] + " " + e["content"][:500]
            if _relevant(txt, kw["vaccine"]) or _relevant(txt, kw["synbio"]):
                sections["cross"].append(dict(e, section="cross"))
            if _relevant(txt, kw["payment"]):
                sections["payment"].append(dict(e, section="payment"))
    for sec in ("vaccine", "synbio"):
        for e in list(sections[sec]):
            if not e["is_duplicate"] and e["has_content"] and _relevant(e["title"] + " " + e["content"][:500], kw["ai"]):
                sections["cross"].append(dict(e, section="cross"))

    out = {}
    for sec, items in sections.items():
        q = quota[sec]
        valid = [x for x in items if not x["is_duplicate"] and x["has_content"] and not x["is_noise"]]

        def key(x):
            base = x["priority"] + (0.5 if not x["pub_date"] else 0)
            kws = kw.get(sec)
            if kws and not _relevant(x["title"] + " " + x["content"][:500], kws):
                base += 3
            if sec == "payment" and _relevant(x["title"] + " " + x["content"][:200], kw["payment_noise"]):
                base += 5
            return (base, -min(x["word_count"], 1500))
        valid.sort(key=key)
        # 同源多样性上限:避免单一高产源(如NVIDIA一天发7篇)刷屏候选池,给模型更均衡的选择
        _div = settings.get("diversity", {})
        cap = _div.get("by_section", {}).get(sec, _div.get("max_per_source", 3))
        seen_src, capped = {}, []
        for x in valid:
            if seen_src.get(x["source"], 0) >= cap:
                continue
            seen_src[x["source"]] = seen_src.get(x["source"], 0) + 1
            capped.append(x)
        cands = capped[:q["candidates"]]
        out[sec] = {"candidates": cands, "quota": {k: q[k] for k in ("min", "target", "max")},
                    "total_raw": len(items), "total_valid": len(valid), "total_candidates": len(cands)}
        print(f"  [{sec}] raw={len(items)} valid={len(valid)} cands={len(cands)}")

    result = {"date": date_str, "generated_at": util.today_str(), "sections": out,
              "meta": {"total_raw": len(raw), **st}}
    out_path = util.path_for("prepared", f"prepared-daily-{date_str}.json")
    json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 预抓默认关闭:实测预抓正文多为 JSON-LD/oEmbed 噪音，且生成子代理只 web_fetch 自己选中的
    # 3-5 条(更省更准)。候选保留 RSS 摘要供选条;深度条目由模型按 common.md §9 自行补抓。
    # 如需重开(本地多干、模型少抓),在 settings.json 设 http.prefetch=true。
    if settings["http"].get("prefetch", False):
        print(f"\n  预抓全文中...")
        for sec in out.values():
            _prefetch(sec["candidates"], settings["http"])
        json.dump(result, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"✅ prepared → {out_path}(预抓={'on' if settings['http'].get('prefetch') else 'off'})")
    return out_path
