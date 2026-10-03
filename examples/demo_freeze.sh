#!/usr/bin/env bash
# 记录临时冻结端到端示例：
#   发布两个版本（单快照令牌）+ 含两个快照的共享凭证 ->
#   鉴权与明确拒绝（缺密钥 401/未知记录 404/空白原因 422）->
#   冻结（当前冻结期原因与开始时间不被重复冻结覆盖）->
#   冻结提交后的公开读取：有效单快照令牌/共享凭证均 403 snapshot unavailable，
#   不扣次数、不写成功读取审计；共享凭证中其他记录的快照仍可读 ->
#   冻结期间新发布快照同样被阻断 -> 解除冻结恢复原本仍有效的读取资格 ->
#   已撤销令牌与已撤回快照解除后不复活 -> 再次冻结开启新冻结期
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

step "1. 创建记录并发布版本 1（令牌上限 3 次）与版本 2"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}')
RID=$(echo "${CREATE}" | jq -r .record_id)
PUB1=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d '{"version": 1, "max_reads": 3}')
SID1=$(echo "${PUB1}" | jq -r .snapshot_id)
TOK1=$(echo "${PUB1}" | jq -r .token)
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者李四，联系方式已变更。",
  "intervals": [{"start": 2, "end": 4}]
}' >/dev/null
PUB2=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d '{"version": 2}')
SID2=$(echo "${PUB2}" | jq -r .snapshot_id)
TOK2=$(echo "${PUB2}" | jq -r .token)
echo "记录 ${RID}: 版本1快照 ${SID1}（3 次额度），版本2快照 ${SID2}"

step "2. 另一条记录（冻结本记录后，它的快照仍应可读）"
CREATE_B=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "另一条机构记录，内容B。", "intervals": [{"start": 4, "end": 7}]}')
RID_B=$(echo "${CREATE_B}" | jq -r .record_id)
PUB_B=$(curl -sS -X POST "${BASE_URL}/records/${RID_B}/publish" "${AUTH[@]}" \
  -d '{"version": 1}')
SID_B=$(echo "${PUB_B}" | jq -r .snapshot_id)
TOK_B=$(echo "${PUB_B}" | jq -r .token)

step "3. 为记录 A 的两个快照签发多快照共享凭证（共享额度 10）"
SUBMIT=$(curl -sS -X POST "${BASE_URL}/bundle-requests" -u "alice:${ALICE_PW}" \
  -H 'Content-Type: application/json' \
  -d "{\"snapshot_ids\": [\"${SID1}\", \"${SID2}\"], \"max_reads\": 10}")
BREQ=$(echo "${SUBMIT}" | jq -r .request_id)
APPROVE=$(curl -sS -X POST "${BASE_URL}/bundle-requests/${BREQ}/approve" -u "bob:${BOB_PW}")
BUNDLE_ID=$(echo "${APPROVE}" | jq -r .bundle_id)
CRED=$(echo "${APPROVE}" | jq -r .credential)
echo "共享凭证: ${CRED}"

step "4. 冻结前：单快照令牌与共享凭证均可读"
curl -sS "${BASE_URL}/public/snapshots/${SID1}?token=${TOK1}" | jq .
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${CRED}" | jq '{record_id, version, status}'

step "5. 明确拒绝：缺密钥 401、未知记录 404、空白原因 422"
printf '缺服务密钥: HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/freeze" \
  -H 'Content-Type: application/json' -d '{"reason":"x"}')"
UNKNOWN="00000000-0000-0000-0000-000000000000"
printf '未知记录:   HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${UNKNOWN}/freeze" "${AUTH[@]}" \
  -d '{"reason":"x"}')"
printf '空白原因:   HTTP %s\n' "$(code -X POST "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" \
  -d '{"reason":"   "}')"

step "6. 首次冻结成功：返回当前冻结期开始时间与原因"
FREEZE=$(curl -sS -X POST "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" \
  -d '{"reason":"接到临时阻断要求，暂停该记录外部阅览"}')
