#!/usr/bin/env bash
# 快照统一外部阅览时段端到端示例：
#   不带时段的既有发布完全兼容 -> 时段校验（成对/开放早于截止/截止未到）->
#   带时段发布与窗口内读取 -> 同一版本后续签发：省略沿用首次设置、显式不同 409 ->
#   窗口外（未开放/已截止）读取：单快照令牌与共享凭证均模糊 403，不扣次数、不写审计 ->
#   受控申请冻结时段：批准时截止已到 422 仍待审可驳回；冻结时段冲突 409
# 依赖：curl、jq、GNU date
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
utc() { date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ; }

step "1. 创建记录；不带时段的发布与读取保持既有行为（响应时段字段为 null）"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
RID=$(echo "${CREATE}" | jq -r .record_id)
PUB=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d '{"version": 1}')
echo "${PUB}" | jq '{snapshot_id, opens_at, closes_at, max_reads}'
SID0=$(echo "${PUB}" | jq -r .snapshot_id)
TOK0=$(echo "${PUB}" | jq -r .token)
printf '无时段快照公开读取: HTTP %s（公开成功响应不含时段字段）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID0}?token=${TOK0}")"

step "2. 时段校验：只填一个 / 开放不早于截止 / 提交时截止已到 -> 422，不产生快照"
printf '只填 opens_at:   HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 1, \"opens_at\": \"$(utc '-1 hour')\"}")"
printf '开放晚于截止:    HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 1, \"opens_at\": \"$(utc '+2 hour')\", \"closes_at\": \"$(utc '+1 hour')\"}")"
printf '截止已到:        HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 1, \"opens_at\": \"$(utc '-2 hour')\", \"closes_at\": \"$(utc '-1 hour')\"}")"

step "3. 修订出版本 2 并带时段发布（开放 1 小时前，截止 1 小时后，可带任意时区）"
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' >/dev/null
OPENS_AT=$(utc '-1 hour')
CLOSES_AT=$(utc '+1 hour')
PUB2=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 2, \"max_reads\": 5, \"opens_at\": \"${OPENS_AT}\", \"closes_at\": \"${CLOSES_AT}\"}")
echo "${PUB2}" | jq '{snapshot_id, opens_at, closes_at, max_reads}'
SID2=$(echo "${PUB2}" | jq -r .snapshot_id)
TOK2=$(echo "${PUB2}" | jq -r .token)
printf '窗口内公开读取: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID2}?token=${TOK2}")"
echo "维护侧记录查询返回快照时段:"
curl -sS "${BASE_URL}/records/${RID}" "${AUTH[@]}" \
  | jq '.snapshots[] | {version, opens_at, closes_at, status}'

step "4. 同一版本只保留首次时段：省略沿用；相同时段（不同时区写法）可再签发；不同则 409"
# 省略时段 -> 沿用首次设置
AGAIN=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d '{"version": 2}')
echo "${AGAIN}" | jq '{token, opens_at, closes_at}'
# 同一时刻的不同时区写法（+08:00）不算冲突
OPENS_E8=$(TZ=Asia/Shanghai date -d "@$(date -d "${OPENS_AT}" +%s)" +%Y-%m-%dT%H:%M:%S+08:00)
CLOSES_E8=$(TZ=Asia/Shanghai date -d "@$(date -d "${CLOSES_AT}" +%s)" +%Y-%m-%dT%H:%M:%S+08:00)
printf '相同时段(+08:00 写法)再签发: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 2, \"opens_at\": \"${OPENS_E8}\", \"closes_at\": \"${CLOSES_E8}\"}")"
# 显式填写不同时段 -> 409，快照不被改写
curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 2, \"opens_at\": \"$(utc '-2 hour')\", \"closes_at\": \"$(utc '+2 hour')\"}" | jq .
echo "冲突后快照时段仍为首设置:"
curl -sS "${BASE_URL}/records/${RID}" "${AUTH[@]}" \
  | jq '.snapshots[] | select(.version == 2) | {version, opens_at, closes_at}'

step "5. 窗口外读取：未开放的快照一律模糊 403，不扣次数、不写审计"
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 2,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' >/dev/null
PUB3=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d "{\"version\": 3, \"max_reads\": 3, \"opens_at\": \"$(utc '+1 hour')\", \"closes_at\": \"$(utc '+2 hour')\"}")
SID3=$(echo "${PUB3}" | jq -r .snapshot_id)
TOK3=$(echo "${PUB3}" | jq -r .token)
printf '未开放读取: HTTP %s  ' "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}")"
curl -sS "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}"
echo
echo "次数未被消耗（仍为 0/3），无成功读取审计事件:"
curl -sS "${BASE_URL}/tokens/${TOK3}" "${AUTH[@]}" | jq '{max_reads, reads_used, remaining_reads}'
curl -sS "${BASE_URL}/snapshots/${SID3}/read-events?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z" \
  "${AUTH[@]}" | jq '.events | length'

