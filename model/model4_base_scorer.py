"""
base_scorer.py
模块二（续）：顺势基础评分体系
- f(x) 评分函数：上涨加仓奖励、震荡中立、下跌惩罚缩量
- Base = f(Mkt_Chg) × Final_Multiplier
"""


def f_score(x: float) -> float:
    """
    基础评分函数 f(x)。

    公式：
    x < -0.5:    max(30, 60 - 30 × |x| / 3.0)
    -0.5 ≤ x ≤ 0.5: 50
    x > 0.5:     60 + 40 × min(1, x / 3.0)

    x 为法定主锚 Mkt_Chg 的瞬时涨跌幅（%）。
    """
    if x < -0.5:
        return max(30.0, 60.0 - 30.0 * abs(x) / 3.0)
    elif x <= 0.5:
        return 50.0
    else:
        return 60.0 + 40.0 * min(1.0, x / 3.0)


def compute_base(mkt_chg: float, final_multiplier: float, cfg: dict = None) -> float:
    """
    基础得分 Base = f(Mkt_Chg) × Final_Multiplier

    mkt_chg: 法定主锚涨跌幅（%）
    final_multiplier: 来自 trend_factor 模块的综合乘数
    """
    return f_score(mkt_chg) * final_multiplier
