"""
config_loader.py
统一配置加载模块：优先从 config.yaml 读取，缺失时使用代码内默认值。
"""

import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# 全量默认值（与设计文档 YAML 完全对齐）
_DEFAULTS = {
    "system": {
        "warmup_days": 20,
        "warmup_position_limit": 0.33,
        "warmup_max_position_ratio": 0.35,
        "allow_immature_signal": False,
    },
    "benchmark": {
        "primary_anchor": "000998.CSI",
        "equity_weight": 0.70,
        "cash_weight": 0.30,
        "deposit_daily_rate": 0.00004,
        "fee_drag_daily": 0.000038,
    },
    "auxiliary_weights": {
        "w_aic": 0.60,
        "w_ce": 0.15,
        "w_semi": 0.15,
        "w_ne": 0.10,
    },
    "drift_monitor": {
        "enable": True,
        "lookback_days": 20,
        "corr_threshold": 0.70,
        "tracking_error_multiplier": 1.5,
        "mae_long_period": 60,
        "alert_consecutive_days": 3,
        "action_reduce_ratio": 0.3,
        "reboot_reduce_ratio": 0.6,
        "action_block_threshold": 0.50,
        "waive_positive_alpha": True,
        "absolute_loss_trap_threshold": -0.02,
        "absolute_dd_trap_threshold": -0.08,
        "absolute_loss_action_ratio": 0.7,
        "absolute_loss_cooldown_days": 3,
    },
    "trend_filter": {
        "ma_period": 60,
        "recent_peak_days": 20,
        "epsilon": 0.001,
        "no_peak_threshold_ratio": 0.005,
        "below_ma_power": 0.50,
        "consecutive_drop_limit": 5,
        "consecutive_drop_power": 0.25,
        "alpha_bonus_high_threshold": -0.02,
        "alpha_bonus_mid_threshold": -0.05,
        "alpha_bonus_stalemate_floor": 0.75,
        "alpha_bonus_resonance_cap": 1.1,
        # 趋势感知止盈：趋势强度判断参数
        "trend_ma_period": 40,
        "trend_strong_5d_return": 0.02,
        "trend_weak_5d_return": 0.0,
    },
    "volume_control": {
        "volume_ma_period": 20,
        "panic_drop_threshold": -2.0,
        "volume_surge_multiplier": 1.5,
        "storm_discount_vix_ratio": 1.3,
        "storm_discount_value": 0.7,
        "v_shape_reversal_gain": 3.0,
        "v_shape_reversal_vol": 1.2,
        "v_shape_cooldown_days": 5,
        "v_shape_correction_days": 10,
        "v_shape_correction_dd": -0.04,
        "v_shape_sell_max_ratio": 0.50,
        "v_shape_sell_current_pos_ratio": 0.15,
        "shrink_multiplier": 0.7,
        "shrink_reward_max": 15,
    },
    "exit_logic": {
        "view_attitude": 1,
        "excess_dd_warning_base": -0.08,
        "excess_dd_force_base": -0.15,
        "attitude_adjust_step": 0.05,
        "tp_level_1": 0.25,
        "tp_sell_ratio_1": 0.33,
        "cool_down_days": 5,
        "tp_reset_method": "reset_to_current_nav",
        "tp_level_2": 0.50,
        "tp_sell_ratio_2": 0.33,
        # 趋势感知止盈：强趋势时抬高的止盈阈值
        "tp_level_1_strong": 0.40,
        "tp_level_2_strong": 0.70,
        "force_liquidation_ignore_fee": True,
        "nav_peak_reset_to_null": True,
        "trailing_stop_activate": 0.30,
        "trailing_stop_drawdown": 0.20,
        # 信号衰减减仓
        "signal_decay_enabled": True,
        "signal_decay_sell_threshold": 20,
        "signal_decay_sell_ratio": 0.50,
        "signal_decay_cooldown_days": 5,
        # 时间止损
        "time_stop_enabled": True,
        "time_stop_days": 60,
        "time_stop_loss_only": True,
        "time_stop_sell_ratio": 1.0,
    },
    "backtest": {
        "initial_capital": 1000,
        "max_position_ratio": 0.80,
        "total_capital": 1000,
        "use_snapshot": True,
        "preheat_days": 90,
        "start_date": None,
    },
    "execution": {
        "m_max_normal": 200,
        "m_min_normal": 20,
        "channel_a_power": 1.3,
        "channel_a_threshold": 30,
        "channel_b_reboot_days": 10,
        "channel_c_chase_threshold": 0.6,
        "v_shape_max_allowed_ratio": 0.30,
    },
    "market_state": {
        "attack_threshold": 10.0,
        "attack_below_ma_power": 0.65,
        "attack_consecutive_drop_power": 0.35,
        "attack_multiplier_min": 0.70,
        "defense_multiplier_min": 0.60,
    },
    "wechat": {
        "enabled": True,
        "webhook_url": "",
        "mention_all": False,
    },
    "schedule": {
        "daily_signal_time": "14:50",
        "closing_collector_time": "23:30",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 覆盖 base"""
    merged = base.copy()
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(config_path: str = None) -> dict:
    """
    加载配置：优先读 YAML，缺失键用默认值补全。
    返回完整的嵌套字典。
    """
    path = Path(config_path) if config_path else CONFIG_PATH
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        return _deep_merge(_DEFAULTS, user_cfg)
    else:
        print(f"[警告] 配置文件 {path} 不存在，使用默认配置。")
        return _DEFAULTS.copy()


# 全局单例，避免重复读取
_cached_cfg = None


def get_config() -> dict:
    """获取全局配置（懒加载单例）"""
    global _cached_cfg
    if _cached_cfg is None:
        _cached_cfg = load_config()
    return _cached_cfg
