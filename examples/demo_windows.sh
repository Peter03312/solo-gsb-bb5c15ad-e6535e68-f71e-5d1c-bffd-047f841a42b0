#!/usr/bin/env bash
# 快照外部阅览时段（统一开放窗口）端到端示例：
#   带窗口直接发布 -> 窗口内可读/窗口外模糊 403 不扣次 -> 维护侧查窗口 ->
#   重复签发：省略沿用 / 相同幂等 / 不同 409 不改写 -> 无窗口快照不能补设 ->
#   受控申请冻结窗口 -> 截止后批准被拒且仍待审可驳回 -> 共享凭证逐快照判定
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator（申请人）
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer（复核人）
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

# 以服务端当前时间为基准生成 ISO 8601 带时区时刻（这里用 UTC；也可传 +08:00 偏移）
iso() { date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v"$1" +%Y-%m-%dT%H:%M:%SZ; }

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

OPEN_SOON="$(iso '+5 seconds')"
CLOSE_LATER="$(iso '+120 seconds')"
OPEN_PAST="$(iso '-30 seconds')"
CLOSE_SOON="$(iso '+3 seconds')"
# 第 2~7 步共用的、在整段演示期间保持开放的窗口（验证“相同设置幂等”需要截止未到）
OPEN_STEP2="$(iso '-10 seconds')"
CLOSE_STEP2="$(iso '+100 seconds')"

step "0. 健康检查"
curl -sS "${BASE_URL}/healthz" | jq .

step "1. 维护端创建记录（版本 1）"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
echo "${CREATE}" | jq .
RID=$(echo "${CREATE}" | jq -r .record_id)

step "2. 发布时携带外部阅览时段（开放可在过去=立即开放；截止必须晚于当前）"
# 本记录使用“长窗口”供第 2、6、7 步复用（相同设置幂等要求截止时刻仍在未来）；
# 窗口边界（开放含、截止不含）另在第 3b 步用短窗口专门演示。
PUB=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d "{
  \"version\": 1,
  \"max_reads\": 5,
  \"window_open_at\": \"${OPEN_STEP2}\",
  \"window_close_at\": \"${CLOSE_STEP2}\"
}")
echo "${PUB}" | jq '{snapshot_id, version, status, token, max_reads, window_open_at, window_close_at}'
SID=$(echo "${PUB}" | jq -r .snapshot_id)
TOKEN=$(echo "${PUB}" | jq -r .token)

step "3. 当前处于窗口内：公开读取 200"
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOKEN}" | jq .

step "3b. 窗口边界（开放时刻起含、截止时刻前不含）：另发一条短窗口快照"
CREATE_S=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "短窗口记录。", "intervals": []}')
RID_S=$(echo "${CREATE_S}" | jq -r .record_id)
PUB_S=$(curl -sS -X POST "${BASE_URL}/records/${RID_S}/publish" "${AUTH[@]}" -d "{
  \"version\": 1, \"max_reads\": 5,
  \"window_open_at\": \"${OPEN_PAST}\", \"window_close_at\": \"${CLOSE_SOON}\"
}")
SID_S=$(echo "${PUB_S}" | jq -r .snapshot_id)
TOKEN_S=$(echo "${PUB_S}" | jq -r .token)
curl -sS "${BASE_URL}/public/snapshots/${SID_S}?token=${TOKEN_S}" | jq .
echo "等待窗口关闭（约 4 秒）……"
sleep 4
printf '窗口外读取: HTTP %s（模糊 403，不透露原因）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID_S}?token=${TOKEN_S}")"
echo "维护侧令牌计数：窗口外的 403 不扣次数（reads_used 仍为 1）"
curl -sS "${BASE_URL}/tokens/${TOKEN_S}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active}'

step "4. 维护侧记录状态返回每个快照的时段"
curl -sS "${BASE_URL}/records/${RID}" "${AUTH[@]}" \
  | jq '.snapshots[] | {version, status, window_open_at, window_close_at}'

step "5. 非法时段被请求层 422 拒绝（只填其一 / 开放不早于截止 / 截止已到）"
printf '只填开放: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\":1,\"window_open_at\":\"${OPEN_SOON}\"}")"
printf '开放晚于截止: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\":1,\"window_open_at\":\"${CLOSE_LATER}\",\"window_close_at\":\"${OPEN_SOON}\"}")"
printf '截止已到: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d '{"version":1,"window_open_at":"2000-01-01T00:00:00Z","window_close_at":"2000-01-02T00:00:00Z"}')"

step "6. 同版本再次签发：省略时段沿用首次设置（签出新令牌，窗口不变）"
PUB2=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d '{"version": 1}')
echo "${PUB2}" | jq '{snapshot_id, token, window_open_at, window_close_at}'
TOKEN2=$(echo "${PUB2}" | jq -r .token)
printf '长窗口仍开放，新令牌可读: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID}?token=${TOKEN2}")"

