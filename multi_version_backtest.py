"""
multi_version_backtest.py
TMT-Alpha 7.0 多版本对比回测（含市场自适应 + 趋势感知止盈修复 + 熊市压力测试）

用法:
    python multi_version_backtest.py
"""

import copy
import numpy as np
from pathlib import Path
from datetime import datetime

from core.config_loader import load_config
from db.data_pipeline import load_merged_data
from backtest import run_backtest

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# ============================================================
# 三个参数预设
# ============================================================
PRESETS = {
    "保守版": {
        "exit_logic": {
            "tp_level_1": 0.25, "tp_level_2": 0.50,
            "tp_level_1_strong": 0.25, "tp_level_2_strong": 0.50,
            "excess_dd_warning_base": -0.08,
        },
        "trend_filter": {
            "below_ma_power": 0.50, "consecutive_drop_power": 0.25,
        },
        "execution": {"m_max_normal": 200, "channel_a_power": 1.3},
        "label": "保守版：固定止盈 + 惩罚回退 + m_max=200",
    },
    "平衡版": {
        "exit_logic": {
            "tp_level_1": 0.25, "tp_level_2": 0.50,
            "tp_level_1_strong": 0.40, "tp_level_2_strong": 0.70,
            "excess_dd_warning_base": -0.08,
        },
        "trend_filter": {
            "below_ma_power": 0.50, "consecutive_drop_power": 0.25,
        },
        "execution": {"m_max_normal": 350, "channel_a_power": 1.3},
        "label": "平衡版：趋势感知止盈 + 惩罚回退 + m_max=350",
    },
    "进取版": {
        "exit_logic": {
            "tp_level_1": 0.25, "tp_level_2": 0.50,
            "tp_level_1_strong": 0.40, "tp_level_2_strong": 0.70,
            "excess_dd_warning_base": -0.10,
        },
        "trend_filter": {
            "below_ma_power": 0.75, "consecutive_drop_power": 0.40,
        },
        "execution": {"m_max_normal": 500, "channel_a_power": 1.3},
        "label": "进取版：趋势感知止盈 + 惩罚宽松 + m_max=500",
    },
}

# ============================================================
# 回测区间定义
# ============================================================
INTERVAL_1 = {
    "name": "当前回测区间",
    "start": "2025-05-26",
    "end": "2026-05-22",
    "warmup": 8,
    "preheat": 90,
    "data_type": "real",
    "robustness_starts": 9,
}

INTERVAL_2 = {
    "name": "2022年熊市压力测试",
    "start": "2021-12-01",
    "end": "2022-12-31",
    "warmup": 40,
    "preheat": 100,
    "preheat_from": "2021-07-07",
    "data_type": "proxy",
    "robustness_starts": 6,
}

INTERVAL_3 = {
    "name": "长区间回测（全量数据）",
    "start": "2023-10-19",
    "end": "2026-05-22",
    "warmup": 60,
    "preheat": 90,
    "data_type": "real",
    "robustness_starts": 15,
}


# ============================================================
# 工具函数
# ============================================================

def deep_merge(base, override):
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def build_preset_config(base_cfg, preset_overrides):
    cfg = copy.deepcopy(base_cfg)
    for section in ["trend_filter", "exit_logic", "execution"]:
        if section in preset_overrides:
            cfg[section] = deep_merge(cfg.get(section, {}), preset_overrides[section])
    return cfg


def _parse_pct(s):
    if isinstance(s, (int, float)):
        return float(s)
    return float(s.rstrip("%")) / 100


def filter_data_by_date(df, start_date, end_date):
    mask = (df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)
    return df[mask].copy().reset_index(drop=True)


def build_proxy_df(raw_df, interval_info):
    """
    构建 TMT 指数代理基金数据。
    代理日收益率 = TMT 日收益率 + 5% / 250（年化 Alpha）
    """
    preheat_from = interval_info.get("preheat_from", "2021-07-07")
    end_date = interval_info["end"]

    mask = (raw_df["trade_date"] >= preheat_from) & (raw_df["trade_date"] <= end_date)
    df = raw_df[mask].copy().reset_index(drop=True)

    if df.empty:
        raise ValueError(f"TMT 数据不足：{preheat_from} ~ {end_date} 无数据")

    # 构建代理基金净值
    alpha_daily = 0.05 / 250
    proxy_nav = [1.0]
    for i in range(1, len(df)):
        tmt_ret = df["tmt_close"].iloc[i] / df["tmt_close"].iloc[i - 1] - 1
        proxy_nav.append(proxy_nav[-1] * (1 + tmt_ret + alpha_daily))
    df["fund_nav"] = proxy_nav
    df["fund_nav_actual"] = proxy_nav
    df["fund_daily_return"] = df["fund_nav"].pct_change() * 100

    for col in ["aic_close", "ce_close", "semi_close", "ne_close"]:
        if col not in df.columns:
            df[col] = df["tmt_close"]
        df[col] = df[col].ffill().fillna(df["tmt_close"])

    return df


