"""
migrate_date_format.py
一次性迁移脚本：将 market_daily 表中 trade_date 为 YYYYMMDD 格式的历史数据
转换为 YYYY-MM-DD 格式。

用法: python scripts/migrate_date_format.py
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "tmt_alpha.db"


def migrate():
    if not DB_PATH.exists():
        print(f"[错误] 数据库不存在: {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    # 查找所有需要迁移的 market_daily 记录（8位纯数字格式）
    cursor.execute("""
        SELECT DISTINCT trade_date FROM market_daily
        WHERE length(trade_date) = 8 AND trade_date NOT LIKE '%-%'
    """)
    old_dates = [row[0] for row in cursor.fetchall()]

    if not old_dates:
        print("[OK] market_daily 中无需要迁移的日期格式")
    else:
        print(f"[迁移] market_daily: 发现 {len(old_dates)} 个 YYYYMMDD 格式日期")
        for old_date in old_dates:
            new_date = f"{old_date[:4]}-{old_date[4:6]}-{old_date[6:8]}"
            cursor.execute(
                "UPDATE market_daily SET trade_date = ? WHERE trade_date = ?",
                (new_date, old_date)
            )
        print(f"  已迁移 {len(old_dates)} 个日期")

    # 查找所有需要迁移的 fund_nav 记录
    cursor.execute("""
        SELECT DISTINCT trade_date FROM fund_nav
        WHERE length(trade_date) = 8 AND trade_date NOT LIKE '%-%'
    """)
    old_dates_fn = [row[0] for row in cursor.fetchall()]

    if not old_dates_fn:
        print("[OK] fund_nav 中无需要迁移的日期格式")
    else:
        print(f"[迁移] fund_nav: 发现 {len(old_dates_fn)} 个 YYYYMMDD 格式日期")
        for old_date in old_dates_fn:
            new_date = f"{old_date[:4]}-{old_date[4:6]}-{old_date[6:8]}"
            cursor.execute(
                "UPDATE fund_nav SET trade_date = ? WHERE trade_date = ?",
                (new_date, old_date)
            )
        print(f"  已迁移 {len(old_dates_fn)} 个日期")

    conn.commit()

    # 验证
    cursor.execute("""
        SELECT COUNT(*) FROM market_daily
        WHERE length(trade_date) = 8 AND trade_date NOT LIKE '%-%'
    """)
    remaining_md = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM fund_nav
        WHERE length(trade_date) = 8 AND trade_date NOT LIKE '%-%'
    """)
    remaining_fn = cursor.fetchone()[0]

    if remaining_md == 0 and remaining_fn == 0:
        print("\n[OK] 所有日期已统一为 YYYY-MM-DD 格式")
    else:
        print(f"\n[警告] 仍有未迁移记录: market_daily={remaining_md}, fund_nav={remaining_fn}")

    conn.close()


if __name__ == "__main__":
    print("=" * 60)
    print("TMT-Alpha 日期格式迁移工具")
    print("=" * 60)
    migrate()
