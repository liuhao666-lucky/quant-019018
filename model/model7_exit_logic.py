"""
exit_logic.py
模块七：真实超额回撤熔断与阶梯止盈 + 移动止盈 (Smart Relative Exit)

所有止盈逻辑统一在此模块，backtest.py 不再自行判断止盈。
通过 current_gain（真实持仓收益率）驱动，不再依赖峰值对比。
"""


class PositionState:
    """持仓状态（在 backtest / strategy 间共享）"""

    def __init__(self):
        self.forced_reduce_end = -999   # 强制降仓冻结截止日
        self.tp_cooldown_end = -999     # 止盈冷却截止日
        self.tp1_triggered = False      # 一档止盈是否已触发（网格锁）
        self.tp2_triggered = False      # 二档止盈是否已触发
        # 移动止盈
        self.trailing_active = False     # 是否已激活移动止盈
        self.trailing_peak = 0.0        # 激活后的历史最高净值


def get_view_attitude(cfg: dict) -> int:
    """季报前瞻态度量化，从配置读取预设值。"""
    el = cfg.get("exit_logic", {})
    return el.get("view_attitude", 0)


def compute_threshold_adjust(view_attitude: int, cfg: dict) -> float:
    """动态阈值调整 Threshold_Adjust = View_Attitude × 0.05"""
    el = cfg.get("exit_logic", {})
    step = el.get("attitude_adjust_step", 0.05)
    return view_attitude * step


def check_excess_dd_warning(excess_dd: float, threshold_adjust: float,
                            cfg: dict) -> bool:
    """
    相对超额预警 (Warning)。
    当 Excess_DD(t) <= (warning_base - Threshold_Adjust) 时触发。
    触发后通道 A 买入指令强制折半。
    """
    el = cfg.get("exit_logic", {})
    base = el.get("excess_dd_warning_base", -0.10)
    return excess_dd <= (base - threshold_adjust)


def check_excess_dd_force(excess_dd: float, threshold_adjust: float,
                          cfg: dict) -> bool:
    """
    系统性跑输强平 (Force Reduce)。
    当 Excess_DD(t) <= (force_base - Threshold_Adjust) 时触发。
    触发后强制将基金仓位降至 50% 以下，冻结至少 5 个交易日。
    """
    el = cfg.get("exit_logic", {})
    base = el.get("excess_dd_force_base", -0.15)
    return excess_dd <= (base - threshold_adjust)


def check_take_profit(current_gain: float, t: int, cfg: dict,
                      state: PositionState) -> tuple:
    """
    阶梯止盈检查（基于真实持仓收益率）。

    返回: (sell_ratio, state)
    sell_ratio > 0 表示需要卖出该比例的持仓。
    """
    if t <= state.tp_cooldown_end:
        return 0.0, state

    el = cfg.get("exit_logic", {})
    tp1, sell1 = el.get("tp_level_1", 0.25), el.get("tp_sell_ratio_1", 0.33)
    tp2, sell2 = el.get("tp_level_2", 0.50), el.get("tp_sell_ratio_2", 0.33)

    if current_gain >= tp2 and not state.tp2_triggered:
        state.tp_cooldown_end = t + el.get("cool_down_days", 5)
        state.tp2_triggered = True
        return sell2, state
    elif current_gain >= tp1 and not state.tp1_triggered:
        state.tp_cooldown_end = t + el.get("cool_down_days", 5)
        state.tp1_triggered = True
        return sell1, state

    # 跌落回成本线附近时重置止盈状态，允许开启下一轮网格
    if current_gain < tp1 * 0.5:
        state.tp1_triggered = False
        state.tp2_triggered = False

    return 0.0, state


def check_trailing_stop(fund_nav: float, current_gain: float, cfg: dict,
                        state: PositionState) -> tuple:
    """
    移动止盈（Trailing Stop）。

    - 当持仓收益率超过 activate（默认 30%）后激活
    - 激活后持续跟踪历史最高净值
    - 当从最高净值回撤超过 drawdown（默认 20%）时触发全部清仓

    返回: (sell_ratio, state)
    sell_ratio = 1.0 表示全部清仓，0.0 表示不操作。
    """
    el = cfg.get("exit_logic", {})
    activate = el.get("trailing_stop_activate", 0.30)
    drawdown = el.get("trailing_stop_drawdown", 0.20)

    if current_gain >= activate:
        state.trailing_active = True

    if not state.trailing_active:
        return 0.0, state

    if fund_nav > state.trailing_peak:
        state.trailing_peak = fund_nav

    if state.trailing_peak > 0:
        if (state.trailing_peak - fund_nav) / state.trailing_peak >= drawdown - 1e-9:
            state.trailing_active = False
            state.trailing_peak = 0.0
            # 清仓联动重置止盈网格
            state.tp1_triggered = False
            state.tp2_triggered = False
            return 1.0, state

    return 0.0, state


def check_exit(df, t: int, cfg: dict, pos_state: PositionState,
               exec_state=None, current_gain: float = 0.0) -> tuple:
    """
    综合退出逻辑检查。

    返回: (action_dict, pos_state)
    action_dict 包含:
      - warning: bool
      - force_reduce: bool
      - sell_ratio: float (止盈卖出比例)
      - action: str ("sell" / "hold")
      - trailing_stop: bool
      - excess_dd: float
      - threshold_adjust: float
    """
    el = cfg.get("exit_logic", {})
    force_days = 5

    excess_dd = df["Excess_DD"].iloc[t]
    fund_nav = df["fund_nav"].iloc[t]

    view = get_view_attitude(cfg)
    thresh_adj = compute_threshold_adjust(view, cfg)

    warning = check_excess_dd_warning(excess_dd, thresh_adj, cfg)
    force_reduce = check_excess_dd_force(excess_dd, thresh_adj, cfg)

    action = "hold"
    sell_ratio = 0.0
    trailing_stop = False

    # 强制平仓（优先级最高）
    if force_reduce and t >= pos_state.forced_reduce_end:
        action = "sell"
        sell_ratio = 0.50
        pos_state.forced_reduce_end = t + force_days
        if exec_state:
            exec_state.forced_reduce_end = t + force_days

    # 止盈检查（仅在未触发强平时）
    if not force_reduce and current_gain > 0:
        tp_ratio, pos_state = check_take_profit(current_gain, t, cfg, pos_state)
        if tp_ratio > 0:
            action = "sell"
            sell_ratio = tp_ratio

        # 移动止盈检查（优先级高于阶梯止盈）
        ts_ratio, pos_state = check_trailing_stop(fund_nav, current_gain, cfg, pos_state)
        if ts_ratio > 0:
            action = "sell"
            sell_ratio = ts_ratio
            trailing_stop = True

    return {
        "warning": warning,
        "force_reduce": force_reduce,
        "sell_ratio": sell_ratio,
        "action": action,
        "excess_dd": excess_dd,
        "threshold_adjust": thresh_adj,
        "trailing_stop": trailing_stop,
    }, pos_state