echo "${FREEZE}" | jq .
FROZEN_AT=$(echo "${FREEZE}" | jq -r .frozen_at)

step "7. 重复冻结（不同原因/空原因）幂等，原因与开始时间不被覆盖"
curl -sS -X POST "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" \
  -d '{"reason":"试图覆盖的另一个原因"}' \
  | jq --arg first "${FROZEN_AT}" -e '{frozen, already_frozen, frozen_at, reason} | .already_frozen == true and .frozen_at == $first and (.reason | contains("临时阻断"))'
curl -sS -X POST "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" -d '{}' \
  | jq '{frozen, already_frozen, frozen_at, reason}'
echo "状态查询（原因仅维护侧可见）:"
curl -sS "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" | jq .

step "8. 冻结提交后的公开读取：单快照令牌与共享凭证均 403 snapshot unavailable"
for T in "${TOK1}" "${TOK2}" "${CRED}" "not-a-token"; do
  printf '版本1(令牌TOK1)/版本2  HTTP %s  ' "$(code "${BASE_URL}/public/snapshots/${SID1}?token=${T}")"
  curl -sS "${BASE_URL}/public/snapshots/${SID1}?token=${T}"
  echo
done

step "9. 阻断不扣次数、不写成功读取审计"
curl -sS "${BASE_URL}/tokens/${TOK1}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active}'
curl -sS "${BASE_URL}/read-bundles/${BUNDLE_ID}" "${AUTH[@]}" \
  | jq '{max_reads, reads_used, remaining_reads, active}'
echo "版本1快照的成功读取审计事件数（冻结前仅成功 1 次，阻断不新增）:"
curl -sS "${BASE_URL}/snapshots/${SID1}/read-events?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z" \
  "${AUTH[@]}" | jq '.events | length'

step "10. 共享凭证中其他记录（记录 B）仍可读"
curl -sS "${BASE_URL}/public/snapshots/${SID_B}?token=${TOK_B}" | jq .

step "11. 冻结期间新发布的快照同样被阻断"
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 2,
  "text": "冻结期间修订发布的版本三。",
  "intervals": [{"start": 0, "end": 3}]
}' >/dev/null
PUB3=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d '{"version": 3, "max_reads": 5}')
SID3=$(echo "${PUB3}" | jq -r .snapshot_id)
TOK3=$(echo "${PUB3}" | jq -r .token)
printf '冻结期内新快照公开读取: HTTP %s（发布本身 201，发布/复核鉴权与响应不变）\n' \
  "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}")"

step "12. 解除冻结：恢复原本仍有效的读取资格（重复解除幂等）"
curl -sS -X POST "${BASE_URL}/records/${RID}/unfreeze" "${AUTH[@]}" | jq .
curl -sS -X POST "${BASE_URL}/records/${RID}/unfreeze" "${AUTH[@]}" \
  | jq '{frozen, already_unfrozen}'
curl -sS "${BASE_URL}/public/snapshots/${SID2}?token=${TOK2}" | jq '{record_id, version, status}'
curl -sS "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}" | jq '{record_id, version, status}'

step "13. 解除不复活已撤销凭证：撤销 TOK3 后再冻结/解除，读取仍 403"
curl -sS -X POST "${BASE_URL}/tokens/${TOK3}/revoke" "${AUTH[@]}" | jq .
curl -sS -X POST "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" -d '{"reason":"第二轮临时阻断"}' >/dev/null
curl -sS -X POST "${BASE_URL}/records/${RID}/unfreeze" "${AUTH[@]}" >/dev/null
printf '已撤销令牌解除后读取: HTTP %s\n' "$(code "${BASE_URL}/public/snapshots/${SID3}?token=${TOK3}")"
echo "冻结状态恢复为未冻结:"
curl -sS "${BASE_URL}/records/${RID}/freeze" "${AUTH[@]}" | jq .

printf '\n\033[1m记录临时冻结演示完成。\033[0m\n'
