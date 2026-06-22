#!/usr/bin/env python3
"""run_daily.py — 日报采集单入口(本地 Mac / launchd 调用)。

一个入口脚本内部串联 fetch → prepare，避免多步调度(根除旧系统的"幻觉分步执行")。
产出:raw-data + prepared-daily-DATE.json + search-manifest，写入共享文件夹，供 Cowork 模型读取生成。

用法:
  python3 run_daily.py                # 采集全部板块(今天)
  python3 run_daily.py --date 2026-06-11
  python3 run_daily.py --section ai   # 只采 AI(调试用)
"""
import argparse, sys, os, traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import fetch, prepare, util


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--section", default=None, help="只采单板块(ai/vaccine/payment/...),默认全部")
    args = ap.parse_args()
    date_str = args.date or util.today_str()

    log_dir = util.path_for("logs")
    log = open(os.path.join(log_dir, f"daily-{date_str}.log"), "a", encoding="utf-8")
    def emit(msg):
        line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
        print(line); log.write(line + "\n"); log.flush()

    emit(f"=== 日报采集开始 {date_str} (section={args.section or 'all'}) ===")
    try:
        # Step1: 采集(全部域或单域)
        if args.section:
            fetch.fetch_all(section_filter=args.section, date_str=date_str)
        else:
            # 一次抓全部 5 个信源数组(ai/vaccine/synbio/cross/payment)。
            # china 无独立信源，由 ai/vaccine 中 dual_china 源派生(prepare 阶段)。
            emit("--- 采集全部信源(5 个数组) ---")
            fetch.fetch_all(section_filter=None, date_str=date_str)
        # Step2: 预处理(去重/分板块/排序/预抓) → prepared JSON
        emit("--- 预处理 ---")
        out = prepare.prepare(date_str=date_str)
        if out:
            emit(f"✅ 完成 → {out}")
        else:
            emit("❌ 预处理无输出(无 raw-data)"); sys.exit(1)
    except Exception as e:
        emit(f"❌ 异常: {e}\n{traceback.format_exc()}")
        sys.exit(1)
    finally:
        log.close()


if __name__ == "__main__":
    main()
