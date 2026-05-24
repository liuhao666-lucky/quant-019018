"""
notifier.py
企业微信机器人 webhook 推送模块。
通过环境变量 WECHAT_WEBHOOK_URL 获取 webhook 地址。
"""

import os
import json
import requests

from core.config_loader import load_config


def _get_webhook_url(cfg: dict = None) -> str:
    """从配置或环境变量获取企业微信 webhook URL"""
    if cfg is None:
        cfg = load_config()

    url = cfg.get("wechat", {}).get("webhook_url", "") or os.environ.get("WECHAT_WEBHOOK_URL", "")
    if not url:
        raise ValueError(
            "企业微信 webhook URL 未配置。请在 config.yaml 中设置 wechat.webhook_url，或设置环境变量 WECHAT_WEBHOOK_URL。"
        )
    return url


def send_markdown(content: str, cfg: dict = None):
    """
    发送 markdown 格式消息到企业微信群。
    content: markdown 文本（最长 4096 字节）
    """
    url = _get_webhook_url(cfg)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content,
        },
    }
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("errcode") != 0:
        print(f"[警告] 企微推送返回异常: {result}")
    return result


def send_signal_notification(signal_dict: dict, cfg: dict = None):
    """
    将策略信号推送至企业微信。
    signal_dict 包含:
      - trade_date: 日期
      - mkt_chg: 法定主锚涨跌幅 (%)
      - score_eff: 有效得分
      - action_ratio: 惩罚系数
      - final_multiplier: 综合乘数
      - channel: 通道类型 (A/B/C/D/无)
      - amount: 建议操作金额
      - excess_dd: 超额回撤
      - warning: 是否触发预警
      - force_reduce: 是否触发强平
      - action: 操作建议 (buy/sell/hold)
    """
    trade_date = signal_dict.get("trade_date", "")
    mkt_chg = signal_dict.get("mkt_chg", 0)
    score_eff = signal_dict.get("score_eff", 0)
    action_ratio = signal_dict.get("action_ratio", 1.0)
    final_mult = signal_dict.get("final_multiplier", 1.0)
    channel = signal_dict.get("channel", "-")
    amount = signal_dict.get("amount", 0)
    excess_dd = signal_dict.get("excess_dd", 0)
    if excess_dd is None or (isinstance(excess_dd, float) and (excess_dd != excess_dd)):
        excess_dd = 0
    warning = signal_dict.get("warning", False)
    force_reduce = signal_dict.get("force_reduce", False)
    action = signal_dict.get("action", "hold")

    # 操作动作映射
    action_map = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "⚪ 观望"}
    action_text = action_map.get(action, action)

    # 预警/熔断标记
    alert_lines = []
    if warning:
        alert_lines.append("> ⚠️ **超额回撤预警已触发**")
    if force_reduce:
        alert_lines.append("> 🚨 **强制平仓已触发**")
    alert_section = "\n".join(alert_lines)

    # 通道颜色标记
    channel_emoji = {"A": "🟢A", "B": "🟡B", "C": "🟠C", "D": "🔴D"}
    channel_text = channel_emoji.get(channel, channel)
    channel_desc = {
        "A": "积极进攻，信号强、风控绿灯",
        "B": "标准执行，正常节奏操作",
        "C": "谨慎试探，有追高风险",
        "D": "防守模式，触发风控或止跌",
    }.get(channel, "")

    # 预警/熔断标记
    alert_lines = []
    if warning:
        alert_lines.append("> ⚠️ **超额回撤预警已触发**：基金持续跑输基准，新买入信号力度打折")
    if force_reduce:
        alert_lines.append("> 🚨 **强制平仓已触发**：超额回撤触及硬止损线，必须减仓")
    alert_section = "\n".join(alert_lines)

    content = f"""## 📊 TMT-Alpha 7.0 每日信号

**日期**: {trade_date}

| 指标 | 数值 | 白话解释 |
|---|---|---|
| Mkt_Chg (主锚涨跌幅) | {mkt_chg:+.2f}% | 基准今天表现，正数=大盘在涨 |
| Score_eff (有效得分) | {score_eff:.1f} | 信号强弱，越高越倾向买入 |
| Action_Ratio (惩罚系数) | {action_ratio:.2f} | 风控打折，<1 说明模型在主动降仓位 |
| Final_Multiplier (综合乘数) | {final_mult:.2f} | 趋势加成，>1 顺势加码，<1 逆势减码 |
| Excess_DD (超额回撤) | {excess_dd:.2%} | 基金跑输基准的幅度，越负越危险 |

**执行通道**: {channel_text}
> {channel_desc}

**建议操作**: {action_text}
**建议金额**: ¥{amount:,.0f}

{alert_section}"""

    try:
        send_markdown(content, cfg)
        print(f"[通知] 企微推送成功 ({trade_date})")
    except Exception as e:
        print(f"[通知] 企微推送失败: {e}")


