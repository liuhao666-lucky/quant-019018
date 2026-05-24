"""
robustness_check.py
TMT-Alpha 7.0 多起始点稳健性检验
从不同起始月份运行回测，收集各基准下的超额收益，评估策略稳健性。
"""

import sys
import csv
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

from core.config_loader import load_config
from db.data_pipeline import load_merged_data
from core.strategy import TMTAlphaStrategy
from model.model7_exit_logic import PositionState

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

TRANSACTION_COST = 0.001


def run_single_backtest(df, cfg, start_idx, warmup_days=60):
    """从指定索引开始运行回测，返回策略净值和基准净值"""
    bt = cfg.get("backtest", {})
    initial_capital = bt.get("initial_capital", 1000)
    max_pos_ratio = bt.get("max_position_ratio", 1.0)
    ex = cfg.get("execution", {})
    m_max = ex.get("m_max_normal", 500)
    el = cfg.get("exit_logic", {})
    tp_level_1 = el.get("tp_level_1", 0.15)
    tp_sell_ratio_1 = el.get("tp_sell_ratio_1", 0.33)
    tp_level_2 = el.get("tp_level_2", 0.30)
    tp_sell_ratio_2 = el.get("tp_sell_ratio_2", 0.33)
    tp_cooldown_days = el.get("cool_down_days", 5)

    strategy = TMTAlphaStrategy(cfg)
    full_df = strategy.prepare_data(df)

    # 截取从 start_idx 开始的数据
    sub_df = full_df.iloc[start_idx:].reset_index(drop=True)
    if len(sub_df) < warmup_days + 20:
        return None

    # 重新初始化策略状态
    strategy = TMTAlphaStrategy(cfg)

    position_value = 0.0
    cash = initial_capital
    nav_list = []
    holding_shares = 0.0
    avg_cost = 0.0
    tp_cooldown_end = -1
    tp1 = False
    tp2 = False

    for t in range(len(sub_df)):
        row = sub_df.iloc[t]
        if t >= warmup_days and t > 0 and position_value > 0:
            r = row.get("R_fund", 0)
            if pd.notna(r) and r == r:
                position_value *= (1 + r / 100)

        if t >= warmup_days:
            signal = strategy.process_day(t, sub_df)
            amount = signal["amount"]
            action = signal["action"]

            # 止盈
            if position_value > 0 and holding_shares > 0 and t > tp_cooldown_end:
                gain = position_value / (holding_shares * avg_cost) - 1 if avg_cost > 0 else 0
                if gain > tp_level_2 and not tp2:
                    sell = position_value * tp_sell_ratio_2
                    position_value -= sell
                    cash += sell
                    holding_shares *= (1 - tp_sell_ratio_2)
                    tp_cooldown_end = t + tp_cooldown_days
                    tp2 = True
                elif gain > tp_level_1 and not tp1:
                    sell = position_value * tp_sell_ratio_1
                    position_value -= sell
                    cash += sell
                    holding_shares *= (1 - tp_sell_ratio_1)
                    tp_cooldown_end = t + tp_cooldown_days
                    tp1 = True

            if action == "buy" and amount > 0:
                amount = min(amount, m_max)
                if amount >= 1.0:
                    current_total = cash + position_value
                    pos_limit = max_pos_ratio * current_total - position_value
                    buy = min(amount, cash, pos_limit)
                    if buy >= 1.0:
                        cost = buy * TRANSACTION_COST
                        cash -= (buy + cost)
                        old = holding_shares * avg_cost
                        avg_cost = (old + buy) / (holding_shares + 1) if (holding_shares + 1) > 0 else 0
                        holding_shares += 1
                        position_value += buy

        nav_list.append((cash + position_value) / initial_capital)

    nav = np.array(nav_list)

    # 基准
    tmt_close = sub_df["tmt_close"].values
    tmt_nav = tmt_close / tmt_close[0]
    fund_nav = sub_df["fund_nav"].ffill().values
    fund_nav = fund_nav / fund_nav[0]

    return {
        "start_date": sub_df["trade_date"].iloc[0],
        "end_date": sub_df["trade_date"].iloc[-1],
        "days": len(nav),
        "strategy_return": nav[-1] / nav[0] - 1,
        "fund_return": fund_nav[-1] / fund_nav[0] - 1,
        "tmt_return": tmt_nav[-1] / tmt_nav[0] - 1,
        "excess_vs_fund": (nav[-1] / nav[0] - 1) - (fund_nav[-1] / fund_nav[0] - 1),
        "excess_vs_tmt": (nav[-1] / nav[0] - 1) - (tmt_nav[-1] / tmt_nav[0] - 1),
    }


def run_robustness_check():
    """从不同起始月份运行回测，输出稳健性汇总"""
    cfg = load_config()
    raw_df = load_merged_data()

    if raw_df.empty:
        print("[错误] 无数据")
        return

    print(f"数据总量: {len(raw_df)} 行，日期范围: {raw_df['trade_date'].iloc[0]} ~ {raw_df['trade_date'].iloc[-1]}")

    # 找到每个月的第一个交易日索引
    dates = raw_df["trade_date"].tolist()
    month_starts = []
    last_month = ""
    for i, d in enumerate(dates):
        month = d[:7]
        if month != last_month:
            month_starts.append(i)
            last_month = month

    warmup = cfg.get("system", {}).get("warmup_days", 60)
    results = []

    print(f"\n共 {len(month_starts)} 个起始月份，预热期 {warmup} 天")
    print("=" * 70)

    for idx in month_starts:
        start_date = dates[idx]
        remaining = len(dates) - idx
        if remaining < warmup + 20:
            print(f"  {start_date} - 数据不足，跳过")
            continue

        result = run_single_backtest(raw_df, cfg, idx, warmup)
        if result:
            results.append(result)
            print(f"  {start_date} ~ {result['end_date']} | "
                  f"策略 {result['strategy_return']:+.1%} | "
                  f"基金 {result['fund_return']:+.1%} | "
                  f"TMT {result['tmt_return']:+.1%} | "
                  f"超基金 {result['excess_vs_fund']:+.1%}")

    # 保存结果
    csv_path = OUTPUT_DIR / "robustness_summary.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"\n稳健性检验结果已保存: {csv_path}")

    # 汇总统计
    if results:
        excess_fund = [r["excess_vs_fund"] for r in results]
        excess_tmt = [r["excess_vs_tmt"] for r in results]
        strategy_rets = [r["strategy_return"] for r in results]

        print("\n" + "=" * 70)
        print("稳健性汇总")
        print("=" * 70)
        print(f"  测试起始点数:     {len(results)}")
        print(f"  策略收益率:       均值 {np.mean(strategy_rets):.1%}, 中位数 {np.median(strategy_rets):.1%}")
        print(f"  超基金(均值):     {np.mean(excess_fund):.1%}")
        print(f"  超基金(中位数):   {np.median(excess_fund):.1%}")
        print(f"  超基金(胜率):     {sum(1 for x in excess_fund if x > 0) / len(excess_fund):.0%}")
        print(f"  超TMT(均值):      {np.mean(excess_tmt):.1%}")
        print(f"  超TMT(胜率):      {sum(1 for x in excess_tmt if x > 0) / len(excess_tmt):.0%}")
        print("=" * 70)

    return results


if __name__ == "__main__":
    run_robustness_check()
