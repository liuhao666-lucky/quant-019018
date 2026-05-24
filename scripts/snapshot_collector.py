"""
snapshot_collector.py
TMT-Alpha 7.0 — 14:45 盘中快照采集脚本

通过免费 API 获取当日约 14:45 的实时数据：
- 中证TMT(000998) 实时涨跌幅、成交量
- 四大子指数实时涨跌幅
- 基金 019018 实时估算净值（若有）

写入 snapshot_1445 表。

用法:
  python scripts/snapshot_collector.py              # 采集当日快照
  python scripts/snapshot_collector.py --date 2025-05-23  # 补录指定日期
"""

import sys
import time
import json
import re
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.data_pipeline import save_snapshot_1445, _normalize_date_str

# 日志配置
LOG_PATH = Path(__file__).parent / "snapshot_collector.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# 指数配置
INDICES = [
    {"code": "000998", "name": "中证TMT", "col": "tmt_chg_pct"},
    {"code": "931160", "name": "通信设备", "col": "ce_chg_pct"},
    {"code": "931494", "name": "消费电子", "col": "aic_chg_pct"},
    {"code": "H30184", "name": "半导体",   "col": "semi_chg_pct"},
    {"code": "000941", "name": "新能源",   "col": "ne_chg_pct"},
]

FUND_CODE = "019018"
ONEDAY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday"
FUND_EST_URL = "https://fundgz.1234567.com.cn/js/{code}.js"

MAX_RETRIES = 3
RETRY_DELAY = 3  # 秒


def _create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.csindex.com.cn/",
    })
    return session


def fetch_index_realtime(session: requests.Session, index_code: str) -> dict:
    """从 csindex oneday 接口获取指数实时数据"""
    url = f"{ONEDAY_URL}?indexCode={index_code}"
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise Exception(f"接口返回 success=false: {data}")

    header = data.get("data", {}).get("intraDayHeader")
    if not header:
        raise Exception(f"未找到 intraDayHeader: {data}")

    return {
        "trade_date": header.get("tradeDate", ""),
        "close": header.get("current"),
        "prev_close": header.get("closePre"),
        "change_pct": header.get("changePct"),
        "volume": header.get("tradingVol"),
    }


def fetch_fund_estimate(fund_code: str) -> dict:
    """
    从天天基金获取基金实时估算净值。
    返回 {"fund_nav_estimated": float} 或空 dict（无数据时）。
    """
    url = FUND_EST_URL.format(code=fund_code)
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fund.eastmoney.com/",
        })
        resp.raise_for_status()
        # JSONP 格式: jsonpgz({...})
        match = re.search(r'\((.*)\)', resp.text)
        if match:
            data = json.loads(match.group(1))
            gsz = data.get("gsz")  # 估算净值
            if gsz:
                return {"fund_nav_estimated": float(gsz)}
    except Exception as e:
        logger.debug(f"基金估算净值获取失败（可能非交易时间）: {e}")
    return {}


def collect_snapshot(trade_date: str = None) -> dict:
    """
    采集 14:45 快照数据。
    trade_date: 指定日期，为 None 时使用当日。
    返回快照 dict。
    """
    session = _create_session()
    try:
        session.get("https://www.csindex.com.cn/", timeout=10)
    except Exception:
        pass

    snapshot = {
        "trade_date": trade_date or datetime.now().strftime("%Y-%m-%d"),
        "tmt_idx": None,
        "tmt_chg_pct": None,
        "tmt_volume": None,
        "aic_chg_pct": None,
        "ce_chg_pct": None,
        "semi_chg_pct": None,
        "ne_chg_pct": None,
        "fund_nav_estimated": None,
    }

    # 采集各指数数据
    for idx in INDICES:
        code = idx["code"]
        col = idx["col"]
        for attempt in range(MAX_RETRIES):
            try:
                data = fetch_index_realtime(session, code)
                if col == "tmt_chg_pct":
                    snapshot["tmt_chg_pct"] = data.get("change_pct")
                    snapshot["tmt_volume"] = data.get("volume")
                    snapshot["tmt_idx"] = data.get("close")
                else:
                    snapshot[col] = data.get("change_pct")
                logger.info(f"  {code} ({idx['name']}): chg={data.get('change_pct')}%")
                break
            except Exception as e:
                logger.warning(f"  {code} 第{attempt+1}次失败: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"  {code} 采集失败，已重试 {MAX_RETRIES} 次")
        time.sleep(0.6)

    # 采集基金估算净值
    fund_data = fetch_fund_estimate(FUND_CODE)
    snapshot.update(fund_data)
    if fund_data:
        logger.info(f"  基金 {FUND_CODE} 估算净值: {fund_data['fund_nav_estimated']}")

    return snapshot


def main():
    parser = argparse.ArgumentParser(description="14:45 盘中快照采集")
    parser.add_argument("--date", type=str, default=None,
                        help="指定日期 (YYYY-MM-DD)，默认为当日")
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("14:45 盘中快照采集开始")

    trade_date = _normalize_date_str(args.date) if args.date else None

    try:
        snapshot = collect_snapshot(trade_date)
        save_snapshot_1445(snapshot)
        logger.info(f"快照采集完成: {snapshot['trade_date']}")
    except Exception as e:
        logger.error(f"快照采集失败: {e}", exc_info=True)
        sys.exit(1)

    logger.info("=" * 50)


if __name__ == "__main__":
    main()
