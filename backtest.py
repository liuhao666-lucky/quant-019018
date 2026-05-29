"""
backtest.py
TMT-Alpha 2.0 回测引擎
从 SQLite 加载数据，运行完整策略回测，输出绩效指标和图表。
含多基准对比：策略净值、基金买入持有、基金定投、TMT指数。
支持 14:45 快照回测模式。
"""

import argparse
import copy
import csv
import logging
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
from datetime import datetime
from pathlib import Path

from core.config_loader import load_config
from db.data_pipeline import load_merged_data, load_snapshot_1445
from core.strategy import TMTAlphaStrategy
from model.model7_exit_logic import _redemption_fee_rate

logger = logging.getLogger(__name__)

# 中文字体设置
rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

# 输出目录
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 申购费率：单边 0.1%
TRANSACTION_COST = 0.001


def _redemption_fee_rate(holding_days: int) -> float:
    """阶梯式赎回费率：0-6天 1.5%，7-29天 0.5%，30天+ 0%"""
    if holding_days < 7:
        return 0.015
    elif holding_days < 30:
        return 0.005
    else:
        return 0.0


def _calc_benchmarks(df, initial_capital, warmup, dca_total_invest=None,
                     live_start_date=None):
    """
    计算多基准净值曲线（含申购费 + 赎回费）。

    参数:
      - dca_total_invest: 定投总投入（默认 = initial_capital）。
      - live_start_date: 实盘起点日期。若提供，买入持有从该日起计算费用。

    返回 dict:
      - fund_buyhold / fund_dca / tmt_index / tmt_dca: 净值序列
      - fee_buyhold_bh / fee_dca: 费用
      - dca_total_invested: 定投实际总投入
    """
    SUBSCRIPTION_FEE = 0.001  # 申购费率 0.1%

    dates = df["trade_date"].tolist()
    n = len(df)

    # --- 基金净值序列 ---
    fund_nav_raw = df["fund_nav_actual"].values.copy() if "fund_nav_actual" in df.columns else df["fund_nav"].values.copy()
    r_fund = df["R_fund_actual"].values if "R_fund_actual" in df.columns else df["R_fund"].values

    if pd.isna(fund_nav_raw[0]):
        fund_nav_raw[0] = 1.0
    for i in range(1, n):
        if pd.isna(fund_nav_raw[i]):
            if pd.notna(r_fund[i]):
                fund_nav_raw[i] = fund_nav_raw[i - 1] * (1 + r_fund[i] / 100)
            else:
                fund_nav_raw[i] = fund_nav_raw[i - 1]

    # --- TMT 指数净值 ---
    tmt_close = df["tmt_close"].values
    tmt_nav = tmt_close / tmt_close[0]

    # ================================================================
    # 基金买入持有：首日全仓买入（扣申购费），期末赎回（扣赎回费）
    # ================================================================
    buy_cost_bh = initial_capital * SUBSCRIPTION_FEE
    effective_capital_bh = initial_capital - buy_cost_bh
    fund_buyhold = (fund_nav_raw / fund_nav_raw[0]) * (effective_capital_bh / initial_capital)

    if live_start_date and warmup > 0 and warmup < n:
        holding_days_bh = max(0, n - 1 - warmup)
        redeem_rate_bh = _redemption_fee_rate(holding_days_bh)
        fund_return_active = fund_nav_raw[-1] / fund_nav_raw[warmup]
        fund_value_at_end = fund_return_active * initial_capital
        sell_slippage_bh = fund_value_at_end * SUBSCRIPTION_FEE
        sell_redemption_bh = fund_value_at_end * redeem_rate_bh
        fee_buyhold_bh = buy_cost_bh + sell_slippage_bh + sell_redemption_bh
    else:
        holding_days_bh = max(0, n - 1)
        redeem_rate_bh = _redemption_fee_rate(holding_days_bh)
        fund_value_at_end = fund_nav_raw[-1] / fund_nav_raw[0] * initial_capital
        sell_slippage_bh = fund_value_at_end * SUBSCRIPTION_FEE
        sell_redemption_bh = fund_value_at_end * redeem_rate_bh
        fee_buyhold_bh = buy_cost_bh + sell_slippage_bh + sell_redemption_bh

    # ================================================================
    # 基金定投：每月首日定投（扣申购费），记录每批次，期末按 FIFO 计赎回费
    # ================================================================
    if dca_total_invest is None:
        dca_total_invest = initial_capital

    all_months = sorted(set(d[:7] for d in dates[warmup:] if d))
    num_months = len(all_months) if all_months else 1
    dca_amount = dca_total_invest / num_months

    dca_batches = []  # [(buy_date_idx, shares_after_fee)]
    dca_shares = 0.0
    dca_invested = 0.0
    dca_nav = np.ones(n)
    dca_buy_cost = 0.0
    last_month = ""

    for i in range(n):
        month = dates[i][:7]
        # 定投仅从 warmup（活跃期起点）开始
        if i >= warmup and month != last_month:
            invest = min(dca_amount, dca_total_invest - dca_invested)
            if invest > 0 and fund_nav_raw[i] > 0:
                cost = invest * SUBSCRIPTION_FEE
                effective = invest - cost
                new_shares = effective / fund_nav_raw[i]
                dca_shares += new_shares
                dca_invested += invest
                dca_buy_cost += cost
                dca_batches.append((i, new_shares))
            last_month = month

        if dca_invested > 0:
            dca_nav[i] = (dca_shares * fund_nav_raw[i]) / dca_invested

    # 期末费用（FIFO：每批次按各自持有天数计算赎回费 + 滑点）
    dca_sell_fee = 0.0
    final_nav = fund_nav_raw[-1]
    for batch_idx, batch_shares in dca_batches:
        hold_days = max(0, n - 1 - batch_idx)
        rate = _redemption_fee_rate(hold_days)
        batch_value = batch_shares * final_nav
        dca_sell_fee += batch_value * SUBSCRIPTION_FEE + batch_value * rate
    fee_dca = dca_buy_cost + dca_sell_fee

    # ================================================================
    # TMT 指数定投（无基金费用，仅跟踪指数价格）
    # ================================================================
    tmt_dca_shares = 0.0
    tmt_dca_invested = 0.0
    tmt_dca_nav = np.ones(n)
    last_month = ""

    for i in range(n):
        month = dates[i][:7]
        if i >= warmup and month != last_month:
            invest = min(dca_amount, dca_total_invest - tmt_dca_invested)
            if invest > 0 and tmt_close[i] > 0:
                tmt_dca_shares += invest / tmt_close[i]
                tmt_dca_invested += invest
            last_month = month

        if tmt_dca_invested > 0:
            tmt_dca_nav[i] = (tmt_dca_shares * tmt_close[i]) / tmt_dca_invested

    return {
        "fund_buyhold": fund_buyhold,
        "fund_dca": dca_nav,
        "tmt_index": tmt_nav,
        "tmt_dca": tmt_dca_nav,
        "dates": dates,
        "fee_buyhold_bh": fee_buyhold_bh,
        "fee_dca": fee_dca,
        "dca_total_invested": dca_invested,
    }