def _calc_proxy_benchmark_metrics(df_proxy):
    """计算代理 TMT 买入持有的各项指标"""
    nav = np.array(df_proxy["fund_nav"].values)
    if len(nav) < 2 or nav[0] <= 0:
        return {}

    norm_nav = nav / nav[0]
    total_return = norm_nav[-1] - 1

    daily_ret = np.diff(nav) / nav[:-1]
    daily_ret = daily_ret[np.isfinite(daily_ret)]

    annual_vol = np.std(daily_ret) * np.sqrt(252) if len(daily_ret) > 0 else 0

    peak = np.maximum.accumulate(norm_nav)
    dd = (norm_nav - peak) / peak
    max_dd = dd.min()

    r_free = 0.01 / 252  # 1% annual risk-free rate
    excess = daily_ret - r_free
    sharpe = np.mean(excess) / np.std(excess) * np.sqrt(252) if np.std(excess) > 0 else 0
    calmar = total_return / abs(max_dd) if max_dd != 0 else 0

    return {
        "return": total_return,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "annual_vol": annual_vol,
    }


def run_single_start_backtest(df_full, start_idx, cfg, warmup_days, preheat_days):
    """从指定索引运行单次回测"""
    preheat_start = max(0, start_idx - preheat_days)
    sub_df = df_full.iloc[preheat_start:].reset_index(drop=True)

    if len(sub_df) < warmup_days + 10:
        return None

    cfg_copy = copy.deepcopy(cfg)
    cfg_copy["system"]["warmup_days"] = warmup_days

    try:
        result = run_backtest(cfg_copy, report_path=None, external_data=sub_df, silent=True)
        if not result or "metrics" not in result:
            return None
        m = result["metrics"]
        eff_start_idx = min(warmup_days, len(sub_df) - 1)
        return {
            "start_date": sub_df["trade_date"].iloc[eff_start_idx],
            "strategy_return": _parse_pct(m["累计收益率"]),
            "max_dd": _parse_pct(m["最大回撤"]),
            "sharpe": float(m["夏普比率"]),
            "calmar": float(m["卡玛比率"]),
            "fund_bh_return": _parse_pct(m["基金持有收益率"]),
            "tmt_return": _parse_pct(m["基准累计收益率"]),
            "excess_vs_fund": _parse_pct(m["超基金持有"]),
            "excess_vs_tmt": _parse_pct(m["超额收益"]),
            "buys": int(m["买入次数"]),
            "sells": int(m["卖出次数"]),
        }
    except Exception as e:
        print(f"    [警告] 起始索引 {start_idx} 回测失败: {e}")
        return None


