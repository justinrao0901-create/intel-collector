"""papers.py — AI 论文候选池(周报用)。本地抓(国际源),产出广候选池供模型精选 8-10 篇五段式精译。

源(2026 优化版):
  HF Daily Papers API(每日社区投票)+ arXiv RSS 6 类(CL/AI/LG/CV/RO/MA)+ HN Algolia 高分
  + Semantic Scholar(引用增速,无 key、best-effort 限流重试)+ alphaXiv / Emergent Mind(趋势,best-effort)
机制:survey 标题加权置顶(保证≥1篇综述进候选)+ 中国机构补偿加分(英文热度天然低)。
真正的中国论文 / 综述兜底由 Cowork 生成侧 WebSearch 补(见 generate/rules/weekly.md)。

用法:python3 lib/papers.py --end 2026-06-13 --days 7
输出:prepared/weekly-papers-END.json
"""
from __future__ import annotations
import os, re, json, time, glob, argparse
from datetime import datetime, timedelta
from . import util
from .fetch import compute_alarms   # 复用日报那套产量告警逻辑


def _prior_papers_health(end_date):
    """取上一份 papers-health(判定论文源骤降归零)。"""
    d = os.path.dirname(util.path_for("prepared", "x"))
    files = sorted(f for f in glob.glob(os.path.join(d, "papers-health-*.json")) if end_date not in f)
    if not files:
        return {}
    try:
        return (json.load(open(files[-1], encoding="utf-8")) or {}).get("sources", {})
    except Exception:
        return {}

ARXIV_CATS = ["cs.CL", "cs.AI", "cs.LG", "cs.CV", "cs.RO", "cs.MA"]

SURVEY_KW = ("survey", "comprehensive review", "a review", "review of", "literature review",
             "systematic review", "taxonomy", "lessons from", "best practices", "a guide to",
             "overview of", "empirical study", "综述", "评述")
CHINA_KW = ("tsinghua", "peking university", "chinese academy", "shanghai ai lab", "shanghai artificial intelligence",
            "fudan", "zhejiang university", "deepseek", "alibaba", "qwen", "zhipu", "glm", "moonshot", "kimi",
            "minimax", "baidu", "tencent", "bytedance", "huawei", "中国", "清华", "北大", "中科院")


def _is_survey(text):
    t = text.lower()
    return any(k in t for k in SURVEY_KW)


def _is_china(text):
    t = text.lower()
    return any(k in t for k in CHINA_KW) or bool(re.search(r"[一-鿿]", text))


def fetch_hf_daily(end, days, http):
    """HF Daily Papers:遍历区间每天,收集带 upvotes 的论文。"""
    out = {}
    for d in range(days):
        ds = (end - timedelta(days=d)).strftime("%Y-%m-%d")
        txt, ok = util.http_get(f"https://huggingface.co/api/daily_papers?date={ds}", http["timeout_s"], 2, http["user_agent"])
        if not ok:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        for p in (data if isinstance(data, list) else []):
            paper = p.get("paper", p)
            pid = paper.get("id", "")
            if not pid:
                continue
            up = paper.get("upvotes", p.get("upvotes", 0)) or 0
            if pid in out:
                out[pid]["upvotes"] = max(out[pid]["upvotes"], up)
            else:
                out[pid] = {
                    "id": pid, "title": paper.get("title", "").strip(),
                    "abstract": util.strip_html(paper.get("summary", ""))[:1200],
                    "link": f"https://arxiv.org/abs/{pid}", "upvotes": up, "hn_points": 0,
                    "citations": None, "source": "HF Daily", "pub": (p.get("publishedAt") or ds)[:10]}
    return list(out.values())


def fetch_arxiv(http):
    """arXiv RSS 6 类新发布。"""
    out = {}
    for cat in ARXIV_CATS:
        txt, ok = util.http_get(f"https://rss.arxiv.org/rss/{cat}", http["timeout_s"], 2, http["user_agent"])
        if not ok:
            continue
        for it in util.parse_feed(txt):
            link = it.get("link", "")
            m = re.search(r"(\d{4}\.\d{4,5})", link)
            pid = m.group(1) if m else link
            if pid in out:
                out[pid]["cats"].append(cat)
                continue
            out[pid] = {"id": pid, "title": it["title"].strip(),
                        "abstract": it.get("summary", "")[:1200], "link": link,
                        "upvotes": 0, "hn_points": 0, "citations": None,
                        "source": "arXiv", "pub": it.get("pub", "")[:10], "cats": [cat]}
    return list(out.values())


def fetch_hn(http):
    """HN Algolia:高分 AI / arxiv 帖。"""
    out = []
    url = "https://hn.algolia.com/api/v1/search_by_date?tags=story&query=arxiv&numericFilters=points%3E40"
    txt, ok = util.http_get(url, http["timeout_s"], 2, http["user_agent"])
    if not ok:
        return out
    try:
        data = json.loads(txt)
    except Exception:
        return out
    for h in data.get("hits", [])[:30]:
        link = h.get("url") or ""
        out.append({"title": h.get("title", ""), "link": link, "hn_points": h.get("points", 0),
                    "id": (re.search(r"(\d{4}\.\d{4,5})", link) or [None, link])[1] if link else h.get("objectID"),
                    "upvotes": 0, "citations": None, "abstract": "", "source": "HN", "pub": (h.get("created_at") or "")[:10]})
    return out


