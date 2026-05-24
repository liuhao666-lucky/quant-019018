"""
data_pipeline.py
TMT-Alpha 7.0 数据管道 —— 直连 SQLite，供后续量化策略调用。
依赖: requests, pandas, sqlite3 (标准库)
"""

import sqlite3
import time
import re
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ==================== 配置 ====================

DB_PATH = Path(__file__).parent / "tmt_alpha.db"

INDEX_LIST = [
    {"code": "000998", "name": "中证TMT产业主题指数", "col_prefix": "tmt"},
    {"code": "931160", "name": "中证全指通信设备指数", "col_prefix": "ce"},
    {"code": "931494", "name": "中证消费电子主题指数", "col_prefix": "aic"},
    {"code": "H30184", "name": "中证全指半导体产品与设备指数", "col_prefix": "semi"},
    {"code": "000941", "name": "中证内地新能源主题指数", "col_prefix": "ne"},
]

FUND_CODE = "019018"  # 易方达信息产业混合C

HISTORY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
ONEDAY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday"
FUND_URL = "https://fund.sina.com.cn/fund/api/fundDetail"
FUND_NETWORTH_URL = "https://fund.sina.com.cn/fund/api/netWorth"

REQUEST_INTERVAL = 0.6  # 请求间隔（秒）


# ==================== 数据库初始化 ====================

def _get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _normalize_date_str(d: str) -> str:
    """将日期字符串统一为 YYYY-MM-DD 格式"""
    d = str(d).strip()
    if len(d) == 8 and "-" not in d:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def _create_tables(conn: sqlite3.Connection):
    """创建所有表（如不存在）"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_daily (
            trade_date  TEXT,
            index_code  TEXT,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            change      REAL,
            change_pct  REAL,
            volume      REAL,
            amount      REAL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT,
            PRIMARY KEY (trade_date, index_code)
        );

        CREATE TABLE IF NOT EXISTS fund_nav (
            trade_date    TEXT PRIMARY KEY,
            net_value     REAL,
            acc_value     REAL,
            daily_return  REAL,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS guidance (
            report_date  TEXT PRIMARY KEY,
            attitude     INTEGER,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS snapshot_1445 (
            trade_date        TEXT PRIMARY KEY,
            tmt_idx           REAL,
            tmt_chg_pct       REAL,
            tmt_volume        REAL,
            aic_chg_pct       REAL,
            ce_chg_pct        REAL,
            semi_chg_pct      REAL,
            ne_chg_pct        REAL,
            fund_nav_estimated REAL,
            created_at        TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()


def add_timestamps_if_missing(conn: sqlite3.Connection):
    """检测 market_daily 和 fund_nav 表是否缺少时间戳字段，若缺失则 ALTER TABLE 添加"""
    cursor = conn.cursor()

    for table, columns in [
        ("market_daily", ["created_at", "updated_at"]),
        ("fund_nav", ["created_at", "updated_at"]),
        ("guidance", ["created_at"]),
    ]:
        cursor.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cursor.fetchall()}
        for col in columns:
            if col not in existing:
                default = " DEFAULT CURRENT_TIMESTAMP" if col == "created_at" else ""
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT{default}")
                print(f"  [迁移] {table} 添加字段 {col}")

    conn.commit()


# ==================== HTTP Session ====================

def _create_session() -> requests.Session:
    """创建带通用请求头的 Session"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.csindex.com.cn/",
    })
    return session


# ==================== 数据拉取函数 ====================

def fetch_index_history(session: requests.Session, index_code: str,
                        start_date: str, end_date: str) -> list:
    """
    从 csindex 拉取指数历史日线数据。
    start_date / end_date 格式: YYYYMMDD
    返回 list[dict]，字段与接口原始返回一致。
    """
    url = (f"{HISTORY_URL}?indexCode={index_code}"
           f"&startDate={start_date}&endDate={end_date}")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != "200" or not result.get("success"):
        raise Exception(f"接口返回失败: {result.get('msg', '未知错误')}")
    return result.get("data", [])


def fetch_index_latest(session: requests.Session, index_code: str) -> dict:
    """
    从 csindex 拉取指数当日数据（含日内分钟线头部）。
    返回解析后的 dict: trade_date, open, close, prev_close, change_pct, volume, amount
    """
    url = f"{ONEDAY_URL}?indexCode={index_code}"
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise Exception(f"接口返回 success=false: {data}")

    header = data.get("data", {}).get("intraDayHeader")
    if not header:
        raise Exception(f"未找到 intraDayHeader: {data}")

    return {
        "trade_date": header.get("tradeDate", ""),
        "open": header.get("openToday"),
        "close": header.get("current"),
        "prev_close": header.get("closePre"),
        "change_pct": header.get("changePct"),
        "volume": header.get("tradingVol"),
        "amount": header.get("tradingValue"),
    }