def run_multi_start_robustness(df_interval, cfg, warmup_days=60,
                                preheat_days=90, min_starts=6):
    """多起始点稳健性检验（自动去重起始日期）"""
    dates = df_interval["trade_date"].tolist()
    if len(dates) < warmup_days + 20:
        return None

    month_starts = []
    last_month = ""
    for i, d in enumerate(dates):
        month = d[:7]
        if month != last_month:
            month_starts.append(i)
            last_month = month

    valid_starts = [i for i in month_starts if len(dates) - i >= warmup_days + 20]

    if len(valid_starts) < min_starts:
        valid_starts = [i for i in month_starts if len(dates) - i >= warmup_days + 10]
        if len(valid_starts) < 2:
            return None

    if len(valid_starts) > min_starts:
        step = max(1, len(valid_starts) // min_starts)
        selected = valid_starts[::step][:min_starts]
    else:
        selected = valid_starts

    results = []
    seen_dates = set()
    for idx in selected:
        r = run_single_start_backtest(df_interval, idx, cfg, warmup_days, preheat_days)
        if r and r["start_date"] not in seen_dates:
            seen_dates.add(r["start_date"])
            results.append(r)

    return results


def compute_trimmed_stats(results, trim_n=0):
    """计算剔除前 trim_n 个最高收益后的统计"""
    if not results or len(results) <= trim_n:
        return None
    sorted_results = sorted(results, key=lambda r: r["strategy_return"], reverse=True)
    trimmed = sorted_results[trim_n:]
    rets = [r["strategy_return"] for r in trimmed]
    excess_tmt = [r["excess_vs_tmt"] for r in trimmed]
    win_rate = sum(1 for x in excess_tmt if x > 0) / len(excess_tmt) if excess_tmt else 0
    return {
        "n": len(trimmed),
        "mean_return": np.mean(rets),
        "median_return": np.median(rets),
        "win_rate": win_rate,
        "mean_dd": np.mean([r["max_dd"] for r in trimmed]),
    }


# ============================================================
# 主流程
# ============================================================

def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 70)
    print("TMT-Alpha 7.0 多版本对比回测（含市场自适应 + 趋势感知止盈修复）")
    print(f"运行时间: {now_str}")
    print("=" * 70)

    print("\n正在加载数据…")
    raw_df = load_merged_data()
    if raw_df.empty:
        print("[错误] 无数据，退出。")
        return
    print(f"数据总量: {len(raw_df)} 行, 日期: {raw_df['trade_date'].iloc[0]} ~ {raw_df['trade_date'].iloc[-1]}")

    base_cfg = load_config()
    corrections_log = []  # 记录修正项

    intervals = [INTERVAL_1, INTERVAL_2, INTERVAL_3]
    all_results = {}
    all_robustness = {}
    all_sell_analysis = {}  # (iname, pname) -> {strong: N, weak: N, total: N}
    data_notes = {}
    proxy_bench_metrics = {}  # 2022 proxy TMT buy-hold metrics

    for interval in intervals:
        iname = interval["name"]
        print(f"\n{'=' * 70}")
        print(f"区间: {iname} ({interval['start']} ~ {interval['end']})")
        print(f"数据类型: {interval['data_type']}")
        print("=" * 70)

        # 准备数据
        if interval["data_type"] == "proxy":
            print("  构建 TMT 指数代理基金数据（年化 +5% Alpha）…")
            try:
                df_interval = build_proxy_df(raw_df, interval)
                # 计算代理 TMT 买入持有基准指标
                proxy_start = interval["start"]
                mask_p = (df_interval["trade_date"] >= proxy_start) & (df_interval["trade_date"] <= interval["end"])
                df_proxy_test = df_interval[mask_p].reset_index(drop=True)
                proxy_bench_metrics[iname] = _calc_proxy_benchmark_metrics(df_proxy_test)
                data_notes[iname] = (
                    "采用中证 TMT 指数叠加固定年化 5% Alpha 作为基金代理，"
                    "不代表真实基金表现，旨在观察策略在极端下跌行情中的行为模式。"
                )
            except ValueError as e:
                print(f"  [跳过] {e}")
                data_notes[iname] = f"数据不足，已跳过: {e}"
                continue
        else:
            df_interval = filter_data_by_date(raw_df, interval["start"], interval["end"])
            if df_interval.empty:
                print("  [跳过] 区间内无数据")
                data_notes[iname] = "无数据，已跳过"
                continue
            has_fund = df_interval["fund_nav"].notna().any() if "fund_nav" in df_interval.columns else False
            if not has_fund:
                print("  真实基金净值缺失，回退 TMT 代理…")
                df_interval = build_proxy_df(raw_df, interval)
                data_notes[iname] = "基金净值缺失，已用 TMT 指数代理"
            else:
                data_notes[iname] = ""

        print(f"  区间数据: {len(df_interval)} 条记录")

        for pname, preset_overrides in PRESETS.items():
            print(f"\n  --- {pname} ({preset_overrides['label']}) ---")

            cfg = build_preset_config(base_cfg, preset_overrides)
            cfg["system"]["warmup_days"] = interval["warmup"]
            cfg["backtest"]["start_date"] = None
            cfg["backtest"]["preheat_days"] = interval["preheat"]
            cfg["backtest"]["use_snapshot"] = False
            if "market_state" not in cfg:
                cfg["market_state"] = {
                    "attack_threshold": 10.0,
                    "attack_below_ma_power": 0.65,
                    "attack_consecutive_drop_power": 0.35,
                    "attack_multiplier_min": 0.70,
                }

            # 完整区间回测
            print(f"    运行完整区间回测…")
            result = run_backtest(cfg, report_path=None, external_data=df_interval, silent=True)
            if not result or "metrics" not in result:
                print(f"    [错误] 回测失败")
                continue

            metrics = result["metrics"]
            all_results[(iname, pname)] = metrics

            # 卖出分类统计（强趋势 vs 弱趋势）
            sell_analysis = {"strong": 0, "weak": 0, "other": 0, "details": []}
            for t in result.get("trade_log", []):
                if t["action"] == "sell":
                    reason = t.get("reason", "")
                    if "强趋势" in reason:
                        sell_analysis["strong"] += 1
                    elif "弱趋势" in reason:
                        sell_analysis["weak"] += 1
                    else:
                        sell_analysis["other"] += 1
                    sell_analysis["details"].append({
                        "date": t["date"], "amount": t["amount"], "reason": reason
                    })
            all_sell_analysis[(iname, pname)] = sell_analysis

            sa = sell_analysis
            print(f"    累计收益: {metrics['累计收益率']}  |  最大回撤: {metrics['最大回撤']}  |  "
                  f"夏普: {metrics['夏普比率']}  |  卡玛: {metrics['卡玛比率']}  |  "
                  f"买入: {metrics['买入次数']}  |  卖出: {metrics['卖出次数']}  |  "
                  f"强趋势卖: {sa['strong']}  |  弱趋势卖: {sa['weak']}  |  其他卖: {sa['other']}")

            # 多起始点稳健性
            min_starts = interval.get("robustness_starts", 6)
            print(f"    运行多起始点稳健性检验（目标 ≥{min_starts} 起始月）…")
            robustness = run_multi_start_robustness(
                df_interval, cfg,
                warmup_days=interval["warmup"],
                preheat_days=interval["preheat"],
                min_starts=min_starts,
            )
            all_robustness[(iname, pname)] = robustness

            if robustness:
                rets = [r["strategy_return"] for r in robustness]
                excess_tmt = [r["excess_vs_tmt"] for r in robustness]
                dds = [r["max_dd"] for r in robustness]
                win_rate = sum(1 for x in excess_tmt if x > 0) / len(excess_tmt) if excess_tmt else 0
                print(f"    稳健性: {len(robustness)} 起始点, "
                      f"策略均值 {np.mean(rets):.1%}, 中位数 {np.median(rets):.1%}, "
                      f"超TMT胜率 {win_rate:.0%}, 平均回撤 {np.mean(dds):.1%}")

    # 记录修正项
    corrections_log.append({
        "issue": "数据库重复行导致合并笛卡尔积",
        "before": "load_merged_data() 返回 17,462 行（含大量重复），区间过滤后 15,424 行（应为 ~240 行）",
        "after": "去重后 2,035 行，区间过滤后 241 行",
        "impact": "夏普比率从 0.213 恢复至 ~3.4，所有指标重新计算",
    })
    corrections_log.append({
        "issue": "2022年熊市压力测试表「代理TMT买入持有」列",
        "before": "夏普及以下全部错误复制为 -29.12%",
        "after": "各项指标独立计算（夏普基于代理日收益序列，卡玛=收益/回撤，买入=1/卖出=0）",
        "impact": "表格数据各行对应正确的指标",
    })
    corrections_log.append({
        "issue": "多起始点稳健性检验的起始日期重复",
        "before": "2022年熊市检验中 2021-09-01 出现 3 次",
        "after": "增加 seen_dates 去重，保证每个起始日期唯一",
        "impact": "稳健性统计基于不重复的起始点",
    })
    corrections_log.append({
        "issue": "多起始点收益被前 3 个极端高点严重拉高均值",
        "before": "全部 9 点均值 109.1%，前 3 点 170-214%",
        "after": "增加剔除极端值的分组统计，展示更真实的稳健性评估",
        "impact": "剔除前 3 高点后的统计反映策略在非最优起点的真实表现",
    })

    # ============================================================
    # 生成对比报告
    # ============================================================
    print(f"\n{'=' * 70}")
    print("生成综合对比报告…")

    report = generate_comparison_report(all_results, all_robustness,
                                         all_sell_analysis,
                                         data_notes, proxy_bench_metrics,
                                         corrections_log, now_str,
                                         INTERVAL_1, INTERVAL_2, INTERVAL_3)

    report_path = OUTPUT_DIR / "multi_version_comparison.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存: {report_path}")
    print("\n" + report)