def fetch_besteffort(http):
    """alphaXiv / Emergent Mind 趋势页(best-effort,失败不影响主流程)。仅取链接+标题作信号。"""
    out = []
    for name, url in (("alphaXiv", "https://www.alphaxiv.org/"), ("Emergent Mind", "https://www.emergentmind.com/")):
        txt, ok = util.http_get(url, http["timeout_s"], 1, http["user_agent"])
        if not ok:
            continue
        for m in re.finditer(r'href="([^"]*?(?:arxiv\.org/abs/|/abs/)(\d{4}\.\d{4,5})[^"]*)"', txt):
            out.append({"id": m.group(2), "title": "", "link": f"https://arxiv.org/abs/{m.group(2)}",
                        "upvotes": 0, "hn_points": 0, "citations": None, "abstract": "", "source": name, "pub": ""})
    return out


def enrich_ss(papers, http, limit=40):
    """Semantic Scholar 补引用数(无 key,限流 + 重试,best-effort)。只查前 limit 篇有 arXiv id 的。"""
    n = 0
    for p in papers:
        if n >= limit:
            break
        pid = p.get("id", "")
        if not re.match(r"^\d{4}\.\d{4,5}$", str(pid)):
            continue
        n += 1
        url = f"https://api.semanticscholar.org/graph/v1/paper/arXiv:{pid}?fields=citationCount,influentialCitationCount,tldr"
        txt, ok = util.http_get(url, http["timeout_s"], 1, http["user_agent"])
        if ok:
            try:
                d = json.loads(txt)
                p["citations"] = d.get("citationCount")
                if d.get("tldr") and not p.get("abstract"):
                    p["abstract"] = (d["tldr"].get("text") or "")[:1200]
            except Exception:
                pass
        time.sleep(1.1)  # 无 key 限流,温柔点


def collect(end_date, days=7):
    _, settings, _ = util.settings()
    http = settings["http"]
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        print("❌ 日期格式错"); return None

    # 合并各源(按 arXiv id / 链接去重,累加热度信号)
    merged = {}
    def add(papers):
        for p in papers:
            key = str(p.get("id") or p.get("link", ""))[:60]
            if not key:
                continue
            if key not in merged:
                merged[key] = p
            else:
                m = merged[key]
                m["upvotes"] = max(m.get("upvotes", 0), p.get("upvotes", 0))
                m["hn_points"] = max(m.get("hn_points", 0), p.get("hn_points", 0))
                if p.get("title") and not m.get("title"):
                    m["title"] = p["title"]
                if p.get("abstract") and not m.get("abstract"):
                    m["abstract"] = p["abstract"]
                srcs = set((m.get("source", "") + "+" + p.get("source", "")).split("+"))
                m["source"] = "+".join(sorted(s for s in srcs if s))

    hf = fetch_hf_daily(end, days, http); print(f"  HF Daily... {len(hf)}"); add(hf)
    ax = fetch_arxiv(http);               print(f"  arXiv 6 类... {len(ax)}"); add(ax)
    hn = fetch_hn(http);                  print(f"  HN... {len(hn)}"); add(hn)
    be = fetch_besteffort(http);          print(f"  alphaXiv / Emergent Mind... {len(be)}"); add(be)
    # 论文源体检(周报核心是论文,arXiv/HF 静默归零必须可见)
    src_health = {"HF Daily": {"count": len(hf), "ok": len(hf) > 0},
                  "arXiv": {"count": len(ax), "ok": len(ax) > 0},
                  "HN": {"count": len(hn), "ok": len(hn) > 0},
                  "alphaXiv·Emergent": {"count": len(be), "ok": len(be) > 0}}
    papers = list(merged.values())
    # 引用增速(综述/存量高引会冒头)
    papers.sort(key=lambda x: (x.get("upvotes", 0) + x.get("hn_points", 0)), reverse=True)
    print(f"  Semantic Scholar 补引用(前40)..."); enrich_ss(papers, http)

    # 评分 + 标记
    for p in papers:
        text = (p.get("title", "") + " " + p.get("abstract", ""))
        p["is_survey"] = _is_survey(text)
        p["is_china"] = _is_china(text)
        score = p.get("upvotes", 0) * 3 + p.get("hn_points", 0) * 1
        if p.get("citations"):
            score += min(p["citations"], 200) * 0.5      # 高引(综述)加分,封顶
        if p["is_survey"]:
            score += 60                                   # 综述加权置顶(保证进候选)
        if p["is_china"]:
            score += 35                                   # 中国机构补偿
        p["score"] = round(score, 1)

    papers.sort(key=lambda x: x["score"], reverse=True)
    pool = papers[:50]   # 广候选池 ~50 篇,模型从中精选 8-10

    out = {"end": end_date, "days": days, "total": len(papers), "pool_size": len(pool),
           "surveys": sum(1 for p in pool if p["is_survey"]), "china": sum(1 for p in pool if p["is_china"]),
           "papers": pool}
    path = util.path_for("prepared", f"weekly-papers-{end_date}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 论文源体检告警(对比上一份产量)
    alarms = compute_alarms(src_health, _prior_papers_health(end_date))
    hpath = util.path_for("prepared", f"papers-health-{end_date}.json")
    json.dump({"end": end_date, "sources": src_health, "alarms": alarms}, open(hpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\n✅ 论文候选池 → {path}")
    print(f"   总抓取 {len(papers)} → 候选池 {len(pool)}(综述 {out['surveys']} / 中国 {out['china']})")
    if alarms["fail"] or alarms["sudden_drop"] or alarms["persistent_zero"]:
        print("⚠️ 论文源健康告警:")
        if alarms["sudden_drop"]: print(f"   🔴 骤降归零(疑似临时失败,论文池可能缩水): {alarms['sudden_drop']}")
        if alarms["fail"]:        print(f"   🔴 抓取失败: {alarms['fail']}")
        if alarms["persistent_zero"]: print(f"   🟡 持续归零: {alarms['persistent_zero']}")
        print(f"   → {hpath}")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    collect(args.end, args.days)


if __name__ == "__main__":
    main()