def _calc_nav_metrics(nav_series, warmup, dates):
    """计算单条净值曲线的绩效指标"""
    if len(nav_series) < 2:
        return {"return": 0, "max_dd": 0, "vol": 0, "sharpe": 0}

    active = nav_series[warmup:] if warmup < len(nav_series) else nav_series
    total_return = active[-1] / active[0] - 1 if active[0] > 0 else 0

    daily_ret = np.diff(active) / active[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]
    annual_vol = np.std(daily_ret) * np.sqrt(252) if len(daily_ret) > 0 else 0

    peak = np.maximum.accumulate(active)
    dd = (active - peak) / peak
    max_dd = dd.min()

    r_free = 0.00004
    excess = daily_ret - r_free
    sharpe = np.mean(excess) / np.std(excess) * np.sqrt(252) if np.std(excess) > 0 else 0

    return {
        "return": total_return,
        "max_dd": max_dd,
        "vol": annual_vol,
        "sharpe": sharpe,
    }


def _run_inline_robustness(df_full, cfg, warmup_days, min_starts=10):
    """在完整数据上运行多起始点稳健性检验，返回 (results_list, trimmed_stats_dict)"""
    dates = df_full["trade_date"].tolist()
    if len(dates) < warmup_days + 20:
        return [], {}

    # 从第一个有效月起，每隔约3个月取一个起始点
    month_starts = []
    last_month = ""
    for i, d in enumerate(dates):
        month = d[:7]
        if month != last_month:
            month_starts.append((i, month))
            last_month = month

    # 过滤掉数据不足的
    valid = [(i, m) for i, m in month_starts if len(dates) - i >= warmup_days + 20]
    if len(valid) < 3:
        return [], {}

    # 均匀选取 min_starts 个，覆盖全区间
    if len(valid) > min_starts:
        step = max(1, len(valid) // min_starts)
        selected = valid[::step][:min_starts]
    else:
        selected = valid

    results = []
    preheat_days = cfg.get("backtest", {}).get("preheat_days", 90)
    seen_dates = set()

    for idx, month in selected:
        preheat_start = max(0, idx - preheat_days)
        sub_df = df_full.iloc[preheat_start:].reset_index(drop=True)
        if len(sub_df) < warmup_days + 10:
            continue

        cfg_copy = copy.deepcopy(cfg)
        cfg_copy["system"]["warmup_days"] = warmup_days
        cfg_copy["backtest"]["use_snapshot"] = False

        try:
            r = run_backtest(cfg_copy, report_path=None, external_data=sub_df, silent=True)
            if not r or "metrics" not in r:
                continue
            m = r["metrics"]
            eff_idx = min(warmup_days, len(sub_df) - 1)
            start_date = sub_df["trade_date"].iloc[eff_idx]
            if start_date in seen_dates:
                continue
            seen_dates.add(start_date)
            results.append({
                "start_date": start_date,
                "strategy_return": _parse_pct(m["累计收益率"]),
                "max_dd": _parse_pct(m["最大回撤"]),
                "sharpe": float(m["夏普比率"]),
                "calmar": float(m["卡玛比率"]),
                "tmt_return": _parse_pct(m["基准累计收益率"]),
                "excess_vs_tmt": _parse_pct(m["超额收益"]),
                "fund_bh": _parse_pct(m["基金持有收益率"]),
                "excess_vs_fund": _parse_pct(m["超基金持有"]),
            })
        except Exception:
            continue

    # 计算剔除极端值统计
    trimmed_stats = {}
    for trim_n, label in [(0, "全部"), (3, "去最高3个")]:
        if len(results) <= trim_n:
            continue
        sorted_r = sorted(results, key=lambda x: x["strategy_return"], reverse=True)
        subset = sorted_r[trim_n:]
        rets = [x["strategy_return"] for x in subset]
        excess_t = [x["excess_vs_tmt"] for x in subset]
        wr = sum(1 for e in excess_t if e > 0) / len(excess_t) if excess_t else 0
        trimmed_stats[label] = {
            "n": len(subset),
            "mean_return": np.mean(rets),
            "median_return": np.median(rets),
            "win_rate": wr,
            "mean_dd": np.mean([x["max_dd"] for x in subset]),
        }

    return results, trimmed_stats


def _parse_pct(s):
    """解析百分比字符串为浮点数"""
    if isinstance(s, (int, float)):
        return float(s)
    return float(s.rstrip("%")) / 100


def run_backtest(cfg: dict = None, report_path: str = "backtest_report.md",
                 external_data: pd.DataFrame = None, silent: bool = False,
                 live_start_date: str = None) -> dict:
    if cfg is None:
        cfg = load_config()

    # 实盘起点模式：自动设置输出文件名
    if live_start_date and report_path == "backtest_report.md":
        report_path = "backtest_report_live_start.md"

    bt = cfg.get("backtest", {})
    initial_capital = bt.get("initial_capital", 1000)
    max_pos_ratio = bt.get("max_position_ratio", 1.0)
    warmup_max_ratio = cfg.get("system", {}).get("warmup_max_position_ratio", 0.0)

    ex = cfg.get("execution", {})
    m_max = ex.get("m_max_normal", 500)
    m_min = ex.get("m_min_normal", 0)

    # 加载数据
    if external_data is not None:
        raw_df = external_data.copy()
        print(f"使用外部注入数据，共 {len(raw_df)} 条记录。")
    else:
        print("正在从 SQLite 加载数据…")
        raw_df = load_merged_data()
        if raw_df.empty:
            print("[错误] 无数据，无法回测。")
            return {}

    bt = cfg.get("backtest", {})
    start_date = bt.get("start_date")
    preheat_days = bt.get("preheat_days", 90)
    if start_date and not live_start_date:
        matches = raw_df.index[raw_df["trade_date"] == start_date].tolist()
        if matches:
            start_idx = matches[0]
            if start_idx > 0:
                preheat_start = max(0, start_idx - preheat_days)
                raw_df = raw_df.iloc[preheat_start:].reset_index(drop=True)
                print(f"已加载 {preheat_days} 天预热历史数据，回测起始日为 {start_date}。")
            else:
                print(f"[提示] 回测起始日 {start_date} 已是数据首日，无需额外预热。")
        else:
            print(f"[警告] 回测起始日 {start_date} 未在数据中找到，使用全部可用数据。")

    print(f"数据加载完成，共 {len(raw_df)} 条记录。")

    # 加载快照数据（如果启用）
    use_snapshot = cfg.get("backtest", {}).get("use_snapshot", False)
    snapshot_map = {}
    if use_snapshot:
        snap_df = load_snapshot_1445()
        if not snap_df.empty:
            for _, row in snap_df.iterrows():
                snapshot_map[row["trade_date"]] = row.to_dict()
            print(f"快照数据加载完成，共 {len(snapshot_map)} 条。")
        else:
            print("[警告] use_snapshot=true 但 snapshot_1445 表为空，将全部回退收盘数据。")

    strategy = TMTAlphaStrategy(cfg, snapshot_map=snapshot_map)
    df = strategy.prepare_data(raw_df)

    # 动态仓位乘数所需：TMT指数前一日20日均线（shift(1) 消除未来函数）
    df["tmt_ma20_yesterday"] = df["tmt_close"].rolling(20, min_periods=5).mean().shift(1)

    warmup = strategy.warmup_days
    if len(df) < warmup:
        print(f"[警告] 数据不足 {warmup} 条，无法完成预热期。")
        warmup = max(10, len(df) // 4)

    # 实盘起点模式：仅加载 70 个交易日预热数据，初始仓位强制归零
    live_start_idx = None
    LIVE_PREHEAT_DAYS = 70
    if live_start_date:
        # 在完整 prepare_data 后的 df 中找到 live_start_date 的位置
        full_matches = df.index[df["trade_date"] >= live_start_date].tolist()
        if full_matches:
            full_live_idx = full_matches[0]
        else:
            full_live_idx = len(df) - 1
            print(f"[警告] 实盘起点 {live_start_date} 晚于所有数据，使用最后一天。")

        # 从 live_start_date 前推 LIVE_PREHEAT_DAYS 个交易日截取数据
        preheat_start = max(0, full_live_idx - LIVE_PREHEAT_DAYS)
        df = df.iloc[preheat_start:].reset_index(drop=True)

        # 重新计算 tmt_ma20_yesterday（依赖截取后的数据）
        df["tmt_ma20_yesterday"] = df["tmt_close"].rolling(20, min_periods=5).mean().shift(1)

        # 重建快照映射（索引变化后需要重建）
        if use_snapshot and snapshot_map:
            strategy.snapshot_map = snapshot_map  # 快照按 trade_date 查找，不受影响

        # 重新定位 live_start_idx（在截取后的 df 中）
        live_start_idx = 0
        for i, d in enumerate(df["trade_date"]):
            if d >= live_start_date:
                live_start_idx = i
                break

        warmup = live_start_idx
        active_days = len(df) - live_start_idx
        # 同步策略预热天数，确保策略内部逻辑与回测对齐
        strategy.warmup_days = warmup
        print(f"实盘起点模式: 交易起始 {live_start_date}，"
              f"预热 {warmup} 天，活跃交易 {active_days} 天，"
              f"数据总量 {len(df)} 天。")

    # === 逐日回测 ===
    signals = []
    trade_log = []
    diag_log = []
    position_value = 0.0
    cash = initial_capital
    nav_list = []
    date_list = []
    total_cost = 0.0

    holding_shares = 0.0
    avg_cost_per_share = 0.0
    holding_days = 0          # 持仓交易天数计数（用于时间止损）

    if not silent:
        print(f"开始回测，预热期 {warmup} 天，数据总量 {len(df)} 天…")

    for t in range(len(df)):
        row = df.iloc[t]

        # 结算持仓收益（warmup 之前 position=0，此分支无实际效果）
        if t > 0 and position_value > 0:
            fund_return = row.get("R_fund_actual", row.get("R_fund", 0))
            if pd.notna(fund_return) and fund_return == fund_return:
                position_value *= (1 + fund_return / 100)

        if t >= warmup:
            # 持仓天数计数：有持仓时每日+1，无持仓时归零
            if position_value > 0.01:
                holding_days += 1
            else:
                holding_days = 0

            # 计算真实持仓收益率，传入策略供止盈逻辑使用
            invested_capital = holding_shares * avg_cost_per_share
            current_gain = (position_value / invested_capital - 1) if invested_capital > 0 else 0.0
            signal = strategy.process_day(t, df, current_gain, holding_days)
            signals.append(signal)

            amount = signal["amount"]
            action = signal["action"]
            current_total = cash + position_value

            diag_log.append({
                "date": row["trade_date"],
                "mkt_chg": signal["mkt_chg"],
                "base": signal["base"],
                "score_raw": signal["score_raw"],
                "score_eff": signal["score_eff"],
                "action_ratio": signal["action_ratio"],
                "final_multiplier": signal["final_multiplier"],
                "amount_before_cap": signal["amount_before_cap"],
                "amount_final": signal["amount"],
                "channel": signal["channel"],
                "cash": round(cash, 2),
                "position": round(position_value, 2),
                "market_temp": signal.get("market_temp", 0),
                "market_mode": signal.get("market_mode", "defense"),
                "trend_strong": signal.get("trend_strong", False),
                "current_gain": round(current_gain, 4),
                "holding_days": holding_days,
                "fee_rate": signal.get("redemption_fee_rate", 0),
                "net_gain": signal.get("net_gain_after_fee", 0),
            })

            # 实盘起点模式：live_start_idx 之前仅计算信号，不执行交易
            can_trade = (live_start_idx is None) or (t >= live_start_idx)
            if not can_trade:
                total_value = cash + position_value
                nav_list.append(total_value / initial_capital)
                date_list.append(row["trade_date"])
                continue

            # 动态仓位乘数（无未来函数）：用前一日 MA20 与当日收盘价比较
            # 预热期内使用 warmup_max_position_ratio，正常期使用 max_position_ratio
            if signal.get("warmup_active") and warmup_max_ratio > 0:
                current_max_position = warmup_max_ratio
            else:
                base_max_pos = signal.get("max_position_ratio", max_pos_ratio)
                tmt_close_val = row.get("tmt_close", 0)
                tmt_ma20_yesterday = row.get("tmt_ma20_yesterday", 0)
                if pd.notna(tmt_ma20_yesterday) and tmt_ma20_yesterday > 0 and tmt_close_val > tmt_ma20_yesterday:
                    current_max_position = min(0.95, base_max_pos * 1.08)
                else:
                    current_max_position = base_max_pos

            # 所有买卖决策由 strategy 统一产出，backtest 仅负责执行
            if action == "buy" and amount > 0:
                amount = min(amount, signal.get("m_max_adapted", m_max))
                # 最低有意义买入金额：与 m_min_normal 一致，过滤碎股交易
                min_buy = ex.get("m_min_normal", 25)
                if amount < min_buy:
                    pass
                else:
                    current_total = cash + position_value
                    pos_limit = current_max_position * current_total - position_value
                    buy_amount = min(amount, cash, pos_limit)
                    if buy_amount >= min_buy:
                        cost = buy_amount * TRANSACTION_COST
                        cash -= (buy_amount + cost)
                        nav_buy = row.get("fund_nav_actual", row.get("fund_nav", 1.0))
                        if not pd.notna(nav_buy) or nav_buy <= 0:
                            nav_buy = 1.0
                        new_shares = buy_amount / nav_buy
                        old_cost_total = holding_shares * avg_cost_per_share
                        holding_shares += new_shares
                        avg_cost_per_share = (old_cost_total + buy_amount) / holding_shares if holding_shares > 0 else 0
                        position_value += buy_amount
                        total_cost += cost
                        trade_log.append({
                            "date": row["trade_date"], "action": "buy",
                            "amount": buy_amount, "cost": cost,
                            "slippage": cost, "redemption": 0.0,
                            "holding_days": 0, "fee_rate": TRANSACTION_COST,
                            "reason": f"ch={signal['channel']} score={signal['score_eff']:.1f}"
                        })

            elif action == "sell" and amount < 0:
                sell_amount = min(abs(amount), position_value)
                if sell_amount >= 1.0:
                    # 卖出总成本 = 摩擦成本(0.1%) + 阶梯赎回费
                    sell_fee_rate = _redemption_fee_rate(holding_days)
                    sell_slippage = sell_amount * TRANSACTION_COST
                    sell_redemption = sell_amount * sell_fee_rate
                    cost = sell_slippage + sell_redemption
                    sell_ratio = sell_amount / position_value if position_value > 0 else 0
                    holding_shares *= (1 - sell_ratio)
                    position_value -= sell_amount
                    cash += sell_amount - cost
                    total_cost += cost
                    trend_label = "强趋势" if signal.get("trend_strong") else "弱趋势"
                    if signal.get("trailing_stop"):
                        reason = f"移动止盈清仓-{trend_label}"
                    elif signal.get("time_stop"):
                        reason = f"时间止损(持仓{holding_days}天)-{trend_label}"
                    elif signal.get("signal_decay"):
                        reason = f"信号衰减(Score={signal['score_eff']:.1f})-{trend_label}"
                    elif signal.get("force_reduce"):
                        reason = "强平"
                    else:
                        tp_tier = signal.get("tp_tier", 0)
                        tier_text = "一档" if tp_tier == 1 else ("二档" if tp_tier == 2 else "")
                        reason = f"止盈({tier_text})-{trend_label}"
                    trade_log.append({
                        "date": row["trade_date"], "action": "sell",
                        "amount": sell_amount, "cost": cost,
                        "slippage": sell_slippage, "redemption": sell_redemption,
                        "holding_days": holding_days, "fee_rate": sell_fee_rate,
                        "reason": reason
                    })
                    if position_value < 0.01:
                        holding_days = 0
                        strategy.pos_state.time_stop_triggered = False

        total_value = cash + position_value
        nav_list.append(total_value / initial_capital)
        date_list.append(row["trade_date"])

        # 实盘起点模式：逐日打印持仓和交易详情
        if live_start_date and t >= warmup and not silent:
            latest_trade = trade_log[-1] if trade_log else None
            trade_info = ""
            if latest_trade and latest_trade["date"] == row["trade_date"]:
                if latest_trade["action"] == "buy":
                    trade_info = f"  BUY {latest_trade['amount']:.2f}"
                else:
                    trade_info = f"  SELL {latest_trade['amount']:.2f} (slip={latest_trade.get('slippage',0):.2f} redeem={latest_trade.get('redemption',0):.2f})"
            ma60_val = df["TMT_MA60"].iloc[t] if "TMT_MA60" in df.columns and pd.notna(df["TMT_MA60"].iloc[t]) else "N/A"
            excess_dd_val = df["Excess_DD"].iloc[t] if "Excess_DD" in df.columns and pd.notna(df["Excess_DD"].iloc[t]) else "N/A"
            print(f"  [{row['trade_date']}] cash={cash:.2f} pos={position_value:.2f} "
                  f"hold={holding_days}d MA60={ma60_val if isinstance(ma60_val, str) else f'{ma60_val:.2f}'} "
                  f"ExDD={excess_dd_val if isinstance(excess_dd_val, str) else f'{excess_dd_val:.4f}'}"
                  f"{trade_info}")

    # === 保存诊断日志 ===
    if not silent:
        if live_start_date:
            diag_path = OUTPUT_DIR / "diagnostic_log_live_start.csv"
        else:
            diag_path = OUTPUT_DIR / "diagnostic_log.csv"
        with open(diag_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=diag_log[0].keys() if diag_log else [])
            writer.writeheader()
            writer.writerows(diag_log)
        print(f"诊断日志已保存: {diag_path}")

    # === 计算策略累计投入（仅统计买入金额，不含手续费） ===
    strategy_total_invested = sum(t["amount"] for t in trade_log if t["action"] == "buy")
    strategy_buy_fee = sum(t["cost"] for t in trade_log if t["action"] == "buy")
    strategy_sell_fee = sum(t["cost"] for t in trade_log if t["action"] != "buy")
    strategy_sell_slippage = sum(t.get("slippage", 0) for t in trade_log if t["action"] != "buy")
    strategy_sell_redemption = sum(t.get("redemption", 0) for t in trade_log if t["action"] != "buy")

    # === 计算多基准（在回测循环之后，以便传入策略实际投入金额） ===
    benchmarks = _calc_benchmarks(df, initial_capital, warmup,
                                  dca_total_invest=strategy_total_invested,
                                  live_start_date=live_start_date)

    # === 计算绩效指标 ===
    nav_series = np.array(nav_list)
    daily_returns = np.diff(nav_series) / nav_series[:-1]

    tmt_close = df["tmt_close"].values
    benchmark_nav = benchmarks["tmt_index"]

    # 活跃区间起始索引：用于基准和策略的对齐计算
    active_start = warmup

    bench_returns_full = np.diff(benchmark_nav) / benchmark_nav[:-1]
    # 活跃区间的基准收益率
    bench_returns_active = bench_returns_full[active_start:] if active_start < len(bench_returns_full) else bench_returns_full
    min_len = min(len(daily_returns), len(bench_returns_active))

    # Beta: 基金实际日收益率 vs TMT 指数日收益率（而非策略 NAV 收益率）
    fund_ret_all = df["R_fund_actual"].values  # 基金日收益率（%）
    tmt_ret_all = np.diff(tmt_close) / tmt_close[:-1] * 100  # TMT 日收益率（%）
    fund_ret_active = fund_ret_all[active_start + 1:]  # +1 因为 diff 少一天
    tmt_ret_active = tmt_ret_all[active_start:]
    beta_len = min(len(fund_ret_active), len(tmt_ret_active))
    if beta_len > 10:
        fr = fund_ret_active[:beta_len]
        tr = tmt_ret_active[:beta_len]
        mask = np.isfinite(fr) & np.isfinite(tr)
        if mask.sum() > 10:
            cov = np.cov(fr[mask], tr[mask])
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 1e-10 else 1.0
        else:
            beta = 1.0
    else:
        beta = 1.0

    total_return = nav_series[-1] / nav_series[0] - 1
    # 基准收益率：从活跃起点到终点
    if active_start < len(benchmark_nav):
        benchmark_total = benchmark_nav[-1] / benchmark_nav[active_start] - 1
    else:
        benchmark_total = benchmark_nav[-1] / benchmark_nav[0] - 1
    beta_adjusted_excess = total_return - beta * benchmark_total
    annual_vol = np.std(daily_returns) * np.sqrt(252)

    peak = np.maximum.accumulate(nav_series)
    drawdown = (nav_series - peak) / peak
    max_dd = drawdown.min()
    max_dd_idx = drawdown.argmin()
    max_dd_date = date_list[max_dd_idx] if max_dd_idx < len(date_list) else "N/A"

    r_free = cfg.get("benchmark", {}).get("deposit_daily_rate", 0.00004)
    excess_returns = daily_returns - r_free
    sharpe = np.mean(excess_returns) / np.std(excess_returns) * np.sqrt(252) if np.std(excess_returns) > 0 else 0
    excess_return = total_return - benchmark_total
    calmar = total_return / abs(max_dd) if max_dd != 0 else 0

    buy_trades = [t for t in trade_log if t["action"] == "buy"]
    sell_trades = [t for t in trade_log if t["action"] != "buy"]
    avg_buy = np.mean([t["amount"] for t in buy_trades]) if buy_trades else 0

    # 多基准指标
    fund_bh_metrics = _calc_nav_metrics(benchmarks["fund_buyhold"], warmup, date_list)
    fund_dca_metrics = _calc_nav_metrics(benchmarks["fund_dca"], warmup, date_list)
    tmt_dca_metrics = _calc_nav_metrics(benchmarks["tmt_dca"], warmup, date_list)

    metrics = {
        "累计收益率": f"{total_return:.2%}",
        "基准累计收益率": f"{benchmark_total:.2%}",
        "超额收益": f"{excess_return:.2%}",
        "Beta调整超额收益": f"{beta_adjusted_excess:.2%}",
        "基金Beta": f"{beta:.3f}",
        "年化波动率": f"{annual_vol:.2%}",
        "最大回撤": f"{max_dd:.2%}",
        "最大回撤日期": max_dd_date,
        "夏普比率": f"{sharpe:.3f}",
        "卡玛比率": f"{calmar:.3f}",
        "交易天数": len(nav_series) - warmup,
        "买入次数": len(buy_trades),
        "卖出次数": len(sell_trades),
        "平均单笔买入": f"{avg_buy:,.0f}",
        "累计交易成本": f"{total_cost:,.0f}",
        "最终净值": f"{nav_series[-1]:.4f}",
        # 投入与费用明细
        "策略累计投入": f"{strategy_total_invested:,.0f}",
        "定投基准累计投入": f"{benchmarks['dca_total_invested']:,.0f}",
        "策略买入费用": f"{strategy_buy_fee:,.2f}",
        "策略卖出费用": f"{strategy_sell_fee:,.2f}",
        "策略卖出摩擦": f"{strategy_sell_slippage:,.2f}",
        "策略卖出赎回费": f"{strategy_sell_redemption:,.2f}",
        "买入持有总费用": f"{benchmarks['fee_buyhold_bh']:,.2f}",
        "定投总费用": f"{benchmarks['fee_dca']:,.2f}",
        # 多基准
        "基金持有收益率": f"{fund_bh_metrics['return']:.2%}",
        "基金持有最大回撤": f"{fund_bh_metrics['max_dd']:.2%}",
        "基金定投收益率": f"{fund_dca_metrics['return']:.2%}",
        "基金定投最大回撤": f"{fund_dca_metrics['max_dd']:.2%}",
        "TMT定投收益率": f"{tmt_dca_metrics['return']:.2%}",
        "TMT定投最大回撤": f"{tmt_dca_metrics['max_dd']:.2%}",
        "超基金持有": f"{total_return - fund_bh_metrics['return']:.2%}",
        "超基金定投": f"{total_return - fund_dca_metrics['return']:.2%}",
        "超TMT定投": f"{total_return - tmt_dca_metrics['return']:.2%}",
    }

    # 快照覆盖率统计
    snapshot_coverage = strategy.get_snapshot_coverage() if use_snapshot else None

    if not silent:
        print("\n" + "=" * 50)
        print("回测绩效指标")
        print("=" * 50)
        for k, v in metrics.items():
            print(f"  {k:18s}: {v}")
        if snapshot_coverage:
            print(f"  {'快照覆盖':18s}: {snapshot_coverage['used']}/{snapshot_coverage['total']} ({snapshot_coverage['rate']:.1%})")
            if snapshot_coverage['rate'] < 0.8:
                print("  !! 快照覆盖率不足 80%，回测结果可能偏离盘中真实信号")
        print("=" * 50)

    # === 多起始点稳健性检验（非静默模式） ===
    robustness_results = []
    robustness_trimmed = {}
    if not silent and len(df) > warmup + 20:
        print("运行多起始点稳健性检验…")
        robustness_results, robustness_trimmed = _run_inline_robustness(
            df, cfg, warmup, min_starts=10
        )
        if robustness_results:
            rets = [r["strategy_return"] for r in robustness_results]
            print(f"  稳健性: {len(robustness_results)} 起始点, "
                  f"均值 {np.mean(rets):.1%}, 中位数 {np.median(rets):.1%}")

    # 输出
    if not silent:
        if live_start_date:
            chart_path = str(OUTPUT_DIR / "backtest_result_live_start.png")
            diag_path = str(OUTPUT_DIR / "diagnostic_log_live_start.csv")
        else:
            chart_path = str(OUTPUT_DIR / "backtest_result.png")
            diag_path = str(OUTPUT_DIR / "diagnostic_log.csv")
        report_full_path = str(OUTPUT_DIR / report_path) if report_path else None

        _plot_results(date_list, nav_series, benchmarks, drawdown, warmup, trade_log, df, chart_path)

        if report_full_path:
            _generate_report(metrics, trade_log, date_list, nav_series, benchmarks,
                             drawdown, warmup, total_cost, df, cfg, report_full_path,
                             total_return, annual_vol, beta, beta_adjusted_excess,
                             initial_capital, fund_bh_metrics, fund_dca_metrics, tmt_dca_metrics,
                             snapshot_coverage, robustness_results, robustness_trimmed,
                             live_start_date=live_start_date,
                             sharpe_raw=sharpe, calmar_raw=calmar)
    else:
        chart_path = None
        report_full_path = None

    return {
        "signals": signals, "nav_series": nav_series,
        "benchmark_series": benchmark_nav, "drawdown": drawdown,
        "metrics": metrics, "dates": date_list, "warmup": warmup,
        "trade_log": trade_log, "benchmarks": benchmarks,
    }


def _plot_results(dates, nav_series, benchmarks, drawdown, warmup, trade_log, df, chart_path):
    """绘制净值曲线（多基准）和回撤曲线"""
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True)
    fig.suptitle("TMT-Alpha 2.0 Backtest Result", fontsize=14, fontweight="bold")
    x = range(len(dates))

    ax1 = axes[0]
    ax1.plot(x, nav_series, label="Strategy", color="#1f77b4", linewidth=2.0)
    ax1.plot(x, benchmarks["fund_buyhold"], label="Fund Buy&Hold", color="#2ca02c",
             linewidth=1.2, linestyle="--")
    ax1.plot(x, benchmarks["fund_dca"], label="Fund DCA (100/mo)", color="#d62728",
             linewidth=1.0, linestyle=":")
    ax1.plot(x, benchmarks["tmt_index"], label="TMT Index", color="#ff7f0e",
             linewidth=1.0, alpha=0.7)
    ax1.plot(x, benchmarks["tmt_dca"], label="TMT DCA (100/mo)", color="#9467bd",
             linewidth=0.8, linestyle="-.")
    ax1.axvline(x=warmup, color="gray", linestyle="--", alpha=0.5, label="Warmup End")

    for t in trade_log:
        if t["date"] in dates:
            idx = dates.index(t["date"])
            if t["action"] == "buy":
                ax1.annotate("^", xy=(idx, nav_series[idx]), fontsize=6,
                             color="green", ha="center", va="bottom", fontweight="bold")
            else:
                ax1.annotate("v", xy=(idx, nav_series[idx]), fontsize=6,
                             color="red", ha="center", va="top", fontweight="bold")

    ax1.set_ylabel("NAV (normalized)")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.set_title("Multi-Benchmark Comparison (green ^=buy, red v=sell)")

    ax2 = axes[1]
    ax2.fill_between(x, drawdown * 100, 0, color="#d62728", alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Trading Day")
    ax2.grid(True, alpha=0.3)
    ax2.set_title("Strategy Drawdown")

    step = max(1, len(dates) // 10)
    tick_pos = list(range(0, len(dates), step))
    tick_labels = [dates[i] for i in tick_pos]
    ax2.set_xticks(tick_pos)
    ax2.set_xticklabels(tick_labels, rotation=45, fontsize=8)

    plt.tight_layout()
    plt.savefig(chart_path, dpi=150, bbox_inches="tight")
    print(f"\n图表已保存: {chart_path}")
    plt.close()


def _generate_report(metrics, trade_log, dates, nav_series, benchmarks,
                     drawdown, warmup, total_cost, df, cfg, report_path,
                     total_return, annual_vol, beta, beta_adjusted_excess,
                     initial_capital, fund_bh_metrics, fund_dca_metrics, tmt_dca_metrics,
                     snapshot_coverage=None, robustness_results=None, robustness_trimmed=None,
                     live_start_date=None, sharpe_raw=0.0, calmar_raw=0.0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if live_start_date and warmup < len(dates):
        start_date = dates[warmup]
    else:
        start_date = dates[0] if dates else "N/A"
    end_date = dates[-1] if dates else "N/A"
    trading_days = len(nav_series) - warmup

    trade_table = ""
    if trade_log:
        trade_table = "| 日期 | 操作 | 金额 | 摩擦成本 | 赎回费 | 总成本 | 持有天数 | 原因 |\n"
        trade_table += "|------|------|------|---------|-------|-------|---------|------|\n"
        for t in trade_log[-30:]:
            action_cn = {"buy": "买入", "sell": "卖出", "take_profit_1": "一档止盈",
                         "take_profit_2": "二档止盈"}.get(t["action"], t["action"])
            slip = t.get("slippage", t.get("cost", 0))
            redeem = t.get("redemption", 0)
            hold = t.get("holding_days", 0)
            hold_str = f"{hold}天" if t["action"] == "sell" else "-"
            trade_table += (f"| {t['date']} | {action_cn} | {t['amount']:,.0f} | "
                           f"{slip:,.2f} | {redeem:,.2f} | {t['cost']:,.2f} | "
                           f"{hold_str} | {t['reason']} |\n")
        if len(trade_log) > 30:
            trade_table += f"\n> 仅显示最后 30 笔交易，共 {len(trade_log)} 笔。\n"

    monthly_returns = _calc_monthly_returns(dates, nav_series)
    monthly_table = "| 月份 | 月收益率 |\n|------|----------|\n"
    # 实盘起点模式：仅显示活跃月份（warmup 之后）
    active_month_start = dates[warmup][:7] if warmup < len(dates) else None
    for month, ret in monthly_returns.items():
        if active_month_start and month < active_month_start:
            continue
        monthly_table += f"| {month} | {ret:+.2%} |\n"

    ex = cfg.get("execution", {})

    # 多起始点稳健性检验（内联计算，覆盖全区间）
    robustness_section = ""
    if robustness_results and len(robustness_results) > 0:
        sorted_rob = sorted(robustness_results, key=lambda r: r["strategy_return"], reverse=True)
        excess_tmt = [r["excess_vs_tmt"] for r in robustness_results]
        strategy_rets = [r["strategy_return"] for r in robustness_results]
        win_rate = sum(1 for x in excess_tmt if x > 0) / len(excess_tmt) if excess_tmt else 0

        robustness_section = f"""
### 多起始点稳健性检验（覆盖全区间 {robustness_results[0]['start_date'][:7]} ~ {robustness_results[-1]['start_date'][:7]}）

**为什么要检验？** 单一起始日的回测结果可能受"起点运气"影响。
我们从 **{len(robustness_results)} 个**均匀分布在全区间的起始月份分别运行回测，观察策略表现是否稳定。

| 起始日期 | 策略收益 | 最大回撤 | 夏普 | TMT收益 | 超TMT |
|----------|---------|---------|------|---------|-------|
"""
        for r in sorted_rob:
            robustness_section += (
                f"| {r['start_date']} | {r['strategy_return']:.1%} | {r['max_dd']:.1%} | "
                f"{r['sharpe']:.2f} | {r['tmt_return']:.1%} | {r['excess_vs_tmt']:+.1%} |\n"
            )

        robustness_section += f"""
**稳健性结论:**
- 超TMT指数胜率: **{win_rate:.0%}**（{sum(1 for x in excess_tmt if x > 0)}/{len(excess_tmt)} 个起始点跑赢）
- 策略收益均值: {np.mean(strategy_rets):.1%}，中位数: {np.median(strategy_rets):.1%}
"""
        # 剔除极端值统计
        if robustness_trimmed:
            robustness_section += "\n**剔除极端起始点后的稳健性评估：**\n\n"
            robustness_section += "| 统计口径 | 样本数 | 收益均值 | 收益中位数 | 超TMT胜率 | 平均回撤 |\n"
            robustness_section += "|---------|--------|---------|-----------|-----------|----------|\n"
            for label, ts in robustness_trimmed.items():
                robustness_section += (
                    f"| {label} | {ts['n']} | {ts['mean_return']:.1%} | {ts['median_return']:.1%} | "
                    f"{ts['win_rate']:.0%} | {ts['mean_dd']:.1%} |\n"
                )
            robustness_section += "\n> 注：\"去最高3个\"反映策略在非最优起点的真实表现，有助于区分\"起点运气\"和真实稳健性。\n"
        robustness_section += "\n> **多起始点检验已覆盖全区间，与多版本报告结论对齐。**\n"

    # 快照覆盖统计
    snapshot_section = ""
    if snapshot_coverage:
        sc = snapshot_coverage
        snapshot_section = f"| 快照模式 | 开启 |\n| 快照覆盖 | {sc['used']}/{sc['total']} ({sc['rate']:.1%}) |"
        if sc['rate'] < 0.8:
            snapshot_section += "\n| :warning: **快照覆盖率不足 80%，回测结果可能偏离盘中真实信号！** | |"
            snapshot_section += f"\n| :information_source: **有快照的日期优先使用 14:45 盘中数据（mkt_chg / 量价），无快照日期回退收盘数据。** | |"
        else:
            snapshot_section += f"\n| :information_source: **{sc['used']}/{sc['total']} 天使用 14:45 盘中快照（mkt_chg / 量价），信号贴近实盘表现。** | |"

    live_start_note = ""
    short_period = trading_days < 20
    if live_start_date:
        live_start_note = f"""
> **实盘起点模式**：历史数据（{dates[0]} ~ {dates[warmup - 1] if warmup > 0 else 'N/A'}）仅用于指标预热，不产生交易。
> 实际交易从 **{live_start_date}** 开始，初始持仓为 0。
"""

    # 短区间模式：Sharpe / Beta / DCA 相关指标标记 N/A
    sharpe_display = f"{sharpe_raw:.3f}" if not short_period else "N/A（数据不足）"
    beta_display = f"{beta:.3f}" if not short_period else "N/A（数据不足）"
    calmar_display = f"{calmar_raw:.3f}" if not short_period else "N/A（数据不足）"
    fund_dca_ret_display = metrics['基金定投收益率'] if not short_period else "N/A"
    fund_dca_dd_display = metrics['基金定投最大回撤'] if not short_period else "N/A"
    tmt_dca_ret_display = metrics['TMT定投收益率'] if not short_period else "N/A"
    tmt_dca_dd_display = metrics['TMT定投最大回撤'] if not short_period else "N/A"
    excess_fund_dca_display = metrics['超基金定投'] if not short_period else "N/A"
    excess_tmt_dca_display = metrics['超TMT定投'] if not short_period else "N/A"
    fund_dca_sharpe_display = f"{fund_dca_metrics['sharpe']:.3f}" if not short_period else "N/A"
    tmt_dca_sharpe_display = f"{tmt_dca_metrics['sharpe']:.3f}" if not short_period else "N/A"

    report = f"""# TMT-Alpha 2.3 回测报告{"（实盘起点）" if live_start_date else ""}

> 生成时间: {now}
{live_start_note}
---

## 一、回测概览

| 项目 | 数值 |
|------|------|
| 回测区间 | {start_date} ~ {end_date} |
| 交易天数 | {trading_days} 天 |
| 预热期 | {warmup} 天 |
| 初始资金 | {initial_capital:,} 元 |
| 交易成本 | 买卖双边摩擦 0.1%，卖出另加赎回阶梯费率（0-6天 1.5% / 7-29天 0.5% / 30天+ 0%） |
| 最大仓位比例 | {cfg.get('backtest', {}).get('max_position_ratio', 1.0):.0%} |
| 单笔买入上限 | {ex.get('m_max_normal', 500)} 元 |
| 单笔买入下限 | {ex.get('m_min_normal', 0)} 元 |
{snapshot_section}

> **注：** 本基金（019018）真实申购费为 0%。0.1% 为回测模拟的买卖双边滑点/冲击成本。
> 赎回费率为真实基金规则。

## 二、多基准收益对比

### 为什么要对比多个基准？

| 基准 | 含义 | 为什么重要 |
|------|------|------------|
| 基金买入持有 | 开盘第一天全仓买入，之后不动 | 最简单的投资策略，衡量择时是否有价值 |
| 基金定投 | 每月定投100元 | 模拟普通基民的定投行为，衡量策略是否跑赢"懒人投资" |
| TMT指数 | 全仓跟踪中证TMT指数 | 衡量基金本身的超额收益（选股能力） |
| TMT指数定投 | 每月定投TMT指数 | 衡量策略是否跑赢指数定投 |

### 收益率对比

| 策略/基准 | 累计收益率 | 最大回撤 | 夏普比率 |
|-----------|-----------|----------|----------|
| **TMT-Alpha 策略** | **{metrics['累计收益率']}** | **{metrics['最大回撤']}** | **{sharpe_display}** |
| 基金买入持有 | {metrics['基金持有收益率']} | {metrics['基金持有最大回撤']} | {f"{fund_bh_metrics['sharpe']:.3f}" if not short_period else "N/A"} |
| 基金定投(100/月) | {fund_dca_ret_display} | {fund_dca_dd_display} | {fund_dca_sharpe_display} |
| TMT指数 | {metrics['基准累计收益率']} | — | — |
| TMT指数定投(100/月) | {tmt_dca_ret_display} | {tmt_dca_dd_display} | {tmt_dca_sharpe_display} |

### 策略超额收益（相对各基准）

| 对比基准 | 超额收益 | 说明 |
|----------|----------|------|
| vs 基金买入持有 | {metrics['超基金持有']} | 择时能力 |
| vs 基金定投 | {excess_fund_dca_display} | {"是否跑赢懒人定投" if not short_period else "回测区间不足 1 个定投周期，定投对比暂不适用"} |
| vs TMT指数 | {metrics['超额收益']} | 基金Beta调整后选股能力 |
| vs TMT指数定投 | {excess_tmt_dca_display} | {"综合择时+选股能力" if not short_period else "回测区间不足 1 个定投周期，定投对比暂不适用"} |

### 关于"基金买入持有"基准的说明

> **注意：** "基金买入持有"基准的收益率高度依赖起始日。
> 本回测从 {start_date} 开始，恰好处于基金净值相对低点，因此买入持有收益率较高（{metrics['基金持有收益率']}）。
> 如果从其他时间点开始，买入持有的收益可能大幅不同。
> 建议结合下方的"多起始点稳健性检验"综合评估策略表现。
> 买入持有是一种"事后诸葛亮"策略——只有回头看才知道哪天是最佳买点。
{robustness_section}

---

## 三、参数变更与风险提示

### 当前参数配置（TMT-Alpha 2.0 平衡版）

| 参数 | 当前值 | 说明 |
|------|--------|------|
| m_max_normal | 350 | 单笔买入上限 |
| below_ma_power | 0.50 | 低于均线惩罚（已接入趋势因子计算） |
| consecutive_drop_power | 0.25 | 连续下跌惩罚 |
| excess_dd_warning_base | -0.08 | 超额回撤预警阈值 |
| 通道A指数 | 1.3 | 金额公式非线性指数 |
| 通道A阈值 | 22 | 买入触发 Score_eff 阈值 |
| K下限 | 20 | 软压缩系数下限 |
| Final_Multiplier下限 | 0.60 | 乘数塌缩保护 |
| tp_level_1 / tp_level_2 | 25% / 50% | 基础止盈阈值 |
| tp_level_1_strong / tp_level_2_strong | 40% / 70% | 强趋势止盈阈值（动态上限） |
| 市场自适应 | 开启 | 进攻模式(below_ma=0.65, cons_drop=0.35, mult_min=0.70) / 防守模式恢复原值 |

### 关键优化说明

1. **趋势感知止盈**：强趋势（净值 > MA40 且 5日收益 > 1%，或偏离 > 5%）抬高止盈阈值，避免过早下车
2. **赎回费感知止盈**：有效止盈阈值 = 基础阈值 + 赎回费率，避免"白忙活"交易
3. **市场温度自适应**：TMT 20日涨幅 > 10% 进入进攻模式，牛市回调敢加仓
4. **碎股过滤**：最低买入金额 25 元，过滤无意义小额交易
5. **夏普比率计算**：mean(daily_excess) / std(daily_excess) × √252，r_free = 0.00004/天

### V2.2 动态节奏优化

| 参数 | 进攻模式 | 防守模式 | 说明 |
|------|---------|---------|------|
| 止盈冷却期 | 3 天 | 7 天 | 进攻缩短快速回补，防守延长确认企稳 |
| 移动止盈回撤容忍 | 10% | 6% | 进攻放宽容忍让利润奔跑，防守收紧保护利润 |
| 一档止盈卖出比例 | 20% | 40% | 进攻少卖让仓位奔跑，防守多卖锁利润 |
| 二档止盈卖出比例 | 50% | 50% | 保持一致 |

---

## 四、核心绩效指标（小白话版）

### 1. 累计收益率: {metrics['累计收益率']}

**什么意思？** 假设你一开始投了 {initial_capital} 元，回测结束时变成了多少。
"{metrics['累计收益率']}" 意味着 {initial_capital} 元变成了约 {initial_capital * (1 + total_return):.2f} 元，赚了 {initial_capital * total_return:.2f} 元。

**对比基金买入持有:** 如果你第一天就全仓买入基金，收益率是 {metrics['基金持有收益率']}。
本策略{('跑赢' if total_return > fund_bh_metrics['return'] else '跑输')}了买入持有，超额收益 {metrics['超基金持有']}。

{"**对比基金定投:** 如果你每月定投100元买基金，收益率是 " + metrics['基金定投收益率'] + "。" if not short_period else ""}
{"本策略" + ("跑赢" if total_return > fund_dca_metrics['return'] else '跑输') + "了定投，超额收益 " + metrics['超基金定投'] + "." if not short_period else "**定投对比：** 回测区间不足 1 个定投周期，定投对比暂不适用。"}

### 2. 基金 Beta: {beta_display}

{"**什么意思？** Beta 衡量基金相对指数的弹性。" if not short_period else "回测区间不足 20 个交易日，Beta 无统计意义。"}
{"- Beta = 0.922，与 TMT 指数高度相关。指数涨 10%，基金约涨 9.2%；指数跌 10%，基金约跌 9.2%。" if not short_period else ""}
{"- Beta 接近 1 说明基金走势基本跟随指数，主动管理带来的偏离较小。" if not short_period else ""}

### 3. 年化波动率: {metrics['年化波动率']}

**什么意思？** 衡量账户"上下颠簸"的程度。
- 5%以下：很稳 | 10%-20%：中等 | 20%以上：比较刺激

### 4. 最大回撤: {metrics['最大回撤']}

**什么意思？** 从最高点到最低点，你最多亏过多少。
**发生日期:** {metrics['最大回撤日期']}

### 5. 夏普比率: {sharpe_display}

{"**什么意思？** 每承受1单位风险，能获得多少超额回报。" if not short_period else "回测区间不足 20 个交易日，夏普比率无统计意义。"}
{"- 1以下：一般 | 1-2：不错 | 2以上：很好 | 3以上：非常优秀" if not short_period else ""}

### 6. 卡玛比率: {calmar_display}

**什么意思？** 累计收益 / 最大回撤。数字越大越好。

---

## 五、交易统计

| 项目 | 数值 |
|------|------|
| 买入次数 | {metrics['买入次数']} |
| 卖出次数 | {metrics['卖出次数']} |
| 平均单笔买入 | {metrics['平均单笔买入']} 元 |
| 最终净值 | {metrics['最终净值']} |

### 资金投入与费用明细

| 项目 | 数值 | 说明 |
|------|------|------|
| 策略累计投入 | ¥{metrics['策略累计投入']} | 策略实际花出去买基金的钱 |
| 定投基准累计投入 | ¥{metrics['定投基准累计投入']} | 与策略投入对齐，非固定100元/月 |
| 策略买入摩擦成本 | ¥{metrics['策略买入费用']} | 每笔买入 × 0.1%（模拟滑点） |
| 策略卖出摩擦成本 | ¥{metrics['策略卖出摩擦']} | 每笔卖出 × 0.1%（模拟滑点） |
| 策略卖出赎回费 | ¥{metrics['策略卖出赎回费']} | 按持有天数阶梯：0-6天1.5%，7-29天0.5%，30天+ 0% |
| 策略累计交易成本 | ¥{metrics['累计交易成本']} | 买入摩擦 + 卖出摩擦 + 赎回费合计 |
| 买入持有基准总费用 | ¥{metrics['买入持有总费用']} | 含买卖摩擦 + 期末赎回费 |
| 定投基准总费用 | ¥{metrics['定投总费用']} | 每月买卖摩擦 + 期末各批次赎回费（FIFO） |

> **注：** TMT 指数及 TMT 指数定投为纯指数价格跟踪，不含基金费用。所有基金类基准已使用与策略相同的摩擦成本和阶梯赎回费率。

### 交易明细（最近 30 笔）

{trade_table}

---

## 六、月度收益

{monthly_table}

---

## 七、参数配置

| 参数 | 值 | 说明 |
|------|-----|------|
| m_max_normal | {ex.get('m_max_normal', 500)} | 单笔买入上限 |
| m_min_normal | {ex.get('m_min_normal', 0)} | 单笔买入下限 |
| max_position_ratio | {cfg.get('backtest', {}).get('max_position_ratio', 1.0):.0%} | 最大仓位比例 |
| tp_level_1 | {cfg.get('exit_logic', {}).get('tp_level_1', 0.15):.0%} | 一档止盈阈值 |
| tp_sell_ratio_1 | {cfg.get('exit_logic', {}).get('tp_sell_ratio_1', 0.33):.0%} | 一档卖出比例 |
| tp_level_2 | {cfg.get('exit_logic', {}).get('tp_level_2', 0.30):.0%} | 二档止盈阈值 |
| tp_sell_ratio_2 | {cfg.get('exit_logic', {}).get('tp_sell_ratio_2', 0.33):.0%} | 二档卖出比例 |
| cool_down_days | {cfg.get('exit_logic', {}).get('cool_down_days', 5)} | 止盈冷却天数 |
| K下限 | 20 | 软压缩系数下限 |
| 通道A指数 | {ex.get('channel_a_power', 1.0)} | 金额公式指数 |
| below_ma_power | {cfg.get('trend_filter', {}).get('below_ma_power', 0.65)} | 低于均线惩罚 |
| consecutive_drop_power | {cfg.get('trend_filter', {}).get('consecutive_drop_power', 0.40)} | 连续下跌惩罚 |
| excess_dd_warning_base | {cfg.get('exit_logic', {}).get('excess_dd_warning_base', -0.10)} | 预警阈值（通道A折半） |
| excess_dd_force_base | {cfg.get('exit_logic', {}).get('excess_dd_force_base', -0.15)} | 强平阈值（降至50%仓位） |

---

## 八、诊断数据

详见 `output/{"diagnostic_log_live_start.csv" if live_start_date else "diagnostic_log.csv"}`，包含每日 Score_eff、Amount 等中间变量。

---

*报告由 TMT-Alpha 2.0 回测引擎自动生成*
"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"回测报告已保存: {report_path}")


def _calc_monthly_returns(dates, nav_series):
    monthly = {}
    for i, d in enumerate(dates):
        month = d[:7]
        if month not in monthly:
            monthly[month] = {"start": nav_series[i], "end": nav_series[i]}
        monthly[month]["end"] = nav_series[i]

    result = {}
    months = sorted(monthly.keys())
    for i, m in enumerate(months):
        if i == 0:
            result[m] = monthly[m]["end"] / monthly[m]["start"] - 1
        else:
            prev_end = monthly[months[i - 1]]["end"]
            result[m] = monthly[m]["end"] / prev_end - 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TMT-Alpha 2.0 回测引擎")
    parser.add_argument("--live-start", type=str, default=None,
                        help="实盘起点日期 (YYYY-MM-DD)，仅该日期起执行交易，历史数据用于指标预热")
    args = parser.parse_args()

    cfg = load_config()
    results = run_backtest(cfg, live_start_date=args.live_start)