step "6. 共享凭证同样受窗口约束：窗口外 403 不扣共享额度，清单内其他快照不受影响"
PUB_B=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "另一条机构记录，内容B。", "intervals": [{"start": 4, "end": 7}]}')
RID_B=$(echo "${PUB_B}" | jq -r .record_id)
PUBB=$(curl -sS -X POST "${BASE_URL}/records/${RID_B}/publish" "${AUTH[@]}" -d '{"version": 1}')
SID_B=$(echo "${PUBB}" | jq -r .snapshot_id)
SUBMIT=$(curl -sS -X POST "${BASE_URL}/bundle-requests" -u "alice:${ALICE_PW}" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\": [\"${SID3}\", \"${SID_B}\"], \"max_reads\": 5}")
BREQ=$(echo "${SUBMIT}" | jq -r .request_id)
APPROVE=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BREQ}/approve" -u "bob:${BOB_PW}")
BUNDLE_ID=$(echo "${APPROVE}" | jq -r .bundle_id)
CRED=$(echo "${APPROVE}" | jq -r .credential)
printf '共享凭证读未开放快照: HTTP %s（不扣额度）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${CRED}")"
printf '共享凭证读清单内无时段快照: HTTP %s\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID_B}?token=${CRED}")"
curl -sS "${BASE_URL}/read-bundles/${BUNDLE_ID}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads}'

step "7. 受控发布：申请冻结时段；批准时截止已到 -> 422，申请仍待审、可驳回"
curl -sS -X PUT "${BASE_URL}/records/${RID}/controlled" "${AUTH[@]}" >/dev/null
# 修订出版本 4（尚无快照），对该版本提交受控申请
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 3,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' >/dev/null
SHORT_CLOSE=$(utc '+2 seconds')
REQ=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish-requests" -u "alice:${ALICE_PW}" \
  -H 'Content-Type: application/json' \
  -d "{\"version\": 4, \"opens_at\": \"$(utc '-1 hour')\", \"closes_at\": \"${SHORT_CLOSE}\"}")
PRID=$(echo "${REQ}" | jq -r .request_id)
echo "申请详情可见冻结时段:"
curl -sS "${BASE_URL}/publish-requests/${PRID}" -u "alice:${ALICE_PW}" \
  | jq '{status, opens_at, closes_at}'
echo "（等待 3 秒让截止时刻过去…）"
sleep 3
printf '截止后批准: HTTP %s  ' "$(code -X POST "${BASE_URL}/publish-requests/${PRID}/approve" -u "bob:${BOB_PW}")"
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID}/approve" -u "bob:${BOB_PW}"
echo
echo "申请仍待审:"
curl -sS "${BASE_URL}/publish-requests/${PRID}" -u "bob:${BOB_PW}" | jq '{status}'
echo "仍可驳回:"
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID}/reject" -u "bob:${BOB_PW}" \
  -H 'Content-Type: application/json' -d '{"reason":"阅览时段已过"}' | jq '{status, reviewed_by}'

step "8. 受控发布：两个申请冻结不同时段，先批准者定版，后者 409 仍待审可驳回"
REQ_A=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish-requests" -u "alice:${ALICE_PW}" \
  -H 'Content-Type: application/json' \
  -d "{\"version\": 4, \"opens_at\": \"$(utc '-1 hour')\", \"closes_at\": \"$(utc '+1 hour')\"}")
PRID_A=$(echo "${REQ_A}" | jq -r .request_id)
REQ_B=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish-requests" -u "alice:${ALICE_PW}" \
  -H 'Content-Type: application/json' \
  -d "{\"version\": 4, \"opens_at\": \"$(utc '-2 hour')\", \"closes_at\": \"$(utc '+3 hour')\"}")
PRID_B=$(echo "${REQ_B}" | jq -r .request_id)
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID_A}/approve" -u "bob:${BOB_PW}" \
  | jq '{status, snapshot_id, opens_at, closes_at}'
printf '冻结不同时段的申请批准: HTTP %s（申请仍待审）\n' \
  "$(code -X POST "${BASE_URL}/publish-requests/${PRID_B}/approve" -u "bob:${BOB_PW}")"
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID_B}/reject" -u "bob:${BOB_PW}" \
  -H 'Content-Type: application/json' -d '{"reason":"时段与首次发布不一致"}' | jq '{status}'

printf '\n\033[1m快照统一外部阅览时段演示完成。\033[0m\n'
