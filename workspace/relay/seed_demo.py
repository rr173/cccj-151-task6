"""离线车队回执中继 —— 演示数据播种（幂等，可重复执行）。

播种 3 个不可变批次包，制造各种可观察状态，便于直接体验页面：
  * DEMO-A：完整转发若干条（若适配器为 stub 默认 ack）
  * DEMO-B：乱序导入（1、3），页面可见缺口 [2] 与缓冲项
  * DEMO-C：不导入任何回执（供终端下载演示）

用法：
  python3 relay/seed_demo.py
  python3 relay/seed_demo.py --reset      # 删除中继库后重新播种
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "backend"))

import core  # noqa: E402
from db import RelayDB  # noqa: E402

DB_PATH = os.environ.get(
    "RELAY_DB",
    os.path.join(HERE, "data", "relay.db"))


def pkg(ref, wave, vehicle, dest, households, summary):
    return {
        "batch_ref": ref,
        "package_id": f"PKG-{ref}",
        "batch_summary": summary,
        "seq_base": 1,
        "issued_at": "2026-10-02T07:30:00Z",
        "source": "seed_demo",
        "wave": wave,
        "vehicle": {"code": vehicle, "seats": 12},
        "destination": {"code": dest},
        "items": [{"household_ref": h, "members": 2} for h in households],
    }


def rcpt(batch_ref, seq, no, rtype, summary, at="2026-10-02T08:00:00Z"):
    return {"batch_ref": batch_ref, "seq": seq, "receipt_no": no, "type": rtype,
            "batch_summary": summary, "at": at}


PACKAGES = [
    pkg("DEMO-A", 1, "V-01", "S1", ["H-1", "H-2", "H-3"],
        "演示A · 波次1 · V-01 · G1 → S1（3户）"),
    pkg("DEMO-B", 1, "V-02", "S2", ["H-4", "H-5"],
        "演示B · 波次1 · V-02 · G2 → S2（2户）"),
    pkg("DEMO-C", 2, "V-03", "S1", ["H-6"],
        "演示C · 波次2 · V-03 · G3 → S1（1户）"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="删除现有中继库后播种")
    args = ap.parse_args()

    if args.reset and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(DB_PATH + suffix)
            except OSError:
                pass

    db = RelayDB(DB_PATH)
    with db.lock:
        for p in PACKAGES:
            try:
                r = core.import_package(db, p, actor="seed", source="seed_demo")
                print(f"  导入 {p['batch_ref']}: {'已存在(幂等)' if r['replay'] else '新增'}")
            except core.RelayError as e:
                print(f"  跳过 {p['batch_ref']}: {e.message}")

        summary_a = PACKAGES[0]["batch_summary"]
        summary_b = PACKAGES[1]["batch_summary"]
        # A：完整 1..3
        for i, t in enumerate(("departed", "failed_to_depart", "arrived"), start=1):
            try:
                core.ingest_receipt(
                    db, rcpt("DEMO-A", i, f"DEMO-A-R{i}", t, summary_a,
                             f"2026-10-02T0{8 + i}:00:00Z"), actor="seed")
            except core.RelayError as e:
                print(f"  A 回执 {i} 已存在：{e.message}")
        # B：只有 1 和 3 → 缺口 2 持久缓冲
        for seq, no, t in ((1, "DEMO-B-R1", "departed"),
                           (3, "DEMO-B-R3", "arrived")):
            try:
                core.ingest_receipt(db, rcpt("DEMO-B", seq, no, t, summary_b),
                                    actor="seed")
            except core.RelayError as e:
                print(f"  B 回执 {seq} 已存在：{e.message}")
        db.commit()
    print(f"演示数据就绪：{os.path.abspath(DB_PATH)}")
    print("打开 http://localhost:8090/relay/ 查看；"
          "补传 DEMO-B 序号 2 可观察缺口补齐后连续转发。")


if __name__ == "__main__":
    main()
