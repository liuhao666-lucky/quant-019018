"""
notifier.py
企业微信机器人 webhook 推送模块。
VERSION = "v2.3_notice_pro"
"""

import os
import requests

from core.config_loader import load_config
from model.model7_exit_logic import _redemption_fee_rate

# 通道描述
CHANNEL_DESC = {
    "A": "积极进攻，信号强、风控绿灯",
    "B": "标准执行，正常节奏操作",
    "C": "谨慎试探，有追高风险",
    "D": "防守模式，触发风控或止跌",
}


def _get_webhook_url(cfg: dict = None) -> str:
    if cfg is None:
        cfg = load_config()
    url = cfg.get("wechat", {}).get("webhook_url", "") or os.environ.get("WECHAT_WEBHOOK_URL", "")
    if not url:
        raise ValueError("企业微信 webhook URL 未配置。")
    return url


def send_markdown(content: str, cfg: dict = None):
    url = _get_webhook_url(cfg)
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("errcode") != 0:
        print(f"[警告] 企微推送返回异常: {result}")
    return result


def _action_ratio_reason(ar: float) -> str:
    """根据 Action_Ratio 值推断触发原因"""
    if ar >= 1.0:
        return ""
    if ar <= 0.55:
        return "P0 单日暴跌保护"
    if ar <= 0.55:
        return "P1 系统性风险保护"
    if ar >= 1.2:
        return "P2 黄金坑加倍"
    if ar <= 0.85:
        return "P3 常规防守"
    return "防锯齿打折"


def _tp_level_display(market_mode: str, cfg: dict) -> tuple:
    """返回当前模式下的止盈阈值"""
    el = cfg.get("exit_logic", {})
    tp1 = el.get("tp_level_1", 0.25)
    tp2 = el.get("tp_level_2", 0.50)
    fee_defense = el.get("trailing_stop_drawdown_defense", 0.08)
    fee_attack = el.get("trailing_stop_drawdown_attack", 0.10)
    trailing_dd = fee_attack if market_mode == "attack" else fee_defense
    return tp1, tp2, trailing_dd


