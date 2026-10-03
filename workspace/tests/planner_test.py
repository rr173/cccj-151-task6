"""规划器单元测试：直接在临时数据库上构造极端态势，验证诊断与约束。

运行：python3 tests/planner_test.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from db import DB          # noqa: E402
from planner import Snapshot, plan, validate_plan  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, detail)


def make_db():
    db = DB(os.path.join(tempfile.mkdtemp(prefix="evac_unit_"), "u.db"))
    c = db.conn
    c.executemany("INSERT INTO grids(code,name,x,y,threatened) VALUES(?,?,?,?,1)",
                  [("G1", "村", 0, 0), ("G2", "村2", 1, 0)])
    c.executemany("INSERT INTO shelters(code,name,x,y,beds,accessible,pets_allowed,status)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  [("S1", "点A", 2, 0, 5, 0, 1, "active"),
                   ("S2", "点B", 2, 1, 2, 1, 0, "active")])
    c.executemany("INSERT INTO roads(code,a,b,length,capacity,status) VALUES(?,?,?,?,'1',?)",
                  [("r1", "G1", "G2", 1, "open"),
                   ("r2", "G2", "S1", 1, "open"),
                   ("r3", "G2", "S2", 1, "open")])
    c.executemany("INSERT INTO vehicles(code,name,seats,accessible,status) VALUES(?,?,?,?,'available')",
                  [("V1", "普车", 10, 0), ("V2", "无障碍", 4, 1)])
    db.commit()
    return db


def add_hh(db, ref, grid, members, mobility=0, pets=0):
    db.conn.execute(
        "INSERT INTO households(ref,name,grid,members,mobility,pets,contact) VALUES(?,?,?,?,?,?,?)",
        (ref, ref, grid, members, mobility, pets, ""))
    db.commit()


def main():
    print("【A】封闭唯一通道 → 不可行且诊断指明道路")
    db = make_db()
    add_hh(db, "H1", "G1", 3)
    db.conn.execute("UPDATE roads SET status='closed' WHERE code IN ('r1')")
    db.commit()
    p = plan(Snapshot(db))
    check("方案不可行", not p["feasible"])
    check("诊断包含道路关闭原因",
          any("道路" in r for u in p["unassigned"] for r in u["reasons"]),
          p["unassigned"])

    print("【B】行动辅助户 + 禁宠点 → 只能去 S2；床位超出时诊断指向床位")
    db = make_db()
    add_hh(db, "H2", "G1", 2, mobility=1)      # 只能 S2（S1 无无障碍）
    add_hh(db, "H3", "G1", 1, mobility=1)      # 只能 S2，床位 2 正好
    add_hh(db, "H4", "G1", 1, mobility=1)      # 第 3 个无障碍户，S2 仅 2 床
    p = plan(Snapshot(db))
    check("床位瓶颈时不可行", not p["feasible"])
    u4 = next((u for u in p["unassigned"] if u["ref"] == "H4"), None)
    check("诊断明确指出床位不足", u4 is not None and any("床位" in r for r in u4["reasons"]),
          u4)

    print("【C】宠物户只能进允许宠物的 S1")
    db = make_db()
    add_hh(db, "H5", "G1", 2, pets=2)
    p = plan(Snapshot(db))
    check("可行", p["feasible"])
    check("宠物户安置在 S1", all(b["shelter"] == "S1" for b in p["batches"]
                                 if "H5" in [x["ref"] for x in b["households"]]))

    print("【D】道路单批次容量=1：两车不能同波次共用该边")
    db = make_db()
    add_hh(db, "H6", "G1", 1)
    add_hh(db, "H7", "G2", 1)
    # 两车都要过 r2，容量 1 → 必须分两个波次
    db.conn.execute("UPDATE roads SET status='closed' WHERE code='r3'")
    db.commit()
    p = plan(Snapshot(db))
    check("可行（分到不同波次）", p["feasible"])
    r2_waves = {b["wave"] for b in p["batches"] if "r2" in b["route_edges"]}
    check("经过 r2 的批次分布在 2 个波次", len(r2_waves) == 2, p["batches"])
    check("校验器通过", validate_plan(Snapshot(db), p["batches"]) == [])

    print("【E】已出发家庭与床位不参与重算、也不可被重新分配")
    db = make_db()
    add_hh(db, "H8", "G1", 2)
    add_hh(db, "H9", "G1", 2)
    # 手工制造一条已出发批次
    db.conn.execute("INSERT INTO batches(id,order_id,seq,wave,vehicle_code,grid,shelter_code,"
                    "route,status,departed_at) VALUES('b1','o1',1,1,'V1','G1','S1',"
                    "'{\"edges\":[\"r1\",\"r2\"],\"nodes\":[\"G1\",\"G2\",\"S1\"]}',"
                    "'departed',1.0)")
    db.conn.execute("INSERT INTO assignments(batch_id,household_ref) VALUES('b1','H8')")
    db.conn.execute("UPDATE vehicles SET status='departed' WHERE code='V1'")
    db.commit()
    snap = Snapshot(db)
    check("已出发户不在待规划集合", "H8" not in {h["ref"] for h in snap.to_plan})
    check("S1 已承诺 2 床", snap.committed_beds["S1"] == 2)
    check("V1 已锁定", "V1" in snap.locked_vehicles)
    p = plan(snap)
    check("重算只安置 H9",
          [x["ref"] for b in p["batches"] for x in b["households"]] == ["H9"])
    # 伪造一个把 H8 重新分配的方案，校验器必须拒绝
    fake = [dict(seq=1, wave=1, vehicle="V2", grid="G1", shelter="S1",
                 route_edges=["r1", "r2"],
                 households=[{"ref": "H8"}, {"ref": "H9"}])]
    errs = validate_plan(Snapshot(db), fake)
    check("校验器拒绝重派已出发家庭", any("已出发" in e for e in errs), errs)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
