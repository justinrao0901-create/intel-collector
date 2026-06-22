#!/usr/bin/env python3
"""run_weekly.py — AI 周报采集单入口(本地 Mac / launchd 调用,每周五)。

聚合本周日报的 AI 重大事件 + 中国动态(从日报提炼,不重新采集)+ 抓 AI 论文候选池,
打包成 prepared/prepared-weekly-WNN.json,供 Cowork 周报生成读取。

覆盖范围:END 往前 7 天(上周五→本周四,或按 --days)。
用法:python3 run_weekly.py            # END=今天
      python3 run_weekly.py --end 2026-06-12
"""
import argparse, sys, os, json, traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import aggregate, papers, util


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    end = args.end or util.today_str()
    y, wk, _ = datetime.strptime(end, "%Y-%m-%d").isocalendar()
    week = f"{y}-W{wk:02d}"

    log = open(os.path.join(util.path_for("logs"), f"weekly-{end}.log"), "a", encoding="utf-8")
    def emit(m):
        line = f"[{datetime.now(timezone.utc).isoformat()}] {m}"
        print(line); log.write(line + "\n"); log.flush()

    emit(f"=== AI 周报采集 {week}(END={end}, {args.days}天) ===")
    try:
        # 1. 从日报归档聚合 AI 重大事件 + 中国动态(新闻类不重新采集)
        emit("聚合本周日报(AI 事件 + 中国动态)...")
        agg = aggregate.aggregate(end, args.days, sections=["ai", "china"])
        events = agg["sections"].get("ai", [])
        china = agg["sections"].get("china", [])
        emit(f"  读到 {agg['dailies_loaded']} 份日报 → AI事件 {len(events)} | 中国 {len(china)}")

        # 2. 抓 AI 论文候选池(HF/arXiv/HN/SS/alphaXiv/Emergent)
        emit("抓论文候选池...")
        papers.collect(end, args.days)
        pp = util.path_for("prepared", f"weekly-papers-{end}.json")
        paper_pool = json.load(open(pp, encoding="utf-8")) if os.path.exists(pp) else {"papers": []}
        # 论文源体检告警(写入周报日志,供 PLAYBOOK-weekly 步0 上报)
        hp = util.path_for("prepared", f"papers-health-{end}.json")
        if os.path.exists(hp):
            al = (json.load(open(hp, encoding="utf-8")) or {}).get("alarms", {})
            if al.get("sudden_drop") or al.get("fail") or al.get("persistent_zero"):
                emit(f"⚠️ 论文源告警: 骤降{al.get('sudden_drop')} 失败{al.get('fail')} 持续0{al.get('persistent_zero')}")

        # 3. 打包 prepared-weekly
        out = {
            "week": week, "end": end, "days": args.days,
            "dailies_loaded": agg["dailies_loaded"],
            "events": events,          # → 🔴 本周重大事件(模型精选 ≤10)
            "china": china,            # → 🇨🇳 中国动态(模型精选 3-5)
            "papers": paper_pool.get("papers", []),  # → 📄 论文精要(模型精选 8-10 五段式)
            "papers_meta": {k: paper_pool.get(k) for k in ("total", "pool_size", "surveys", "china")},
        }
        outpath = util.path_for("prepared", f"prepared-weekly-{end}.json")
        json.dump(out, open(outpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        emit(f"✅ 完成 → {outpath}")
        emit(f"   {week}: 事件{len(events)} 中国{len(china)} 论文池{len(out['papers'])}(综述{out['papers_meta'].get('surveys')}/中国{out['papers_meta'].get('china')})")
        if agg["dailies_loaded"] == 0:
            emit("⚠️ 本周无日报归档——重大事件/中国动态将为空,Cowork 侧需 WebSearch 兜底")
        elif agg["dailies_loaded"] < args.days:
            emit(f"⚠️ 残周告警:仅 {agg['dailies_loaded']}/{args.days} 份日报归档,重大事件/中国动态可能偏薄,Cowork 侧酌情 WebSearch 补全(本周非完整周)")
    except Exception as e:
        emit(f"❌ 异常: {e}\n{traceback.format_exc()}")
        sys.exit(1)
    finally:
        log.close()


if __name__ == "__main__":
    main()