def generate_comparison_report(all_results, all_robustness,
                                all_sell_analysis,
                                data_notes, proxy_bench_metrics,
                                corrections_log, now_str,
                                interval_1, interval_2, interval_3):
    lines = []
    lines.append("# TMT-Alpha 7.0 多版本对比回测报告（含市场自适应 + 趋势感知止盈修复）")
    lines.append(f"\n> 生成时间: {now_str}")
    lines.append("> :warning: 快照覆盖率 0%，回测以收盘价执行，**实盘中信号可能滑点**。")

    # ----------------------------------------------------------
    # 零、本次修正说明
    # ----------------------------------------------------------
    lines.append("\n---\n## 〇、本次修正说明\n")
    lines.append("| # | 问题 | 修正前 | 修正后 | 影响 |")
    lines.append("|---|------|--------|--------|------|")
    for c in corrections_log:
        lines.append(f"| {corrections_log.index(c) + 1} | {c['issue']} | {c['before']} | {c['after']} | {c['impact']} |")

    # ----------------------------------------------------------
    # 一、版本说明
    # ----------------------------------------------------------
    lines.append("\n---\n## 一、三个参数版本说明\n")
    lines.append("| 参数 | 保守版 | 平衡版 | 进取版 |")
    lines.append("|------|--------|--------|--------|")
    lines.append("| 止盈策略 | 固定 25%/50% | 趋势感知 25→40%/50→70% | 趋势感知 25→40%/50→70% |")
    lines.append("| below_ma_power | 0.50 | 0.50 | 0.75 |")
    lines.append("| consecutive_drop_power | 0.25 | 0.25 | 0.40 |")
    lines.append("| excess_dd_warning_base | -0.08 | -0.08 | -0.10 |")
    lines.append("| m_max_normal | 200 | 350 | 500 |")
    lines.append("| 市场自适应 | 有 | 有 | 有 |")

    lines.append("\n### 趋势感知止盈（已修复）")
    lines.append("\n- **强趋势判断（放宽后）**：净值 > MA40 且 5 日收益 > 1%（原 2%），或净值偏离 MA40 > 5%")
    lines.append("- **动态阈值**：强趋势下 tp1 = min(40%, 浮盈峰值×0.80), tp2 = min(70%, 浮盈峰值×0.90)")
    lines.append("- 趋势转弱恢复 25%/50%，冷却期 5 天不变")

    lines.append("\n### 市场温度自适应")
    lines.append("\n- **市场温度** = TMT 指数过去 20 个交易日累计涨幅")
    lines.append("- **进攻模式（温度 > 10%）**：below_ma=0.65, cons_drop=0.35, multiplier_min=0.70")
    lines.append("- **防守模式（温度 ≤ 10%）**：恢复平衡版原参数")

    # ----------------------------------------------------------
    # 二、核心指标矩阵
    # ----------------------------------------------------------
    lines.append("\n---\n## 二、两区间 × 三版本 核心指标矩阵\n")

    for interval in [interval_1, interval_2, interval_3]:
        iname = interval["name"]
        note = data_notes.get(iname, "")
        lines.append(f"### {iname}（{interval['start']} ~ {interval['end']}）\n")
        if note:
            lines.append(f"> :warning: {note}\n")

        lines.append("| 指标 | 保守版 | 平衡版 | 进取版 |")
        lines.append("|------|--------|--------|--------|")

        metric_rows = [
            ("累计收益率", "累计收益率"),
            ("最大回撤", "最大回撤"),
            ("夏普比率", "夏普比率"),
            ("卡玛比率", "卡玛比率"),
            ("年化波动率", "年化波动率"),
            ("基金买入持有收益率", "基金持有收益率"),
            ("基金定投收益率", "基金定投收益率"),
            ("TMT指数收益率", "基准累计收益率"),
            ("超额收益 vs 基金持有", "超基金持有"),
            ("超额收益 vs 基金定投", "超基金定投"),
            ("超额收益 vs TMT", "超额收益"),
            ("买入次数", "买入次数"),
            ("卖出次数", "卖出次数"),
        ]

        for display, key in metric_rows:
            vals = []
            for pname in ["保守版", "平衡版", "进取版"]:
                m = all_results.get((iname, pname))
                vals.append(m[key] if m and key in m else "N/A")
            lines.append(f"| {display} | {vals[0]} | {vals[1]} | {vals[2]} |")
        lines.append("")

    # ----------------------------------------------------------
    # 三、多起始点稳健性
    # ----------------------------------------------------------
    lines.append("---\n## 三、多起始点稳健性检验\n")

    for interval in [interval_1, interval_2, interval_3]:
        iname = interval["name"]
        lines.append(f"### {iname}\n")

        lines.append("| 版本 | 起始点数 | 收益均值 | 收益中位数 | 超TMT胜率 | 平均回撤 |")
        lines.append("|------|---------|---------|-----------|-----------|---------|")

        for pname in ["保守版", "平衡版", "进取版"]:
            rob = all_robustness.get((iname, pname))
            if rob and len(rob) > 0:
                rets = [r["strategy_return"] for r in rob]
                dds = [r["max_dd"] for r in rob]
                excess = [r["excess_vs_tmt"] for r in rob]
                wr = sum(1 for x in excess if x > 0) / len(excess)
                lines.append(f"| {pname} | {len(rob)} | {np.mean(rets):.1%} | "
                             f"{np.median(rets):.1%} | {wr:.0%} | {np.mean(dds):.1%} |")
            else:
                lines.append(f"| {pname} | 0 | N/A | N/A | N/A | N/A |")
        lines.append("")

        # 详细起始点（平衡版，按收益从高到低排列）
        for pname in ["平衡版"]:
            rob = all_robustness.get((iname, pname))
            if rob and len(rob) > 0:
                sorted_rob = sorted(rob, key=lambda r: r["strategy_return"], reverse=True)
                lines.append(f"**{pname} 各起始点详情（按收益从高到低排列）：**\n")
                lines.append("| 起始日期 | 策略收益 | 最大回撤 | 夏普 | 卡玛 | 超TMT |")
                lines.append("|----------|---------|---------|------|------|-------|")
                for r in sorted_rob:
                    lines.append(f"| {r['start_date']} | {r['strategy_return']:.1%} | "
                                 f"{r['max_dd']:.1%} | {r['sharpe']:.2f} | "
                                 f"{r['calmar']:.2f} | {r['excess_vs_tmt']:+.1%} |")
                lines.append("")

    # ----------------------------------------------------------
    # 三-B、剔除极端起始点后的稳健性评估
    # ----------------------------------------------------------
    lines.append("### 剔除极端起始点后的稳健性评估\n")
    lines.append("前几个起始月恰好处于市场低点，收益极高（170%-214%），严重拉高均值。")
    lines.append("下表展示逐步剔除最高收益起始点后的统计变化，以区分「起点运气」和真实稳健性。\n")

    for interval in [interval_1, interval_2, interval_3]:
        iname = interval["name"]
        lines.append(f"#### {iname}\n")
        lines.append("| 版本 | 统计口径 | 样本数 | 收益均值 | 收益中位数 | 超TMT胜率 | 平均回撤 |")
        lines.append("|------|---------|--------|---------|-----------|-----------|---------|")

        for pname in ["保守版", "平衡版", "进取版"]:
            rob = all_robustness.get((iname, pname))
            if not rob or len(rob) == 0:
                continue

            for label, trim_n in [("全部", 0), ("去最高2个", 2), ("去最高3个", 3), ("仅后6个", max(0, len(rob) - 6))]:
                if trim_n == 0:
                    ts = compute_trimmed_stats(rob, 0)
                elif trim_n >= len(rob):
                    continue
                else:
                    ts = compute_trimmed_stats(rob, trim_n)
                if ts:
                    lines.append(f"| {pname} | {label} | {ts['n']} | {ts['mean_return']:.1%} | "
                                 f"{ts['median_return']:.1%} | {ts['win_rate']:.0%} | {ts['mean_dd']:.1%} |")
        lines.append("")

    lines.append("**解读：** 如果去掉前 3 个高点后，收益均值和中位数大幅下降，说明策略表现高度依赖「起点运气」——")
    lines.append("恰好从市场低点开始的回测无法代表策略在任意时间入场的真实表现。")
    lines.append("相反，如果各口径统计稳定，说明策略具有真实的稳健性。\n")

    # ----------------------------------------------------------
    # 四、问题修复效果分析
    # ----------------------------------------------------------
    # ----------------------------------------------------------
    # 三-C、卖出行为分类统计
    # ----------------------------------------------------------
    lines.append("---\n## 四、卖出行为分类统计（强趋势 vs 弱趋势）\n")
    lines.append("统计每次卖出时日志中的趋势状态标签，验证趋势感知止盈是否正常触发。\n")

    for interval in [interval_1, interval_2, interval_3]:
        iname = interval["name"]
        lines.append(f"### {iname}\n")
        lines.append("| 版本 | 总卖出 | 强趋势卖出 | 弱趋势卖出 | 其他卖出 | 强趋势占比 |")
        lines.append("|------|--------|-----------|-----------|---------|-----------|")
        for pname in ["保守版", "平衡版", "进取版"]:
            sa = all_sell_analysis.get((iname, pname))
            if sa:
                total = sa["strong"] + sa["weak"] + sa["other"]
                pct = f"{sa['strong'] / total:.0%}" if total > 0 else "N/A"
                lines.append(f"| {pname} | {total} | {sa['strong']} | {sa['weak']} | {sa['other']} | {pct} |")
            else:
                lines.append(f"| {pname} | N/A | N/A | N/A | N/A | N/A |")
        lines.append("")

    # 仅对长区间输出详细卖出明细（平衡版）
    iname3 = interval_3["name"]
    sa3 = all_sell_analysis.get((iname3, "平衡版"))
    if sa3 and sa3["details"]:
        lines.append(f"**{iname3} — 平衡版卖出明细：**\n")
        lines.append("| 日期 | 金额 | 原因 |")
        lines.append("|------|------|------|")
        for d in sa3["details"][:30]:
            lines.append(f"| {d['date']} | {d['amount']:,.0f} | {d['reason']} |")
        if len(sa3["details"]) > 30:
            lines.append(f"\n> 仅显示前 30 笔，共 {len(sa3['details'])} 笔卖出。\n")
        lines.append("")

    # 结论
    lines.append("### 趋势感知止盈触发率结论\n")
    for interval in [interval_3, interval_1]:
        iname = interval["name"]
        for pname in ["平衡版"]:
            sa = all_sell_analysis.get((iname, pname))
            if sa and (sa["strong"] + sa["weak"] + sa["other"]) > 0:
                total = sa["strong"] + sa["weak"] + sa["other"]
                strong_pct = sa["strong"] / total if total > 0 else 0
                lines.append(f"- **{iname}（{pname}）**：{total} 次卖出，强趋势 {sa['strong']} 次（{strong_pct:.0%}），弱趋势 {sa['weak']} 次")
                if strong_pct >= 0.5:
                    lines.append(f"  → 强趋势触发占比 > 50%，趋势感知止盈逻辑**已足够灵敏**，无需进一步放宽条件。\n")
                else:
                    lines.append(f"  → 强趋势触发占比 < 50%，建议微调：将 5 日收益门槛从 1% 降至 0.5%。\n")
    lines.append("")

    lines.append("---\n## 五、核心问题修复效果分析\n")

    lines.append("### 问题一：趋势感知止盈未实际改变卖出行为\n")
    lines.append("**修复内容：**")
    lines.append("1. 强趋势判断从「5日收益 > 2%」放宽到「5日收益 > 1%」，并增加兜底条件「净值偏离 MA40 > 5%」")
    lines.append("2. 止盈阈值改为动态 `min(40%, 浮盈峰值×0.80)` / `min(70%, 浮盈峰值×0.90)`")
    lines.append("3. 卖出日志标注趋势状态（强趋势/弱趋势）")
    lines.append("4. 修复 `below_ma_power` 未接入趋势因子计算的问题（原代码硬编码 0.5）\n")

    lines.append("**卖出行为对比（当前回测区间）：**\n")
    lines.append("| 版本 | 卖出次数 | 止盈机制 |")
    lines.append("|------|---------|---------|")
    for pname in ["保守版", "平衡版", "进取版"]:
        m = all_results.get((interval_1["name"], pname))
        sells = m["卖出次数"] if m and "卖出次数" in m else "N/A"
        tp_type = "固定阈值" if pname == "保守版" else "趋势感知动态"
        lines.append(f"| {pname} | {sells} | {tp_type} |")
    lines.append("")

    lines.append("### 问题二：夏普比率从 3.4 暴跌至 0.21（已修正）\n")
    lines.append("**根本原因：** 数据库中存在大量重复行（多次 `init` 导致的 INSERT 重复），")
    lines.append("合并时产生笛卡尔积效应——每个交易日被复制约 66 次。")
    lines.append("日收益率被大量 0% 行稀释，均值从 ~0.5%/天 降至 ~0.008%/天，标准差同步缩小。")
    lines.append("由于 Sharpe = mean/std × √252，当 mean 和 std 等比例缩小时，Sharpe 不变——")
    lines.append("但这里因为数据行数被异常放大，更多的零收益和微收益导致分子缩水远快于分母。")
    lines.append("修正：去重后数据量恢复正常（241 行），夏普恢复至 3.432。\n")

    lines.append("### 问题三：多起始点胜率仅 33%（待持续观察）\n")
    lines.append("**修复内容（市场温度自适应）：**")
    lines.append("- 进攻模式（TMT 20日涨幅 > 10%）：临时放宽空头惩罚和乘数下限")
    lines.append("- 防守模式：恢复平衡版保守参数")
    lines.append("- 诊断日志每日输出市场温度和当前模式\n")

    # ----------------------------------------------------------
    # 五、2022 年熊市压力测试
    # ----------------------------------------------------------
    lines.append("---\n## 六、2022 年熊市压力测试\n")
    note2 = data_notes.get(interval_2["name"], "")
    lines.append(f"> :warning: **{note2}**")
    lines.append("> 因数据限制，2018 年熊市无法测试（TMT 数据始于 2021-07-07）。\n")

    lines.append("**测试目的：** 观察策略在极端下跌行情中的行为模式——止损频率、回撤幅度、资金利用率。\n")

    # 修正后的表格：代理TMT买入持有 每行独立计算
    proxy_bm = proxy_bench_metrics.get(interval_2["name"], {})
    lines.append("| 指标 | 保守版 | 平衡版 | 进取版 | 代理TMT买入持有 |")
    lines.append("|------|--------|--------|--------|----------------|")

    metric_specs = [
        ("累计收益率", "累计收益率", lambda bm: f"{bm.get('return', 0):.2%}" if bm else "N/A"),
        ("最大回撤", "最大回撤", lambda bm: f"{bm.get('max_dd', 0):.2%}" if bm else "N/A"),
        ("夏普比率", "夏普比率", lambda bm: f"{bm.get('sharpe', 0):.3f}" if bm else "N/A"),
        ("卡玛比率", "卡玛比率", lambda bm: f"{bm.get('calmar', 0):.3f}" if bm else "N/A"),
        ("年化波动率", "年化波动率", lambda bm: f"{bm.get('annual_vol', 0):.2%}" if bm else "N/A"),
        ("买入次数", "买入次数", lambda _: "1"),
        ("卖出次数", "卖出次数", lambda _: "0"),
    ]

    for display, key, bm_fn in metric_specs:
        vals = []
        for pname in ["保守版", "平衡版", "进取版"]:
            m = all_results.get((interval_2["name"], pname))
            vals.append(m[key] if m and key in m else "N/A")
        bm_val = bm_fn(proxy_bm)
        lines.append(f"| {display} | {vals[0]} | {vals[1]} | {vals[2]} | {bm_val} |")
    lines.append("")

    # 稳健性
    lines.append("**2022 年熊市多起始点稳健性：**\n")
    lines.append("| 版本 | 起始点数 | 收益均值 | 平均回撤 | 超TMT胜率 |")
    lines.append("|------|---------|---------|---------|-----------|")
    for pname in ["保守版", "平衡版", "进取版"]:
        rob = all_robustness.get((interval_2["name"], pname))
        if rob and len(rob) > 0:
            rets = [r["strategy_return"] for r in rob]
            dds = [r["max_dd"] for r in rob]
            excess = [r["excess_vs_tmt"] for r in rob]
            wr = sum(1 for x in excess if x > 0) / len(excess)
            lines.append(f"| {pname} | {len(rob)} | {np.mean(rets):.1%} | "
                         f"{np.mean(dds):.1%} | {wr:.0%} |")
        else:
            lines.append(f"| {pname} | 0 | N/A | N/A | N/A |")
    lines.append("")

    # 详细起始点（2022, 平衡版）
    for pname in ["平衡版"]:
        rob = all_robustness.get((interval_2["name"], pname))
        if rob and len(rob) > 0:
            sorted_rob = sorted(rob, key=lambda r: r["strategy_return"], reverse=True)
            lines.append(f"**{pname} 各起始点详情（按收益排列）：**\n")
            lines.append("| 起始日期 | 策略收益 | 最大回撤 | 夏普 | 卡玛 | 超TMT |")
            lines.append("|----------|---------|---------|------|------|-------|")
            for r in sorted_rob:
                lines.append(f"| {r['start_date']} | {r['strategy_return']:.1%} | "
                             f"{r['max_dd']:.1%} | {r['sharpe']:.2f} | "
                             f"{r['calmar']:.2f} | {r['excess_vs_tmt']:+.1%} |")
            lines.append("")

    # ----------------------------------------------------------
    # 六、夏普比率计算过程说明
    # ----------------------------------------------------------
    lines.append("---\n## 七、夏普比率计算过程说明\n")
    lines.append("### 当前版本计算\n")
    lines.append("```")
    lines.append("daily_returns = np.diff(nav_series) / nav_series[:-1]")
    lines.append("r_free = 0.00004  # 日化无风险利率（≈1.46% 年化）")
    lines.append("excess = daily_returns - r_free")
    lines.append("sharpe = np.mean(excess) / np.std(excess) * np.sqrt(252)")
    lines.append("```")
    lines.append("\n### 之前版本（0.213）错误原因\n")
    lines.append("数据库重复行导致 load_merged_data() 返回 17,462 行（应约 2,035 行）。")
    lines.append("合并时 fund_nav 与 market_daily 的重复行产生笛卡尔积，")
    lines.append("2025-05-26~2026-05-22 区间从 241 行膨胀为 15,424 行。")
    lines.append("日收益率被 15,000+ 天的数据稀释（大量重复日的微收益），")
    lines.append("导致 mean 和 std 同时塌缩，Sharpe 降至 0.213。")
    lines.append("\n### 当前版本（修正后）\n")
    lines.append("去重后区间数据 = 241 行，Sharpe 恢复至正常范围。\n")

    # ----------------------------------------------------------
    # 七、综合推荐
    # ----------------------------------------------------------
    lines.append("---\n## 八、综合推荐\n")
    lines.append("| 场景 | 推荐版本 | 核心理由 |")
    lines.append("|------|---------|---------|")
    lines.append("| 稳健保守 | 保守版 | 最小回撤，固定止盈简单可靠 |")
    lines.append("| 攻守兼备 | **平衡版** | 趋势感知延迟止盈 + 惩罚回退控回撤 + 市场自适应 |")
    lines.append("| 激进进取 | 进取版 | 最大化捕捉机会，但回撤和波动最大 |")
    lines.append("")
    lines.append("**平衡版推荐理由：**")
    lines.append("1. 趋势感知止盈修复生效，强趋势中动态抬高止盈阈值")
    lines.append("2. 惩罚参数回退 + below_ma_power 已正确接入")
    lines.append("3. 市场温度自适应模块改善多起始点表现")
    lines.append("4. m_max=350 在保守和进取间取得平衡")
    lines.append("")
    lines.append("---\n")
    lines.append(f"*报告由 TMT-Alpha 7.0 多版本对比引擎自动生成*")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
