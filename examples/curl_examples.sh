#!/usr/bin/env bash
# 数控刀路禁入区审查服务调用示例
# 用法: bash examples/curl_examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1) 登记禁区 1：10x10 mm 方块，Z 闭合范围 [-5, 5] =="
curl -s -X POST "$BASE/zones" \
  -H 'Content-Type: application/json' \
  -d '{
        "zone_no": 1,
        "polygon": [[0,0],[10,0],[10,10],[0,10]],
        "z_min": -5,
        "z_max": 5,
        "description": "基座（毫米）"
      }'; echo

echo "== 2) 查看禁区及版本 =="
curl -s "$BASE/zones/1"; echo

echo "== 3) 提交审查：横穿禁区（应当碰撞，首个碰撞在第 2 行） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 5, 0],
        "tool_radius": 0.0,
        "gcode": "G21 G90 G17\nG1 X15 Y5 Z0\n"
      }'; echo

echo "== 4) 提交审查：从禁区上方经过（无碰撞） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 50, 0],
        "tool_radius": 0.0,
        "gcode": "G1 X15 Y50 Z0\n"
      }'; echo

echo "== 5) 非法指令（G54 坐标系）必须被拒绝，且不产生报告 =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0],
        "tool_radius": 0.0,
        "gcode": "G54 G1 X1\n"
      }'

echo "== 6) 登记禁区新版本（几何平移到远处），旧版本保持不变 =="
curl -s -X POST "$BASE/zones/1/versions" \
  -H 'Content-Type: application/json' \
  -d '{
        "polygon": [[100,100],[110,100],[110,110],[100,110]],
        "z_min": -5,
        "z_max": 5
      }'; echo

echo "== 7) 同一刀路按新版本审查（无碰撞） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 2}],
        "start": [-5, 5, 0],
        "tool_radius": 0.0,
        "gcode": "G1 X15 Y5 Z0\n"
      }'; echo

echo "== 8) 旧报告内容不随后续禁区更新改变 =="
curl -s "$BASE/reports/1"; echo

echo "== 9) 可选：启用全程最小水平净间隙计算（include_min_clearance=true） =="
# 先把禁区恢复为版本 1 风格（如已执行第 6 步，可显式引用 v1）；
# 路径 y=11 与方块边 y=10 距离 1mm，刀半径 0.5 => 最小净间隙 0.5mm
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
        "include_min_clearance": true
      }'; echo

echo "== 10) 未启用最小净间隙的请求：min_clearance 为 null，旧调用完全兼容 =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n"
      }'; echo

echo "== 11) 多刀：传入 tools 刀号->半径映射；Tn 在其所在行先于运动生效 =="
# 初始刀位/首段用 tool_radius=0.5（刀号为空）；T1 换为 r=2，路径 y=11 距边 1，
# 2mm 大刀在第 2 行运动中碰撞；首碰撞固化原行号、刀号 1 与半径 2
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X0 Y11 Z0\nT1 G1 X15 Y11 Z0\n",
        "tools": {"1": 2.0},
        "include_min_clearance": true
      }'; echo

echo "== 12) 多刀：独立 T 行先按新半径检查当前静止刀位 =="
# 运动到 (0,11) 后独立换 T1(r=1)，该静止点距禁区边恰好 1mm => 接触碰撞在第 2 行
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X0 Y11 Z0\nT1\nG1 X15 Y20 Z0\n",
        "tools": {"1": 1.0}
      }'; echo

echo "== 13) 多刀非法输入一律 422 且不生成报告：未映射刀号/非整数 T/同行多 T/非法半径 =="
curl -s -o /dev/null -w "未映射 T9: HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0], "tool_radius": 0.5,
        "gcode": "T9 G1 X1\n",
        "tools": {"1": 2.0}
      }'
curl -s -o /dev/null -w "非整数 T1.5: HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0], "tool_radius": 0.5,
        "gcode": "T1.5 G1 X1\n",
        "tools": {"1": 2.0}
      }'
curl -s -o /dev/null -w "同行多个 T: HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0], "tool_radius": 0.5,
        "gcode": "T1 T2 G1 X1\n",
        "tools": {"1": 2.0, "2": 1.0}
      }'
curl -s -o /dev/null -w "非法负半径: HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0], "tool_radius": 0.5,
        "gcode": "G1 X1\n",
        "tools": {"1": -0.2}
      }'

echo "== 14) 不传 tools：T 字仍被忽略，单刀旧行为完全兼容 =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "T9 G1 X15 Y11 Z0\n"
      }'; echo
