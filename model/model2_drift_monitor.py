"""
drift_monitor.py
模块一（续）：Alpha 护航型漂移雷达 (Directional Drift Monitor)
- 单日超额 Alpha_daily
- 累计超额 Cum_Alpha_20d
- 绝对净值回撤 Fund_DD_20d
- Action_Ratio 判定树（含 3 日防锯齿冷却锁）
"""

import numpy as np
import pandas as pd


class CooldownState:
    """防锯齿冷却锁状态"""

    def __init__(self):
        self.cooldown_remaining = 0  # 剩余冷却天数
        self.last_action_ratio = 1.0  # 上一次的 Action_Ratio

    def tick(self):
        """每个交易日结束后调用，冷却计数器减一"""
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1

    def lock(self, days: int, ratio: float):
        """进入冷却期"""
        self.cooldown_remaining = days
        self.last_action_ratio = ratio

    @property
    def is_cooling(self) -> bool:
        return self.cooldown_remaining > 0


def compute_correlation_20d(df: pd.DataFrame, t: int, lookback: int = 20) -> float:
    """
    计算基金收益率与 Mkt_Chg 的 20 日滚动相关系数。
    需要 df 中有 R_fund 和 Mkt_Chg 列。
    """
    start = max(0, t - lookback + 1)
    fund_r = df["R_fund"].iloc[start:t + 1].dropna()
    mkt_r = df["Mkt_Chg"].iloc[start:t + 1].dropna()

    # 取交集索引
    common_idx = fund_r.index.intersection(mkt_r.index)
    if len(common_idx) < 5:
        return 0.5  # 数据不足时返回中性值

    corr = fund_r.loc[common_idx].corr(mkt_r.loc[common_idx])
    return corr if not np.isnan(corr) else 0.5


def compute_mae_20d(df: pd.DataFrame, t: int, lookback: int = 20) -> float:
    """
    计算近 20 日平均绝对偏差 MAE_20d。
    MAE_i = |R_fund(i) - Mkt_Chg(i)|
    """
    start = max(0, t - lookback + 1)
    fund_r = df["R_fund"].iloc[start:t + 1]
    mkt_r = df["Mkt_Chg"].iloc[start:t + 1]
    mae = (fund_r - mkt_r).abs().mean()
    return mae if not np.isnan(mae) else 0.0


def compute_mae_avg_60d(df: pd.DataFrame, t: int, period: int = 60) -> float:
    """
    扩展窗口均值 MAE_avg_60d。
    从第 0 天到第 t 天（最多 60 天）的 MAE 均值。
    """
    start = max(0, t - period + 1)
    fund_r = df["R_fund"].iloc[start:t + 1]
    mkt_r = df["Mkt_Chg"].iloc[start:t + 1]
    mae = (fund_r - mkt_r).abs().mean()
    return mae if not np.isnan(mae) else 0.0


