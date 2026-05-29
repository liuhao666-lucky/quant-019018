"""
closing_collector.py
TMT-Alpha 2.0 — 当天 23:30 收盘数据采集脚本

定时任务由外部触发（如 cron/Task Scheduler），脚本仅实现采集逻辑。
获取收盘后的确切数据，更新 market_daily 和 fund_nav 表。
采集完成后推送收盘汇总到企业微信。

用法:
  python scripts/closing_collector.py              # 采集当天收盘数据
  python scripts/closing_collector.py --date 2025-05-23  # 补录指定日期
"""

import sys
import time
import json
import re
import logging
import argparse
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.data_pipeline import (
    _get_conn, _normalize_date_str, _create_session,
    fetch_index_history, _parse_history_rows,
    fetch_fund_nav_history, save_fund_nav, load_snapshot_1445,
)
from core.config_loader import load_config
from core.notifier import send_closing_summary

# 日志配置
LOG_PATH = Path(__file__).parent / "closing_collector.log"
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
    {"code": "000998", "name": "中证TMT", "col_prefix": "tmt"},
    {"code": "931160", "name": "通信设备", "col_prefix": "ce"},
    {"code": "931494", "name": "消费电子", "col_prefix": "aic"},
    {"code": "H30184", "name": "半导体",   "col_prefix": "semi"},
    {"code": "000941", "name": "新能源",   "col_prefix": "ne"},
]

MAX_RETRIES = 3
RETRY_DELAY = 3