def send_signal_notification(signal_dict: dict, cfg: dict = None):
    """专业化五段式信号推送（v2.3_notice_pro）"""
    if cfg is None:
        cfg = load_config()

    # === 提取数据 ===
    d = signal_dict
    trade_date = d.get("trade_date", "")
    action = d.get("action", "hold")
    amount = d.get("amount", 0)
    channel = d.get("channel", "-")
    market_mode = d.get("market_mode", "defense")
    market_temp = d.get("market_temp", 0)

    # 市场数据
    mkt_chg = d.get("mkt_chg", 0)
    tmt_chg = d.get("tmt_chg_pct", 0)
    r_aic = d.get("r_aic", 0)
    r_ce = d.get("r_ce", 0)
    r_semi = d.get("r_semi", 0)
    r_ne = d.get("r_ne", 0)
    ma60 = d.get("ma60", 0)
    tmt_close = d.get("tmt_close", 0)

    # 信号数据
    score_eff = d.get("score_eff", 0)
    score_raw = d.get("score_raw", 0)
    base = d.get("base", 0)
    final_mult = d.get("final_multiplier", 1.0)
    action_ratio = d.get("action_ratio", 1.0)
    amount_before_cap = d.get("amount_before_cap", 0)

    # 风控
    excess_dd = d.get("excess_dd", 0) or 0
    warning = d.get("warning", False)
    force_reduce = d.get("force_reduce", False)
    trend_strong = d.get("trend_strong", False)

    # 持仓
    holding_days = d.get("holding_days", 0)
    current_gain = d.get("current_gain", 0)
    fee_rate = d.get("redemption_fee_rate", 0)
    fee_amount = d.get("redemption_fee_amount", 0)
    net_gain = d.get("net_gain_after_fee", 0)
    total_capital = d.get("total_capital", 1000)

    # === 第一部分：市场环境与温度 ===
    mode_text = "🟢 进攻" if market_mode == "attack" else "🔵 防守"
    mode_rule = "TMT 20日涨幅 > 10%" if market_mode == "attack" else "TMT 20日涨幅 ≤ 10%"

    ma60_bias = ((tmt_close / ma60) - 1) * 100 if ma60 > 0 else 0
    ma60_text = f"{ma60_bias:+.2f}%（{'均线上方，趋势向好' if ma60_bias > 0 else '均线下方，趋势偏弱'}）"

    env_summary = ""
    if market_mode == "attack" and ma60_bias > 0:
        env_summary = "市场处于进攻模式，TMT 位于均线上方，环境对策略友好"
    elif market_mode == "attack":
        env_summary = "市场处于进攻模式，但 TMT 位于均线下方，需留意回调风险"
    elif ma60_bias > 0:
        env_summary = "市场处于防守模式，TMT 位于均线上方，等待进攻信号"
    else:
        env_summary = "市场处于防守模式，TMT 位于均线下方，控制仓位为主"

    part1 = f"""## 📊 TMT-Alpha 信号 · {trade_date}

---

### 一、市场环境

> **市场温度**: {market_temp:+.2f} | 模式: {mode_text}（规则：{mode_rule}）
> **TMT 实时涨跌**: {tmt_chg:+.2f}%
> **MA60 乖离率**: {ma60_text}
> **白话**: {env_summary}"""

    # === 第二部分：核心信号拆解 ===
    # 权重说明
    part2 = f"""
---

### 二、核心信号

**法定主锚 Mkt_Chg**: {mkt_chg:+.2f}%
> 计算权重：TMT×70% + 四核辅助×30%

**四核辅助指标**:
| 指标 | 涨跌 | 说明 |
|------|------|------|
| 消费电子 (AIC) | {r_aic:+.2f}% | 消费电子产业链 |
| 通信设备 (CE) | {r_ce:+.2f}% | 通信基础设施 |
| 半导体 (SEMI) | {r_semi:+.2f}% | 芯片与半导体 |
| 新能源 (NE) | {r_ne:+.2f}% | 新能源产业链 |

**总评分 Score_eff**: {score_eff:.1f} / 100
> 路径：基础分 {base:.1f} → 趋势乘数 {final_mult:.2f} → 软压缩 → {score_eff:.1f}

**执行通道**: {'🟢' if channel=='A' else '🟡' if channel=='B' else '🟠' if channel=='C' else '🔴'}{channel}
> {CHANNEL_DESC.get(channel, '无信号')}"""

    # === 第三部分：风控与资金管理 ===
    ar_reason = _action_ratio_reason(action_ratio)
    ar_line = f"{action_ratio:.2f}"
    if action_ratio < 1.0 and ar_reason:
        ar_line += f" ⚠️ {ar_reason}"

    # 碎股过滤
    filter_note = ""
    if amount_before_cap > 0 and abs(amount) < 25 and action == "hold":
        filter_note = f"\n> 📌 **碎股过滤**: 信号建议 ¥{amount_before_cap:.0f}，但低于最低买入门槛 ¥25，已跳过"

    part3 = f"""
---

### 三、风控与资金

**最终乘数**: {final_mult:.2f}
> 影响因子：趋势强={trend_strong}，Alpha加成={d.get('alpha_bonus',0):.2f}

**Action_Ratio**: {ar_line}

**预警状态**: {'⚠️ 超额回撤预警' if warning else '✅ 正常'} | {'🚨 强平触发' if force_reduce else '✅ 正常'}
{filter_note}"""

    # === 第四部分：持仓与赎回费 ===
    part4 = ""
    if holding_days > 0:
        gain_pct = f"{current_gain:.2%}" if current_gain != 0 else "0.00%"
        fee_pct = f"{fee_rate * 100:.1f}%"
        tp1, tp2, trailing_dd = _tp_level_display(market_mode, cfg)
        tp1_eff = tp1 + fee_rate
        tp2_eff = tp2 + fee_rate

        part4 = f"""
---

### 四、持仓与赎回费

**当前持仓**: 持有 {holding_days} 天 | 累计收益率 {gain_pct}
**预估赎回费率**: {fee_pct}
**若今日卖出**: 赎回费约 ¥{fee_amount:.2f}，净收益约 {net_gain:.2%}

**止盈水位**（{mode_text}模式）:
> 一档有效阈值: {tp1_eff:.1%}（基础 {tp1:.0%} + 赎回费 {fee_pct}）
> 二档有效阈值: {tp2_eff:.1%}（基础 {tp2:.0%} + 赎回费 {fee_pct}）
> 移动止盈回撤容忍: {trailing_dd:.0%}"""

    # === 第五部分：最终建议 ===
    action_map = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "⚪ 观望"}
    action_text = action_map.get(action, action)

    advice_detail = ""
    if action == "sell" and amount < 0:
        advice_detail = f"\n> 💰 预估赎回费: ¥{fee_amount:.2f}"
        if current_gain > 0:
            advice_detail += f"\n> 📊 扣费后净收益: {net_gain:.2%}"
    elif action == "buy" and amount > 0:
        # 预估仓位占比
        est_pos = (abs(amount) + (total_capital * current_gain if current_gain > 0 else 0)) / total_capital * 100
        est_pos = min(est_pos, 100)
        advice_detail = f"\n> 📊 预估仓位占比: ~{est_pos:.0f}%（买入后）"
    elif action == "hold":
        if abs(amount_before_cap) > 0 and abs(amount) < 25:
            advice_detail = "\n> 信号金额低于门槛，跳过"
        else:
            advice_detail = "\n> 模型选择按兵不动"

    part5 = f"""
---

### 五、最终建议

**{action_text} ¥{abs(amount):,.0f}**
{advice_detail}"""

    # === 组装 ===
    content = part1 + part2 + part3 + part4 + part5

    try:
        send_markdown(content, cfg)
        print(f"[通知] 企微推送成功 ({trade_date})")
    except Exception as e:
        print(f"[通知] 企微推送失败: {e}")