def compute_action_ratio(df: pd.DataFrame, t: int, cfg: dict,
                         cooldown: CooldownState,
                         r_fund_override: float = None,
                         cum_alpha_override: float = None) -> tuple:
    """
    Action_Ratio 判定树（核心决策逻辑）。

    返回: (action_ratio: float, cooldown: CooldownState)

    判定树层级：
    第一层 - Alpha 豁免 + 绝对亏损防锯齿（冷却锁）
    第二层 - 负向偏离惩罚
    第三层 - 系统失效阻断

    r_fund_override / cum_alpha_override: 快照回测时，14:45 基金净值尚未公布，
    使用 snapshot.fund_nav_estimated 推算的估算值，避免回测引入未来函数。
    """
    dm = cfg.get("drift_monitor", {})

    if not dm.get("enable", True):
        return 1.0, cooldown

    lookback = dm.get("lookback_days", 20)
    corr_thresh = dm.get("corr_threshold", 0.70)
    te_mult = dm.get("tracking_error_multiplier", 1.5)
    mae_long = dm.get("mae_long_period", 60)
    alert_days = dm.get("alert_consecutive_days", 3)
    reduce_ratio = dm.get("action_reduce_ratio", 0.3)
    reboot_ratio = dm.get("reboot_reduce_ratio", 0.6)
    block_thresh = dm.get("action_block_threshold", 0.50)

    # 读取当前行的预计算指标（优先使用 _live 列，14:45 可观测数据未经 shift）
    cum_alpha = cum_alpha_override if cum_alpha_override is not None else df.get("Cum_Alpha_20d_live", df["Cum_Alpha_20d"]).iloc[t]
    fund_dd_20d = df.get("Fund_DD_20d_live", df["Fund_DD_20d"]).iloc[t]
    r_fund = r_fund_override if r_fund_override is not None else df.get("R_fund_live", df["R_fund"]).iloc[t]

    # 冷却期内直接返回锁定值
    if cooldown.is_cooling:
        ratio = cooldown.last_action_ratio
        cooldown.tick()
        return ratio, cooldown

    # === 第一层：Alpha 豁免与分段式绝对亏损防锯齿 ===
    # 优先级: P0 单日暴跌 > P1 系统性风险(0.5) > P2 黄金坑(1.3) > P3 常规防守(0.8)
    if dm.get("waive_positive_alpha", True) and cum_alpha > 0:

        # P0: 单日暴跌（最高优先级，覆盖所有分段逻辑）
        abs_loss_thresh = dm.get("absolute_loss_trap_threshold", -0.02)
        loss_ratio = dm.get("absolute_loss_action_ratio", 0.7)
        cooldown_days = dm.get("absolute_loss_cooldown_days", 3)
        if r_fund <= abs_loss_thresh * 100:
            cooldown.lock(cooldown_days, loss_ratio)
            return loss_ratio, cooldown

        # P1-P3: 基于 fund_dd_20d 的分段逻辑
        systemic_dd = dm.get("trap_systemic_dd_threshold", -0.15)
        systemic_ratio = dm.get("trap_systemic_action_ratio", 0.5)
        golden_upper = dm.get("trap_golden_pit_dd_upper", -0.10)
        golden_lower = dm.get("trap_golden_pit_dd_lower", -0.15)
        golden_ratio = dm.get("trap_golden_pit_action_ratio", 1.3)
        golden_cooldown = dm.get("trap_golden_pit_cooldown_days", 1)
        normal_defense_dd = dm.get("absolute_dd_trap_threshold", -0.08)
        normal_defense_ratio = dm.get("trap_normal_defense_action_ratio", 0.8)

        if fund_dd_20d <= systemic_dd:
            # P1: 系统性风险 <= -15% → 重防守 0.5
            cooldown.lock(cooldown_days, systemic_ratio)
            return systemic_ratio, cooldown
        elif golden_lower < fund_dd_20d <= golden_upper:
            # P2: 黄金坑反弹区 (-15%, -10%] → 加倍买入 1.3
            cooldown.lock(golden_cooldown, golden_ratio)
            return golden_ratio, cooldown
        elif fund_dd_20d <= normal_defense_dd:
            # P3: 常规防守 (-10%, -8%] → 轻度打折 0.8
            cooldown.lock(cooldown_days, normal_defense_ratio)
            return normal_defense_ratio, cooldown
        else:
            return 1.0, cooldown

    # === 第二层：负向偏离惩罚 ===
    corr = compute_correlation_20d(df, t, lookback)
    mae_20d = compute_mae_20d(df, t, lookback)
    mae_avg_60d = compute_mae_avg_60d(df, t, mae_long)

    # 检查是否连续 3 日满足 0.50 <= Corr < 0.70
    consec_corr_ok = True
    for i in range(max(0, t - alert_days + 1), t + 1):
        c = compute_correlation_20d(df, i, lookback)
        if not (0.50 <= c < corr_thresh):
            consec_corr_ok = False
            break

    if cum_alpha <= 0 and consec_corr_ok and mae_20d > te_mult * mae_avg_60d:
        return reduce_ratio, cooldown  # 常规期 0.3

    # === 第三层：系统失效阻断 ===
    if cum_alpha <= 0 and corr < block_thresh:
        return 0.0, cooldown

    # 默认：正常运行
    return 1.0, cooldown
