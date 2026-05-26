"""
market_state.py
模块八：市场温度自适应

根据 TMT 指数过去 20 个交易日的累计涨跌幅，动态调整策略激进程度。
- 进攻模式（温度 > 10%）：放宽空头惩罚，牛市回调中敢于加仓
- 防守模式（温度 ≤ 10%）：恢复平衡版保守参数
"""


def compute_market_temperature(df, t: int, lookback: int = 20) -> float:
    """
    市场温度 = TMT 指数过去 20 个交易日的累计涨幅（%）。

    返回: 温度值（%），数据不足时返回 0.0
    """
    if t < lookback:
        return 0.0
    close_t = df["tmt_close"].iloc[t]
    close_prev = df["tmt_close"].iloc[t - lookback]
    if close_prev <= 0:
        return 0.0
    return (close_t / close_prev - 1) * 100


def get_market_mode(temperature: float, cfg: dict) -> str:
    """返回 "attack" 或 "defense" """
    threshold = cfg.get("market_state", {}).get("attack_threshold", 10.0)
    return "attack" if temperature > threshold else "defense"


def get_adaptive_params(mode: str, cfg: dict) -> dict:
    """
    根据市场模式返回自适应参数覆盖。

    进攻模式：牛市中的小回调不缩手，并放大单笔和仓位上限
    - below_ma_power: 0.65（轻惩罚，敢于在回调时加仓）
    - consecutive_drop_power: 0.35
    - multiplier_min: 0.70（抬高乘数下限，维持较高仓位）
    - m_max_multiplier: 1.30（单笔买入上限放大30%）
    - max_position_ratio: 0.95（仓位上限从85%提升至95%）

    防守模式：恢复平衡版原参数
    - below_ma_power: 0.50
    - consecutive_drop_power: 0.25
    - multiplier_min: 0.60
    - m_max_multiplier: 1.00（不放大）
    - max_position_ratio: 使用 backtest.max_position_ratio 原值
    """
    ms = cfg.get("market_state", {})
    tf = cfg.get("trend_filter", {})
    bt = cfg.get("backtest", {})

    if mode == "attack":
        return {
            "below_ma_power": ms.get("attack_below_ma_power", 0.65),
            "consecutive_drop_power": ms.get("attack_consecutive_drop_power", 0.35),
            "multiplier_min": ms.get("attack_multiplier_min", 0.70),
            "m_max_multiplier": ms.get("attack_m_max_multiplier", 1.30),
            "max_position_ratio": ms.get("attack_max_position_ratio", 0.95),
        }
    else:
        return {
            "below_ma_power": tf.get("below_ma_power", 0.50),
            "consecutive_drop_power": tf.get("consecutive_drop_power", 0.25),
            "multiplier_min": ms.get("defense_multiplier_min", 0.60),
            "m_max_multiplier": 1.00,
            "max_position_ratio": bt.get("max_position_ratio", 0.85),
        }
