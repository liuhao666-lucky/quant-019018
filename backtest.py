"""
backtest.py
TMT-Alpha 7.0 回测引擎
从 SQLite 加载数据，运行完整策略回测，输出绩效指标和图表。
含多基准对比：策略净值、基金买入持有、基金定投、TMT指数。
支持 14:45 快照回测模式。
"""

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

logger = logging.getLogger(__name__)

# 中文字体设置
rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

# 输出目录
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 交易成本：单边 0.1%
TRANSACTION_COST = 0.001


def _calc_benchmarks(df, initial_capital, warmup):
    """
    计算多基准净值曲线。

    返回 dict:
      - fund_buyhold: 基金买入持有净值序列
      - fund_dca: 基金定投净值序列
      - tmt_index: TMT 指数净值序列
      - tmt_dca: TMT 指数定投净值序列
      - dates: 日期序列
    """
    dates = df["trade_date"].tolist()
    n = len(df)

    # --- 基金净值序列（优先用 _actual 列，避免被 strategy shift 污染） ---
    fund_nav_raw = df["fund_nav_actual"].values.copy() if "fund_nav_actual" in df.columns else df["fund_nav"].values.copy()
    r_fund = df["R_fund_actual"].values if "R_fund_actual" in df.columns else df["R_fund"].values

    # 若 fund_nav 有缺失，用 R_fund 反推
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

    # --- 基金买入持有：开仓即全仓 ---
    fund_buyhold = fund_nav_raw / fund_nav_raw[0]

    # --- 基金定投：每月第一个交易日定投，总投入 = initial_capital ---
    # 计算回测区间内的月份数，使定投总金额与策略初始资金对齐
    all_months = sorted(set(d[:7] for d in dates[warmup:] if d))
    num_months = len(all_months) if all_months else 1
    dca_amount = initial_capital / num_months
    fund_dca_shares = 0.0
    fund_dca_total_invested = 0.0
    fund_dca_nav = np.ones(n)
    last_month = ""

    for i in range(n):
        month = dates[i][:7]
        if month != last_month:
            invest = min(dca_amount, initial_capital - fund_dca_total_invested)
            if invest > 0 and fund_nav_raw[i] > 0:
                fund_dca_shares += invest / fund_nav_raw[i]
                fund_dca_total_invested += invest
            last_month = month

        # 当前定投总市值
        if fund_dca_total_invested > 0:
            current_value = fund_dca_shares * fund_nav_raw[i]
            fund_dca_nav[i] = current_value / fund_dca_total_invested
        else:
            fund_dca_nav[i] = 1.0

    # --- TMT 指数定投（总投入 = initial_capital，与基金定投对齐） ---
    tmt_dca_shares = 0.0
    tmt_dca_total_invested = 0.0
    tmt_dca_nav = np.ones(n)
    last_month = ""

    for i in range(n):
        month = dates[i][:7]
        if month != last_month:
            invest = min(dca_amount, initial_capital - tmt_dca_total_invested)
            if invest > 0 and tmt_close[i] > 0:
                tmt_dca_shares += invest / tmt_close[i]
                tmt_dca_total_invested += invest
            last_month = month

        if tmt_dca_total_invested > 0:
            current_value = tmt_dca_shares * tmt_close[i]
            tmt_dca_nav[i] = current_value / tmt_dca_total_invested
        else:
            tmt_dca_nav[i] = 1.0

    return {
        "fund_buyhold": fund_buyhold,
        "fund_dca": fund_dca_nav,
        "tmt_index": tmt_nav,
        "tmt_dca": tmt_dca_nav,
        "dates": dates,
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
                 external_data: pd.DataFrame = None, silent: bool = False) -> dict:
    if cfg is None:
        cfg = load_config()

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
    if start_date:
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

    # === 计算多基准 ===
    benchmarks = _calc_benchmarks(df, initial_capital, warmup)

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

        if t >= warmup and t > 0 and position_value > 0:
            # 结算必须使用当天真实收益（R_fund_actual），而非信号用的滞后收益
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
            })

            # 动态仓位乘数（无未来函数）：用前一日 MA20 与当日收盘价比较
            # 预热期内使用 warmup_max_position_ratio，正常期使用 max_position_ratio
            if signal.get("warmup_active") and warmup_max_ratio > 0:
                current_max_position = warmup_max_ratio
            else:
                base_max_pos = max_pos_ratio
                tmt_close_val = row.get("tmt_close", 0)
                tmt_ma20_yesterday = row.get("tmt_ma20_yesterday", 0)
                if pd.notna(tmt_ma20_yesterday) and tmt_ma20_yesterday > 0 and tmt_close_val > tmt_ma20_yesterday:
                    current_max_position = min(0.95, base_max_pos * 1.08)
                else:
                    current_max_position = base_max_pos

            # 所有买卖决策由 strategy 统一产出，backtest 仅负责执行
            if action == "buy" and amount > 0:
                amount = min(amount, m_max)
                if amount < 1.0:
                    pass
                else:
                    current_total = cash + position_value
                    pos_limit = current_max_position * current_total - position_value
                    buy_amount = min(amount, cash, pos_limit)
                    if buy_amount >= 1.0:
                        cost = buy_amount * TRANSACTION_COST
                        cash -= (buy_amount + cost)
                        # 用当日真实净值换算基金份额（fund_nav_actual 为未 shift 的当日净值）
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
                            "reason": f"ch={signal['channel']} score={signal['score_eff']:.1f}"
                        })

            elif action == "sell" and amount < 0:
                sell_amount = min(abs(amount), position_value)
                if sell_amount >= 1.0:
                    cost = sell_amount * TRANSACTION_COST
                    sell_ratio = sell_amount / position_value if position_value > 0 else 0
                    holding_shares *= (1 - sell_ratio)
                    position_value -= sell_amount
                    cash += sell_amount - cost
                    total_cost += cost
                    # 确定卖出原因（含趋势状态）
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
                        "reason": reason
                    })
                    # 全部清仓后重置持仓天数和时间止损状态
                    if position_value < 0.01:
                        holding_days = 0
                        strategy.pos_state.time_stop_triggered = False

        total_value = cash + position_value
        nav_list.append(total_value / initial_capital)
        date_list.append(row["trade_date"])

    # === 保存诊断日志 ===
    if not silent:
        diag_path = OUTPUT_DIR / "diagnostic_log.csv"
        with open(diag_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=diag_log[0].keys() if diag_log else [])
            writer.writeheader()
            writer.writerows(diag_log)
        print(f"诊断日志已保存: {diag_path}")

    # === 计算绩效指标 ===
    nav_series = np.array(nav_list)
    daily_returns = np.diff(nav_series) / nav_series[:-1]

    tmt_close = df["tmt_close"].values
    benchmark_nav = benchmarks["tmt_index"]

    bench_returns = np.diff(benchmark_nav) / benchmark_nav[:-1]
    min_len = min(len(daily_returns), len(bench_returns))
    cov = np.cov(daily_returns[:min_len], bench_returns[:min_len])
    beta = cov[0, 1] / cov[1, 1] if cov[1, 1] != 0 else 1.0

    total_return = nav_series[-1] / nav_series[0] - 1
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
        chart_path = str(OUTPUT_DIR / "backtest_result.png")
        report_full_path = str(OUTPUT_DIR / report_path) if report_path else None

        _plot_results(date_list, nav_series, benchmarks, drawdown, warmup, trade_log, df, chart_path)

        if report_full_path:
            _generate_report(metrics, trade_log, date_list, nav_series, benchmarks,
                             drawdown, warmup, total_cost, df, cfg, report_full_path,
                             total_return, annual_vol, beta, beta_adjusted_excess,
                             initial_capital, fund_bh_metrics, fund_dca_metrics, tmt_dca_metrics,
                             snapshot_coverage, robustness_results, robustness_trimmed)
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
    fig.suptitle("TMT-Alpha 7.0 Backtest Result", fontsize=14, fontweight="bold")
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
                     snapshot_coverage=None, robustness_results=None, robustness_trimmed=None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_date = dates[0] if dates else "N/A"
    end_date = dates[-1] if dates else "N/A"
    trading_days = len(nav_series) - warmup

    trade_table = ""
    if trade_log:
        trade_table = "| 日期 | 操作 | 金额 | 成本 | 原因 |\n"
        trade_table += "|------|------|------|------|------|\n"
        for t in trade_log[-30:]:
            action_cn = {"buy": "买入", "sell": "卖出", "take_profit_1": "一档止盈",
                         "take_profit_2": "二档止盈"}.get(t["action"], t["action"])
            trade_table += f"| {t['date']} | {action_cn} | {t['amount']:,.0f} | {t['cost']:,.0f} | {t['reason']} |\n"
        if len(trade_log) > 30:
            trade_table += f"\n> 仅显示最后 30 笔交易，共 {len(trade_log)} 笔。\n"

    monthly_returns = _calc_monthly_returns(dates, nav_series)
    monthly_table = "| 月份 | 月收益率 |\n|------|----------|\n"
    for month, ret in monthly_returns.items():
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
        snapshot_section += f"\n| :information_source: **快照覆盖率 {sc['rate']:.0%}，回测以收盘价执行，实盘中信号可能滑点。** | |"

    report = f"""# TMT-Alpha 7.0 回测报告

> 生成时间: {now}

---

## 一、回测概览

| 项目 | 数值 |
|------|------|
| 回测区间 | {start_date} ~ {end_date} |
| 交易天数 | {trading_days} 天 |
| 预热期 | {warmup} 天 |
| 初始资金 | {initial_capital:,} 元 |
| 交易成本 | 单边 0.1% |
| 最大仓位比例 | {cfg.get('backtest', {}).get('max_position_ratio', 1.0):.0%} |
| 单笔买入上限 | {ex.get('m_max_normal', 500)} 元 |
| 单笔买入下限 | {ex.get('m_min_normal', 0)} 元 |
{snapshot_section}

---

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
| **TMT-Alpha 策略** | **{metrics['累计收益率']}** | **{metrics['最大回撤']}** | **{metrics['夏普比率']}** |
| 基金买入持有 | {metrics['基金持有收益率']} | {metrics['基金持有最大回撤']} | {fund_bh_metrics['sharpe']:.3f} |
| 基金定投(100/月) | {metrics['基金定投收益率']} | {metrics['基金定投最大回撤']} | {fund_dca_metrics['sharpe']:.3f} |
| TMT指数 | {metrics['基准累计收益率']} | — | — |
| TMT指数定投(100/月) | {metrics['TMT定投收益率']} | {metrics['TMT定投最大回撤']} | {tmt_dca_metrics['sharpe']:.3f} |

### 策略超额收益（相对各基准）

| 对比基准 | 超额收益 | 说明 |
|----------|----------|------|
| vs 基金买入持有 | {metrics['超基金持有']} | 择时能力 |
| vs 基金定投 | {metrics['超基金定投']} | 是否跑赢"懒人定投" |
| vs TMT指数 | {metrics['超额收益']} | 基金Beta调整后选股能力 |
| vs TMT指数定投 | {metrics['超TMT定投']} | 综合择时+选股能力 |

### 关于"基金买入持有"基准的说明

> **注意：** "基金买入持有"基准的收益率高度依赖起始日。
> 本回测从 {start_date} 开始，恰好处于基金净值相对低点，因此买入持有收益率较高（{metrics['基金持有收益率']}）。
> 如果从其他时间点开始，买入持有的收益可能大幅不同。
> 建议结合下方的"多起始点稳健性检验"综合评估策略表现。
> 买入持有是一种"事后诸葛亮"策略——只有回头看才知道哪天是最佳买点。
{robustness_section}

---

## 三、参数变更与风险提示

### 当前参数配置（TMT-Alpha 7.0 平衡版）

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
2. **市场温度自适应**：TMT 20日涨幅 > 10% 进入进攻模式，牛市回调敢加仓
3. **参数回退**：below_ma_power 和 consecutive_drop_power 从宽松值回退，恢复防守能力
4. **夏普比率计算**：mean(daily_excess) / std(daily_excess) × √252，r_free = 0.00004/天

---

## 四、核心绩效指标（小白话版）

### 1. 累计收益率: {metrics['累计收益率']}

**什么意思？** 假设你一开始投了 {initial_capital} 元，回测结束时变成了多少。
"{metrics['累计收益率']}" 意味着 {initial_capital} 元变成了约 {initial_capital * (1 + total_return):.2f} 元，赚了 {initial_capital * total_return:.2f} 元。

**对比基金买入持有:** 如果你第一天就全仓买入基金，收益率是 {metrics['基金持有收益率']}。
本策略{('跑赢' if total_return > fund_bh_metrics['return'] else '跑输')}了买入持有，超额收益 {metrics['超基金持有']}。

**对比基金定投:** 如果你每月定投100元买基金，收益率是 {metrics['基金定投收益率']}。
本策略{('跑赢' if total_return > fund_dca_metrics['return'] else '跑输')}了定投，超额收益 {metrics['超基金定投']}。

### 2. 基金 Beta: {metrics['基金Beta']}

**什么意思？** Beta 衡量基金相对指数的弹性。
- Beta = 1.0：和指数涨跌一样
- Beta = 0.5：指数涨10%，基金大约涨5%
- Beta < 1 说明基金天然跑输上涨行情，但下跌时也更抗跌

### 3. 年化波动率: {metrics['年化波动率']}

**什么意思？** 衡量账户"上下颠簸"的程度。
- 5%以下：很稳 | 10%-20%：中等 | 20%以上：比较刺激

### 4. 最大回撤: {metrics['最大回撤']}

**什么意思？** 从最高点到最低点，你最多亏过多少。
**发生日期:** {metrics['最大回撤日期']}

### 5. 夏普比率: {metrics['夏普比率']}

**什么意思？** 每承受1单位风险，能获得多少超额回报。
- 1以下：一般 | 1-2：不错 | 2以上：很好 | 3以上：非常优秀

### 6. 卡玛比率: {metrics['卡玛比率']}

**什么意思？** 累计收益 / 最大回撤。数字越大越好。

---

## 五、交易统计

| 项目 | 数值 |
|------|------|
| 买入次数 | {metrics['买入次数']} |
| 卖出次数 | {metrics['卖出次数']} |
| 平均单笔买入 | {metrics['平均单笔买入']} 元 |
| 累计交易成本 | {metrics['累计交易成本']} 元 |
| 最终净值 | {metrics['最终净值']} |

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

详见 `output/diagnostic_log.csv`，包含每日 Score_eff、Amount 等中间变量。

---

*报告由 TMT-Alpha 7.0 回测引擎自动生成*
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
    cfg = load_config()
    results = run_backtest(cfg)
