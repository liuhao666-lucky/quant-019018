"""
exit_logic.py
模块七：真实超额回撤熔断与阶梯止盈 + 移动止盈 (Smart Relative Exit)

所有止盈逻辑统一在此模块，backtest.py 不再自行判断止盈。
通过 current_gain（真实持仓收益率）驱动，不再依赖峰值对比。
"""


def _redemption_fee_rate(holding_days: int) -> float:
    """阶梯式赎回费率：0-6天 1.5%，7-29天 0.5%，30天+ 0%"""
    if holding_days < 7:
        return 0.015
    elif holding_days < 30:
        return 0.005
    else:
        return 0.0


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
        # 信号衰减减仓冷却
        self.signal_decay_cooldown_end = -999
        # 时间止损：首次买入日期（用于计算持仓天数）
        self.first_buy_date = None       # 首次建仓日期 str
        self.time_stop_triggered = False # 是否已触发时间止损（防止重复触发）
        # 趋势感知止盈：持仓期间浮盈最高点
        self.gain_peak = 0.0            # 当前持仓周期内的最高收益率


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
                      state: PositionState, trend_strong: bool = False,
                      holding_days: int = 0, market_mode: str = "defense") -> tuple:
    """
    阶梯止盈检查（基于真实持仓收益率），支持趋势感知动态阈值和市场自适应卖出比例。
    赎回费率已纳入考量：有效阈值 = 基础阈值 + 赎回费率。

    返回: (sell_ratio, state, tp_tier)
    tp_tier: 0=未触发, 1=一档, 2=二档（供日志用）
    """
    if t <= state.tp_cooldown_end:
        return 0.0, state, 0

    # 更新持仓浮盈最高点
    if current_gain > state.gain_peak:
        state.gain_peak = current_gain

    el = cfg.get("exit_logic", {})

    # 基础止盈阈值（静态或趋势感知动态）
    tp1_base = el.get("tp_level_1", 0.25)
    tp2_base = el.get("tp_level_2", 0.50)

    if trend_strong:
        tp1_cfg = el.get("tp_level_1_strong", 0.40)
        tp2_cfg = el.get("tp_level_2_strong", 0.70)
        tp1_dyn = min(tp1_cfg, state.gain_peak * 0.80) if state.gain_peak > 0 else tp1_cfg
        tp2_dyn = min(tp2_cfg, state.gain_peak * 0.90) if state.gain_peak > 0 else tp2_cfg
        # 动态阈值不低于静态基础，且 tp1 < tp2
        tp1_base = max(tp1_dyn, tp1_base)
        tp2_base = max(tp2_dyn, tp2_base)
        tp1_base = min(tp1_base, tp2_base - 0.05)

    # 赎回费感知：有效阈值 = 基础阈值 + 赎回费率
    # 等价于：净浮盈 = 浮盈 - 赎回费率 >= 基础阈值
    fee_rate = _redemption_fee_rate(holding_days)
    tp1 = tp1_base + fee_rate
    tp2 = tp2_base + fee_rate

    # 市场自适应卖出比例：进攻模式少卖让仓位奔跑，防守模式多卖锁利润
    if market_mode == "attack":
        sell1 = el.get("tp_sell_ratio_1_attack", 0.20)
        sell2 = el.get("tp_sell_ratio_2_attack", 0.50)
    else:
        sell1 = el.get("tp_sell_ratio_1", 0.40)
        sell2 = el.get("tp_sell_ratio_2", 0.50)

    # 动态冷却期：进攻模式缩短（快速回补），防守模式延长（确认企稳）
    if market_mode == "attack":
        cooldown = el.get("cool_down_days_attack", 3)
    else:
        cooldown = el.get("cool_down_days_defense", 7)

    if current_gain >= tp2 and not state.tp2_triggered:
        state.tp_cooldown_end = t + cooldown
        state.tp2_triggered = True
        return sell2, state, 2
    elif current_gain >= tp1 and not state.tp1_triggered:
        state.tp_cooldown_end = t + cooldown
        state.tp1_triggered = True
        return sell1, state, 1

    # 跌落回成本线附近时重置止盈状态
    if current_gain < 0.05:
        state.tp1_triggered = False
        state.tp2_triggered = False
        state.gain_peak = 0.0

    return 0.0, state, 0


def check_trailing_stop(fund_nav: float, current_gain: float, cfg: dict,
                        state: PositionState, market_mode: str = "defense") -> tuple:
    """
    移动止盈（Trailing Stop）。

    - 当持仓收益率超过 activate（默认 30%）后激活
    - 激活后持续跟踪历史最高净值
    - 当从最高净值回撤超过 drawdown 时触发全部清仓
    - 进攻模式放宽容忍（10%），防守模式收紧止损（6%）

    返回: (sell_ratio, state)
    sell_ratio = 1.0 表示全部清仓，0.0 表示不操作。
    """
    el = cfg.get("exit_logic", {})
    activate = el.get("trailing_stop_activate", 0.30)
    # 动态回撤阈值：进攻模式放宽容忍，防守模式收紧
    if market_mode == "attack":
        drawdown = el.get("trailing_stop_drawdown_attack", 0.10)
    else:
        drawdown = el.get("trailing_stop_drawdown_defense", 0.06)

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
            state.gain_peak = 0.0
            return 1.0, state

    return 0.0, state


