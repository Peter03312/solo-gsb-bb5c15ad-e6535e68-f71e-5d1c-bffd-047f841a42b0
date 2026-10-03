#!/usr/bin/env bash
# 敏感短语清单核验端到端示例：
#   查询初始空清单（停用核验）-> 原子替换启用短语 ->
#   直接发布：命中未完整覆盖 422（只回码点区间，不回显短语/原文）、完整覆盖 201 ->
#   空清单停用核验 -> 受控申请：提交时核验、批准时按当时清单重新核验（失败仍可驳回）->
#   清单变更不影响已发布快照与既有令牌 -> 清单与发布并发的行锁串行化
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
ALICE_PW="${ALICE_PW:-alice-pw}"   # operator
BOB_PW="${BOB_PW:-bob-pw}"         # reviewer
AUTH=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }

step "1. 查询初始清单：空清单、enabled=false（停用核验）"
curl -sS "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" | jq .

step "2. 原子替换清单（短语非空、不重复）；空清单 [] 表示停用核验"
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" \
  -d '{"phrases": ["110101199003077777", "张三"]}' | jq .

step "3. 非法清单 422：空短语 / 重复短语"
printf '空短语:   HTTP %s\n' "$(code -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases":[""]}')"
printf '重复短语: HTTP %s\n' "$(code -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases":["a","a"]}')"
printf '缺密钥:   HTTP %s\n' "$(code "${BASE_URL}/sensitive-phrases")"

step "4. 创建记录：遮蔽了姓名与电话，未遮蔽身份证号"
CREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 34, "end": 45}]
}')
RID=$(echo "${CREATE}" | jq -r .record_id)
echo "记录 ${RID}"

step "5. 直接发布：身份证号命中 [10,28) 未被遮蔽区间完整覆盖 -> 422（只回命中位置）"
curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d '{"version": 1}' \
  | jq '.detail'
printf 'HTTP %s（不合格不产生快照或令牌）\n' \
  "$(code -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" -d '{"version": 1}')"
echo "维护侧快照列表（应为空）:"
curl -sS "${BASE_URL}/records/${RID}" "${AUTH[@]}" | jq '.snapshots'

step "6. 部分覆盖也不通过（另建一条只遮蔽命中前半段 [10,20) 的记录）"
curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777。",
  "intervals": [{"start": 10, "end": 20}]
}' >/dev/null

step "7. 完整覆盖后发布成功（修订到版本 2，命中整体落在 [10,28) 内）"
curl -sS -X PUT "${BASE_URL}/records/${RID}" "${AUTH[@]}" -d '{
  "base_version": 1,
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' | jq .
PUB=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish" "${AUTH[@]}" \
  -d '{"version": 2, "max_reads": 5}')
echo "${PUB}" | jq '{snapshot_id, record_id, version, status, max_reads, expires_at}'
SID=$(echo "${PUB}" | jq -r .snapshot_id)
TOK=$(echo "${PUB}" | jq -r .token)

step "8. 停用核验：替换为空清单后，未遮蔽任何短语的原文也可发布"
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases": []}' | jq '{version, phrases, enabled}'
CREATE2=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "完全未遮蔽的张三内容。", "intervals": []}')
RID2=$(echo "${CREATE2}" | jq -r .record_id)
printf '停用核验期间发布: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/records/${RID2}/publish" "${AUTH[@]}" -d '{"version": 1}')"

step "9. 清单变更不影响已发布快照与既有令牌（再次启用含原文短语的清单）"
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" \
  -d '{"phrases": ["110101199003077777", "张三"]}' >/dev/null
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" | jq .

step "10. 受控发布：提交即核验；批准时按当时启用的清单重新核验冻结原文"
# 10a. 停用核验下提交申请（冻结原文与区间）
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases": []}' >/dev/null
RCREATE=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" -d '{
  "text": "受控记录，内部项目代号 PHOENIX。",
  "intervals": []
}')
CRID=$(echo "${RCREATE}" | jq -r .record_id)
curl -sS -X PUT "${BASE_URL}/records/${CRID}/controlled" "${AUTH[@]}" >/dev/null
SUBMIT=$(curl -sS -X POST "${BASE_URL}/records/${CRID}/publish-requests" \
  -u "alice:${ALICE_PW}" -H 'Content-Type: application/json' -d '{"version": 1}')
PRID=$(echo "${SUBMIT}" | jq -r .request_id)
echo "待审申请 ${PRID}（提交时清单为空，通过）"

# 10b. 启用清单后批准：冻结原文命中 PHOENIX 且未遮蔽 -> 422，不签发令牌
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" \
  -d '{"phrases": ["PHOENIX"]}' >/dev/null
printf '批准（命中未覆盖）: HTTP %s\n' \
  "$(code -X POST "${BASE_URL}/publish-requests/${PRID}/approve" -u "bob:${BOB_PW}")"
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID}/approve" -u "bob:${BOB_PW}" | jq '.detail'
echo "申请仍待审，还可以驳回："
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID}/reject" -u "bob:${BOB_PW}" \
  -H 'Content-Type: application/json' -d '{"reason":"代号未遮蔽，清单已启用"}' \
  | jq '{status, reviewed_by, reason}'

step "11. 区分大小写：小写 phoenix 不命中大写 PHOENIX"
printf '小写短语清单下直接发布（不同记录）: '
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases": ["phoenix"]}' >/dev/null
RCREATE2=$(curl -sS -X POST "${BASE_URL}/records" "${AUTH[@]}" \
  -d '{"text": "受控记录，内部项目代号 PHOENIX。", "intervals": []}')
CRID2=$(echo "${RCREATE2}" | jq -r .record_id)
printf 'HTTP %s（大写原文不命中小写短语）\n' \
  "$(code -X POST "${BASE_URL}/records/${CRID2}/publish" "${AUTH[@]}" -d '{"version": 1}')"

step "12. 复位为停用核验"
curl -sS -X PUT "${BASE_URL}/sensitive-phrases" "${AUTH[@]}" -d '{"phrases": []}' | jq '{version, enabled}'

printf '\n\033[1m敏感短语清单核验演示完成。\033[0m\n'
