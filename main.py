"""
main.py
TMT-Alpha 2.0 实盘主程序
每日 14:45 运行: 拉取实时指数 → 写入快照表 snapshot_1445 → 计算信号 → 企业微信推送

注意: main.py 不写入 market_daily 和 fund_nav。
收盘数据由 23:30 的 scripts/closing_collector.py 负责。
"""

import sys
import re
import json
import time
import logging
import requests
import pandas as pd
from datetime import datetime

from core.config_loader import load_config
from db.data_pipeline import load_merged_data, save_snapshot_1445, _normalize_date_str
from core.strategy import TMTAlphaStrategy
from core.notifier import send_signal_notification

logger = logging.getLogger(__name__)

# 五个指数（与 data_pipeline 保持一致）
INDICES = [
    {"code": "000998", "name": "中证TMT", "col_prefix": "tmt"},
    {"code": "931160", "name": "通信设备", "col_prefix": "ce"},
    {"code": "931494", "name": "消费电子", "col_prefix": "aic"},
    {"code": "H30184", "name": "半导体",   "col_prefix": "semi"},
    {"code": "000941", "name": "新能源",   "col_prefix": "ne"},
]

ONEDAY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday"
FUND_EST_URL = "https://fundgz.1234567.com.cn/js/019018.js"
REQUEST_INTERVAL = 0.6


def _create_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.csindex.com.cn/",
    })
    return session


def _fetch_index_realtime(session, index_code):
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
        "trade_date": _normalize_date_str(header.get("tradeDate", "")),
        "open": header.get("openToday"),
        "close": header.get("current"),
        "prev_close": header.get("closePre"),
        "change_pct": header.get("changePct"),
        "volume": header.get("tradingVol"),
    }


def _fetch_fund_estimate():
    """从天天基金获取基金实时估算净值"""
    try:
        resp = requests.get(FUND_EST_URL, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://fund.eastmoney.com/",
        })
        resp.raise_for_status()
        match = re.search(r"\((.*)\)", resp.text)
        if match:
            data = json.loads(match.group(1))
            gsz = data.get("gsz")
            if gsz:
                return float(gsz)
    except Exception:
        pass
    return None


def _fetch_all_indices():
    """
    拉取五个指数的实时数据 + 基金估算净值，写入 snapshot_1445。
    返回 (snapshot_dict, index_data_dict)。
    index_data_dict: {col_prefix: {close, change_pct, volume, ...}}
    """
    session = _create_session()
    try:
        session.get("https://www.csindex.com.cn/", timeout=10)
    except Exception:
        pass

    index_data = {}
    for idx in INDICES:
        code = idx["code"]
        prefix = idx["col_prefix"]
        try:
            data = _fetch_index_realtime(session, code)
            index_data[prefix] = data
            print(f"  {code} ({idx['name']}): close={data['close']}, chg={data['change_pct']}%")
        except Exception as e:
            print(f"  [错误] {code} 拉取失败: {e}")
        time.sleep(REQUEST_INTERVAL)

    # 基金估算净值
    fund_est = _fetch_fund_estimate()
    if fund_est:
        print(f"  基金估算净值: {fund_est}")
    else:
        print(f"  基金估算净值: 未获取到")

    # 构建快照并写入 snapshot_1445
    tmt = index_data.get("tmt", {})
    today = datetime.now().strftime("%Y-%m-%d")

    snapshot = {
        "trade_date": today,
        "tmt_idx": tmt.get("close"),
        "tmt_chg_pct": tmt.get("change_pct"),
        "tmt_volume": tmt.get("volume"),
        "aic_chg_pct": index_data.get("aic", {}).get("change_pct"),
        "ce_chg_pct": index_data.get("ce", {}).get("change_pct"),
        "semi_chg_pct": index_data.get("semi", {}).get("change_pct"),
        "ne_chg_pct": index_data.get("ne", {}).get("change_pct"),
        "fund_nav_estimated": fund_est,
    }

    try:
        save_snapshot_1445(snapshot)
        print(f"  快照已写入 snapshot_1445: {today}")
    except Exception as e:
        print(f"  [警告] 快照写入失败: {e}")

    return snapshot, index_data