def check_signal_decay_sell(score_eff: float, t: int, cfg: dict,
                            pos_state: PositionState) -> tuple:
    """
    信号衰减减仓：当 Score_eff 跌破阈值时，主动卖出部分持仓。

    触发条件：
    - signal_decay_enabled = true
    - score_eff < signal_decay_sell_threshold（默认 20）
    - 不在冷却期内

    返回: (sell_ratio, pos_state)
    """
    el = cfg.get("exit_logic", {})
    if not el.get("signal_decay_enabled", True):
        return 0.0, pos_state

    if t <= pos_state.signal_decay_cooldown_end:
        return 0.0, pos_state

    threshold = el.get("signal_decay_sell_threshold", 20)
    if score_eff < threshold:
        ratio = el.get("signal_decay_sell_ratio", 0.50)
        cooldown = el.get("signal_decay_cooldown_days", 5)
        pos_state.signal_decay_cooldown_end = t + cooldown
        return ratio, pos_state

    return 0.0, pos_state


def check_time_stop(holding_days: int, current_gain: float, cfg: dict,
                    pos_state: PositionState) -> tuple:
    """
    时间止损：持仓超过 N 个交易日后，若亏损超过阈值，强制全部卖出。

    触发条件：
    - time_stop_enabled = true
    - holding_days > time_stop_days（默认 60）
    - time_stop_loss_only = false 时无条件触发
    - time_stop_loss_only = true 时，要求 current_gain <= time_stop_loss_threshold
    - 未触发过（防止同一段持仓重复触发）

    返回: (sell_ratio, pos_state)
    """
    el = cfg.get("exit_logic", {})
    if not el.get("time_stop_enabled", True):
        return 0.0, pos_state

    if pos_state.time_stop_triggered:
        return 0.0, pos_state

    max_days = el.get("time_stop_days", 60)
    loss_only = el.get("time_stop_loss_only", True)
    loss_threshold = el.get("time_stop_loss_threshold", -0.065)

    if holding_days > max_days:
        if not loss_only:
            pos_state.time_stop_triggered = True
            return el.get("time_stop_sell_ratio", 1.0), pos_state
        elif current_gain <= loss_threshold:
            # 仅在亏损超过阈值时才触发，避免微亏被洗出
            pos_state.time_stop_triggered = True
            return el.get("time_stop_sell_ratio", 1.0), pos_state

    return 0.0, pos_state


def check_exit(df, t: int, cfg: dict, pos_state: PositionState,
               exec_state=None, current_gain: float = 0.0,
               score_eff: float = 50.0, holding_days: int = 0,
               trend_strong: bool = False, market_mode: str = "defense") -> tuple:
    """
    综合退出逻辑检查。

    参数:
      - score_eff: 当前信号压缩得分（用于信号衰减减仓判断）
      - holding_days: 持仓交易天数（用于时间止损判断）
      - trend_strong: 策略层传入的趋势强度判断

    返回: (action_dict, pos_state)
    action_dict 包含:
      - warning: bool
      - force_reduce: bool
      - sell_ratio: float (止盈卖出比例)
      - action: str ("sell" / "hold")
      - trailing_stop: bool
      - signal_decay: bool
      - time_stop: bool
      - tp_tier: int (0=未触发, 1=一档, 2=二档，供日志用)
      - trend_strong: bool (回传供日志用)
      - excess_dd: float
      - threshold_adjust: float
    """
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
    signal_decay = False
    time_stop = False
    tp_tier = 0

    # 强制平仓（优先级最高）
    if force_reduce and t >= pos_state.forced_reduce_end:
        action = "sell"
        sell_ratio = 0.50
        pos_state.forced_reduce_end = t + force_days
        if exec_state:
            exec_state.forced_reduce_end = t + force_days

    # === 时间止损（优先级高于止盈，低于强平） ===
    if not force_reduce and holding_days > 0:
        ts_ratio, pos_state = check_time_stop(holding_days, current_gain, cfg, pos_state)
        if ts_ratio > 0:
            action = "sell"
            sell_ratio = ts_ratio
            time_stop = True

    # 止盈检查（仅在未触发强平且未触发时间止损时）
    # 赎回费已集成在 check_take_profit 内：净浮盈 = 浮盈 - 赎回费率
    if not force_reduce and not time_stop and current_gain > 0:
        tp_ratio, pos_state, tp_tier = check_take_profit(
            current_gain, t, cfg, pos_state, trend_strong,
            holding_days=holding_days, market_mode=market_mode
        )
        if tp_ratio > 0:
            action = "sell"
            sell_ratio = tp_ratio

        # 移动止盈检查：同样扣除赎回费后再判断
        fee_rate_for_trailing = _redemption_fee_rate(holding_days)
        net_gain_for_trailing = current_gain - fee_rate_for_trailing
        ts_ratio, pos_state = check_trailing_stop(fund_nav, net_gain_for_trailing, cfg, pos_state, market_mode)
        if ts_ratio > 0:
            action = "sell"
            sell_ratio = ts_ratio
            trailing_stop = True

    # === 信号衰减减仓（最低优先级，不覆盖已触发的卖出） ===
    # 赎回费同样适用：净收益不足时跳过
    if not force_reduce and not time_stop and sell_ratio == 0:
        fee_rate_for_decay = _redemption_fee_rate(holding_days)
        net_gain_for_decay = current_gain - fee_rate_for_decay
        sd_ratio, pos_state = check_signal_decay_sell(score_eff, t, cfg, pos_state)
        if sd_ratio > 0 and net_gain_for_decay >= 0:
            action = "sell"
            sell_ratio = sd_ratio
            signal_decay = True

    # 赎回费率信息（供信号推送和回测使用）
    fee_rate = _redemption_fee_rate(holding_days)

    return {
        "warning": warning,
        "force_reduce": force_reduce,
        "sell_ratio": sell_ratio,
        "action": action,
        "excess_dd": excess_dd,
        "threshold_adjust": thresh_adj,
        "trailing_stop": trailing_stop,
        "signal_decay": signal_decay,
        "time_stop": time_stop,
        "tp_tier": tp_tier,
        "trend_strong": trend_strong,
        "redemption_fee_rate": fee_rate,
    }, pos_state
