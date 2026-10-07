#!/usr/bin/env bash
# 抢修最低保障供水预演服务 —— 端到端调用示例（curl + jq/python3）。
# 两阶段：先判定全部保障量能否同时满足（不可行则给出缺口与瓶颈设施），
# 可行则保持全部保障量、按优先级逐点最大化额外供水（必要时重排先前流量）。
# 各点 by_source 按“最终有效流量”逐点归属：来源之和=supplied、
# 各水源跨点合计=source_utilization.used，且来源沿开启单向管段实际可达。
# 只读预演：不改变管网、阀位、复水计划或修订号。
# 用法：bash examples/guarantee_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

# 不依赖 jq 也能取 revision（优先 jq，回退 python3，再回退 grep/sed）
get_field() {  # $1=JSON文件 $2=字段名
  if [ -n "$JQ" ]; then jq -r ".$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"
  else
    grep -o "\"$2\": *[0-9-]*" "$1" | head -1 | sed 's/.*: *//'
  fi
}

echo "== 0. 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 1. 整批录入示例管网（原子覆盖），取得当前修订号 =="
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /tmp/guarantee_load.json
pp < /tmp/guarantee_load.json
REV="$(get_field /tmp/guarantee_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 保障可行：D1=4/D2=2/D3=1 全部保障，再按优先级最大化额外供水 =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 4},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
        ]
      }" | pp

echo
echo "== 3. 保障不可行：D1=10/D2=8/D3=6 超出可供能力，返回缺口与瓶颈设施（不返回部分方案） =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 10},
          {\"demand_id\": \"D2\", \"amount\": 8},
          {\"demand_id\": \"D3\", \"amount\": 6}
        ]
      }" | pp

echo
echo "== 4. 保障量超需求（D1: 10.5 > 10）：400，定位 guarantees[0].amount =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 10.5},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 5. 未知/错类/重复/缺失条目：400，逐条带定位，整单拒绝 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"GHOST\", \"amount\": 1},
          {\"demand_id\": \"S1\",    \"amount\": 1},
          {\"demand_id\": \"D1\",    \"amount\": 1},
          {\"demand_id\": \"D2\",    \"amount\": 1},
          {\"demand_id\": \"D2\",    \"amount\": 2}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 6. 过期修订号（阀变后仍携带旧 revision=$REV）：409，不返回混合版本结果 =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' > /dev/null
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 4},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
        ]
      }"
pp < /tmp/resp.json
# 恢复阀位
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "open"}]}' > /dev/null
echo "(V3 已恢复开启)"

echo
echo "== 7. 路径重排后的逐点水源归属：S1/S2 各 6，S1->J->D1、S2->J->D1、S1->D2 均导通 =="
# D2 只能由 S1 到达（S1->D2）；为同时足额保障 D1、D2，D1 的流被重排为 S2->J->D1。
# 期望：D1.by_source={"S2":6}，D2.by_source={"S1":6}（旧实现会记反）。
REROUTE_NET='{
  "sources":[{"id":"S1","capacity":6},{"id":"S2","capacity":6}],
  "demands":[{"id":"D1","demand":6,"priority":9},{"id":"D2","demand":6,"priority":1}],
  "junctions":[{"id":"J"}],
  "pipes":[
    {"id":"P1","from_node":"S1","to_node":"J","capacity":6},
    {"id":"P2","from_node":"S2","to_node":"J","capacity":6},
    {"id":"P3","from_node":"J","to_node":"D1","capacity":6},
    {"id":"P4","from_node":"S1","to_node":"D2","capacity":6}
  ],
  "valves":[],"forbidden_pairs":[]
}'
curl -sS -X PUT "$BASE/api/network" -H 'Content-Type: application/json' \
  --data "$REROUTE_NET" > /tmp/reroute_load.json
RREV="$(get_field /tmp/reroute_load.json revision)"
echo "(重排管网修订号 revision=$RREV)"
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $RREV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 6},
          {\"demand_id\": \"D2\", \"amount\": 6}
        ]
      }" | pp

echo
echo "== 8. 确认预演不写入：管网与修订号均未被本轮预演改变 =="
curl -sS "$BASE/api/network" | pp
# 恢复演示管网，便于重复执行本脚本
curl -sS -X PUT "$BASE/api/network" -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /dev/null
echo "(已恢复 sample_network.json)"
