# intel-collector

情报采集器(纯 Python 标准库)。GitHub Actions 定时运行采集脚本,抓取**公开信源**
(RSS / API / 网页)→ 产出 `prepared/prepared-*.json` 候选包 → 提交回本仓库。
下游的"生成 / 精译 / 推送"在另一侧进行;本仓库只负责**确定性采集**。

## 运行
- `.github/workflows/collect-daily.yml` — 每日北京 07:07 自动跑 `run_daily.py`;也可在 **Actions** 页手动触发。
- 输出在 `prepared/`:`prepared-daily-<date>.json`(候选包)+ `search-manifest` / `recent-coverage` / `source-health`。

## 脚本
- `run_daily.py` / `run_weekly.py` / `run_payment.py` / `run_monthly.py` — 四类报告的采集入口
- `lib/` — 采集库(HTTP / RSS·Atom·RDF 解析、去重 / 打分、聚合、源体检);**纯标准库,无需 pip**
- `config/` — 信源清单 / 全局设置 / 关键词

> 本仓库仅含采集层与公开信源清单,**不含任何密钥、个人路径或下游业务逻辑**。