def update_market_daily(trade_date: str, rows: list, index_code: str):
    """
    更新 market_daily 表（INSERT OR UPDATE）。
    若记录已存在则更新收盘字段，不修改 created_at。
    若不存在则插入。
    """
    conn = _get_conn()
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rows:
            td = _normalize_date_str(r.get("trade_date", ""))
            if td != trade_date:
                continue

            # 检查记录是否存在
            cursor = conn.execute(
                "SELECT 1 FROM market_daily WHERE trade_date = ? AND index_code = ?",
                (td, index_code)
            )
            exists = cursor.fetchone() is not None

            if exists:
                conn.execute("""
                    UPDATE market_daily SET
                        open = COALESCE(?, open),
                        high = COALESCE(?, high),
                        low = COALESCE(?, low),
                        close = ?,
                        change = COALESCE(?, change),
                        change_pct = ?,
                        volume = ?,
                        amount = COALESCE(?, amount)
                    WHERE trade_date = ? AND index_code = ?
                """, (
                    r.get("open"), r.get("high"), r.get("low"),
                    r.get("close"), r.get("change"), r.get("change_pct"),
                    r.get("volume"), r.get("amount"),
                    td, index_code,
                ))
            else:
                conn.execute("""
                    INSERT INTO market_daily
                    (trade_date, index_code, open, high, low, close,
                     change, change_pct, volume, amount, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    td, index_code,
                    r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                    r.get("change"), r.get("change_pct"),
                    r.get("volume"), r.get("amount"),
                    now_str,
                ))

        conn.commit()
        logger.info(f"  market_daily 更新完成: {index_code}")
    finally:
        conn.close()


def collect_closing(trade_date: str = None):
    """
    采集收盘数据并更新数据库。
    trade_date: 指定日期 (YYYY-MM-DD)，为 None 时采集当天（23:30 运行）。
    """
    if trade_date is None:
        # 默认采集当天（23:30 运行，收盘数据已就绪）
        trade_date = datetime.now().strftime("%Y-%m-%d")
    else:
        trade_date = _normalize_date_str(trade_date)

    logger.info(f"采集目标日期: {trade_date}")

    # csindex API 需要 YYYYMMDD 格式
    date_api = trade_date.replace("-", "")

    session = _create_session()
    try:
        session.get("https://www.csindex.com.cn/", timeout=10)
    except Exception:
        pass

    # 采集各指数收盘数据
    for idx in INDICES:
        code = idx["code"]
        name = idx["name"]
        for attempt in range(MAX_RETRIES):
            try:
                raw = fetch_index_history(session, code, date_api, date_api)
                rows = _parse_history_rows(raw)
                if rows:
                    update_market_daily(trade_date, rows, code)
                    logger.info(f"  {code} ({name}): 收盘数据已更新")
                else:
                    logger.warning(f"  {code} ({name}): 无数据返回（可能非交易日）")
                break
            except Exception as e:
                logger.warning(f"  {code} 第{attempt+1}次失败: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error(f"  {code} 采集失败，已重试 {MAX_RETRIES} 次")
        time.sleep(0.6)

    # 采集基金净值
    logger.info(f"采集基金净值...")
    for attempt in range(MAX_RETRIES):
        try:
            rows = fetch_fund_nav_history()
            # 只更新目标日期的数据
            target_rows = [r for r in rows if r.get("trade_date") == trade_date]
            if target_rows:
                save_fund_nav(target_rows)
                logger.info(f"  基金净值已更新: {trade_date}")
            else:
                # 如果没有目标日期，写入全部（增量更新）
                save_fund_nav(rows)
                logger.info(f"  基金净值已全量更新（目标日期 {trade_date} 无独立记录）")
            break
        except Exception as e:
            logger.warning(f"  基金净值第{attempt+1}次失败: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"  基金净值采集失败，已重试 {MAX_RETRIES} 次")


def _build_closing_summary(trade_date: str, data_ok: bool) -> dict:
    """
    采集完成后计算收盘汇总指标，用于企业微信推送。
    加载已更新的数据，运行 prepare_data 获取当日真实 Excess_DD 等指标。
    """
    try:
        from db.data_pipeline import load_merged_data, load_snapshot_1445
        from core.strategy import TMTAlphaStrategy

        raw_df = load_merged_data()
        if raw_df.empty:
            return {"trade_date": trade_date, "data_ok": data_ok}

        strategy = TMTAlphaStrategy(load_config())
        df = strategy.prepare_data(raw_df)

        # 取最后一行的真实指标（_actual 列为未 shift 的当日数据）
        row = df.iloc[-1]
        r_tmt = row.get("R_TMT", 0)
        r_fund = row.get("R_fund_actual", row.get("R_fund", 0))
        excess_dd = row.get("Excess_DD_actual", row.get("Excess_DD", 0))

        # 单日 Alpha = R_fund - Mkt_Chg
        cfg = load_config()
        bm = cfg.get("benchmark", {})
        r_deposit = bm.get("deposit_daily_rate", 0.00004)
        mkt_chg = bm.get("equity_weight", 0.70) * r_tmt + bm.get("cash_weight", 0.30) * (r_deposit * 100)
        alpha = r_fund - mkt_chg

        # 14:45 盘中快照
        snap_df = load_snapshot_1445(trade_date)
        has_snapshot = not snap_df.empty
        tmt_intraday = snap_df.iloc[0]["tmt_chg_pct"] if has_snapshot else None

        # 14:45 信号复盘：优先从快照表读取已存的 14:45 信号
        signal_action = "-"
        signal_channel = "-"
        signal_amount = 0
        if has_snapshot:
            snap_row = snap_df.iloc[0]
            saved_action = snap_row.get("signal_action") if "signal_action" in snap_df.columns else None
            if pd.notna(saved_action) and saved_action is not None and str(saved_action) not in ("", "None", "nan"):
                # 直接从快照读取（与 14:45 完全一致，避免收盘数据差异导致金额漂移）
                signal_action = str(saved_action)
                signal_channel = str(snap_row.get("signal_channel", "-"))
                signal_amount = float(snap_row.get("signal_amount", 0)) if pd.notna(snap_row.get("signal_amount")) else 0
            else:
                # 快照中无信号数据（旧版兼容），回退重算
                try:
                    snap = snap_row.to_dict()
                    snap_strategy = TMTAlphaStrategy(cfg, snapshot_map={trade_date: snap})
                    snap_df_prep = snap_strategy.prepare_data(raw_df.copy())
                    t = len(snap_df_prep) - 1
                    signal = snap_strategy.process_day(t, snap_df_prep)
                    signal_action = signal.get("action", "-")
                    signal_channel = signal.get("channel", "-")
                    signal_amount = signal.get("amount", 0)
                except Exception as e:
                    logger.warning(f"信号复盘失败: {e}")

        return {
            "trade_date": trade_date,
            "tmt_close_chg": r_tmt,
            "tmt_intraday_chg": tmt_intraday,
            "fund_chg": r_fund,
            "alpha_daily": alpha,
            "excess_dd": excess_dd,
            "signal_action": signal_action,
            "signal_channel": signal_channel,
            "signal_amount": signal_amount,
            "data_ok": data_ok,
        }
    except Exception as e:
        logger.warning(f"收盘汇总计算失败: {e}")
        return {"trade_date": trade_date, "data_ok": False}


def main():
    parser = argparse.ArgumentParser(description="当天 23:30 收盘数据采集")
    parser.add_argument("--date", type=str, default=None,
                        help="指定日期 (YYYY-MM-DD)，默认为当天")
    parser.add_argument("--no-notify", action="store_true",
                        help="跳过企业微信推送")
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("收盘数据采集开始")

    data_ok = True
    try:
        if args.date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")
        else:
            target_date = _normalize_date_str(args.date)
        collect_closing(target_date)
        logger.info("收盘数据采集完成")
    except Exception as e:
        logger.error(f"收盘数据采集失败: {e}", exc_info=True)
        data_ok = False

    # 推送收盘汇总到企业微信
    if not args.no_notify:
        try:
            summary = _build_closing_summary(target_date if data_ok else datetime.now().strftime("%Y-%m-%d"), data_ok)
            cfg = load_config()
            if cfg.get("wechat", {}).get("enabled", True):
                send_closing_summary(summary, cfg)
        except Exception as e:
            logger.warning(f"收盘汇总推送失败: {e}")

    logger.info("=" * 50)

    if not data_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
