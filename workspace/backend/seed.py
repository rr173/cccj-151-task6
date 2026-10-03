"""演示场景种子数据（重复执行幂等：按主键 upsert）。

布局（x,y 为地图坐标）：

  [G1 云顶村北]---r1---[G2 云顶村南]---r3---[G3 溪口]---r5---[S1 云顶体育馆]
        |                  |                   |          (80床/无障碍/可宠)
       r6                 r2                  r4
        |                  |                   |
  [G4 松坡]---r7---[G5 老林场]---r8---[G6 河岸]---r9---[S2 西山学校]
        \                              |              (40床/不可宠)
         r10---------------------------+---r11------->[S3 湖滨营地]
                                                        (30床/无障碍)
                                                                   [S4 镇礼堂]
                                                                    (20床/暂停接收演示)
道路 r2 为容量瓶颈（单批次 1 车）；r10 为备用通道。
"""
import time


def seed(db, reset=False):
    with db.lock:
        c = db.conn
        if reset:
            for t in ("assignments", "batches", "orders", "plans", "event_log",
                      "households", "vehicles", "roads", "shelters", "grids"):
                c.execute(f"DELETE FROM {t}")
            # 分支沙盒是独立生命周期：重置实战时一并清空分支元数据、审计与幂等凭证
            for t in ("sim_ops", "branch_audit", "branches"):
                c.execute(f"DELETE FROM {t}")
            c.execute("UPDATE meta SET value='0' WHERE key='revision'")

        grids = [
            ("G1", "云顶村北", 1.0, 3.0, 1),
            ("G2", "云顶村南", 3.2, 3.0, 1),
            ("G3", "溪口", 5.6, 3.0, 1),
            ("G4", "松坡", 1.0, 1.2, 0),
            ("G5", "老林场", 3.4, 1.2, 1),
            ("G6", "河岸", 5.6, 1.2, 1),
        ]
        c.executemany(
            "INSERT INTO grids(code,name,x,y,threatened) VALUES(?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name,x=excluded.x,y=excluded.y",
            grids,
        )

        shelters = [
            ("S1", "云顶体育馆", 8.4, 3.0, 80, 1, 1, "active"),
            ("S2", "西山学校",   8.4, 1.2, 40, 1, 0, "active"),
            ("S3", "湖滨营地",   9.6, 2.1, 30, 1, 1, "active"),
            ("S4", "镇礼堂",     7.2, 4.3, 20, 0, 1, "suspended"),
        ]
        c.executemany(
            "INSERT INTO shelters(code,name,x,y,beds,accessible,pets_allowed,status)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name",
            shelters,
        )

        roads = [
            ("r1", "G1", "G2", 2.2, 2, "open"),
            ("r2", "G2", "G5", 1.9, 1, "open"),   # 瓶颈
            ("r3", "G2", "G3", 2.4, 2, "open"),
            ("r4", "G3", "G6", 1.8, 2, "open"),
            ("r5", "G3", "S1", 2.8, 2, "open"),
            ("r6", "G1", "G4", 1.8, 2, "open"),
            ("r7", "G4", "G5", 2.4, 2, "open"),
            ("r8", "G5", "G6", 2.2, 2, "open"),
            ("r9", "G6", "S2", 2.8, 2, "open"),
            ("r10", "G4", "G6", 4.8, 1, "open"),  # 备用通道
            ("r11", "G6", "S3", 4.2, 2, "open"),
        ]
        c.executemany(
            "INSERT INTO roads(code,a,b,length,capacity,status) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET a=excluded.a,b=excluded.b,"
            "length=excluded.length,capacity=excluded.capacity",
            roads,
        )

        vehicles = [
            ("V1", "大巴-01", 15, 0),
            ("V2", "大巴-02", 12, 0),
            ("V3", "中巴-03", 9, 1),
            ("V4", "无障碍小巴-04", 6, 1),
            ("V5", "小巴-05", 5, 0),
        ]
        c.executemany(
            "INSERT INTO vehicles(code,name,seats,accessible,status)"
            " VALUES(?,?,?,?,'available') ON CONFLICT(code) DO UPDATE SET"
            " name=excluded.name,seats=excluded.seats,accessible=excluded.accessible",
            vehicles,
        )

        # ref, 姓名, 网格, 人数, 行动辅助, 宠物, 联络
        households = [
            ("H01", "陈家", "G1", 4, 0, 1, "138-0001"),
            ("H02", "林家", "G1", 3, 1, 0, "138-0002"),
            ("H03", "黄家", "G1", 2, 0, 0, "138-0003"),
            ("H04", "吴家", "G2", 5, 0, 2, "138-0004"),
            ("H05", "徐家", "G2", 2, 1, 0, "138-0005"),
            ("H06", "马家", "G2", 3, 0, 0, "138-0006"),
            ("H07", "赵家", "G3", 4, 0, 0, "138-0007"),
            ("H08", "孙家", "G5", 6, 0, 1, "138-0008"),
            ("H09", "李家", "G5", 2, 0, 0, "138-0009"),
            ("H10", "周家", "G6", 4, 1, 0, "138-0010"),
            ("H11", "郑家", "G6", 3, 0, 0, "138-0011"),
        ]
        c.executemany(
            "INSERT OR IGNORE INTO households(ref,name,grid,members,mobility,pets,contact)"
            " VALUES(?,?,?,?,?,?,?)",
            households,
        )

        db.commit()
