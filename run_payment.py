#!/usr/bin/env python3
"""run_payment.py — 支付双周报采集单入口(本地 Mac / launchd 周六调用)。

间隔硬控(≥14 天,OpenClaw 教训:不靠模型判断)+ 聚合 14 天日报💳板块(事件池)
+ 抓深度文章 Layer1 RSS 候选池,打包 prepared-payment-END.json 供 Cowork 生成读取。
Layer2(搜索)/ Layer3(作者)由 Cowork 侧现抓(见 generate/rules/payment.md)。

schedule.json 在「推送成功后」由 Cowork 更新(不在此写,避免失败后空等一个周期)。

用法: python3 run_payment.py [--end DATE] [--days 14] [--force]
"""
import argparse, sys, os, json, traceback
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import aggregate, util
from lib.fetch import compute_alarms

# 深度文章 Layer1 候选源(Mac 可 RSS 抓 → 候选索引)。无稳定 RSS 的(FXC/同业官博/Mastercard/
# Messari/中国媒体)由 Cowork 侧 Layer2/3 web_fetch/WebSearch 现抓,见 rules/payment.md。
DEEP_SOURCES = [
    {"name": "Bits about Money", "url": "https://www.bitsaboutmoney.com/archive/rss/"},
    {"name": "Net Interest",     "url": "https://www.netinterest.co/feed"},
    {"name": "The Diff",         "url": "https://www.thediff.co/feed"},
    {"name": "The Block",        "url": "https://www.theblock.co/rss.xml"},
    {"name": "Finextra",         "url": "https://www.finextra.com/rss/headlines.aspx"},
    {"name": "Payments Cards & Mobile", "url": "https://paymentsindustryintelligence.com/feed"},
    {"name": "Thunes",           "url": "https://www.thunes.com/feed"},
    {"name": "Ledger Insights",  "url": "https://www.ledgerinsights.com/feed/"},
    {"name": "PYMNTS Cross-Border", "url": "https://www.pymnts.com/tag/cross-border-payments/feed/"},
    # Circle Blog 无稳定 RSS(已移除),改由日报 payment 段(scrape)+ Layer2 WebSearch 覆盖
]
SCHEDULE_REL = "payment/biweekly-schedule.json"
MIN_INTERVAL_DAYS = 14


def _interval_ok(end, force):
    """硬控:距上次 ≥14 天才跑。无 schedule=首次=跑。"""
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


def collect_deep(end, days, http, log, cap=8):
    """抓 Layer1 RSS → 深度文章候选索引(14 天窗)。带源体检 + 每源上限(防高产新闻源淹没低频精析源)。"""
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    _floor = datetime.min.replace(tzinfo=timezone.utc)
    cands, health = [], {}
    for s in DEEP_SOURCES:
        items, ok = [], False
        try:
            txt, ok = util.http_get(s["url"], http["timeout_s"], 2, http["user_agent"])
            if ok:
                for it in util.parse_feed(txt):
                    pub = util.parse_date(it.get("pub"))
                    if pub and (end_dt - pub).days > days:
                        continue
                    items.append({"title": it["title"], "link": it["link"], "source": s["name"],
                                  "pub": it.get("pub", ""), "summary": (it.get("summary") or "")[:600]})
        except Exception as e:
            log(f"  [deep] {s['name']:26} ERR {e}")
        raw_n = len(items)
        if cap and raw_n > cap:                      # 每源上限:保留最新 cap 篇,防高产源刷屏候选索引
            items.sort(key=lambda x: util.parse_date(x.get("pub")) or _floor, reverse=True)
            items = items[:cap]
        health[s["name"]] = {"count": len(items), "ok": ok}   # 体检计 capped 后数;0=零产告警
        cands.extend(items)
        tag = (f"OK {raw_n}→{len(items)}" if cap and raw_n > cap else f"OK {len(items)}") if ok else "FAIL"
        log(f"  [deep] {s['name']:26} {tag}")
    return cands, health


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default=None)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    end = args.end or util.today_str()
    _, settings, _ = util.settings()
    http = settings["http"]
    _div = settings.get("diversity", {})
    deep_cap = _div.get("by_section", {}).get("payment_deep", _div.get("max_per_source", 3))

    log_f = open(os.path.join(util.path_for("logs"), f"payment-{end}.log"), "a", encoding="utf-8")
    def emit(m):
        line = f"[{datetime.now(timezone.utc).isoformat()}] {m}"; print(line); log_f.write(line + "\n"); log_f.flush()

    emit(f"=== 支付双周报采集 END={end} ({args.days}天) ===")
    ok_run, why = _interval_ok(end, args.force)
    emit(f"间隔判断: {why}")
    if not ok_run:
        emit("→ 未到周期,跳过(用 --force 强制)"); log_f.close(); sys.exit(0)
    try:
        emit("聚合 14 天日报💳支付板块...")
        agg = aggregate.aggregate(end, args.days, sections=["payment"])
        events = agg["sections"].get("payment", [])
        emit(f"  读到 {agg['dailies_loaded']} 份日报 → 支付事件池 {len(events)}")

        emit(f"抓深度文章 Layer1 候选(每源上限 {deep_cap})...")
        deep_cands, deep_health = collect_deep(end, args.days, http, emit, cap=deep_cap)
        alarms = compute_alarms(deep_health, {})   # 深度源首期无 prior,标 fail/zero
        emit(f"  深度候选 {len(deep_cands)} 篇 / {len(DEEP_SOURCES)} 源")
        if alarms["fail"] or alarms["persistent_zero"]:
            emit(f"  ⚠️ 深度源告警: 失败{alarms['fail']} 零产{alarms['persistent_zero']}")

        out = {"end": end, "days": args.days, "dailies_loaded": agg["dailies_loaded"],
               "events": events, "deep_candidates": deep_cands, "deep_health": deep_health,
               "meta": {"events": len(events), "deep": len(deep_cands)}}
        outpath = util.path_for("prepared", f"prepared-payment-{end}.json")
        json.dump(out, open(outpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        emit(f"✅ 完成 → {outpath}")
        emit(f"   事件{len(events)} 深度候选{len(deep_cands)}")
        if agg["dailies_loaded"] < args.days:
            emit(f"⚠️ 仅 {agg['dailies_loaded']}/{args.days} 份日报归档,事件池可能偏薄(Cowork 侧酌情 WebSearch 补)")
    except Exception as e:
        emit(f"❌ 异常: {e}\n{traceback.format_exc()}"); sys.exit(1)
    finally:
        log_f.close()


if __name__ == "__main__":
    main()
