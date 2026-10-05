#!/usr/bin/env bash
# 批量核验调用示例：发行方集中提交排片，运营方一次请求按序核验 1～100 个场次。
# 建议在全新服务实例（空库）上执行：
#   docker compose down -v && docker compose up --build -d
#   bash examples/demo_batch.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }
post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入授权：hall-1 自身额度 2（不绑池）；容量 2 的影片共享池绑定 hall-2、hall-3 =="
echo "      另录 hall-4：自身额度 2、不绑池，用于演示批内重放不重复扣额 =="
post /licenses '{
  "film_id": "film-autumn-river", "venue_id": "hall-1",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
  "max_sessions": 2, "note": "批量演示：自身额度 2"
}' | pretty
post /pools '{"film_id":"film-autumn-river","max_sessions":2,"note":"批量演示：跨场馆池容量 2"}' | pretty
post /licenses '{
  "film_id": "film-autumn-river", "venue_id": "hall-2",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
  "max_sessions": 10, "pool_id": 1, "note": "绑定共享池"
}' | pretty
post /licenses '{
  "film_id": "film-autumn-river", "venue_id": "hall-3",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
  "max_sessions": 10, "pool_id": 1, "note": "绑定共享池"
}' | pretty
post /licenses '{
  "film_id": "film-autumn-river", "venue_id": "hall-4",
  "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-31T23:59:59Z",
  "max_sessions": 2, "note": "批量演示：自身额度 2"
}' | pretty

echo
echo "== 2. 一批 5 项按序核验：hall-1 前两项消耗自身额度（剩余 1、0），第三项 QUOTA_EXHAUSTED；"
echo "      hall-2/hall-3 两项消耗共享池（池剩余 1、0）。业务拒绝照样落流水，批处理继续 =="
post /verifications/batch '{
  "items": [
    {"film_id":"film-autumn-river","venue_id":"hall-1","start_time":"2026-10-05T19:00:00Z","idempotency_key":"batch-001"},
    {"film_id":"film-autumn-river","venue_id":"hall-1","start_time":"2026-10-05T21:00:00Z","idempotency_key":"batch-002"},
    {"film_id":"film-autumn-river","venue_id":"hall-1","start_time":"2026-10-06T19:00:00Z","idempotency_key":"batch-003"},
    {"film_id":"film-autumn-river","venue_id":"hall-2","start_time":"2026-10-05T19:00:00Z","idempotency_key":"batch-004"},
    {"film_id":"film-autumn-river","venue_id":"hall-3","start_time":"2026-10-05T19:00:00Z","idempotency_key":"batch-005"}
  ]
}' | pretty

echo
echo "== 3. 重放历史键（同键同内容）：全部 replay=true，返回原流水、不重复扣额 =="
post /verifications/batch '{
  "items": [
    {"film_id":"film-autumn-river","venue_id":"hall-1","start_time":"2026-10-05T19:00:00Z","idempotency_key":"batch-001"},
    {"film_id":"film-autumn-river","venue_id":"hall-3","start_time":"2026-10-05T19:00:00Z","idempotency_key":"batch-005"}
  ]
}' | pretty

echo
echo "== 4. 同批内同键同内容（hall-4 自身额度 2）：第 0 项新键核准（剩 1），第 1 项同键同内容"
echo "      replay=true 不占额，第 2 项新键得以拿到最后一份额度（剩 0）而不是被 QUOTA_EXHAUSTED 拒绝 =="
post /verifications/batch '{
  "items": [
    {"film_id":"film-autumn-river","venue_id":"hall-4","start_time":"2026-10-08T18:00:00Z","idempotency_key":"batch-006"},
    {"film_id":"film-autumn-river","venue_id":"hall-4","start_time":"2026-10-08T18:00:00Z","idempotency_key":"batch-006"},
    {"film_id":"film-autumn-river","venue_id":"hall-4","start_time":"2026-10-08T20:00:00Z","idempotency_key":"batch-007"}
  ]
}' | pretty

echo
echo "== 5. 同键不同内容：整批失败 409（响应带 index=1 与 existing），整批回滚不留新流水 =="
curl -s -X POST "$BASE/verifications/batch" \
  -H 'Content-Type: application/json' -d '{
  "items": [
    {"film_id":"film-autumn-river","venue_id":"hall-2","start_time":"2026-10-08T19:00:00Z","idempotency_key":"batch-008"},
    {"film_id":"film-autumn-river","venue_id":"hall-2","start_time":"2026-10-09T19:00:00Z","idempotency_key":"batch-001"}
  ]
}' | pretty
curl -s -o /dev/null -w 'HTTP %{http_code}（整批失败；冲突无副作用，可重复验证）\n' \
  -X POST "$BASE/verifications/batch" \
  -H 'Content-Type: application/json' -d '{
  "items": [
    {"film_id":"film-autumn-river","venue_id":"hall-2","start_time":"2026-10-08T19:00:00Z","idempotency_key":"batch-008"},
    {"film_id":"film-autumn-river","venue_id":"hall-2","start_time":"2026-10-09T19:00:00Z","idempotency_key":"batch-001"}
  ]
}'

echo
echo "== 6. 回滚校验：batch-008 在上一步整批回滚后并不存在（404），池余量仍为 0 =="
curl -s "$BASE/verifications/batch-008" | pretty
curl -s -o /dev/null -w 'GET /verifications/batch-008 -> HTTP %{http_code}\n' \
  "$BASE/verifications/batch-008"
curl -s "$BASE/pools/1" | pretty

echo
echo "== 7. 批量与单场接口共用同一套规则与写锁：单场再核验 hall-2 同样被 POOL_EXHAUSTED 拒绝 =="
post /verifications '{
  "film_id":"film-autumn-river","venue_id":"hall-2",
  "start_time":"2026-10-10T19:00:00Z","idempotency_key":"single-after-batch"
}' | pretty

echo
echo "== 8. 入参校验：空列表 / 超过 100 项 / 单项时间非法均返回 422，且不产生流水 =="
curl -s -o /dev/null -w 'empty list -> HTTP %{http_code}\n' \
  -X POST "$BASE/verifications/batch" -H 'Content-Type: application/json' -d '{"items":[]}'
BASE_URL="$BASE" python3 - <<'PY'
import json, os, urllib.request, urllib.error
req = urllib.request.Request(
    os.environ["BASE_URL"] + "/verifications/batch",
    data=json.dumps({"items": [
        {"film_id": "f", "venue_id": "v", "start_time": "2026-10-05T19:00:00Z",
         "idempotency_key": f"too-many-{i}"} for i in range(101)
    ]}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as exc:
    print(f"101 items -> HTTP {exc.code}")
PY
curl -s -o /dev/null -w 'bad start_time -> HTTP %{http_code}\n' \
  -X POST "$BASE/verifications/batch" -H 'Content-Type: application/json' -d '{
  "items":[{"film_id":"film-autumn-river","venue_id":"hall-1",
            "start_time":"not-a-time","idempotency_key":"bad-item"}]}'

echo
echo "== 9. 核验流水：batch-001..005 之外，第 4 步只新增 batch-006 一条（批内重放不新增），"
echo "      第 5 步整批回滚无 batch-008 =="
curl -s "$BASE/verifications?limit=50" | pretty
