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
    compute_mkt_chg, compute_returns,
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
from model.model8_market_state import (
    compute_market_temperature, get_market_mode, get_adaptive_params,
)

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
        # === 保存未 shift 的实时列（用于防锯齿、14:45 可观测逻辑）===
        # 这些列在 T 日 14:45 可通过快照数据估算，回测中以 T 日收盘数据模拟
        df["R_fund_live"] = df["R_fund"].copy()
        df["Fund_DD_20d_live"] = df["Fund_DD_20d"].copy()
        df["Cum_Alpha_20d_live"] = df["Cum_Alpha_20d"].copy()

        # 将所有基金衍生列后移 1 天：t 时刻只能看到 t-1 的数据
        # 注意：R_fund / Fund_DD_20d / Cum_Alpha_20d 的 _live 版本已保存，不参与 shift
        df["R_fund_actual"] = df["R_fund"]
        df["fund_nav_actual"] = df["fund_nav"]
        df["Excess_DD_actual"] = df["Excess_DD"]
        cols_to_shift = [
            "fund_nav", "R_fund", "Excess_NAV", "Excess_DD",
            "Alpha_daily", "Cum_Alpha_20d", "Fund_NAV_Max_20d", "Fund_DD_20d",
        ]
        df[cols_to_shift] = df[cols_to_shift].shift(1).ffill().bfill()

        # === 趋势感知止盈：计算基金净值 40 日均线和 5 日收益率 ===
        # 使用已 shift 的 fund_nav（即 t 时刻策略可观测的最新净值），无未来函数
        tf_cfg = self.cfg.get("trend_filter", {})
        trend_ma = tf_cfg.get("trend_ma_period", 40)
        df["fund_ma40"] = df["fund_nav"].rolling(window=trend_ma, min_periods=trend_ma).mean()
        df["fund_5d_return"] = df["fund_nav"].pct_change(periods=5) * 100  # 百分比

        return df

    def _detect_trend_strength(self, row, cfg) -> bool:
        """
        趋势强度判断（放宽条件）。

        强趋势条件（满足任一即可）：
        1. 净值 > MA40 且近 5 日收益率 > 1%（从 2% 放宽）
        2. 净值偏离 MA40 超过 5%（兜底：捕捉匀速慢涨行情）

        返回: True = 强趋势
        """
        tf_cfg = cfg.get("trend_filter", {})
        fund_nav_val = row["fund_nav"]
        fund_ma40 = row.get("fund_ma40", fund_nav_val)
        fund_5d_ret = row.get("fund_5d_return", 0.0)

        if pd.isna(fund_ma40) or pd.isna(fund_5d_ret) or fund_ma40 <= 0:
            return False

        # 条件1：净值在 MA40 之上且 5 日收益 > 1%
        strong_5d = tf_cfg.get("trend_strong_5d_return", 0.01) * 100  # 1% 转为百分比
        cond1 = fund_nav_val > fund_ma40 and fund_5d_ret > strong_5d

        # 条件2：净值偏离 MA40 超过 5%（兜底慢涨行情）
        deviation_pct = (fund_nav_val / fund_ma40 - 1) * 100
        cond2 = deviation_pct > 5.0

        return cond1 or cond2

    def process_day(self, t: int, df: pd.DataFrame, current_gain: float = 0.0,
                    holding_days: int = 0) -> dict:
        """
        处理单个交易日，生成完整信号。
        t: 当前交易日在 DataFrame 中的索引
        df: prepare_data 处理后的 DataFrame
        current_gain: 真实持仓收益率（position_value / invested_capital - 1），默认 0 无持仓
        holding_days: 持仓交易天数（用于时间止损判断），默认 0 无持仓
        返回: signal_dict
        """
        row = df.iloc[t]
        cfg = self.cfg

        # === 基础数据 ===
        trade_date = row["trade_date"]
        mkt_chg = row["Mkt_Chg"]
        r_fund = row.get("R_fund_live", row["R_fund"])
        excess_dd = row["Excess_DD"]
        tmt_volume = row["tmt_volume"]
        v_ma20 = row["V_MA20"] if pd.notna(row["V_MA20"]) else tmt_volume
        vix_10d = row["VIX_10d"] if pd.notna(row["VIX_10d"]) else 0.0
        vix_avg_60d = row["VIX_avg_60d"] if pd.notna(row["VIX_avg_60d"]) else 0.0

        r_aic = row.get("R_AIC", 0)
        r_ce = row.get("R_CE", 0)
        r_semi = row.get("R_SEMI", 0)
        r_ne = row.get("R_NE", 0)

        # === 市场温度自适应 ===
        market_temp = compute_market_temperature(df, t, 20)
        market_mode = get_market_mode(market_temp, cfg)
        adaptive_params = get_adaptive_params(market_mode, cfg)

        # === 快照模式：优先使用 14:45 盘中数据 ===
        snapshot_fallback = False
        if self.use_snapshot and t >= self.warmup_days:
            self.snapshot_total += 1
            snap = self.snapshot_map.get(trade_date)
            if snap:
                self.snapshot_used += 1
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

        # === 模块二：趋势因子 → Final_Multiplier（含市场自适应参数） ===
        trend_factor = compute_trend_factor(df, t, cfg, adaptive_params)
        alpha_bonus = compute_alpha_bonus(excess_dd, cfg)
        final_mult = compute_final_multiplier(trend_factor, alpha_bonus, cfg, adaptive_params)

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

        # 预热期内使用更低的仓位上限
        warmup_max_ratio = self.cfg.get("system", {}).get("warmup_max_position_ratio", 0.0)
        if t < self.warmup_days and warmup_max_ratio > 0:
            max_allowed = total_capital * warmup_max_ratio

        amount_before_cap = 0.0
        amount, channel, self.exec_state = execute_channel(
            score_eff, mkt_chg, v_today, v_ma20, excess_dd, t,
            max_allowed, cfg, self.exec_state
        )
        amount_before_cap = amount

        # === 模块五：退出逻辑 ===
        # 趋势感知：放宽条件 + 兜底判断
        trend_strong = self._detect_trend_strength(row, cfg)

        exit_result, self.pos_state = check_exit(
            df, t, cfg, self.pos_state, self.exec_state, current_gain,
            score_eff=score_eff, holding_days=holding_days,
            trend_strong=trend_strong
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

        # 预热期标记
        warmup_active = (t < self.warmup_days)

        signal = {
            "trade_date": trade_date,
            "t": t,
            "warmup_active": warmup_active,
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
            "signal_decay": exit_result.get("signal_decay", False),
            "time_stop": exit_result.get("time_stop", False),
            "tp_tier": exit_result.get("tp_tier", 0),
            "trend_strong": trend_strong,
            "threshold_adjust": exit_result["threshold_adjust"],
            "snapshot_used": not snapshot_fallback if self.use_snapshot else None,
            # 市场温度自适应
            "market_temp": round(market_temp, 2),
            "market_mode": market_mode,
        }

        return signal

    def get_snapshot_coverage(self) -> dict:
        """返回快照覆盖率统计"""
        return {
            "used": self.snapshot_used,
            "total": self.snapshot_total,
            "rate": self.snapshot_used / self.snapshot_total if self.snapshot_total > 0 else 0,
        }
