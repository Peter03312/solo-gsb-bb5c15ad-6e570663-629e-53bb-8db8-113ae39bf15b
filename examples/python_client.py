"""数控刀路禁入区审查服务 Python 调用示例（仅需 requests 或 httpx）。

运行：
    pip install httpx
    BASE_URL=http://localhost:8080 python examples/python_client.py
"""
from __future__ import annotations

import os

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8080")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10.0)

    print("health:", c.get("/health").json())

    # 1. 登记禁区（毫米多边形 + 闭合 Z 范围）
    z = c.post("/zones", json={
        "zone_no": 1,
        "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
        "z_min": -5.0,
        "z_max": 5.0,
        "description": "基座",
    })
    z.raise_for_status()
    print("zone created:", z.json())

    # 2. 提交审查：G21 公制 / G90 绝对 / G17 XY，直线横穿禁区
    gcode = "G21 G90 G17\nG1 X-5 Y5 Z0\nG1 X15 Y5 Z0\n"
    review = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 5.0, 0.0],
        "tool_radius": 0.0,
        "gcode": gcode,
    })
    review.raise_for_status()
    body = review.json()
    print("review:", body["conclusion"],
          "line:", body["collision_line_no"],
          "zone:", body["collision_zone_no"])
    report_id = body["report_id"]

    # 3. 圆弧整圆（同起终点 + I/J 非零）示例，禁区在圆顶附近时会在弧中途碰撞
    arc = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-50.0, -50.0, 0.0],
        "tool_radius": 0.0,
        "gcode": ("G90 G21 G17\n"
                  "G1 X10 Y0 Z0\n"
                  "G3 X10 Y0 I-10 J0\n"),  # 绕 (0,0) 的逆时针整圆
    })
    arc.raise_for_status()
    print("full-circle review:", arc.json()["has_collision"])

    # 4. 非法 G-code：必须 422 拒绝，且不产生报告
    bad = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0.0, 0.0, 0.0],
        "tool_radius": 0.0,
        "gcode": "G54 G1 X1\n",
    })
    print("invalid gcode status:", bad.status_code, bad.json())

    # 5. 登记新版本（修改不可变 => 新版本），旧报告不变
    c.post("/zones/1/versions", json={
        "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
        "z_min": -5.0,
        "z_max": 5.0,
    }).raise_for_status()
    old_report = c.get(f"/reports/{report_id}").json()
    print("immutable old report:", old_report["used_zone_versions"],
          old_report["has_collision"])

    # 6. 可选：启用全程最小水平净间隙（沿用既有审查输入）
    #    路径 y=11 与方块上边 y=10 距离 1mm，刀半径 0.5 => 净间隙 0.5mm
    clr = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
        "include_min_clearance": True,
    })
    clr.raise_for_status()
    body = clr.json()
    mc = body["min_clearance"]
    print("min clearance:", mc)
    assert mc["min_clearance_mm"] == 0.5
    # 结果随报告固化：GET /reports/{id} 与提交响应一致
    assert c.get(f"/reports/{body['report_id']}").json()["min_clearance"] == mc

    # 7. 未启用时字段为 null（旧请求/旧客户端完全兼容）
    legacy = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
    }).json()
    assert legacy["min_clearance"] is None
    print("legacy compatible: min_clearance =", legacy["min_clearance"])

    # 8. 多刀：传入 tools（非负整数刀号 -> 非负有限毫米半径映射）。
    #    初始刀位仍用 tool_radius（刀号为空）；T1 在其所在行先于运动生效，
    #    之后运动沿用 r=2 的大刀直至下次切换。
    multi = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X0 Y11 Z0\nT1 G1 X15 Y11 Z0\n",
        "tools": {"1": 2.0},
        "include_min_clearance": True,
    })
    multi.raise_for_status()
    b = multi.json()
    print("multi-tool collision:",
          b["collision_line_no"], b["collision_tool_no"],
          b["collision_tool_radius_mm"],
          b["min_clearance"])
    assert b["collision_tool_no"] == 1
    assert b["collision_tool_radius_mm"] == 2.0
    assert b["min_clearance"]["tool_no"] == 1
    # 结果随报告固化
    assert c.get(f"/reports/{b['report_id']}").json()["collision_tool_no"] == 1

    # 9. 独立 T 行：换刀后先按新半径检查当前静止刀位，再执行后续运动
    static_hit = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X0 Y11 Z0\nT1\nG1 X15 Y20 Z0\n",
        "tools": {"1": 1.0},
    })
    static_hit.raise_for_status()
    sb = static_hit.json()
    print("standalone T-line first hit line:", sb["collision_line_no"])
    assert sb["collision_line_no"] == 2 and sb["collision_tool_no"] == 1

    # 10. 未传 tools 时 T 字仍被忽略（单刀旧行为）；非法多刀输入一律 422
    ignore_t = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "T9 G1 X15 Y11 Z0\n",
    }).json()
    assert not ignore_t["has_collision"] and ignore_t["collision_tool_no"] is None
    for bad in (
        {"gcode": "T9 G1 X1\n", "tools": {"1": 2.0}},       # 未映射刀号
        {"gcode": "T1.5 G1 X1\n", "tools": {"1": 2.0}},     # T 非整数
        {"gcode": "T-1 G1 X1\n", "tools": {"1": 2.0}},      # T 为负
        {"gcode": "T1 T2 G1 X1\n",                          # 同行多个 T
         "tools": {"1": 2.0, "2": 1.0}},
        {"gcode": "G1 X1\n", "tools": {"1": -0.2}},         # 非法半径
    ):
        rr = c.post("/reviews", json={
            "zones": [{"zone_no": 1, "version": 1}],
            "start": [0.0, 0.0, 0.0], "tool_radius": 0.5, **bad})
        assert rr.status_code == 422, (bad, rr.status_code)
    print("multi-tool invalid inputs all rejected with 422")


if __name__ == "__main__":
    main()