def fetch_fund_nav() -> list:
    """
    从新浪接口拉取基金净值历史。
    返回 list[dict]，每个字典包含 date, netval, ljval, val_rate。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://fund.sina.cn",
        "Referer": "https://fund.sina.cn/",
    }
    session = requests.Session()
    session.get("https://fund.sina.com.cn/", headers=headers, timeout=10)

    timestamp_ms = int(time.time() * 1000)
    data = {
        "fundcode": FUND_CODE,
        "type": "1,2,3,4,5",
        "openLoader": "true",
        "_": str(timestamp_ms),
    }
    resp = session.post(FUND_URL, headers=headers, data=data, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    if result.get("code") != 0:
        raise Exception(f"接口返回错误: {result.get('msg')}")

    return result["data"]["market"]["history"]


def fetch_fund_nav_history(fund_code: str = FUND_CODE, years: int = 2) -> list:
    """
    从新浪 netWorth 接口拉取基金历史净值（JSONP 格式）。
    years: 拉取年数，t=7 对应约 2 年。
    返回 list[dict]，字段: trade_date, net_value, acc_value, daily_return
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Referer": "https://fund.sina.cn/",
    }
    session = requests.Session()
    session.get("https://fund.sina.com.cn/", headers=headers, timeout=10)

    ts = int(time.time() * 1000)
    # t 参数控制时间范围: t=7 约 2 年
    t_param = 7 if years >= 2 else (3 if years >= 1 else 1)
    callback = f"jQuery_{ts}"
    url = (f"{FUND_NETWORTH_URL}?callback={callback}"
           f"&fundcode={fund_code}&t={t_param}&_={ts}")

    resp = session.get(url, headers=headers, timeout=15)
    resp.raise_for_status()

    # 解析 JSONP: callback({...})
    match = re.search(r'\((.*)\)', resp.text)
    if not match:
        raise Exception(f"JSONP 解析失败: {resp.text[:200]}")

    result = json.loads(match.group(1))
    if result.get("code") != 0:
        raise Exception(f"接口返回错误: {result.get('msg')}")

    raw = result.get("data", [])
    rows = []
    for item in raw:
        # ENDDATE 格式 YYYYMMDD → YYYY-MM-DD
        d = item.get("ENDDATE", "")
        if len(d) == 8:
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        rows.append({
            "trade_date": d,
            "net_value": float(item.get("UNITNAV", 0)),
            "acc_value": float(item.get("UNITACCNAV", 0)),
            "daily_return": float(item.get("NAVGRTD", 0)),
        })
    return rows


# ==================== 数据存储函数 ====================

def _amount_to_yuan(val) -> float:
    """
    将接口返回的成交金额转换为元。
    - history 接口: 亿元 → 元 (×1e8)
    - oneday 接口: 百万元 → 元 (×1e6)
    这里统一由调用方传入已换算的值，本函数仅做安全转换。
    """
    if val is None:
        return None
    return float(val)


def save_market_daily(rows: list, index_code: str):
    """
    将指数数据写入 market_daily 表（INSERT OR REPLACE）。
    rows 为 list[dict]，字段需包含:
      trade_date, open, high, low, close, change, change_pct, volume, amount
    amount 单位需为元。日期统一为 YYYY-MM-DD。
    """
    conn = _get_conn()
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT OR REPLACE INTO market_daily
            (trade_date, index_code, open, high, low, close,
             change, change_pct, volume, amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM market_daily
                              WHERE trade_date = ? AND index_code = ?), ?),
                    ?)
        """
        records = []
        for r in rows:
            td = _normalize_date_str(r.get("trade_date", ""))
            records.append((
                td,
                index_code,
                r.get("open"),
                r.get("high"),
                r.get("low"),
                r.get("close"),
                r.get("change"),
                r.get("change_pct"),
                r.get("volume"),
                r.get("amount"),
                td, index_code, now_str,  # COALESCE 参数
                now_str,                   # updated_at
            ))
        conn.executemany(sql, records)
        conn.commit()
        print(f"  写入 market_daily: {len(records)} 条 ({index_code})")
    finally:
        conn.close()


def save_fund_nav(rows: list):
    """
    将基金净值数据写入 fund_nav 表（INSERT OR REPLACE）。
    rows 为 list[dict]，字段: trade_date, net_value, acc_value, daily_return
    日期统一为 YYYY-MM-DD。
    """
    conn = _get_conn()
    try:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT OR REPLACE INTO fund_nav
            (trade_date, net_value, acc_value, daily_return, created_at, updated_at)
            VALUES (?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM fund_nav WHERE trade_date = ?), ?),
                    ?)
        """
        records = []
        for r in rows:
            td = _normalize_date_str(r.get("trade_date", ""))
            records.append((
                td,
                r.get("net_value"),
                r.get("acc_value"),
                r.get("daily_return"),
                td, now_str,  # COALESCE 参数
                now_str,      # updated_at
            ))
        conn.executemany(sql, records)
        conn.commit()
        print(f"  写入 fund_nav: {len(records)} 条")
    finally:
        conn.close()


