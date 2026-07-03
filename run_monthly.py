#!/usr/bin/env python3
"""run_monthly.py — 疫苗 & 合成生物学月报采集(本地 Mac / launchd,≥25 天一期)。

间隔硬控(≥25 天,OpenClaw 教训:不靠模型判断)
+ 聚合 30 天日报 💉疫苗/🧬合成生物/🔗交叉(事件→重大事件/融资/监管)
+ 专抓 vaccine/synbio 期刊与综述刊 RSS(→ 论文池 primary + 深度评述池 review)
+ ClinicalTrials.gov 管线数据(→ 临床管线表)
打包 prepared-monthly-END.json,供 Cowork 生成读取。中国动态/Layer2 深度源由 Cowork 侧现抓。

schedule.json 在「推送成功后」由 Cowork 更新(不在此写)。
用法: python3 run_monthly.py [--end DATE] [--days 30] [--force]
"""
import argparse, sys, os, json, traceback
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import aggregate, util
from lib.fetch import compute_alarms, _fetch_api

SCHEDULE_REL = "monthly/monthly-schedule.json"
MIN_INTERVAL_DAYS = 25


def _interval_ok(end, force):
    if force:
        return True, "force"
    p = os.path.join(os.path.expanduser(util.root_dir()), SCHEDULE_REL)
    if not os.path.exists(p):
        return True, "首次(无 schedule)"
    try:
        last = json.load(open(p, encoding="utf-8")).get("last_run")
        d = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        if d < MIN_INTERVAL_DAYS:
            return False, f"距上次 {last} 仅 {d} 天(<{MIN_INTERVAL_DAYS}),跳过"
        return True, f"距上次 {last} {d} 天"
    except Exception as e:
        return True, f"schedule 解析失败({e}),按首次跑"


