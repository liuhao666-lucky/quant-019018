"""
strategy.py
TMT-Alpha 7.0 主策略类
逐日串联所有模块，输出每日信号 dict。
支持 14:45 快照回测模式。
"""

import logging
import numpy as np
import pandas as pd

from core.config_loader import get_config
from model.model1_benchmark import (
    compute_mkt_chg, compute_aux, compute_returns,
    compute_mkt_chg_series, compute_aux_series,
    compute_excess_nav, compute_alpha_daily, compute_vix_proxy,
)
from model.model2_drift_monitor import CooldownState, compute_action_ratio
from model.model3_trend_factor import (
    compute_trend_factor, compute_alpha_bonus, compute_final_multiplier,
)
from model.model4_base_scorer import compute_base
from model.model5_intraday_filter import (
    compute_tau_vol, compute_storm_discount,
    compute_omega_shrink, compute_omega_ext, compute_omega_bias,
    estimate_today_volume,
)
from model.model6_soft_compressor import ExecutionState, compute_score_eff, execute_channel
from model.model7_exit_logic import PositionState, check_exit

logger = logging.getLogger(__name__)


class TMTAlphaStrategy:
    """
    TMT-Alpha 7.0 主策略类。

    使用方法:
        strategy = TMTAlphaStrategy(cfg)
        df = strategy.prepare_data(raw_df)
        for t in range(strategy.warmup_days, len(df)):
            signal = strategy.process_day(t, df)

    快照回测:
        strategy = TMTAlphaStrategy(cfg, snapshot_map=snapshot_dict)
        # snapshot_dict: {trade_date: {tmt_chg_pct, tmt_volume, ...}}
    """

    def __init__(self, cfg: dict = None, snapshot_map: dict = None):
        self.cfg = cfg or get_config()
        self.warmup_days = self.cfg.get("system", {}).get("warmup_days", 60)
        self.warmup_limit = self.cfg.get("system", {}).get("warmup_position_limit", 0.33)
        self.use_snapshot = self.cfg.get("backtest", {}).get("use_snapshot", False)
        self.snapshot_map = snapshot_map or {}
        self.snapshot_used = 0
        self.snapshot_total = 0

        # 状态对象
        self.cooldown = CooldownState()
        self.exec_state = ExecutionState()
        self.pos_state = PositionState()

    def prepare_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        预计算所有中间指标，为逐日遍历做准备。
        关键：仅用 ffill() 填充净值缺失，若基金数据晚于指数数据则截断对齐，
        严禁 bfill() 引入未来函数。
        """
        df = df.copy()

        # 仅前向填充基金净值（禁止 bfill，防止未来函数）
        df["fund_nav"] = df["fund_nav"].ffill()
        df["fund_daily_return"] = df["fund_daily_return"].ffill()

        # 若 fund_nav 开头仍为 NaN（基金数据晚于指数数据），截断对齐
        # 否则 compute_excess_nav 中 nav0=NaN → 整列 Excess_DD 塌缩为 NaN
        nav_vals = df["fund_nav"].values
        first_valid = None
        for i, v in enumerate(nav_vals):
            if pd.notna(v) and v > 0:
                first_valid = i
                break

        if first_valid is None:
            raise ValueError("fund_nav 列全部为 NaN 或 0，无法计算超额指标。请检查 fund_nav 表是否有数据。")

        if first_valid > 0:
            df = df.iloc[first_valid:].reset_index(drop=True)

        # 计算收益率列
        df = compute_returns(df)

        # 计算 Mkt_Chg 和 R_Aux 序列
        df["Mkt_Chg"] = compute_mkt_chg_series(df, self.cfg)
        df["R_Aux"] = compute_aux_series(df, self.cfg)

        # 计算超额指标
        df = compute_excess_nav(df, self.cfg)

        # 计算 Alpha 相关指标
        df = compute_alpha_daily(df, self.cfg)

        # 计算波动率代理
        df = compute_vix_proxy(df, self.cfg)

        # 计算成交量均线
        vc = self.cfg.get("volume_control", {})
        vol_period = vc.get("volume_ma_period", 20)
        df["V_MA20"] = df["tmt_volume"].rolling(window=vol_period, min_periods=5).mean()

        # 计算 TMT 10 日均线（用于乖离率）
        df["TMT_MA10"] = df["tmt_close"].rolling(window=10, min_periods=3).mean()

        # 计算 MA60（用于趋势因子），早期数据不足时也向前回退计算
        df["TMT_MA60"] = df["tmt_close"].rolling(window=60, min_periods=1).mean()

        # === 剔除未来函数：隔离 14:45 信号计算与 15:00 真实结算 ===
        # 实盘中 14:45 无法获取当日 fund_nav / Excess_DD 等收盘后数据，
        # 因此将所有基金衍生列后移 1 天：t 时刻只能看到 t-1 的数据
        df["R_fund_actual"] = df["R_fund"]
        df["fund_nav_actual"] = df["fund_nav"]
        df["Excess_DD_actual"] = df["Excess_DD"]
        cols_to_shift = [
            "fund_nav", "R_fund", "Excess_NAV", "Excess_DD",
            "Alpha_daily", "Cum_Alpha_20d", "Fund_NAV_Max_20d", "Fund_DD_20d",
        ]
        df[cols_to_shift] = df[cols_to_shift].shift(1).ffill().bfill()

        return df

    def process_day(self, t: int, df: pd.DataFrame, current_gain: float = 0.0) -> dict:
        """
        处理单个交易日，生成完整信号。
        t: 当前交易日在 DataFrame 中的索引
        df: prepare_data 处理后的 DataFrame
        current_gain: 真实持仓收益率（position_value / invested_capital - 1），默认 0 无持仓
        返回: signal_dict
        """
        row = df.iloc[t]
        cfg = self.cfg

        # === 基础数据 ===
        trade_date = row["trade_date"]
        mkt_chg = row["Mkt_Chg"]
        r_fund = row["R_fund"]
        excess_dd = row["Excess_DD"]
        tmt_volume = row["tmt_volume"]
        v_ma20 = row["V_MA20"] if pd.notna(row["V_MA20"]) else tmt_volume
        vix_10d = row["VIX_10d"] if pd.notna(row["VIX_10d"]) else 0.0
        vix_avg_60d = row["VIX_avg_60d"] if pd.notna(row["VIX_avg_60d"]) else 0.0

        r_aic = row.get("R_AIC", 0)
        r_ce = row.get("R_CE", 0)
        r_semi = row.get("R_SEMI", 0)
        r_ne = row.get("R_NE", 0)

        # === 快照模式：优先使用 14:45 盘中数据 ===
        snapshot_fallback = False
        if self.use_snapshot and t >= self.warmup_days:
            self.snapshot_total += 1
            snap = self.snapshot_map.get(trade_date)
            if snap:
                self.snapshot_used += 1
                # 用快照的盘中涨跌幅替换收盘涨跌幅
                if snap.get("tmt_chg_pct") is not None:
                    mkt_chg = compute_mkt_chg(snap["tmt_chg_pct"], cfg)
                if snap.get("tmt_volume") is not None:
                    tmt_volume = snap["tmt_volume"]
                if snap.get("aic_chg_pct") is not None:
                    r_aic = snap["aic_chg_pct"]
                if snap.get("ce_chg_pct") is not None:
                    r_ce = snap["ce_chg_pct"]
                if snap.get("semi_chg_pct") is not None:
                    r_semi = snap["semi_chg_pct"]
                if snap.get("ne_chg_pct") is not None:
                    r_ne = snap["ne_chg_pct"]
            else:
                snapshot_fallback = True
                logger.warning(f"[警告] {trade_date} 无快照，回退收盘数据")

        # === 模块一：漂移雷达 → Action_Ratio ===
        action_ratio, self.cooldown = compute_action_ratio(
            df, t, cfg, self.cooldown
        )

        # === 模块二：趋势因子 → Final_Multiplier ===
        trend_factor = compute_trend_factor(df, t, cfg)
        alpha_bonus = compute_alpha_bonus(excess_dd, cfg)
        final_mult = compute_final_multiplier(trend_factor, alpha_bonus, cfg)

        # === 模块二：基础评分 ===
        base = compute_base(mkt_chg, final_mult, cfg)

        # === 模块三：量价过滤与极值补偿 ===
        v_today = estimate_today_volume(tmt_volume, 225, cfg)
        tau_vol = compute_tau_vol(mkt_chg, v_today, v_ma20, cfg)
        storm = compute_storm_discount(vix_10d, vix_avg_60d, cfg)

        omega_shrink = compute_omega_shrink(mkt_chg, v_today, v_ma20, cfg)
        omega_ext = compute_omega_ext(r_aic, r_ce, r_ne, r_semi, cfg)
        omega_bias = compute_omega_bias(df, t, cfg)

        # Score_raw
        score_raw = (base + omega_shrink + omega_ext + omega_bias) * storm * tau_vol

        # === 模块四：软压缩 → Score_eff ===
        score_eff = compute_score_eff(score_raw, vix_10d, vix_avg_60d, cfg)

        # === 模块四：执行通道 ===
        bt = cfg.get("backtest", {})
        total_capital = bt.get("total_capital", 1000)
        max_pos_ratio = bt.get("max_position_ratio", 1.0)
        max_allowed = total_capital * max_pos_ratio

        amount_before_cap = 0.0
        amount, channel, self.exec_state = execute_channel(
            score_eff, mkt_chg, v_today, v_ma20, excess_dd, t,
            max_allowed, cfg, self.exec_state
        )
        amount_before_cap = amount

        # === 模块五：退出逻辑 ===
        exit_result, self.pos_state = check_exit(
            df, t, cfg, self.pos_state, self.exec_state, current_gain
        )

        # === 综合决策 ===
        action = "hold"
        final_amount = 0.0

        if exit_result["force_reduce"]:
            action = "sell"
            final_amount = -max_allowed * exit_result["sell_ratio"]
        elif exit_result["sell_ratio"] > 0:
            action = "sell"
            final_amount = -max_allowed * exit_result["sell_ratio"]
        elif amount > 0:
            adjusted_amount = amount * action_ratio
            if exit_result["warning"] and channel == "A":
                adjusted_amount *= 0.5
            final_amount = adjusted_amount
            action = "buy" if adjusted_amount > 0 else "hold"
        elif amount < 0:
            action = "sell"
            final_amount = amount

        # 系统预热期限制仓位
        if t < self.warmup_days and self.cfg.get("system", {}).get("allow_immature_signal", False) is False:
            final_amount *= self.warmup_limit

        signal = {
            "trade_date": trade_date,
            "t": t,
            "mkt_chg": round(mkt_chg, 4),
            "r_fund": round(r_fund, 4),
            "excess_dd": round(excess_dd, 6),
            "action_ratio": round(action_ratio, 4),
            "trend_factor": round(trend_factor, 4),
            "alpha_bonus": round(alpha_bonus, 2),
            "final_multiplier": round(final_mult, 4),
            "base": round(base, 2),
            "omega_shrink": round(omega_shrink, 2),
            "omega_ext": round(omega_ext, 2),
            "omega_bias": round(omega_bias, 2),
            "storm_discount": storm,
            "tau_vol": tau_vol,
            "score_raw": round(score_raw, 2),
            "score_eff": round(score_eff, 2),
            "channel": channel,
            "amount_before_cap": round(amount_before_cap, 2),
            "amount": round(final_amount, 2),
            "action": action,
            "warning": exit_result["warning"],
            "force_reduce": exit_result["force_reduce"],
            "trailing_stop": exit_result.get("trailing_stop", False),
            "threshold_adjust": exit_result["threshold_adjust"],
            "snapshot_used": not snapshot_fallback if self.use_snapshot else None,
        }

        return signal

    def get_snapshot_coverage(self) -> dict:
        """返回快照覆盖率统计"""
        return {
            "used": self.snapshot_used,
            "total": self.snapshot_total,
            "rate": self.snapshot_used / self.snapshot_total if self.snapshot_total > 0 else 0,
        }