def _append_today_row(df: pd.DataFrame, index_data: dict, fund_est: float = None) -> pd.DataFrame:
    """
    将当天 14:45 实时数据追加为 DataFrame 的最后一行。
    market_daily 不含当天数据（由 23:30 closing_collector 写入），
    因此需要手动构造当天行来驱动信号计算。

    fund_est: 天天基金估算净值，用于计算当日估算涨跌幅（驱动防锯齿逻辑）
    """
    today = datetime.now().strftime("%Y-%m-%d")

    row = {"trade_date": today}
    for idx in INDICES:
        prefix = idx["col_prefix"]
        data = index_data.get(prefix, {})
        if prefix == "tmt":
            row["tmt_open"] = data.get("open")
            row["tmt_high"] = None
            row["tmt_low"] = None
            row["tmt_close"] = data.get("close")
            row["tmt_change_pct"] = data.get("change_pct")
            row["tmt_volume"] = data.get("volume")
            row["tmt_amount"] = None
        else:
            row[f"{prefix}_close"] = data.get("close")

    # 基金净值：优先使用 14:45 估算净值（用于防锯齿等实时逻辑）
    # 如果未获取到，留 None 由 prepare_data 中 ffill 兜底
    row["fund_nav"] = fund_est if fund_est else None
    row["fund_acc_value"] = None
    row["fund_daily_return"] = None

    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    return df


def run_daily():
    """每日 14:45 实盘主流程"""
    print("=" * 60)
    print(f"易方达信息产业混合C (019018)量化决策  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    cfg = load_config()

    # 第一步：拉取实时数据 → 写入 snapshot_1445
    print("\n[1/3] 拉取 14:45 实时数据…")
    try:
        snapshot, index_data = _fetch_all_indices()
    except Exception as e:
        print(f"[错误] 数据拉取失败: {e}")
        return

    if not index_data.get("tmt"):
        print("[错误] TMT 指数数据缺失，可能非交易日，退出。")
        return

    # 交易日校验：API 返回的 tradeDate 必须与今天一致
    api_trade_date = index_data["tmt"].get("trade_date", "")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if api_trade_date != today_str:
        print(f"[提示] API 返回日期 {api_trade_date} ≠ 今天 {today_str}，非交易日或数据未更新，跳过信号。")
        return

    # 第二步：加载历史数据 + 追加当日实时行 → 计算信号
    print("\n[2/3] 加载数据并计算信号…")
    raw_df = load_merged_data()
    if raw_df.empty:
        print("[错误] 无历史数据，请先执行 python db/data_pipeline.py init")
        return

    fund_est = snapshot.get("fund_nav_estimated") if snapshot else None
    raw_df = _append_today_row(raw_df, index_data, fund_est)

    strategy = TMTAlphaStrategy(cfg)
    df = strategy.prepare_data(raw_df)

    warmup = strategy.warmup_days
    if len(df) < warmup:
        print(f"[警告] 数据不足 {warmup} 条，信号可能不稳定。")

    t = len(df) - 1
    signal = strategy.process_day(t, df)

    # 第三步：推送企业微信通知
    print("\n[3/3] 推送信号通知…")
    _print_signal(signal)

    try:
        send_signal_notification(signal, cfg)
    except Exception as e:
        print(f"[警告] 企微推送失败: {e}")
        print("请检查 config.yaml 中 wechat.webhook_url 或环境变量 WECHAT_WEBHOOK_URL 是否已设置。")

    print("\n完成。")


def _print_signal(signal: dict):
    """打印信号摘要"""
    print("\n" + "-" * 50)
    print(f"日期:           {signal['trade_date']}")
    print(f"Mkt_Chg:        {signal['mkt_chg']:+.2f}%")
    print(f"Score_eff:      {signal['score_eff']:.1f}")
    print(f"Action_Ratio:   {signal['action_ratio']:.2f}")
    print(f"Final_Multi:    {signal['final_multiplier']:.2f}")
    print(f"通道:           {signal['channel']}")
    print(f"操作:           {signal['action']}")
    print(f"金额:           {signal['amount']:,.0f}")
    if signal["warning"]:
        print("⚠  超额回撤预警")
    if signal["force_reduce"]:
        print("!! 强制平仓触发")
    print("-" * 50)


if __name__ == "__main__":
    run_daily()
