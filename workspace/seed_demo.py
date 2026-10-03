"""命令行工具：初始化/重置演示数据。

用法：
  python3 seed_demo.py            # 空库时写入演示数据（幂等）
  python3 seed_demo.py --reset    # 清空并重建演示数据
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

from db import DB          # noqa: E402
from seed import seed      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空并重建演示数据")
    ap.add_argument("--db", default=os.environ.get(
        "EVAC_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "evac.db")))
    args = ap.parse_args()

    db = DB(args.db)
    seed(db, reset=args.reset)
    db.set_meta("seeded", "1")
    if args.reset:
        db.bump_revision()
        db.log_event("reset", "{}")
        db.commit()
    print(f"演示数据已写入 {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
