"""端到端与单元测试。使用 SQLite 覆盖数据库，无需 PostgreSQL。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import gcode as gc  # noqa: E402
from app import models  # noqa: E402
from app import schemas  # noqa: E402
from app.collision import (Zone, build_check_events, check_path,
                           check_path_events, initial_collision,
                           minimum_clearance, minimum_clearance_events)  # noqa: E402
from app.geometry import GeometryError, normalize_polygon  # noqa: E402
from app.main import app  # noqa: E402
from app.database import Base, engine  # noqa: E402

Base.metadata.create_all(bind=engine)
client = TestClient(app)

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
SAFE = (-50.0, -50.0, 0.0)


def parse(text, start=SAFE):
    return gc.parse_gcode(text, start)


class GeometryTests(unittest.TestCase):
    def test_bad_polygons(self):
        with self.assertRaises(GeometryError):
            normalize_polygon([[0, 0], [1, 1]])
        with self.assertRaises(GeometryError):
            normalize_polygon([[0, 0], [1, 0], [0.5, 0]])  # 零面积
        with self.assertRaises(GeometryError):  # 蝴蝶结自相交
            normalize_polygon([[0, 0], [10, 10], [10, 0], [0, 10]])

    def test_closing_duplicate_point_accepted(self):
        poly = normalize_polygon([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]])
        self.assertEqual(len(poly), 4)


class LineCollisionTests(unittest.TestCase):
    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def test_mid_segment_hit_not_endpoints(self):
        # 两端都在方块外侧，但路径横穿方块内部（仅采样端点会漏检）
        motions = parse("G90 G21 G17\nG1 X-5 Y5\nG1 X15 Y5\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 3)

    def test_pass_above_zone(self):
        motions = parse("G1 X-5 Y20\nG1 X15 Y20\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.5))

    def test_tool_radius_touch_at_distance(self):
        # 路径距边恰为 1mm，刀半径 1 => 边界接触算碰撞
        motions = parse("G1 X-5 Y11\nG1 X15 Y11\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.99))
        hit = check_path(motions, [self.zone], 1.0)
        self.assertIsNotNone(hit)

    def test_z_out_of_range_no_hit(self):
        motions = parse("G1 X-5 Y5 Z10\nG1 X15 Y5 Z10\n").motions
        self.assertIsNone(check_path(motions, [self.zone], 0.0))
        # 斜线 Z 从范围外穿入范围内：途中碰撞
        motions2 = parse("G1 X-5 Y5 Z20\nG1 X15 Y5 Z0\n").motions
        hit = check_path(motions2, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_diagonal_through_corner(self):
        motions = parse("G1 X-2 Y-2\nG1 X12 Y12\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 2)

    def test_grazing_tangent_contact(self):
        # 与边重合贴边（从边外沿 X 轴进入）
        motions = parse("G1 X-5 Y0\nG1 X10 Y0\n").motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_line_completely_inside(self):
        motions = gc.parse_gcode("G1 X2 Y2\nG1 X8 Y8\n",
                                 (2.0, 2.0, 0.0)).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)


class ArcCollisionTests(unittest.TestCase):
    def setUp(self):
        # 弧：圆心 (0,0) 半径 10，从 (10,0) 逆时针整圆；禁区跨在圆顶 (0,10)
        self.zone = Zone(7, normalize_polygon(
            [[-1, 9], [1, 9], [1, 11], [-1, 11]]), -5.0, 5.0)

    def test_full_circle_hits_mid_arc(self):
        text = "G90 G17 G21\nG1 X10 Y0 Z0\nG3 X10 Y0 I-10 J0\n"
        motions = gc.parse_gcode(text, SAFE).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 3)
        self.assertEqual(hit.zone_no, 7)
        # 首个碰撞约在 1/4 圆处（进度 ~0.25）
        self.assertAlmostEqual(hit.progress, 0.25, delta=0.02)

    def test_clockwise_full_circle(self):
        text = "G1 X10 Y0\nG2 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        hit = check_path(motions, [self.zone], 0.0)
        self.assertIsNotNone(hit)

    def test_arc_misses(self):
        # 禁区在圆外 (0,20) 附近
        zone = Zone(1, normalize_polygon([[19, -1], [21, -1], [21, 1], [19, 1]]),
                    -5, 5)
        text = "G1 X10 Y0\nG3 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        self.assertIsNone(check_path(motions, [zone], 0.0))

    def test_arc_contact_with_radius(self):
        # 弧半径 10，禁区边在半径 10.5 处：刀半径 0.5 恰好接触
        zone = Zone(1, normalize_polygon(
            [[-1, 10.5], [1, 10.5], [1, 12], [-1, 12]]), -5, 5)
        text = "G1 X10 Y0\nG3 X10 Y0 I-10 J0\n"
        motions = parse(text).motions
        self.assertIsNone(check_path(motions, [zone], 0.4))
        self.assertIsNotNone(check_path(motions, [zone], 0.5))

    def test_semicircle_inside_zone(self):
        # 整个半圆都在大禁区内
        zone = Zone(1, normalize_polygon(
            [[-15, -15], [15, -15], [15, 15], [-15, 15]]), -5, 5)
        text = "G90\nG1 X10 Y0 Z50\nG1 Z0\nG3 X-10 Y0 I-10 J0\n"
        motions = gc.parse_gcode(text, (10.0, 0.0, 50.0)).motions
        hit = check_path(motions, [zone], 0.0)
        self.assertIsNotNone(hit)
        # 第 3 行 Z 向下降进入高度范围时已位于禁区内
        self.assertEqual(hit.line_no, 3)


class ParserModalTests(unittest.TestCase):
    def test_incremental_and_unit(self):
        # G20 英寸：X 走 1 英寸 = 25.4mm；G91 增量
        r = gc.parse_gcode("G20 G91\nG1 X1 Y0\n", (0, 0, 0))
        self.assertAlmostEqual(r.final_position[0], 25.4, places=6)

    def test_missing_axis_inherits(self):
        r = gc.parse_gcode("G90\nG1 X5 Y7 Z3\nG1 X8\n", (0, 0, 0))
        self.assertEqual(r.final_position, (8.0, 7.0, 3.0))

    def test_arc_z_change_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0 Z0\nG3 X0 Y10 Z1 I-10 J0\n", (0, 0, 0))

    def test_arc_zero_ij_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0\nG3 X0 Y10\n", (0, 0, 0))

    def test_arc_radius_mismatch_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X10 Y0\nG3 X0 Y9 I-10 J0\n", (0, 0, 0))

    def test_unsupported_g_commands(self):
        for bad in ("G54 G1 X1", "G18 G1 X1", "G81 X1 Y1 Z-1", "G28"):
            with self.assertRaises(gc.GCodeError):
                gc.parse_gcode(bad, (0, 0, 0))

    def test_r_arc_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X0 Y0\nG2 X10 Y0 R5\n", (0, 0, 0))

    def test_comments_and_line_numbers(self):
        text = "G90 ; start\n(hole path)\nN10 G1 X5 Y5\n"
        r = gc.parse_gcode(text, (0, 0, 0))
        # 第 1 行 G90 是有效非空行；第 2 行纯注释；第 3 行运动
        self.assertEqual(r.nonblank_count, 2)
        self.assertEqual(r.motions[0].line_no, 3)

    def test_unbalanced_paren_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("G1 X1 (oops\n", (0, 0, 0))

    def test_arc_is_constant_z_incremental_z0(self):
        r = gc.parse_gcode("G90\nG1 X10 Y0 Z2\nG91\nG3 X0 Y0 I-10 J0\n",
                           (0, 0, 0))
        self.assertEqual(len(r.motions), 2)
        self.assertAlmostEqual(abs(r.motions[1].sweep), 6.2831853, places=5)

    def test_ij_relative_to_start(self):
        r = gc.parse_gcode("G1 X10 Y0\nG3 X0 Y10 I-10 J0\n", (0, 0, 0))
        arc = r.motions[1]
        self.assertAlmostEqual(arc.center[0], 0.0)
        self.assertAlmostEqual(arc.center[1], 0.0)


class InitialPointTests(unittest.TestCase):
    def test_initial_inside(self):
        zone = Zone(1, normalize_polygon(SQUARE), -5, 5)
        self.assertEqual(initial_collision((5, 5, 0), [zone], 0.0), 1)
        self.assertIsNone(initial_collision((5, 5, 9), [zone], 0.0))


class ApiTests(unittest.TestCase):
    def test_01_full_flow(self):
        # 登记禁区
        r = client.post("/zones", json={
            "zone_no": 100, "polygon": SQUARE, "z_min": -5, "z_max": 5,
            "description": "基座"})
        self.assertEqual(r.status_code, 201, r.text)

        # 非法几何拒绝
        r = client.post("/zones", json={
            "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
            "z_min": -5, "z_max": 5})
        self.assertEqual(r.status_code, 422)

        # 横穿禁区 => 碰撞
        gcode = "G90 G21 G17\nG1 X-5 Y5 Z0\nG1 X15 Y5 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0, "gcode": gcode})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["has_collision"])
        self.assertEqual(body["collision_line_no"], 3)
        self.assertEqual(body["collision_zone_no"], 100)

        # 无碰撞刀路
        gcode_ok = "G1 X-5 Y50 Z0\nG1 X15 Y50 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [-5, 50, 0], "tool_radius": 0.0, "gcode": gcode_ok})
        body = r.json()
        self.assertFalse(body["has_collision"])
        self.assertIsNone(body["collision_line_no"])

        # 非法 gcode 拒绝，且不留报告
        existing = client.get("/zones/100").json()
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [0, 0, 0], "tool_radius": 0.0,
            "gcode": "G54 G1 X1\n"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(client.get("/zones/100").json(), existing)

        # 不存在的禁区/版本
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 999, "version": 1}],
            "start": [0, 0, 0], "tool_radius": 0.0, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 404)
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 9}],
            "start": [0, 0, 0], "tool_radius": 0.0, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 404)

        # 刀具半径非法
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 100, "version": 1}],
            "start": [0, 0, 0], "tool_radius": -1, "gcode": "G1 X1\n"})
        self.assertEqual(r.status_code, 422)

    def test_02_versions_and_immutable_report(self):
        r = client.post("/zones", json={
            "polygon": SQUARE, "z_min": -5, "z_max": 5})
        no = r.json()["zone_no"]
        r = client.post("/reviews", json={
            "zones": [{"zone_no": no, "version": 1}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"})
        self.assertTrue(r.json()["has_collision"])
        report_v1 = r.json()["report_id"]

        # 修改 => 不可变新版本（方块平移到 (100,100) 附近）
        far = [[100, 100], [110, 100], [110, 110], [100, 110]]
        r = client.post(f"/zones/{no}/versions",
                        json={"polygon": far, "z_min": -5, "z_max": 5})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["version"], 2)

        # 旧报告不随禁区更新改变
        old = client.get(f"/reports/{report_v1}").json()
        self.assertEqual(old["used_zone_versions"], {str(no): 1})
        self.assertTrue(old["has_collision"])

        # 同一刀路按 v2 审查 => 无碰撞
        r = client.post("/reviews", json={
            "zones": [{"zone_no": no, "version": 2}],
            "start": [-5, 5, 0], "tool_radius": 0.0,
            "gcode": "G1 X15 Y5 Z0\n"})
        self.assertFalse(r.json()["has_collision"])

        detail = client.get(f"/zones/{no}").json()
        self.assertEqual([v["version"] for v in detail["versions"]], [1, 2])

    def test_03_multiple_zones_first_one_wins(self):
        client.post("/zones", json={"zone_no": 200, "polygon": SQUARE,
                                   "z_min": -5, "z_max": 5})
        poly2 = [[11, 0], [20, 0], [20, 10], [11, 10]]
        client.post("/zones", json={"zone_no": 300, "polygon": poly2,
                                   "z_min": -5, "z_max": 5})
        gcode = "G1 X30 Y5 Z0\n"
        r = client.post("/reviews", json={
            "zones": [{"zone_no": 300, "version": 1},
                      {"zone_no": 200, "version": 1}],
            "start": [-10, 5, 0], "tool_radius": 0.0, "gcode": gcode})
        body = r.json()
        self.assertTrue(body["has_collision"])
        # 路径先进入 200 号禁区（x=10 边早于 300 的 x=11 边）
        self.assertEqual(body["collision_zone_no"], 200)


class ClearanceTests(unittest.TestCase):
    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def _clr(self, text, start, r=0.0, zones=None):
        motions = gc.parse_gcode(text, start).motions
        return minimum_clearance(motions, zones or [self.zone], r, start)

    def test_line_outside_positive(self):
        # y=11 与边距离 1，刀半径 0.5 => 净间隙 0.5
        cp = self._clr("G1 X-5 Y11\nG1 X15 Y11\n", (-5.0, 11.0, 0.0), 0.5)
        self.assertAlmostEqual(cp.net, 0.5, places=9)
        self.assertEqual(cp.zone_no, 1)
        self.assertEqual(cp.line_no, 2)
        # 最近点为垂足段内任意点，按段内进度取最早（x=0）
        self.assertAlmostEqual(cp.point[0], 0.0, places=9)
        self.assertAlmostEqual(cp.point[1], 11.0, places=9)

    def test_line_penetration_negative(self):
        # 横穿方块：路径穿入填充多边形内部，距离 0 - 刀半径 => 净间隙 -0.5；
        # 第一个零距离点在进入边界 x=0 处
        cp = self._clr("G1 X-5 Y5\nG1 X15 Y5\n", (-5.0, 5.0, 0.0), 0.5)
        self.assertEqual(cp.net, -0.5)
        self.assertAlmostEqual(cp.point[0], 0.0, places=9)
        self.assertAlmostEqual(cp.point[1], 5.0, places=9)
        self.assertEqual(cp.line_no, 2)

    def test_line_inside_negative(self):
        # 起点已在多边形内部，唯一运动段全程侵入 => 净间隙为 -刀半径
        motions = gc.parse_gcode("G1 X8 Y8\n", (2.0, 2.0, 0.0)).motions
        cp = minimum_clearance(motions, [self.zone], 0.5, (2.0, 2.0, 0.0))
        self.assertAlmostEqual(cp.net, -0.5, places=9)
        self.assertEqual(cp.line_no, 1)
        self.assertEqual(tuple(round(v, 9) for v in cp.point), (2.0, 2.0, 0.0))

    def test_boundary_contact_zero(self):
        # 路径 y=11 与边 y=10 距离恰为刀半径 1：刀具外沿刚好接触边界 => 0
        cp = self._clr("G1 X-5 Y11\nG1 X15 Y11\n", (-5.0, 11.0, 0.0), 1.0)
        self.assertEqual(cp.net, 0.0)
        # 刀半径 0，路径本身贴边：距离 0 - 0 = 0
        cp0 = self._clr("G1 X-5 Y10\nG1 X15 Y10\n", (-5.0, 10.0, 0.0), 0.0)
        self.assertEqual(cp0.net, 0.0)
        # 刀半径 > 0 且路径落在填充多边形边界上：距离 0 - r 为负（侵入）
        cpneg = self._clr("G1 X-5 Y10\nG1 X15 Y10\n", (-5.0, 10.0, 0.0), 1.0)
        self.assertEqual(cpneg.net, -1.0)

    def test_closest_only_inside_z_window(self):
        # Z 从 10 降到 -10：t<=0.25 时 z>=5 不在窗口；窗口起点 t=0.25
        # 对应 x=0（边界，距离 0）——整体最小值为边界接触 0。
        cp = self._clr("G1 X-5 Y5 Z10\nG1 X15 Y5 Z-10\n",
                       (-5.0, 5.0, 10.0), 0.0)
        self.assertEqual(cp.net, 0.0)
        self.assertAlmostEqual(cp.point[0], 0.0, places=8)
        self.assertAlmostEqual(cp.point[2], 5.0, places=8)

    def test_positive_minimum_partial_window(self):
        # 窗口只覆盖路径中段 t∈[0.25,0.75]（z 从 10 降到 -10），y=15 在方块外：
        # 该段到方块的最近点 (10,10)，最小距离 5，在窗口进入时刻 t=0.25（x=0）取得
        cp = self._clr("G1 X-5 Y15 Z10\nG1 X15 Y15 Z-10\n",
                       (-5.0, 15.0, 10.0), 0.0)
        self.assertAlmostEqual(cp.net, 5.0, places=7)
        self.assertAlmostEqual(cp.point[0], 0.0, places=7)
        self.assertAlmostEqual(cp.point[1], 15.0, places=7)
        self.assertAlmostEqual(cp.point[2], 5.0, places=7)
        self.assertEqual(cp.line_no, 2)

    def test_all_outside_z_window_empty(self):
        cp = self._clr("G1 X-5 Y5 Z20\nG1 X15 Y5 Z20\n",
                       (-5.0, 5.0, 20.0), 0.0)
        self.assertIsNone(cp)

    def test_full_circle(self):
        # 圆心 (0,0) 半径 10 整圆；禁区为圆顶 2x2 方块，弧从右边 x=1 处进入
        poly = normalize_polygon([[-1, 9], [1, 9], [1, 11], [-1, 11]])
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.0,
                       zones=[Zone(7, poly, -5.0, 5.0)])
        self.assertEqual(cp.net, 0.0)
        self.assertEqual(cp.line_no, 2)
        self.assertEqual(cp.zone_no, 7)
        self.assertAlmostEqual(cp.progress, 0.23406, delta=1e-4)

    def test_arc_contact_radius_zero_clearance(self):
        poly = normalize_polygon([[-1, 10.5], [1, 10.5], [1, 12], [-1, 12]])
        # 刀半径 0.5 恰好接触 => 0；0.4 => 0.1
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.5,
                       zones=[Zone(1, poly, -5.0, 5.0)])
        self.assertEqual(cp.net, 0.0)
        self.assertAlmostEqual(cp.point[1], 10.0, places=8)
        cp = self._clr("G1 X10 Y0\nG3 X10 Y0 I-10 J0\n",
                       (-50.0, -50.0, 0.0), 0.4,
                       zones=[Zone(1, poly, -5.0, 5.0)])
        self.assertAlmostEqual(cp.net, 0.1, places=8)

    def test_no_motions_uses_initial_position(self):
        cp = minimum_clearance([], [self.zone], 0.5, (5.0, 5.0, 0.0))
        self.assertIsNotNone(cp)
        self.assertAlmostEqual(cp.net, -0.5, places=9)
        self.assertIsNone(cp.line_no)
        self.assertEqual(cp.point, (5.0, 5.0, 0.0))
        # 初始点不在任何禁区高度 => 空
        self.assertIsNone(
            minimum_clearance([], [self.zone], 0.0, (5.0, 5.0, 9.0)))
        # 空程序（仅注释/标记）同样按初始刀位
        parsed = gc.parse_gcode("(only comment)\n%\n", (-5.0, 5.0, 0.0))
        cp = minimum_clearance(parsed.motions, [self.zone], 0.0, (-5.0, 5.0, 0.0))
        self.assertAlmostEqual(cp.net, 5.0, places=9)

    def test_tie_break_order_then_progress_then_zone(self):
        # 两条等距运动：取顺序更早的段
        z1 = Zone(1, normalize_polygon(
            [[10, -1], [12, -1], [12, 1], [10, 1]]), -5.0, 5.0)
        motions = gc.parse_gcode(
            "G1 X0 Y0\nG1 X11 Y0\nG1 X0 Y0\nG1 X11 Y0\n", (0.0, 0.0, 0.0)).motions
        cp = minimum_clearance(motions, [z1], 0.0, (0.0, 0.0, 0.0))
        self.assertEqual(cp.line_no, 2)  # 第一条到达 y=0 附近的运动
        # 同段同进度、两个等距禁区 => 小编号
        za = Zone(2, normalize_polygon(
            [[10, 0], [12, 0], [12, 2], [10, 2]]), -5.0, 5.0)
        zb = Zone(3, normalize_polygon(
            [[10, -2], [12, -2], [12, 0], [10, 0]]), -5.0, 5.0)
        motions = gc.parse_gcode("G1 X11 Y0\n", (0.0, 0.0, 0.0)).motions
        cp = minimum_clearance(motions, [za, zb], 0.0, (0.0, 0.0, 0.0))
        self.assertEqual(cp.zone_no, 2)

    def test_vertical_move_xy_static(self):
        # XY 不动（在禁区外 5mm），Z 从高空下降进入窗口：进入点 z=5
        cp = self._clr("G1 X-5 Y5 Z20\nG1 X-5 Y5 Z0\n",
                       (-5.0, 5.0, 20.0), 0.0)
        self.assertAlmostEqual(cp.net, 5.0, places=9)
        self.assertAlmostEqual(cp.point[2], 5.0, places=8)


class ClearanceApiTests(unittest.TestCase):
    ZONE_NO = 400

    def setUp(self):
        existing = client.get(f"/zones/{self.ZONE_NO}")
        if existing.status_code == 404:
            r = client.post("/zones", json={
                "zone_no": self.ZONE_NO, "polygon": SQUARE,
                "z_min": -5, "z_max": 5})
            self.assertEqual(r.status_code, 201, r.text)

    def _review(self, **kw):
        body = {
            "zones": [{"zone_no": 400, "version": 1}],
            "start": [-5.0, 5.0, 0.0], "tool_radius": 0.5,
            "gcode": "G1 X15 Y5 Z0\n"}
        body.update(kw)
        return client.post("/reviews", json=body)

    def test_opt_in_returns_and_persists(self):
        # y=11 与方块上边距离 1，刀半径 1 => 刀具外沿恰好接触，净间隙 0
        r = self._review(include_min_clearance=True, start=[-5.0, 11.0, 0.0],
                         tool_radius=1.0, gcode="G1 X15 Y11 Z0\n")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["min_clearance_enabled"])
        mc = b["min_clearance"]
        self.assertEqual(mc["min_clearance_mm"], 0.0)
        self.assertEqual(mc["line_no"], 1)
        self.assertEqual(mc["zone_no"], 400)
        self.assertAlmostEqual(mc["point_mm"][0], 0.0, places=8)
        self.assertAlmostEqual(mc["point_mm"][1], 11.0, places=8)
        # 接触即碰撞：首碰撞结论仍按既有规则，与最小点独立计算
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)
        # 报告查询一致（固化）
        rid = b["report_id"]
        got = client.get(f"/reports/{rid}").json()
        self.assertTrue(got["min_clearance_enabled"])
        self.assertEqual(got["min_clearance"], mc)

    def test_opt_in_negative_clearance_separate_collision(self):
        # 横穿禁区内部 => 最小净间隙为 -刀半径（负值），同时首碰撞规则不变
        r = self._review(include_min_clearance=True)
        b = r.json()
        self.assertAlmostEqual(b["min_clearance"]["min_clearance_mm"], -0.5,
                               places=9)
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)

    def test_opt_out_compatible(self):
        r = self._review()
        b = r.json()
        self.assertFalse(b["min_clearance_enabled"])
        self.assertIsNone(b["min_clearance"])
        rid = b["report_id"]
        got = client.get(f"/reports/{rid}").json()
        self.assertFalse(got["min_clearance_enabled"])
        self.assertIsNone(got["min_clearance"])

    def test_empty_when_never_in_z_range(self):
        r = self._review(start=[-5.0, 5.0, 20.0],
                         gcode="G1 X15 Y5 Z20\n",
                         include_min_clearance=True)
        b = r.json()
        self.assertTrue(b["min_clearance_enabled"])
        self.assertIsNone(b["min_clearance"])
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertIsNone(got["min_clearance"])

    def test_no_motion_initial_position(self):
        r = self._review(start=[5.0, 5.0, 0.0], gcode="(nothing here)\n",
                         include_min_clearance=True)
        b = r.json()
        self.assertEqual(b["input_summary"]["motion_count"], 0)
        mc = b["min_clearance"]
        self.assertAlmostEqual(mc["min_clearance_mm"], -0.5, places=9)
        self.assertIsNone(mc["line_no"])
        self.assertEqual(mc["point_mm"], [5.0, 5.0, 0.0])

    def test_invalid_input_no_report(self):
        # 先取一份成功报告作为“最后报告号”锚点
        ok = self._review(include_min_clearance=False)
        last_id = ok.json()["report_id"]
        # 非法刀半径/非法 G-code/非法开关类型 => 422；不存在禁区/版本 => 404
        bad422 = (
            {"tool_radius": -1.0, "include_min_clearance": True},
            {"gcode": "G54 G1 X1\n", "include_min_clearance": True},
            {"include_min_clearance": "yes"},
            {"include_min_clearance": 1},
            {"include_min_clearance": ["x"]},
            {"include_min_clearance": None},
        )
        for bad in bad422:
            r = self._review(**bad)
            self.assertEqual(r.status_code, 422, r.text)
        r = self._review(zones=[{"zone_no": 999, "version": 1}],
                         include_min_clearance=True)
        self.assertEqual(r.status_code, 404)
        # 全部拒绝后没有新报告：last_id 存在而下一号 404
        self.assertEqual(client.get(f"/reports/{last_id}").status_code, 200)
        self.assertEqual(client.get(f"/reports/{last_id + 1}").status_code, 404)

    def test_immutable_report_snapshot(self):
        r1 = self._review(include_min_clearance=True)
        rid = r1.json()["report_id"]
        mc1 = r1.json()["min_clearance"]
        # 禁区改版（平移），旧报告固化值不变
        client.post("/zones/400/versions", json={
            "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
            "z_min": -5, "z_max": 5})
        got = client.get(f"/reports/{rid}").json()
        self.assertEqual(got["min_clearance"], mc1)


class MultiToolParserTests(unittest.TestCase):
    """多刀模式解析：T 校验与换刀事件记录；未传映射时 T 仍被忽略。"""

    MAP = {0: 0.0, 1: 1.0, 2: 2.0}

    def test_switch_recorded_for_standalone_and_motion_lines(self):
        r = gc.parse_gcode("T1\nG1 X5 Y0 Z0\nT2 G1 X8 Y0 Z0\n", (0, 0, 0),
                           tool_radii=self.MAP)
        self.assertEqual([sw.tool_no for sw in r.tool_switches], [1, 2])
        self.assertEqual([sw.line_no for sw in r.tool_switches], [1, 3])

    def test_tool_zero_allowed(self):
        r = gc.parse_gcode("T0\n", (0, 0, 0), tool_radii=self.MAP)
        self.assertEqual(r.tool_switches[0].tool_no, 0)

    def test_unmapped_tool_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("T9 G1 X1\n", (0, 0, 0), tool_radii=self.MAP)

    def test_non_integer_or_negative_t_rejected(self):
        for bad in ("T1.5 G1 X1\n", "T-1 G1 X1\n", "T-2 G1 X1\n"):
            with self.assertRaises(gc.GCodeError):
                gc.parse_gcode(bad, (0, 0, 0), tool_radii=self.MAP)

    def test_multiple_t_same_line_rejected(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("T1 T2 G1 X1\n", (0, 0, 0), tool_radii=self.MAP)

    def test_empty_map_rejects_any_t(self):
        with self.assertRaises(gc.GCodeError):
            gc.parse_gcode("T1\n", (0, 0, 0), tool_radii={})

    def test_t_ignored_without_map(self):
        # 未传映射（默认 None）：任意 T 都解析后忽略，无换刀事件，行为同旧版
        r = gc.parse_gcode("T9\nT1 T2 G1 X5 Y0 Z0\n", (0, 0, 0))
        self.assertEqual(r.tool_switches, [])
        self.assertEqual(len(r.motions), 1)


class MultiToolEngineTests(unittest.TestCase):
    """多刀碰撞/净间隙事件流：半径按事件发生时生效，静态换刀先行。"""

    def setUp(self):
        # 10x10 方块禁区，z ∈ [-5, 5]
        self.zone = Zone(1, normalize_polygon(SQUARE), -5.0, 5.0)

    def _events(self, text, start, initial_r, radii):
        parsed = gc.parse_gcode(text, start, tool_radii=radii)
        return build_check_events(parsed.motions, parsed.tool_switches, radii,
                                  start, initial_r)

    def test_radius_active_for_motion_on_same_line(self):
        # y=11 距边 1：r=0.5 不碰，r=2 碰；T1 与运动同行立即生效
        ev = self._events("T1 G1 X15 Y11 Z0\n", (-5.0, 11.0, 0.0), 0.5, {1: 2.0})
        hit = check_path_events(ev, [self.zone])
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 1)
        self.assertEqual(hit.tool_no, 1)
        self.assertEqual(hit.tool_radius, 2.0)

    def test_standalone_t_checks_static_position_before_later_motion(self):
        # 第 1 行运动到 (0,11)（距边 1），独立 T1(r=1) 在该静止点先碰，
        # 即使第 3 行用旧/小半径不会碰，首碰撞仍归属第 2 行换刀
        ev = self._events(
            "G1 X0 Y11 Z0\nT1\nG1 X15 Y20 Z0\n", (-5.0, 11.0, 0.0), 0.5,
            {1: 1.0})
        hit = check_path_events(ev, [self.zone])
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 2)
        self.assertEqual(hit.tool_no, 1)
        self.assertEqual(hit.tool_radius, 1.0)

    def test_switch_between_motions_applies_to_following_segment(self):
        # 首段 r=0.5 不碰；独立换到 r=2 后静止点 (-5,11) 距边 5 不碰；
        # 第 3 段用 r=2 在 x=0 处首碰
        ev = self._events(
            "G1 X-5 Y11 Z0\nT1\nG1 X15 Y11 Z0\n", (-10.0, 11.0, 0.0), 0.5,
            {1: 2.0})
        hit = check_path_events(ev, [self.zone])
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line_no, 3)
        self.assertEqual(hit.tool_no, 1)
        self.assertEqual(hit.tool_radius, 2.0)

    def test_initial_position_hit_has_empty_tool_number(self):
        # 初始刀位在禁区内部：初始静止事件先行命中，行号/刀号均为 None
        ev = self._events("T1\nG1 X6 Y5 Z0\n", (5.0, 5.0, 0.0), 0.5, {1: 2.0})
        hit = check_path_events(ev, [self.zone])
        self.assertIsNotNone(hit)
        self.assertIsNone(hit.line_no)
        self.assertIsNone(hit.tool_no)
        self.assertEqual(hit.tool_radius, 0.5)

    def test_clearance_uses_event_radius_and_persists_tool(self):
        # 首段 r=0.5 => 净间隙 0.5；T1(r=0.25) 静止点距边 5 => 4.75；
        # 次段 r=0.25 => 净间隙 0.75。整体最小 0.5 在第 1 行（初始刀）
        ev = self._events(
            "G1 X0 Y11 Z0\nT1\nG1 X15 Y11 Z0\n", (-5.0, 11.0, 0.0), 0.5,
            {1: 0.25})
        cp = minimum_clearance_events(ev, [self.zone])
        self.assertAlmostEqual(cp.net, 0.5, places=9)
        self.assertEqual(cp.line_no, 1)
        self.assertIsNone(cp.tool_no)
        self.assertEqual(cp.tool_radius, 0.5)

    def test_clearance_initial_position_event_precedes_motions(self):
        # 初始刀位在禁区内部（净间隙 -初始半径），优先于后续换刀事件
        ev = self._events("T1\nG1 X6 Y5 Z0\n", (5.0, 5.0, 0.0), 0.5, {1: 0.1})
        cp = minimum_clearance_events(ev, [self.zone])
        self.assertAlmostEqual(cp.net, -0.5, places=9)
        self.assertIsNone(cp.line_no)
        self.assertIsNone(cp.tool_no)
        self.assertEqual(cp.tool_radius, 0.5)

    def test_clearance_tie_prefers_execution_order(self):
        # 初始刀位 (0,15) 距方块 5；首段从该点竖直远离，全程距离 ≥5 且在
        # t=0 同为 5。初始静止事件（order=0，line_no=null）与首段起点等值，
        # 按执行顺序取初始刀位；之后换到更大的刀不再影响该裁决。
        ev = self._events(
            "G1 X0 Y30 Z0\nT1\n", (0.0, 15.0, 0.0), 0.0, {1: 0.0})
        cp = minimum_clearance_events(ev, [self.zone])
        self.assertEqual(cp.net, 5.0)
        self.assertIsNone(cp.line_no)
        self.assertIsNone(cp.tool_no)
        self.assertEqual(cp.tool_radius, 0.0)
        self.assertEqual(cp.point, (0.0, 15.0, 0.0))


class MultiToolApiTests(unittest.TestCase):
    ZONE_NO = 500

    def setUp(self):
        existing = client.get(f"/zones/{self.ZONE_NO}")
        if existing.status_code == 404:
            r = client.post("/zones", json={
                "zone_no": self.ZONE_NO, "polygon": SQUARE,
                "z_min": -5, "z_max": 5})
            self.assertEqual(r.status_code, 201, r.text)

    def _review(self, **kw):
        body = {
            "zones": [{"zone_no": self.ZONE_NO, "version": 1}],
            "start": [-5.0, 11.0, 0.0], "tool_radius": 0.5,
            "gcode": "G1 X15 Y11 Z0\n"}
        body.update(kw)
        return client.post("/reviews", json=body)

    def test_multi_tool_collision_persists_tool_fields(self):
        r = self._review(tools={"1": 2.0}, gcode="T1 G1 X15 Y11 Z0\n")
        self.assertEqual(r.status_code, 200, r.text)
        b = r.json()
        self.assertTrue(b["has_collision"])
        self.assertEqual(b["collision_line_no"], 1)
        self.assertEqual(b["collision_zone_no"], self.ZONE_NO)
        self.assertEqual(b["collision_tool_no"], 1)
        self.assertEqual(b["collision_tool_radius_mm"], 2.0)
        self.assertEqual(b["input_summary"]["tools"], {"1": 2.0})
        # GET 报告固化一致
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertEqual(got["collision_tool_no"], 1)
        self.assertEqual(got["collision_tool_radius_mm"], 2.0)

    def test_multi_tool_initial_hit_fields(self):
        r = self._review(tools={"1": 2.0}, start=[5.0, 5.0, 0.0],
                         gcode="T1\nG1 X6 Y5 Z0\n")
        b = r.json()
        self.assertTrue(b["has_collision"])
        self.assertIsNone(b["collision_line_no"])
        self.assertIsNone(b["collision_tool_no"])
        self.assertEqual(b["collision_tool_radius_mm"], 0.5)
        self.assertEqual(
            client.get(f"/reports/{b['report_id']}").json()["message"],
            "初始刀位点位于禁区范围内")

    def test_multi_tool_clearance_shape(self):
        r = self._review(tools={"1": 0.25}, include_min_clearance=True,
                         gcode="T1 G1 X15 Y11 Z0\n")
        mc = r.json()["min_clearance"]
        self.assertEqual(mc["min_clearance_mm"], 0.75)
        self.assertEqual(mc["tool_no"], 1)
        self.assertEqual(mc["tool_radius_mm"], 0.25)
        rid = r.json()["report_id"]
        self.assertEqual(client.get(f"/reports/{rid}").json()["min_clearance"], mc)

    def test_invalid_tools_422_without_report(self):
        last_id = self._review(tools={"1": 2.0},
                               gcode="G1 X15 Y50 Z0\n").json()["report_id"]
        bad = (
            {"tools": {"1": -1.0}},                       # 负半径
            {"tools": {"1": "x"}},                        # 非法半径
            {"tools": {"1": True}},                       # 布尔半径
            {"tools": {"-1": 1.0}},                       # 负刀号
            {"tools": {"1.5": 1.0}},                      # 非整数刀号
            {"tools": {"x": 1.0}},                        # 非数值刀号
            {"tools": {"01": 1.0}},                       # 非规范整数刀号
            {"tools": []},                                # 非对象
            {"tools": "1:2"},                             # 非对象
        )
        for extra in bad:
            r = self._review(gcode="G1 X1\n", **extra)
            self.assertEqual(r.status_code, 422, extra)
        # G-code 侧的 T 非法同样 422
        bad_gcode = (
            ("T2 G1 X15 Y11 Z0\n", {"tools": {"1": 2.0}}),       # 未映射
            ("T1.5 G1 X1\n", {"tools": {"1": 2.0}}),             # 非整数 T
            ("T-1 G1 X1\n", {"tools": {"1": 2.0}}),              # 负 T
            ("T1 T2 G1 X1\n", {"tools": {"1": 2.0, "2": 1.0}}),  # 同行多 T
        )
        for gc_txt, extra in bad_gcode:
            r = self._review(gcode=gc_txt, **extra)
            self.assertEqual(r.status_code, 422, gc_txt)
            self.assertEqual(r.json()["detail"]["error"], "invalid_gcode")
        # 非有限半径无法合法出现在 JSON 文本中，直接在模型层验证同样拒绝
        for bad_r in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(Exception):
                schemas.ReviewRequest(
                    zones=[{"zone_no": self.ZONE_NO, "version": 1}],
                    start=[-5.0, 11.0, 0.0], tool_radius=0.5,
                    gcode="G1 X1\n", tools={1: bad_r})
        # 全部拒绝后无新报告
        self.assertEqual(client.get(f"/reports/{last_id}").status_code, 200)
        self.assertEqual(client.get(f"/reports/{last_id + 1}").status_code, 404)

    def test_legacy_ignores_t_and_shape_unchanged(self):
        # 未传 tools：T 字忽略，碰撞顶层刀号字段为 null，净间隙对象不含 tool_*
        r = self._review(gcode="T9 G1 X15 Y11 Z0\n",
                         include_min_clearance=True)
        b = r.json()
        self.assertFalse(b["has_collision"])
        self.assertIsNone(b["collision_tool_no"])
        self.assertIsNone(b["collision_tool_radius_mm"])
        self.assertIsNone(b["input_summary"]["tools"])
        mc = b["min_clearance"]
        self.assertNotIn("tool_no", mc)
        self.assertNotIn("tool_radius_mm", mc)
        got = client.get(f"/reports/{b['report_id']}").json()
        self.assertIsNone(got["collision_tool_no"])
        self.assertNotIn("tool_no", got["min_clearance"])

    def test_empty_map_enables_multi_tool(self):
        # 空映射：无 T 的程序正常审查（多刀形状，初始刀）；出现 T 即 422
        r = self._review(tools={}, gcode="G1 X15 Y50 Z0\n")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["input_summary"]["tools"], {})
        r = self._review(tools={}, gcode="T1 G1 X15 Y50 Z0\n")
        self.assertEqual(r.status_code, 422)


class MultiToolMigrationTests(unittest.TestCase):
    """v1.1 之前的旧库升级：多刀新列幂等补齐，旧报告读取为 null。"""

    def test_idempotent_add_columns_and_old_report(self):
        import tempfile
        from sqlalchemy import create_engine, inspect, text
        from sqlalchemy.orm import sessionmaker
        from app.main import _ensure_report_columns

        dbfile = tempfile.mktemp(suffix=".db")
        eng = create_engine(f"sqlite:///{dbfile}", future=True)
        # 模拟旧库：先建出不含多刀列（也不含净间隙列）的最小 reports 表
        with eng.begin() as conn:
            conn.execute(text(
                "CREATE TABLE reports ("
                "id INTEGER PRIMARY KEY, created_at TIMESTAMP, "
                "zone_versions JSON NOT NULL, input_summary JSON NOT NULL, "
                "has_collision BOOLEAN NOT NULL, collision_line_no INTEGER, "
                "collision_zone_no INTEGER, conclusion TEXT NOT NULL)"))
            conn.execute(text(
                "INSERT INTO reports (zone_versions, input_summary, "
                "has_collision, conclusion) VALUES "
                "('{}', '{}', 0, 'no_collision')"))
        _ensure_report_columns(eng)
        _ensure_report_columns(eng)  # 幂等：再次执行不报错
        cols = {c["name"] for c in inspect(eng).get_columns("reports")}
        self.assertIn("collision_tool_no", cols)
        self.assertIn("collision_tool_radius", cols)
        self.assertIn("min_clearance", cols)
        Session = sessionmaker(bind=eng)
        with Session() as s:
            rep = s.get(models.Report, 1)
            self.assertIsNone(rep.collision_tool_no)
            self.assertIsNone(rep.collision_tool_radius)
            self.assertFalse(bool(rep.min_clearance_enabled))
            self.assertIsNone(rep.min_clearance)


if __name__ == "__main__":
    unittest.main(verbosity=2)