def collect_pool(end, days, http, log):
    """专抓 vaccine+synbio 的 rss(论文 primary + 综述 review)+ ClinicalTrials api(管线)。带源体检。"""
    sources, _, _ = util.settings()
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    papers, pipeline, health = [], [], {}
    for sec in ("vaccine", "synbio"):
        for src in sources[sec]:
            if not src.get("enabled", True):
                continue
            m = src.get("method")
            if m == "rss":
                items, ok = [], False
                try:
                    txt, ok = util.http_get(src["url"], http["timeout_s"], 2, http["user_agent"])
                    if ok:
                        for it in util.parse_feed(txt):
                            pub = util.parse_date(it.get("pub"))
                            if pub and (end_dt - pub).days > days:
                                continue
                            items.append({"title": it["title"], "link": it["link"], "section": sec,
                                          "source": src["name"], "is_review": bool(src.get("review")),
                                          "pub": it.get("pub", ""), "summary": (it.get("summary") or "")[:1000]})
                except Exception as e:
                    log(f"  [{sec}] {src['name']:30} ERR {e}")
                papers.extend(items)
                health[src["name"]] = {"section": sec, "count": len(items), "ok": ok}
                log(f"  [{sec}] {src['name']:30} {'OK '+str(len(items)) if ok else 'FAIL':>7}{'  [review]' if src.get('review') else ''}")
            elif m == "api" and "clinicaltrials.gov" in (src.get("url") or ""):
                try:
                    items, ok = _fetch_api(src, http)
                except Exception:
                    items, ok = [], False
                pipeline.extend(items)
                health[src["name"]] = {"section": sec, "count": len(items), "ok": ok}
                log(f"  [{sec}] {src['name']:30} {'OK '+str(len(items)) if ok else 'FAIL':>7}  (临床管线)")
    return papers, pipeline, health


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--online-only", action="store_true",
                    help="只抓期刊/综述/临床在线池,跳过间隔判断 + 跳过本地日报聚合(events 空);供 GitHub collect-monthly 用")
    ap.add_argument("--no-online", action="store_true",
                    help="只聚合本地日报事件(疫苗/合成生物/交叉),跳过间隔判断 + 跳过在线抓取;供 Cowork 本地离线运行,论文/管线池由 GitHub 另抓")
    args = ap.parse_args()
    end = args.end or util.today_str()
    _, settings, _ = util.settings()
    http = settings["http"]

    log_f = open(os.path.join(util.path_for("logs"), f"monthly-{end}.log"), "a", encoding="utf-8")
    def emit(m):
        line = f"[{datetime.now(timezone.utc).isoformat()}] {m}"; print(line); log_f.write(line + "\n"); log_f.flush()

    emit(f"=== 疫苗&合成生物月报采集 END={end} ({args.days}天) ===")
    if not (args.online_only or args.no_online):
        ok_run, why = _interval_ok(end, args.force)
        emit(f"间隔判断: {why}")
        if not ok_run:
            emit("→ 未到周期,跳过(用 --force 强制)"); log_f.close(); sys.exit(0)
    try:
        # 本地日报事件(疫苗/合成生物/交叉;--online-only 跳过)
        if args.online_only:
            events = {k: [] for k in ("vaccine", "synbio", "cross")}; dailies_loaded = 0
            emit("--online-only:跳过本地日报聚合(events 空;由 Cowork 本地另聚)")
        else:
            emit("聚合 30 天日报 💉疫苗/🧬合成生物/🔗交叉...")
            agg = aggregate.aggregate(end, args.days, sections=["vaccine", "synbio", "cross"])
            events = {k: agg["sections"].get(k, []) for k in ("vaccine", "synbio", "cross")}
            dailies_loaded = agg["dailies_loaded"]
            emit(f"  读到 {dailies_loaded} 份日报 → 疫苗{len(events['vaccine'])} 合成生物{len(events['synbio'])} 交叉{len(events['cross'])}")

        # 在线:期刊论文池 + 综述评述池 + ClinicalTrials 管线(--no-online 跳过)
        if args.no_online:
            papers, pipeline, health, prim, rev = [], [], {}, [], []
            emit("--no-online:跳过在线抓取(论文/管线空;由 GitHub collect-monthly 另抓、Pages 提供)")
        else:
            emit("专抓期刊与综述刊(论文池 + 深度评述池)+ ClinicalTrials 管线...")
            papers, pipeline, health = collect_pool(end, args.days, http, emit)
            prim = [p for p in papers if not p["is_review"]]
            rev = [p for p in papers if p["is_review"]]
            emit(f"  论文候选 {len(prim)}(原始) | 深度评述候选 {len(rev)}(review刊) | 管线 {len(pipeline)}")
            alarms = compute_alarms(health, {})
            if alarms["fail"] or alarms["persistent_zero"]:
                emit(f"  ⚠️ 源告警: 失败{alarms['fail']} 零产{alarms['persistent_zero']}")

        out = {"end": end, "days": args.days, "dailies_loaded": dailies_loaded,
               "events": events, "papers": papers, "pipeline": pipeline, "health": health,
               "meta": {"events": {k: len(v) for k, v in events.items()},
                        "papers_primary": len(prim), "papers_review": len(rev), "pipeline": len(pipeline)}}
        outpath = util.path_for("prepared", f"prepared-monthly-{end}.json")
        json.dump(out, open(outpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        emit(f"✅ 完成 → {outpath}")
        emit(f"   论文池 原始{len(prim)}/综述{len(rev)} | 管线{len(pipeline)} | 事件 疫苗{len(events['vaccine'])}+合成生物{len(events['synbio'])}+交叉{len(events['cross'])}")
        if not args.online_only and dailies_loaded < args.days * 0.5:
            emit(f"⚠️ 仅 {dailies_loaded}/{args.days} 份日报归档,事件/融资/监管池可能偏薄(Cowork 侧酌情 WebSearch 补)")
    except Exception as e:
        emit(f"❌ 异常: {e}\n{traceback.format_exc()}"); sys.exit(1)
    finally:
        log_f.close()


if __name__ == "__main__":
    main()