def send_closing_summary(summary: dict, cfg: dict = None):
    """
    发送 23:30 收盘汇总到企业微信。
    summary 包含:
      - trade_date: 日期
      - tmt_close_chg: TMT 收盘涨跌幅 (%)
      - tmt_intraday_chg: TMT 14:45 盘中涨跌幅 (%)
      - fund_chg: 基金日收益率 (%)
      - alpha_daily: 单日超额 (%)
      - excess_dd: 更新后超额回撤
      - signal_action: 14:45 信号操作 (buy/sell/hold)
      - signal_channel: 14:45 执行通道
      - signal_amount: 14:45 建议金额
      - data_ok: 数据采集是否成功
    """
    trade_date = summary.get("trade_date", "")
    tmt_close_chg = summary.get("tmt_close_chg", 0)
    tmt_intraday_chg = summary.get("tmt_intraday_chg")
    fund_chg = summary.get("fund_chg", 0)
    alpha_daily = summary.get("alpha_daily", 0)
    excess_dd = summary.get("excess_dd", 0)
    if excess_dd is None or (isinstance(excess_dd, float) and (excess_dd != excess_dd)):
        excess_dd = 0
    signal_action = summary.get("signal_action", "-")
    signal_channel = summary.get("signal_channel", "-")
    signal_amount = summary.get("signal_amount", 0)
    data_ok = summary.get("data_ok", True)

    # 操作映射
    action_map = {"buy": "🟢 买入", "sell": "🔴 卖出", "hold": "⚪ 观望"}
    action_text = action_map.get(signal_action, signal_action)
    channel_emoji = {"A": "🟢A", "B": "🟡B", "C": "🟠C", "D": "🔴D"}
    channel_text = channel_emoji.get(signal_channel, signal_channel)

    # 盘中 → 收盘 TMT 走势
    if tmt_intraday_chg is not None:
        drift = tmt_close_chg - tmt_intraday_chg
        drift_str = f"{drift:+.2f}%"
        drift_note = "尾盘拉升" if drift > 0.5 else ("尾盘跳水" if drift < -0.5 else "窄幅震荡")
    else:
        drift_str = "无快照数据"
        drift_note = ""

    # 超额与风控白话
    if excess_dd > -0.02:
        dd_level = "🟢 安全区，基金跑赢或微幅跑输"
    elif excess_dd > -0.05:
        dd_level = "🟡 注意区，超额回撤有所扩大"
    elif excess_dd > -0.10:
        dd_level = "🟠 警戒区，接近预警线，密切关注"
    else:
        dd_level = "🔴 危险区，已触发/接近风控线"

    # 信号复盘白话
    signal_feedback = ""
    if signal_action == "buy":
        signal_feedback = "模型认为今天是加仓时机"
    elif signal_action == "sell":
        signal_feedback = "模型触发了卖出或减仓信号"
    else:
        signal_feedback = "模型选择按兵不动"

    # 数据状态
    status_text = "✅ 正常" if data_ok else "⚠️ 部分失败"

    content = f"""## 📈 TMT-Alpha 7.0 收盘汇总

**日期**: {trade_date}

### 今日市场
| 指标 | 数值 | 白话解释 |
|---|---|---|
| TMT 收盘涨跌幅 | {tmt_close_chg:+.2f}% | 基准指数全天实际涨跌 |
| TMT 盘中 (14:45) | {tmt_intraday_chg:+.2f}% | 发信号时的盘中涨跌 |
| 尾盘变动 | {drift_str} {drift_note} | 14:45→收盘的差值 |
| 基金日收益 | {fund_chg:+.2f}% | 基金今天实际涨了/跌了多少 |
| 单日超额 | {alpha_daily:+.2f}% | 基金vs基准，正数=今天跑赢了 |
| 超额回撤 (更新后) | {excess_dd:.2%} | 累计跑输幅度，已包含今日 |

> {dd_level}

### 今日信号回顾 (14:45)
| 指标 | 数值 | 白话解释 |
|---|---|---|
| 操作建议 | {action_text} | {signal_feedback} |
| 执行通道 | {channel_text} | A积极→D防守 |
| 建议金额 | ¥{signal_amount:,.0f} | 模型建议的操作金额 |

数据采集: {status_text}"""

    try:
        send_markdown(content, cfg)
        print(f"[通知] 收盘汇总推送成功 ({trade_date})")
    except Exception as e:
        print(f"[通知] 收盘汇总推送失败: {e}")
