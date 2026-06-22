"""aggregate.py — 从日报归档聚合「重大事件 / 中国动态」,供周报/月报/双周报复用。

SERVICE_SPEC 核心设计:周报/月报/双周报的新闻类内容**从日报历史提炼、不重新采集**。
读 daily/DATE.md 区间 → 按板块抽条目(标题/链接/🔴/🐾研判/日期)→ 去重 → 🔴优先排序。

用法(测试):python3 lib/aggregate.py --end 2026-06-11 --days 7 --sections ai,china
"""
from __future__ import annotations
import os, re, glob, json, argparse
from datetime import datetime, timedelta
from . import util

SEC_EMOJI = {"🤖": "ai", "💉": "vaccine", "💳": "payment", "🇨🇳": "china", "🧬": "synbio", "🔗": "cross"}


def parse_daily(path):
    """解析一份 daily/DATE.md → {section: [item,...]}。"""
    by_sec, cur, item = {}, None, None

    def flush():
        nonlocal item
        if item and cur:
            item["body"] = " ".join(item["body"])[:600]
            by_sec.setdefault(cur, []).append(item)
        item = None

    for raw in open(path, encoding="utf-8"):
        st = raw.strip()
        if st.startswith("## "):
            flush(); cur = next((n for e, n in SEC_EMOJI.items() if e in st), None); continue
        if st.startswith("### ") and cur:
            flush()
            title = re.sub(r"^###\s*", "", st)
            item = {"title": title, "is_major": "🔴" in title, "is_brief": "⚡" in title,
                    "link": "", "research": "", "source": "", "body": []}
            continue
        if item is None:
            continue
        if st.startswith("**Source**") or re.match(r"^Source", st):
            item["source"] = st.replace("**", "")
        elif st.startswith("🔗") and "http" in st:
            m = re.search(r"https?://\S+", st)
            if m:
                item["link"] = m.group(0)
        elif st.startswith("🐾"):
            item["research"] = st
        elif st and not st.startswith(("> ", "📍", "【", "*本", "---")):
            item["body"].append(st)
    flush()
    return by_sec


def aggregate(end_date, days, sections=None):
    """聚合 [end-days+1, end] 的日报 → {section: [item,...]}。🔴优先,跨日去重。"""
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return {}
    daily_dir = util.path_for("daily")
    agg, seen = {}, set()
    loaded = 0
    for d in range(days):
        ds = (end - timedelta(days=d)).strftime("%Y-%m-%d")
        path = os.path.join(daily_dir, f"{ds}.md")
        if not os.path.exists(path):
            continue
        loaded += 1
        for sec, items in parse_daily(path).items():
            if sections and sec not in sections:
                continue
            for it in items:
                key = util.normalize_url(it["link"]) or it["title"][:40].lower()
                if key in seen:
                    continue
                seen.add(key)
                it["date"] = ds
                agg.setdefault(sec, []).append(it)
    # 排序:🔴 重大优先,其次按日期新→旧;🔵简讯沉底
    for sec in agg:
        agg[sec].sort(key=lambda x: (not x["is_major"], x["is_brief"], x.get("date", "")), reverse=False)
    return {"end": end_date, "days": days, "dailies_loaded": loaded,
            "sections": {s: agg.get(s, []) for s in (sections or agg.keys())}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--sections", default="")
    args = ap.parse_args()
    secs = [s.strip() for s in args.sections.split(",") if s.strip()] or None
    r = aggregate(args.end, args.days, secs)
    print(f"聚合 {args.end} 往前 {args.days} 天 | 读到 {r['dailies_loaded']} 份日报")
    for sec, items in r["sections"].items():
        majors = sum(1 for i in items if i["is_major"])
        print(f"  [{sec}] {len(items)} 条(🔴{majors})")
        for it in items[:4]:
            print(f"      {'🔴' if it['is_major'] else '  '} {it['title'][:60]}")


if __name__ == "__main__":
    main()