def send_closing_summary(summary: dict, cfg: dict = None):
    """发送 23:30 收盘汇总到企业微信。"""
    if cfg is None:
        cfg = load_config()

    trade_date = summary.get("trade_date", "")
    tmt_close_chg = summary.get("tmt_close_chg", 0)
    tmt_intraday_chg = summary.get("tmt_intraday_chg")
    fund_chg = summary.get("fund_chg", 0)
    alpha_daily = summary.get("alpha_daily", 0)
    excess_dd = summary.get("excess_dd", 0) or 0
    signal_action = summary.get("signal_action", "-")
    signal_channel = summary.get("signal_channel", "-")
    signal_amount = summary.get("signal_amount", 0)
    data_ok = summary.get("data_ok", True)

    action_map = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "⚪ 观望"}
    action_text = action_map.get(signal_action, signal_action)
    channel_emoji = {"A": "🟢A", "B": "🟡B", "C": "🟠C", "D": "🔴D"}
    channel_text = channel_emoji.get(signal_channel, signal_channel)

    if tmt_intraday_chg is not None:
        drift = tmt_close_chg - tmt_intraday_chg
        drift_str = f"{drift:+.2f}%"
        drift_note = "尾盘拉升" if drift > 0.5 else ("尾盘跳水" if drift < -0.5 else "窄幅震荡")
    else:
        drift_str = "无快照数据"
        drift_note = ""

    if excess_dd > -0.02:
        dd_level = "🟢 安全区"
    elif excess_dd > -0.05:
        dd_level = "🟡 注意区"
    elif excess_dd > -0.10:
        dd_level = "🟠 警戒区"
    else:
        dd_level = "🔴 危险区"

    signal_feedback = {"buy": "模型认为今天是加仓时机", "sell": "模型触发了卖出信号"}.get(signal_action, "模型选择按兵不动")
    status_text = "✅ 正常" if data_ok else "⚠️ 部分失败"

    content = f"""## 📈 TMT-Alpha 收盘汇总 · {trade_date}

### 今日市场
| 指标 | 数值 |
|------|------|
| TMT 收盘 | {tmt_close_chg:+.2f}% |
| TMT 盘中 (14:45) | {tmt_intraday_chg:+.2f}% |
| 尾盘变动 | {drift_str} {drift_note} |
| 基金日收益 | {fund_chg:+.2f}% |
| 单日超额 | {alpha_daily:+.2f}% |
| 超额回撤 | {excess_dd:.2%} {dd_level} |

### 14:45 信号回顾
> {action_text} ¥{signal_amount:,.0f} | {channel_text} | {signal_feedback}

数据采集: {status_text}"""

    try:
        send_markdown(content, cfg)
        print(f"[通知] 收盘汇总推送成功 ({trade_date})")
    except Exception as e:
        print(f"[通知] 收盘汇总推送失败: {e}")