def save_snapshot_1445(data: dict):
    """
    将 14:45 盘中快照写入 snapshot_1445 表（INSERT OR REPLACE）。
    data 字段: trade_date, tmt_idx, tmt_chg_pct, tmt_volume,
               aic_chg_pct, ce_chg_pct, semi_chg_pct, ne_chg_pct, fund_nav_estimated
    日期统一为 YYYY-MM-DD。
    """
    conn = _get_conn()
    try:
        td = _normalize_date_str(data.get("trade_date", ""))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = """
            INSERT OR REPLACE INTO snapshot_1445
            (trade_date, tmt_idx, tmt_chg_pct, tmt_volume,
             aic_chg_pct, ce_chg_pct, semi_chg_pct, ne_chg_pct,
             fund_nav_estimated, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        conn.execute(sql, (
            td,
            data.get("tmt_idx"),
            data.get("tmt_chg_pct"),
            data.get("tmt_volume"),
            data.get("aic_chg_pct"),
            data.get("ce_chg_pct"),
            data.get("semi_chg_pct"),
            data.get("ne_chg_pct"),
            data.get("fund_nav_estimated"),
            now_str,
        ))
        conn.commit()
        print(f"  写入 snapshot_1445: {td}")
    finally:
        conn.close()


def load_snapshot_1445(trade_date: str = None) -> pd.DataFrame:
    """
    从 snapshot_1445 表读取快照数据。
    trade_date: 指定日期（YYYY-MM-DD），为 None 时读取全部。
    """
    conn = _get_conn()
    try:
        if trade_date:
            td = _normalize_date_str(trade_date)
            df = pd.read_sql(
                "SELECT * FROM snapshot_1445 WHERE trade_date = ?",
                conn, params=[td]
            )
        else:
            df = pd.read_sql("SELECT * FROM snapshot_1445", conn)
        if not df.empty:
            df["trade_date"] = df["trade_date"].apply(_normalize_date_str)
        return df
    finally:
        conn.close()


# ==================== 辅助：历史数据格式转换 ====================

def _parse_history_rows(raw_data: list) -> list:
    """将 history 接口原始 data 转换为统一字段 dict list"""
    rows = []
    for item in raw_data:
        amount_raw = item.get("tradingValue")
        # history 接口 amount 单位为亿元，转换为元
        amount_yuan = float(amount_raw) * 1e8 if amount_raw is not None else None

        rows.append({
            "trade_date": _normalize_date_str(item.get("tradeDate", "")),
            "open": item.get("open"),
            "high": item.get("high"),
            "low": item.get("low"),
            "close": item.get("close"),
            "change": item.get("change"),
            "change_pct": item.get("changePct"),
            "volume": item.get("tradingVol"),
            "amount": amount_yuan,
        })
    return rows


def _parse_latest_row(row: dict) -> list:
    """将 oneday 接口返回的单日 dict 包装为 list，并转换 amount 单位（百万元→元）"""
    amount_raw = row.get("amount")
    # oneday 接口 amount 单位为百万元，转换为元
    amount_yuan = float(amount_raw) * 1e6 if amount_raw is not None else None

    parsed = {
        "trade_date": _normalize_date_str(row.get("trade_date", "")),
        "open": row.get("open"),
        "high": None,   # oneday 接口不返回 high/low/change
        "low": None,
        "close": row.get("close"),
        "change": None,
        "change_pct": row.get("change_pct"),
        "volume": row.get("volume"),
        "amount": amount_yuan,
    }
    return [parsed]


def _parse_fund_rows(raw_data: list) -> list:
    """将新浪基金接口原始 history 转换为统一字段 dict list"""
    rows = []
    for item in raw_data:
        rows.append({
            "trade_date": item["date"],
            "net_value": item["netval"],
            "acc_value": item["ljval"],
            "daily_return": item["val_rate"],
        })
    return rows


# ==================== 一键初始化 ====================

def initialize_database():
    """
    一键初始化数据库：
    1. 创建所有表
    2. 拉取五个指数过去 365 天历史数据
    3. 拉取基金全部历史净值
    """
    print("=" * 60)
    print("TMT-Alpha 数据库初始化")
    print("=" * 60)

    conn = _get_conn()
    _create_tables(conn)
    add_timestamps_if_missing(conn)
    conn.close()
    print("[OK] 数据表已创建\n")

    # 计算日期范围
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
    print(f"指数历史数据范围: {start_date} ~ {end_date}\n")

    session = _create_session()

    # 尝试访问首页获取 Cookie
    try:
        session.get("https://www.csindex.com.cn/", timeout=10)
    except Exception:
        pass

    # 拉取五个指数历史数据
    for idx in INDEX_LIST:
        code = idx["code"]
        name = idx["name"]
        print(f"正在拉取 {code} ({name}) 历史数据…")
        try:
            raw = fetch_index_history(session, code, start_date, end_date)
            rows = _parse_history_rows(raw)
            save_market_daily(rows, code)
        except Exception as e:
            print(f"  失败，重试一次… ({e})")
            time.sleep(2)
            try:
                raw = fetch_index_history(session, code, start_date, end_date)
                rows = _parse_history_rows(raw)
                save_market_daily(rows, code)
            except Exception as e2:
                print(f"  重试仍失败: {e2}")
        time.sleep(REQUEST_INTERVAL)

    print()

    # 拉取基金净值（使用 netWorth 接口，拉取约 2 年历史）
    print(f"正在拉取基金 {FUND_CODE} 历史净值（netWorth 接口）…")
    try:
        rows = fetch_fund_nav_history()
        save_fund_nav(rows)
    except Exception as e:
        print(f"  失败，重试一次… ({e})")
        time.sleep(2)
        try:
            rows = fetch_fund_nav_history()
            save_fund_nav(rows)
        except Exception as e2:
            print(f"  重试仍失败: {e2}")

    print("\n初始化完成。")


# ==================== 每日增量更新 ====================

def daily_update():
    """
    每日增量更新：拉取五个指数当日数据 + 基金最新净值。
    建议在 14:50 之后调用。
    """
    print("=" * 60)
    print(f"每日增量更新  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 确保时间戳字段存在
    conn = _get_conn()
    _create_tables(conn)
    add_timestamps_if_missing(conn)
    conn.close()

    session = _create_session()
    try:
        session.get("https://www.csindex.com.cn/", timeout=10)
    except Exception:
        pass

    # 拉取五个指数当日数据
    for idx in INDEX_LIST:
        code = idx["code"]
        name = idx["name"]
        print(f"拉取 {code} ({name}) 当日数据…")
        try:
            row = fetch_index_latest(session, code)
            rows = _parse_latest_row(row)
            save_market_daily(rows, code)
        except Exception as e:
            print(f"  失败: {e}")
        time.sleep(REQUEST_INTERVAL)

    print()

    # 拉取基金最新净值（netWorth 接口，拉取全部历史并增量写入）
    print(f"拉取基金 {FUND_CODE} 最新净值…")
    try:
        rows = fetch_fund_nav_history()
        save_fund_nav(rows)
    except Exception as e:
        print(f"  失败: {e}")

    print("\n每日更新完成。")


# ==================== 数据加载 ====================

# 列映射: 指数 col_prefix -> 该指数在 DataFrame 中的列名前缀
_INDEX_COL_MAP = {idx["code"]: idx["col_prefix"] for idx in INDEX_LIST}


def load_merged_data() -> pd.DataFrame:
    """
    从 SQLite 读取所有数据，合并为一个 DataFrame。
    列: trade_date, tmt_open, tmt_high, tmt_low, tmt_close, tmt_change_pct,
        tmt_volume, tmt_amount, aic_close, ce_close, semi_close, ne_close,
        fund_nav, fund_daily_return
    """
    conn = _get_conn()

    # 读取 market_daily
    md = pd.read_sql("SELECT * FROM market_daily", conn)
    # 读取 fund_nav
    fn = pd.read_sql("SELECT * FROM fund_nav", conn)
    conn.close()

    if md.empty:
        print("[警告] market_daily 表为空")
        return pd.DataFrame()

    # 统一日期格式为 YYYY-MM-DD（兼容 20250524 和 2025-05-24 两种格式）
    md["trade_date"] = md["trade_date"].apply(_normalize_date_str)
    if not fn.empty:
        fn["trade_date"] = fn["trade_date"].apply(_normalize_date_str)

    # 以 000998 (TMT) 为主表，构建基础 DataFrame
    tmt_code = "000998"
    tmt_df = md[md["index_code"] == tmt_code].copy()
    tmt_df = tmt_df.rename(columns={
        "open": "tmt_open",
        "high": "tmt_high",
        "low": "tmt_low",
        "close": "tmt_close",
        "change_pct": "tmt_change_pct",
        "volume": "tmt_volume",
        "amount": "tmt_amount",
    })
    tmt_df = tmt_df[["trade_date", "tmt_open", "tmt_high", "tmt_low",
                      "tmt_close", "tmt_change_pct", "tmt_volume", "tmt_amount"]]

    # 合并其他四个指数的 close 列
    for idx in INDEX_LIST:
        code = idx["code"]
        prefix = idx["col_prefix"]
        if code == tmt_code:
            continue
        sub = md[md["index_code"] == code][["trade_date", "close"]].rename(
            columns={"close": f"{prefix}_close"}
        )
        tmt_df = tmt_df.merge(sub, on="trade_date", how="left")

    # 合并基金净值，重命名列以统一命名
    if not fn.empty:
        fn = fn.rename(columns={
            "net_value": "fund_nav",
            "acc_value": "fund_acc_value",
            "daily_return": "fund_daily_return",
        })
        tmt_df = tmt_df.merge(fn, on="trade_date", how="left")

    tmt_df = tmt_df.sort_values("trade_date").reset_index(drop=True)

    if len(tmt_df) < 300:
        print(f"[警告] 数据行数仅 {len(tmt_df)} 条（<300），请检查数据完整性。")

    return tmt_df


# ==================== 数据校验 ====================

def validate_data(df: pd.DataFrame):
    """
    校验 DataFrame：
    - 检查必要列是否存在
    - 检查收盘价 > 0
    - 检查全空列
    - 打印数据摘要
    """
    print("\n" + "=" * 60)
    print("数据校验")
    print("=" * 60)

    if df.empty:
        print("[错误] DataFrame 为空，无法校验。")
        return

    # 必要列
    required_cols = [
        "trade_date", "tmt_close", "aic_close", "ce_close",
        "semi_close", "ne_close", "fund_nav",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        print(f"[错误] 缺少必要列: {missing}")
    else:
        print("[OK] 所有必要列均存在")

    # 收盘价 > 0
    close_cols = [c for c in df.columns if c.endswith("_close")]
    for col in close_cols:
        invalid = df[col].dropna()
        invalid = (invalid <= 0).sum()
        if invalid > 0:
            print(f"[警告] {col} 存在 {invalid} 条 <=0 的记录")

    # 全空列
    all_null = [c for c in df.columns if df[c].isna().all()]
    if all_null:
        print(f"[警告] 以下列全部为空: {all_null}")
    else:
        print("[OK] 无全空列")

    # 数据摘要
    print(f"\n起止日期: {df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]}")
    print(f"总行数:   {len(df)}")
    print("\n各列有效值数量:")
    for col in df.columns:
        if col == "trade_date":
            continue
        valid = df[col].notna().sum()
        print(f"  {col:20s}  {valid:>6d} / {len(df)}")

    print("=" * 60)


# ==================== 入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "update":
        daily_update()
    elif len(sys.argv) > 1 and sys.argv[1] == "init":
        initialize_database()
    elif len(sys.argv) > 1 and sys.argv[1] == "load":
        df = load_merged_data()
        validate_data(df)
        print(df.tail())
    elif len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        df = load_snapshot_1445()
        if df.empty:
            print("snapshot_1445 表为空")
        else:
            print(f"snapshot_1445 共 {len(df)} 条记录:")
            print(df.tail(10))
    else:
        print("用法:")
        print("  python data_pipeline.py init       # 首次初始化（建表+拉取历史）")
        print("  python data_pipeline.py update     # 每日增量更新")
        print("  python data_pipeline.py load       # 加载数据并校验")
        print("  python data_pipeline.py snapshot   # 查看 14:45 快照数据")