step "7. 显式相同时段 -> 幂等(201)；显式不同时段 -> 409，快照不被改写"
printf '相同设置重复发布: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\":1,\"window_open_at\":\"${OPEN_STEP2}\",\"window_close_at\":\"${CLOSE_STEP2}\"}")"
echo "不同设置："
curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d "{
  \"version\": 1, \"window_open_at\": \"${OPEN_SOON}\", \"window_close_at\": \"${CLOSE_LATER}\"
}" | jq .

step "8. 无窗口快照永远不能补设窗口（首次未带即无时段限制）"
CREATE2=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "另一条记录，内容B。", "intervals": [{"start": 0, "end": 3}]}')
RID2=$(echo "${CREATE2}" | jq -r .record_id)
curl -sS -X POST "${BASE_URL}/records/${RID2}/publish" "${AUTH[@]}" -d '{"version": 1}' | jq .
printf '后续补设窗口: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID2}/publish" "${AUTH[@]}" \
  -d "{\"version\":1,\"window_open_at\":\"${OPEN_SOON}\",\"window_close_at\":\"${CLOSE_LATER}\"}")"

step "9. 受控发布：申请随版本冻结窗口，批准时截止已到则拒绝批准（仍待审、可驳回）"
CREATE3=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "受控记录原文。", "intervals": []}')
RID3=$(echo "${CREATE3}" | jq -r .record_id)
curl -sS -X PUT "${BASE_URL}/records/${RID3}/controlled" "${AUTH[@]}" | jq .
SUB=$(curl -sS -X POST "${BASE_URL}/records/${RID3}/publish-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" -d "{
    \"version\": 1,
    \"window_open_at\": \"$(iso '-10 seconds')\",
    \"window_close_at\": \"$(iso '+2 seconds')\"
  }")
echo "${SUB}" | jq '{request_id, status, window_open_at, window_close_at}'
REQ=$(echo "${SUB}" | jq -r .request_id)
echo "申请详情可见冻结时段（不含原文）："
curl -sS "${BASE_URL}/publish-requests/${REQ}" -u "alice:${ALICE_PW}" \
  | jq '{status, window_open_at, window_close_at, snapshot_id}'
echo "等待冻结窗口关闭后再批准……"
sleep 3
printf '截止后批准: HTTP %s（拒绝批准）\n' \
  "$(code -X POST "${BASE_URL}/publish-requests/${REQ}/approve" -u "bob:${BOB_PW}")"
echo "申请仍待审，可由复核人驳回："
curl -sS -X POST "${BASE_URL}/publish-requests/${REQ}/reject" \
  -u "bob:${BOB_PW}" -H "Content-Type: application/json" \
  -d '{"reason":"窗口已过，不予公开"}' | jq '{status, reason}'

step "10. 共享凭证：窗口按快照独立判定，窗口外只拒该项、不扣共享额度"
# 两条记录各自发布：A 带窗口（即将关闭），B 无窗口
CREATE_A=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "共享清单 A 内容。", "intervals": []}')
RID_A=$(echo "${CREATE_A}" | jq -r .record_id)
SID_A=$(curl -sS -X POST "${BASE_URL}/records/${RID_A}/publish" "${AUTH[@]}" -d "{
  \"version\":1,\"window_open_at\":\"$(iso '-10 seconds')\",\"window_close_at\":\"$(iso '+2 seconds')\"
}" | jq -r .snapshot_id)
CREATE_B=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "共享清单 B 内容。", "intervals": []}')
RID_B=$(echo "${CREATE_B}" | jq -r .record_id)
SID_B=$(curl -sS -X POST "${BASE_URL}/records/${RID_B}/publish" "${AUTH[@]}" \
  -d '{"version":1}' | jq -r .snapshot_id)
BREQ=$(curl -sS -X POST "${BASE_URL}/bundle-requests" \
  -u "alice:${ALICE_PW}" -H "Content-Type: application/json" \
  -d "{\"snapshot_ids\":[\"${SID_A}\",\"${SID_B}\"],\"max_reads\":5}")
BID=$(echo "${BREQ}" | jq -r .request_id)
CRED=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BID}/approve" -u "bob:${BOB_PW}" | jq -r .credential)
printf '窗口内读 A: HTTP %s；读 B: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID_A}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${SID_B}?token=${CRED}")"
echo "等待 A 的窗口关闭……"
sleep 3
printf '窗口外读 A: HTTP %s；B 不受影响: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID_A}?token=${CRED}")" \
  "$(code "${BASE_URL}/public/snapshots/${SID_B}?token=${CRED}")"
echo "共享额度（窗口外 403 未扣次，reads_used 应为 3）："
curl -sS "${BASE_URL}/read-bundles" "${AUTH[@]}" \
  | jq ".bundles[] | select(.credential==\"${CRED}\") | {max_reads, reads_used, remaining_reads}"

printf '\n\033[1m外部阅览时段演示完成。\033[0m\n'
