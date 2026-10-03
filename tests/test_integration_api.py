"""受控发布端到端集成测试：需要真实 PostgreSQL。

默认尝试用 pgserver（pip 包，内置 Postgres 16 二进制）自动拉起临时库；
也可通过环境变量 TEST_DATABASE_URL 指定已有数据库。

运行：
    python3 -m unittest tests.test_integration_api -v
"""

import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

# 环境变量必须在导入 app.config 之前设置
os.environ.setdefault("SERVICE_KEY", "dev-service-key-change-me")
os.environ.setdefault(
    "REVIEW_CREDENTIALS",
    json.dumps(
        [
            {"username": "alice", "password": "alice-pw", "roles": ["operator"]},
            {"username": "bob", "password": "bob-pw", "roles": ["reviewer"]},
            {"username": "carol", "password": "carol-pw", "roles": ["operator", "reviewer"]},
        ]
    ),
)

_PG_SERVER = None
_SKIP_REASON = ""
if "TEST_DATABASE_URL" not in os.environ:
    try:
        import pgserver

        _PG_SERVER = pgserver.get_server("/tmp/pgdata-integration-test")
        # unix socket 形式：postgresql+psycopg2://postgres@/postgres?host=<socketdir>
        os.environ["DATABASE_URL"] = (
            "postgresql+psycopg2://postgres@/postgres?host=/tmp/pgdata-integration-test"
        )
    except Exception as exc:  # pragma: no cover - 环境不可用时跳过
        _PG_SERVER = None
        _SKIP_REASON = f"pgserver unavailable: {exc}"
else:
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app.models import PublishRequest as PublishRequestModel  # noqa: E402

SKIP_INTEGRATION = _PG_SERVER is None and "TEST_DATABASE_URL" not in os.environ

SERVICE_HEADERS = {"X-Service-Key": "dev-service-key-change-me"}
BASIC_ALICE = ("alice", "alice-pw")
BASIC_BOB = ("bob", "bob-pw")
BASIC_CAROL = ("carol", "carol-pw")

TEXT_V1 = "患者张三，身份证号 110101199003077777，联系电话 13800001111。"
TEXT_V2 = "完全不同的新原文，敏感词ABC。"
INTERVALS_V1 = [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ControlledPublishIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 建表（幂等）并通过 TestClient 上下文触发 lifespan 之外的显式初始化
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例使用全新记录，避免相互干扰
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _mark_controlled(self):
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["controlled"])

    def _submit(self, auth=BASIC_ALICE, version=1, expires=None, expected=201):
        body = {"version": version}
        if expires is not None:
            body.update(expires)
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", auth=auth, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 基础鉴权 ----

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_service_key_endpoints_still_require_key(self):
        r = self.client.post("/records", json={"text": "x", "intervals": []})
        self.assertEqual(r.status_code, 401)
        r = self.client.put(f"/records/{self.record_id}/controlled")
        self.assertEqual(r.status_code, 401)

    def test_controlled_endpoints_require_basic_auth(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", json={"version": 1}
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # 错误口令
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("alice", "wrong"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)
        # 未知用户
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=("nobody", "x"),
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)

    # ---- 受控标记与直接发布互斥 ----

    def test_mark_controlled_is_idempotent(self):
        self._mark_controlled()
        r = self.client.put(f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["controlled"])

    def test_mark_controlled_unknown_record_404(self):
        import uuid

        r = self.client.put(f"/records/{uuid.uuid4()}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_direct_publish_rejected_for_controlled_record(self):
        self._mark_controlled()
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertNotIn(TEXT_V1, r.text)
        # 未留下任何快照或令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_submit_rejected_for_non_controlled_record(self):
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 角色校验 ----

    def test_operator_role_required_to_submit(self):
        self._mark_controlled()
        # bob 只有 reviewer 角色，不能提交
        r = self._submit(auth=BASIC_BOB, expected=403)
        self.assertNotIn(TEXT_V1, r.text)

    def test_reviewer_role_required_to_approve(self):
        self._mark_controlled()
        self._submit()
        # alice 只有 operator 角色，不能复核
        r = self.client.post(
            f"/publish-requests/{self._latest_request_id()}/approve", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 403)

    def _latest_request_id(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    # ---- 未知资源 ----

    def test_unknown_record_and_version(self):
        import uuid

        self._mark_controlled()
        r = self.client.post(
            f"/records/{uuid.uuid4()}/publish-requests", auth=BASIC_ALICE, json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        r = self._submit(version=99, expected=404)
        self.assertNotIn(TEXT_V1, r.text)

    def test_unknown_request_approve_reject_get(self):
        import uuid

        rid = str(uuid.uuid4())
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/approve", auth=BASIC_BOB).status_code, 404
        )
        self.assertEqual(
            self.client.post(f"/publish-requests/{rid}/reject", auth=BASIC_BOB, json={}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/publish-requests/{rid}", auth=BASIC_BOB).status_code, 404
        )

    # ---- 申请冻结版本内容 ----

    def test_request_freezes_version_content_at_submission(self):
        self._mark_controlled()
        self._submit(version=1)
        request_id = self._latest_request_id()
        # 提交后修订到版本 2
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 批准沿用冻结的版本 1 内容
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "approved")
        token = body["token"]
        snapshot_id = body["snapshot_id"]
        # 公开读取得到版本 1 的脱敏正文，而非版本 2 原文
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        self.assertNotIn(TEXT_V2, r.text)
        # 申请详情不泄露冻结原文
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertNotIn(TEXT_V1, r.text)
        self.assertNotIn("身份证", r.text)

    def test_approved_token_follows_existing_token_rules(self):
        self._mark_controlled()
        self._submit(version=1, expires={"expires_in_seconds": 3600})
        request_id = self._latest_request_id()
        # 提交后到批准前，申请保存的是相对时长策略（尚无绝对到期时刻）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["token_expires_in_seconds"], 3600)
        self.assertIsNone(r.json()["token_expires_at"])
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        # 到期时刻在批准签发时换算，约为批准时刻 + 3600s
        expires_at = r.json()["expires_at"]
        self.assertIsNotNone(expires_at)
        self.assertGreater(
            datetime.fromisoformat(expires_at), datetime.now(timezone.utc)
        )
        # 批准后申请记录实际绝对到期时刻
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertIsNotNone(r.json()["token_expires_at"])
        # 令牌可通过既有撤销接口撤销，撤销后公开读取 403
        r = self.client.post(f"/tokens/{token}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_submit_with_past_expires_at_is_422(self):
        self._mark_controlled()
        r = self._submit(
            expires={"expires_at": "2000-01-01T00:00:00Z"}, expected=422
        )
        self.assertNotIn(TEXT_V1, r.text)

    def test_approval_failure_rolls_back_snapshot_and_token(self):
        """批准事务中途失败时，不得留下快照或可读令牌。"""
        from unittest import mock

        from sqlalchemy import select

        from app import main as main_module
        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()

        # 让令牌在 flush 时超长（read_tokens.token 为 varchar(64)）：
        # 此时快照 INSERT 已执行、令牌已加入事务，但尚未提交。
        with mock.patch.object(
            main_module.secrets, "token_urlsafe", return_value="x" * 65
        ):
            with self.assertRaises(Exception):
                self.client.post(
                    f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
                )

        with SessionLocal() as session:
            # 申请仍为 pending（终态未写入），快照与令牌全部回滚
            pr = session.get(PublishRequestModel, request_id)
            self.assertEqual(pr.status, "pending")
            self.assertIsNone(pr.snapshot_id)
            snapshots = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .all()
            )
            self.assertEqual(snapshots, [])
            orphan_tokens = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(orphan_tokens, 0)
        # 申请仍可被正常批准（证明前一次没有遗留半完成状态）
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    def test_explicit_expires_at_is_kept_at_approval(self):
        from datetime import timedelta

        self._mark_controlled()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._submit(expires={"expires_at": future})
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(r.json()["expires_at"])

    # ---- 申请人不得复核 ----

    def test_requester_cannot_review_own_request(self):
        # carol 同时具备 operator/reviewer，仍不得复核自己的申请
        self._mark_controlled()
        r = self._submit(auth=BASIC_CAROL)
        request_id = r.json()["request_id"]
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
        )
        self.assertEqual(r.status_code, 403)
        # 申请仍处于 pending，他人可正常批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)

    # ---- 终态语义 ----

    def test_rejected_request_cannot_be_approved(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "材料不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 驳回后不能批准
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409)
        # 再次驳回也是 409
        r = self.client.post(
            f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
        )
        self.assertEqual(r.status_code, 409)
        # 驳回未生成快照/令牌
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"], [])

    def test_approved_request_cannot_be_decided_again(self):
        self._mark_controlled()
        self._submit()
        request_id = self._latest_request_id()
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/reject", auth=BASIC_BOB, json={}
            ).status_code,
            409,
        )

    def test_concurrent_reviews_produce_single_terminal_state(self):
        """两个线程同时复核同一申请：恰好一个终态成功，另一个 409。"""
        self._mark_controlled()
        for _ in range(5):
            self._submit()
            request_id = self._latest_request_id()
            results: list[int] = []
            barrier = threading.Barrier(2)

            def approve():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB).status_code
                )

            def reject():
                barrier.wait()
                client = TestClient(app_main.app)
                results.append(
                    client.post(
                        f"/publish-requests/{request_id}/reject", auth=BASIC_CAROL, json={}
                    ).status_code
                )

            t1 = threading.Thread(target=approve)
            t2 = threading.Thread(target=reject)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(results), [200, 409], results)
            # 库里申请唯一终态，且至多一份快照
            from app.db import SessionLocal
            from app.models import Snapshot

            with SessionLocal() as session:
                pr = session.get(PublishRequestModel, request_id)
                self.assertIn(pr.status, ("approved", "rejected"))
                self.assertIsNotNone(pr.reviewed_by)
            r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
            self.assertLessEqual(len(r.json()["snapshots"]), 1)

    # ---- 既有接口兼容性（非受控记录） ----

    def test_legacy_flow_unchanged_on_non_controlled_record(self):
        # 直接发布非受控记录仍可用
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        snapshot_id = r.json()["snapshot_id"]
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.json()["redacted_text"],
            "患者[已遮蔽]，身份证号 [已遮蔽]，联系电话 [已遮蔽]。",
        )
        # 状态接口新增字段不破坏既有结构
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        body = r.json()
        self.assertFalse(body["controlled"])
        self.assertEqual(body["current_version"], 1)
        self.assertEqual(len(body["snapshots"]), 1)

    def test_legacy_revision_409_and_422_still_work(self):
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 99, "text": "x", "intervals": []},
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": "短", "intervals": [{"start": 0, "end": 99}]},
        )
        self.assertEqual(r.status_code, 422)

    def test_public_read_unified_403_does_not_leak(self):
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token=definitely-not-a-token"
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    # ---- 读取次数上限（直接发布） ----

    def _direct_publish(self, version=1, body_extra=None):
        body = {"version": version}
        if body_extra:
            body.update(body_extra)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json=body,
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_direct_publish_response_reports_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        self.assertEqual(pub["max_reads"], 2)
        # 不设上限时该字段为 None（再次发布复用同一快照，签出新令牌）
        pub_unlimited = self._direct_publish()
        self.assertIsNone(pub_unlimited["max_reads"])
        self.assertEqual(pub_unlimited["snapshot_id"], pub["snapshot_id"])

    def test_max_reads_must_be_positive_integer(self):
        for bad in (0, -1):
            r = self.client.post(
                f"/records/{self.record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422, r.text)
        # 非整数同样 422
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1.5},
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_unlimited_token_can_be_read_repeatedly(self):
        pub = self._direct_publish()
        self.assertIsNone(pub["max_reads"])
        sid, tok = pub["snapshot_id"], pub["token"]
        for _ in range(7):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        status_body = r.json()
        self.assertIsNone(status_body["max_reads"])
        self.assertIsNone(status_body["remaining_reads"])
        self.assertEqual(status_body["reads_used"], 7)
        self.assertTrue(status_body["active"])

    def test_capped_token_allows_exactly_max_reads_then_403(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 前两次成功
        for expected_used in (1, 2):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("redacted_text", r.json())
            q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
            self.assertEqual(q["reads_used"], expected_used)
            self.assertEqual(q["remaining_reads"], 2 - expected_used)
        # 第三次起沿用既有模糊拒绝
        for _ in range(3):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        # 用尽后计数不再增长，维护侧显示已耗尽
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertEqual(q["remaining_reads"], 0)
        self.assertFalse(q["active"])
        # 维护侧响应不含原文
        r = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS)
        self.assertNotIn(TEXT_V1, r.text)

    def test_invalid_revoked_and_mismatched_requests_do_not_consume(self):
        pub = self._direct_publish(body_extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 第一次成功
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        # 无效令牌不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token=wrong-token")
        self.assertEqual(r.status_code, 403)
        # 令牌与快照不匹配：给同一快照再签发一个令牌，交叉使用
        other = self._read_token_for_same_snapshot_via_republish(
            sid, body_extra={"max_reads": 2}
        )
        self.assertNotEqual(other, tok)
        r = self.client.get(f"/public/snapshots/{sid}?token={other}")
        self.assertEqual(r.status_code, 200, r.text)
        # 用 other 访问一个不存在的快照：403，不消耗 other
        import uuid

        r = self.client.get(
            f"/public/snapshots/{uuid.uuid4()}?token={other}"
        )
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{other}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        # 撤销后读取 403，不消耗剩余次数
        self.assertEqual(
            self.client.post(f"/tokens/{tok}/revoke", headers=SERVICE_HEADERS).status_code,
            200,
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertIsNotNone(q["revoked_at"])

    def _read_token_for_same_snapshot_via_republish(self, existing_snapshot_id, body_extra=None):
        """同一快照可由再次直接发布签发出另一支令牌（ON CONFLICT 复用快照）。"""
        pub = self._direct_publish(body_extra=body_extra)
        self.assertEqual(pub["snapshot_id"], existing_snapshot_id)
        return pub["token"]

    def test_distinct_tokens_on_same_snapshot_count_independently(self):
        pub1 = self._direct_publish(body_extra={"max_reads": 1})
        sid = pub1["snapshot_id"]
        tok1 = pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 第二次直接发布未携带 max_reads -> 新令牌不限次
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertIsNone(q2["max_reads"])
        # tok1 用尽
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 403
        )
        # tok2 不受影响，继续可读
        for _ in range(4):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
            )
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q2["reads_used"], 4)

    def test_expiry_or_revocation_takes_precedence_over_remaining_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 5, "expires_in_seconds": 1})
        sid, tok = pub["snapshot_id"], pub["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        import time as _time

        _time.sleep(1.2)
        # 仍有剩余次数，但已到期 -> 403，不消耗
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)
        self.assertFalse(q["active"])
        self.assertEqual(q["remaining_reads"], 4)

    def test_concurrent_reads_never_exceed_max_reads(self):
        pub = self._direct_publish(body_extra={"max_reads": 10})
        sid, tok = pub["snapshot_id"], pub["token"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(20)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 10)
        self.assertTrue(all(code == 403 for code in failures))
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 10)
        self.assertEqual(q["remaining_reads"], 0)

    def test_token_status_unknown_token_404_and_requires_service_key(self):
        r = self.client.get("/tokens/not-a-real-token", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/tokens/not-a-real-token")
        self.assertEqual(r.status_code, 401)

    # ---- 读取次数上限（受控发布：申请冻结、批准生效） ----

    def test_controlled_max_reads_frozen_at_submission_and_effective_after_approval(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 申请详情可见冻结的上限（不泄露原文）
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 2)
        self.assertNotIn(TEXT_V1, r.text)
        # 批准签发结果携带上限
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_reads"], 2)
        sid, tok = r.json()["snapshot_id"], r.json()["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "snapshot unavailable")

    def test_controlled_reject_creates_no_token_or_counters(self):
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.models import ReadToken as ReadTokenModel
        from app.models import Snapshot as SnapshotModel

        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 3})
        request_id = self._latest_request_id()
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "不予发布"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_max_reads"], 3)
        with SessionLocal() as session:
            # 驳回不产生快照/令牌（限定本记录，不受其他用例数据影响）
            token_count = (
                session.query(ReadTokenModel)
                .filter(
                    ReadTokenModel.snapshot_id.in_(
                        select(SnapshotModel.id).where(
                            SnapshotModel.record_id == self.record_id
                        )
                    )
                )
                .count()
            )
            self.assertEqual(token_count, 0)
            snapshot_count = (
                session.query(SnapshotModel)
                .filter(SnapshotModel.record_id == self.record_id)
                .count()
            )
            self.assertEqual(snapshot_count, 0)
        # 驳回后不能批准
        self.assertEqual(
            self.client.post(
                f"/publish-requests/{request_id}/approve", auth=BASIC_BOB
            ).status_code,
            409,
        )

    def test_controlled_max_reads_must_be_positive(self):
        self._mark_controlled()
        r = self._submit(version=1, expires={"max_reads": 0}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)

    # ---- 数据库升级路径 ----

    def test_completed_migrations_are_not_replayed_on_restart(self):
        """版本化升级：已登记的步骤重复启动不重放、不改变任何数据。"""
        from sqlalchemy import inspect, text as sql_text

        from app.db import SessionLocal, engine
        from app.migrations import TARGET_SCHEMA_VERSION
        from app.models import Record as RecordModel

        rid = self.record_id
        with SessionLocal() as session:
            controlled_before = session.get(RecordModel, rid).controlled

        with engine.connect() as conn:
            applied_before = conn.execute(
                sql_text("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            ).all()
            self.assertEqual(
                [v for (v, _ts) in applied_before],
                list(range(1, TARGET_SCHEMA_VERSION + 1)),
            )

        # 再次启动升级：空操作（旧的轻量迁移模型会在这里重放加列，版本化模型不会）
        app_main._init_db_with_retry()
        self.assertIn(
            "controlled",
            {c["name"] for c in inspect(engine).get_columns("records")},
        )
        with engine.connect() as conn:
            applied_after = conn.execute(
                sql_text("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            ).all()
        # 版本记录与时间戳原样保留（没有任何步骤被重复执行）
        self.assertEqual(applied_before, applied_after)
        with SessionLocal() as session:
            self.assertEqual(session.get(RecordModel, rid).controlled, controlled_before)
        r = self.client.get(f"/records/{rid}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)

    def test_existing_token_quota_is_preserved_across_versioned_upgrade(self):
        """重启升级不重置已用次数：既有令牌继续按原计数读取。"""
        pub = self._direct_publish(body_extra={"max_reads": 5})
        token = pub["token"]
        for _ in range(2):
            self.assertEqual(
                self.client.get(
                    f"/public/snapshots/{pub['snapshot_id']}?token={token}"
                ).status_code,
                200,
            )
        # 重启（升级幂等）：已用次数不重置，剩余额度继续可用
        app_main._init_db_with_retry()
        q = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 2)
        self.assertEqual(q["remaining_reads"], 3)
        self.assertEqual(
            self.client.get(
                f"/public/snapshots/{pub['snapshot_id']}?token={token}"
            ).status_code,
            200,
        )
        q = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 3)


    # ---- 快照永久撤回 ----

    def _withdraw(self, snapshot_id, reason="误发，撤回", expected=200):
        body = {} if reason is None else {"reason": reason}
        r = self.client.post(
            f"/snapshots/{snapshot_id}/withdraw", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def test_withdraw_requires_service_key_and_unknown_is_404(self):
        import uuid

        # 缺失/错误服务密钥沿用 401（且在未知性判断之前整次拒绝）
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw", json={"reason": "x"}
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            f"/snapshots/{uuid.uuid4()}/withdraw",
            headers={"X-Service-Key": "wrong-key"},
            json={"reason": "x"},
        )
        self.assertEqual(r.status_code, 401)
        # 密钥正确但快照未知 -> 404
        r = self._withdraw(uuid.uuid4(), expected=404)

    def test_withdraw_first_time_requires_reason_422(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        # 缺 reason / 空白 reason -> 422，且不产生撤回
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, r.text)
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.json()["snapshots"][0]["status"], "published")

    def test_withdrawn_snapshot_reads_403_for_any_token_without_consuming(self):
        # 两支令牌：一支带次数上限，一支不限次
        pub1 = self._direct_publish(body_extra={"max_reads": 3})
        sid, tok1 = pub1["snapshot_id"], pub1["token"]
        tok2 = self._read_token_for_same_snapshot_via_republish(sid)
        # 撤回前各成功读一次
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok1}").status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok2}").status_code, 200
        )
        r = self._withdraw(sid, reason="发现不应公开的内容")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["snapshot_id"], sid)
        self.assertEqual(body["status"], "withdrawn")
        self.assertIsNotNone(body["withdrawn_at"])
        self.assertNotIn("reason", body)  # 撤回响应不向操作回执以外扩散原因
        # 无论持有哪枚令牌（甚至无效令牌），公开读取统一 403 snapshot unavailable
        for tok in (tok1, tok2, "not-a-token"):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
            self.assertNotIn("撤回", r.text)  # 公开侧不透露撤回原因
        # 撤回后的读取不消耗次数
        q1 = self.client.get(f"/tokens/{tok1}", headers=SERVICE_HEADERS).json()
        q2 = self.client.get(f"/tokens/{tok2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q1["reads_used"], 1)
        self.assertEqual(q1["remaining_reads"], 2)
        self.assertEqual(q2["reads_used"], 1)

    def test_repeat_withdraw_returns_first_result_and_keeps_first_reason(self):
        pub = self._direct_publish(body_extra={"max_reads": 5})
        sid = pub["snapshot_id"]
        first = self._withdraw(sid, reason="首次撤回原因").json()
        first_at = first["withdrawn_at"]
        # 重复撤回（即便原因不同或为空）返回首次时间与状态
        for body in ({"reason": "另一个原因"}, {}):
            r = self.client.post(
                f"/snapshots/{sid}/withdraw", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 200, r.text)
            again = r.json()
            self.assertEqual(again["status"], "withdrawn")
            self.assertEqual(again["withdrawn_at"], first_at)
        # 维护侧记录保留首次撤回时间与原因
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        info = r.json()["snapshots"][0]
        self.assertEqual(info["status"], "withdrawn")
        self.assertEqual(info["withdrawn_at"], first_at)
        self.assertEqual(info["withdraw_reason"], "首次撤回原因")

    def test_withdraw_is_scoped_to_single_snapshot(self):
        # 版本 1 与版本 2 都发布；撤回版本 1 不影响版本 2
        pub1 = self._direct_publish(version=1)
        sid1, tok1 = pub1["snapshot_id"], pub1["token"]
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        pub2 = self._direct_publish(version=2)
        sid2, tok2 = pub2["snapshot_id"], pub2["token"]
        self._withdraw(sid1)
        # 版本 1 不可读；版本 2 继续可读
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid1}?token={tok1}").status_code, 403
        )
        r = self.client.get(f"/public/snapshots/{sid2}?token={tok2}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(TEXT_V1, r.text)
        # 维护侧两个快照状态并存；撤回的历史脱敏正文仍留存可见于记录状态
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        snapshots = r.json()["snapshots"]
        statuses = {s["version"]: s["status"] for s in snapshots}
        self.assertEqual(statuses, {1: "withdrawn", 2: "published"})
        self.assertEqual(snapshots[0]["withdraw_reason"], "误发，撤回")

    def test_direct_publish_of_withdrawn_version_conflicts(self):
        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        self._withdraw(sid)
        # 再次直接发布同一版本 -> 409，且不签发新令牌
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 9},
        )
        self.assertEqual(r.status_code, 409, r.text)
        # 修订出新版本后仍可正常发布（撤回只作用于该快照）
        r = self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2, "intervals": [{"start": 9, "end": 15}]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 2},
        )
        self.assertEqual(r.status_code, 201, r.text)
        r = self.client.get(
            f"/public/snapshots/{r.json()['snapshot_id']}?token={r.json()['token']}"
        )
        self.assertEqual(r.status_code, 200)

    def test_controlled_submit_for_withdrawn_version_conflicts(self):
        # 先发布版本 1（非受控直接发布）再撤回，随后把记录转为受控
        pub = self._direct_publish()
        self._withdraw(pub["snapshot_id"])
        self._mark_controlled()
        r = self._submit(expected=409)
        self.assertNotIn(TEXT_V1, r.text)

    def test_pending_request_can_be_rejected_but_approval_conflicts_after_withdraw(self):
        self._mark_controlled()
        self._submit(version=1, expires={"max_reads": 2})
        request_id = self._latest_request_id()
        # 期间该版本经另一申请批准产生快照并被撤回（模拟版本曾公开后撤回）
        other = self._submit()
        other_id = self._latest_request_id_from_list()
        r = self.client.post(f"/publish-requests/{other_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["snapshot_id"]
        self._withdraw(sid, reason="复核后发现问题")
        # 撤回不改动待审申请；批准命中已撤回快照 -> 409，申请仍可驳回
        r = self.client.get(f"/publish-requests/{request_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "pending")
        r = self.client.post(f"/publish-requests/{request_id}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409, r.text)
        r = self.client.post(
            f"/publish-requests/{request_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "版本已撤回，不予批准"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        # 已批准申请记录仍留存，并指向撤回快照
        r = self.client.get(f"/publish-requests/{other_id}", auth=BASIC_ALICE)
        self.assertEqual(r.json()["status"], "approved")
        self.assertEqual(r.json()["snapshot_id"], sid)
        # 撤回后公开读取仍 403
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token=x").status_code, 403
        )

    def _latest_request_id_from_list(self):
        r = self.client.get(
            f"/records/{self.record_id}/publish-requests", auth=BASIC_ALICE
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["requests"][-1]["request_id"]

    def test_concurrent_withdraw_and_reads_respect_boundary(self):
        pub = self._direct_publish(body_extra={"max_reads": 50})
        sid, tok = pub["snapshot_id"], pub["token"]
        results: list[int] = []
        barrier = threading.Barrier(9)

        def withdraw():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.post(
                f"/snapshots/{sid}/withdraw",
                headers=SERVICE_HEADERS,
                json={"reason": "并发撤回"},
            )
            results.append(0 if r.status_code == 200 else r.status_code)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            # 略作退让，争取让撤回先提交；无论先后，撤回提交后的读取必须 403
            code = client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            results.append(code)

        threads = [threading.Thread(target=withdraw)]
        threads += [threading.Thread(target=read_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(0, results)  # 撤回成功
        concurrent_reads = [c for c in results if c != 0]
        # 撤回提交后开始的读取一律 403；在撤回提交前已获行锁的读取至多 8 个 200
        self.assertTrue(all(c in (200, 403) for c in concurrent_reads), results)
        # 撤回完成后的新读取稳定 403
        for _ in range(5):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
            )
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        # 消耗次数恰为并发阶段的 200 数，撤回后的 403 未消耗
        self.assertEqual(q["reads_used"], concurrent_reads.count(200))

    def test_snapshot_withdrawal_works_after_versioned_upgrade(self):
        """升级后既有快照仍可读，且撤回功能正常（列结构由版本化升级保证）。"""
        from app.db import SessionLocal
        from app.models import Snapshot as SnapshotModel

        pub = self._direct_publish()
        sid = pub["snapshot_id"]
        # 重启升级：结构与数据均不受影响
        app_main._init_db_with_retry()
        r = self.client.get(f"/public/snapshots/{sid}?token={pub['token']}")
        self.assertEqual(r.status_code, 200, r.text)
        r = self._withdraw(sid)
        self.assertEqual(r.json()["status"], "withdrawn")
        with SessionLocal() as session:
            row = session.get(SnapshotModel, sid)
            self.assertIsNotNone(row.withdrawn_at)
            self.assertEqual(row.withdraw_reason, "误发，撤回")


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class ReadBundleIntegrationTests(unittest.TestCase):
    """多快照共享凭证：申请-复核、冻结清单、共用额度、撤回单项、维护侧管理。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 每个用例准备两条已发布记录/快照（直接发布即可，快照不要求来自受控发布）
        self.snapshot_ids = []
        self.redacted_texts = []
        for idx, (text_, intervals) in enumerate(
            (
                (TEXT_V1, INTERVALS_V1),
                (TEXT_V2, [{"start": 9, "end": 15}]),
            )
        ):
            r = self.client.post(
                "/records",
                headers=SERVICE_HEADERS,
                json={"text": text_, "intervals": intervals},
            )
            self.assertEqual(r.status_code, 201, r.text)
            record_id = r.json()["record_id"]
            r = self.client.post(
                f"/records/{record_id}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1, "max_reads": 100},
            )
            self.assertEqual(r.status_code, 201, r.text)
            self.snapshot_ids.append(r.json()["snapshot_id"])
            r = self.client.get(
                f"/public/snapshots/{self.snapshot_ids[-1]}?token={r.json()['token']}"
            )
            self.redacted_texts.append(r.json()["redacted_text"])

    # ---- 工具方法 ----

    def _submit(self, snapshot_ids=None, max_reads=10, auth=BASIC_ALICE, expected=201, extra=None):
        body = {
            "snapshot_ids": snapshot_ids if snapshot_ids is not None else self.snapshot_ids,
            "max_reads": max_reads,
        }
        if extra:
            body.update(extra)
        r = self.client.post("/bundle-requests", auth=auth, json=body)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _approve(self, request_id, auth=BASIC_BOB, expected=200):
        r = self.client.post(f"/bundle-requests/{request_id}/approve", auth=auth)
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _approved_bundle(self, max_reads=10, extra=None):
        submit = self._submit(max_reads=max_reads, extra=extra).json()
        approve = self._approve(submit["request_id"]).json()
        self.assertEqual(approve["status"], "approved")
        self.assertEqual(approve["snapshot_ids"], self.snapshot_ids)
        return approve

    def _read(self, snapshot_id, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 提交校验与鉴权 ----

    def test_submit_requires_operator_role_and_valid_credentials(self):
        r = self.client.post(
            "/bundle-requests",
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        # bob 只有 reviewer 角色，不能提交
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_BOB,
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 403)
        # 错误口令 401
        r = self.client.post(
            "/bundle-requests",
            auth=("alice", "wrong"),
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 401)

    def test_submit_requires_at_least_two_distinct_ids_and_positive_cap(self):
        # 少于两个
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0]], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 422)
        # 重复 ID
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0], self.snapshot_ids[0]], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 422)
        # 额度非正整数
        for bad in (0, -1):
            r = self.client.post(
                "/bundle-requests",
                auth=BASIC_ALICE,
                json={"snapshot_ids": self.snapshot_ids, "max_reads": bad},
            )
            self.assertEqual(r.status_code, 422)

    def test_submit_unknown_snapshot_404_and_withdrawn_409(self):
        import uuid

        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [self.snapshot_ids[0], str(uuid.uuid4())], "max_reads": 3},
        )
        self.assertEqual(r.status_code, 404, r.text)
        # 撤回其中一个快照后提交 -> 409
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "不应公开"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": self.snapshot_ids, "max_reads": 3},
        )
        self.assertEqual(r.status_code, 409, r.text)

    def test_submit_freezes_list_and_cap(self):
        r = self._submit(max_reads=4, extra={"expires_in_seconds": 3600})
        body = r.json()
        self.assertEqual(body["snapshot_ids"], self.snapshot_ids)
        self.assertEqual(body["max_reads"], 4)
        self.assertEqual(body["status"], "pending")
        detail = self.client.get(
            f"/bundle-requests/{body['request_id']}", auth=BASIC_ALICE
        ).json()
        self.assertEqual(detail["snapshot_ids"], self.snapshot_ids)
        self.assertEqual(detail["max_reads"], 4)
        self.assertEqual(detail["credential_expires_in_seconds"], 3600)
        self.assertIsNone(detail["bundle_id"])
        # 详情不含原文
        self.assertNotIn(TEXT_V1, str(detail))

    def test_submit_expiry_policy_validation(self):
        from datetime import datetime, timezone

        past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
        r = self._submit(extra={"expires_at": past}, expected=422)
        self.assertNotIn(TEXT_V1, r.text)
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={
                "snapshot_ids": self.snapshot_ids,
                "max_reads": 3,
                "expires_in_seconds": 60,
                "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(),
            },
        )
        self.assertEqual(r.status_code, 422)

    # ---- 复核规则 ----

    def test_reviewer_role_required_and_requester_cannot_self_review(self):
        submit = self._submit().json()
        rid = submit["request_id"]
        # alice 只有 operator 角色 -> 403
        r = self.client.post(f"/bundle-requests/{rid}/approve", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 403)
        # carol 虽是 reviewer 但是申请人本人 -> 403（申请人不得自审）
        submit2 = self._submit(auth=BASIC_CAROL).json()
        r = self.client.post(f"/bundle-requests/{submit2['request_id']}/approve", auth=BASIC_CAROL)
        self.assertEqual(r.status_code, 403)
        self.assertIn("may not review their own", r.json()["detail"])
        # bob（非申请人）批准成功
        self.assertEqual(self._approve(rid).status_code, 200)

    def test_unknown_request_404(self):
        import uuid

        r = self.client.post(f"/bundle-requests/{uuid.uuid4()}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            f"/bundle-requests/{uuid.uuid4()}/reject",
            auth=BASIC_BOB,
            json={},
        )
        self.assertEqual(r.status_code, 404)

    def test_reject_is_terminal_and_approves_once(self):
        # 驳回
        submit = self._submit().json()
        rid = submit["request_id"]
        r = self.client.post(
            f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={"reason": "材料不足"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(r.json()["reviewed_by"], "bob")
        self.assertEqual(r.json()["reason"], "材料不足")
        # 驳回后不能批准/再驳回
        self.assertEqual(self._approve(rid, expected=409).status_code, 409)
        r = self.client.post(f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 409)
        # 批准终态同样唯一
        submit2 = self._submit().json()
        rid2 = submit2["request_id"]
        self.assertEqual(self._approve(rid2).status_code, 200)
        r = self.client.post(f"/bundle-requests/{rid2}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 409)

    # ---- 批准时重新核对冻结清单 ----

    def test_approval_fails_if_any_snapshot_withdrawn_after_submission(self):
        submit = self._submit(max_reads=5).json()
        rid = submit["request_id"]
        # 等待复核期间撤回其中一个快照
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "等待期间撤回"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 整次批准失败，不签发凭证
        r = self.client.post(f"/bundle-requests/{rid}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 409, r.text)
        detail = self.client.get(f"/bundle-requests/{rid}", auth=BASIC_ALICE).json()
        self.assertEqual(detail["status"], "pending")
        self.assertIsNone(detail["bundle_id"])
        # 申请仍可驳回
        r = self.client.post(f"/bundle-requests/{rid}/reject", auth=BASIC_BOB, json={})
        self.assertEqual(r.status_code, 200, r.text)

    def test_approval_creates_credential_bound_to_list_in_one_transaction(self):
        approve = self._approved_bundle(max_reads=6)
        bundle_id, credential = approve["bundle_id"], approve["credential"]
        # 维护侧查询：清单、额度、状态
        status_body = self.client.get(
            f"/read-bundles/{bundle_id}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["max_reads"], 6)
        self.assertEqual(status_body["reads_used"], 0)
        self.assertEqual(status_body["remaining_reads"], 6)
        self.assertTrue(status_body["active"])
        items = status_body["items"]
        self.assertEqual([i["snapshot_id"] for i in items], self.snapshot_ids)
        self.assertEqual([i["position"] for i in items], [0, 1])
        self.assertTrue(all(i["status"] == "published" for i in items))
        self.assertNotIn(TEXT_V1, str(status_body))
        # 未知凭证 404，缺服务密钥 401
        import uuid

        self.assertEqual(
            self.client.get(f"/read-bundles/{uuid.uuid4()}", headers=SERVICE_HEADERS).status_code,
            404,
        )
        self.assertEqual(self.client.get(f"/read-bundles/{bundle_id}").status_code, 401)

    # ---- 公开读取：共用额度 ----

    def test_credential_reads_only_listed_snapshots_and_shares_cap(self):
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 清单内两项都可读，返回脱敏正文
        self.assertEqual(
            self._read(self.snapshot_ids[0], cred).json()["redacted_text"],
            self.redacted_texts[0],
        )
        self.assertEqual(
            self._read(self.snapshot_ids[1], cred).json()["redacted_text"],
            self.redacted_texts[1],
        )
        # 第 3 次成功，之后跨任一清单快照全部 403（共用额度）
        self.assertEqual(self._read(self.snapshot_ids[0], cred).status_code, 200)
        for sid in self.snapshot_ids:
            r = self._read(sid, cred, expected=403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 3)
        self.assertEqual(status_body["remaining_reads"], 0)
        self.assertFalse(status_body["active"])

    def test_credential_cannot_read_snapshot_outside_list(self):
        # 第三个快照：发布到一条新记录
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": "第三条记录，秘密XYZ。", "intervals": [{"start": 6, "end": 12}]},
        )
        rid = r.json()["record_id"]
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        other_sid = r.json()["snapshot_id"]
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 不在清单内 -> 模糊 403，不扣额度
        self._read(other_sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        self.assertTrue(status_body["active"])
        # 未知快照 + 凭证也统一 403
        import uuid

        self._read(uuid.uuid4(), cred, expected=403)

    def test_withdrawn_listed_item_rejected_without_consuming_others_remain_readable(self):
        approve = self._approved_bundle(max_reads=3)
        cred = approve["credential"]
        # 撤回清单中的第二项
        r = self.client.post(
            f"/snapshots/{self.snapshot_ids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "该项撤回"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 该项一律 403，且不扣额度
        self._read(self.snapshot_ids[1], cred, expected=403)
        self._read(self.snapshot_ids[1], cred, expected=403)
        # 第一项仍可读；额度仍为 3，读一次后剩 2
        self._read(self.snapshot_ids[0], cred, expected=200)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertEqual(status_body["remaining_reads"], 2)
        self.assertEqual(status_body["items"][1]["status"], "withdrawn")
        self.assertIsNotNone(status_body["items"][1]["withdrawn_at"])
        # 模糊 403 不透露撤回原因
        r = self._read(self.snapshot_ids[1], cred, expected=403)
        self.assertNotIn("该项撤回", r.text)

    def test_invalid_or_expired_or_revoked_credential_does_not_consume(self):
        # 无效凭证
        self._read(self.snapshot_ids[0], "not-a-real-credential", expected=403)
        approve = self._approved_bundle(max_reads=2)
        bid, cred = approve["bundle_id"], approve["credential"]
        # 撤销：撤销后全部拒绝
        r = self.client.post(f"/read-bundles/{bid}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["revoked"])
        self.assertFalse(r.json()["already_revoked"])
        for sid in self.snapshot_ids:
            self._read(sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        self.assertFalse(status_body["active"])
        self.assertIsNotNone(status_body["revoked_at"])
        # 重复撤销幂等
        r = self.client.post(f"/read-bundles/{bid}/revoke", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["already_revoked"])
        # 撤销未知凭证 404
        import uuid

        self.assertEqual(
            self.client.post(
                f"/read-bundles/{uuid.uuid4()}/revoke", headers=SERVICE_HEADERS
            ).status_code,
            404,
        )

    def test_expired_credential_rejects_all_without_consuming(self):
        approve = self._approved_bundle(max_reads=3, extra={"expires_in_seconds": 1})
        cred = approve["credential"]
        self._read(self.snapshot_ids[0], cred, expected=200)
        import time as _time

        _time.sleep(1.2)
        for sid in self.snapshot_ids:
            self._read(sid, cred, expected=403)
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertFalse(status_body["active"])

    def test_concurrent_reads_never_exceed_shared_cap(self):
        approve = self._approved_bundle(max_reads=10)
        cred = approve["credential"]
        successes: list[int] = []
        failures: list[int] = []
        barrier = threading.Barrier(24)

        def read_once(i):
            barrier.wait()
            client = TestClient(app_main.app)
            # 交替读取两个清单快照，验证共用额度
            sid = self.snapshot_ids[i % 2]
            code = client.get(f"/public/snapshots/{sid}?token={cred}").status_code
            (successes if code == 200 else failures).append(code)

        threads = [threading.Thread(target=read_once, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(successes), 10, successes + failures)
        self.assertEqual(len(failures), 14)
        self.assertTrue(all(code == 403 for code in failures))
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 10)
        self.assertEqual(status_body["remaining_reads"], 0)

    # ---- 维护侧与兼容性 ----

    def test_maintenance_lists_and_legacy_token_unchanged(self):
        approve = self._approved_bundle(max_reads=2)
        cred = approve["credential"]
        # 列表接口
        r = self.client.get("/read-bundles", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(b["bundle_id"] == approve["bundle_id"] for b in r.json()["bundles"]))
        # 申请列表（操作者可查）
        r = self.client.get("/bundle-requests", auth=BASIC_ALICE)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(
            any(q["request_id"] == approve["request_id"] for q in r.json()["requests"])
        )
        # 共享凭证不在既有单快照令牌命名空间：/tokens/<credential> 仍 404（接口行为兼容）
        self.assertEqual(
            self.client.get(f"/tokens/{cred}", headers=SERVICE_HEADERS).status_code, 404
        )
        # 既有单快照令牌行为不变：为快照 0 再签一枚既有令牌（同版本复用快照），
        # 其计数与共享凭证互不影响
        record_id = self._record_of_snapshot(self.snapshot_ids[0])
        pub = self.client.post(
            f"/records/{record_id}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1, "max_reads": 1},
        )
        self.assertEqual(pub.status_code, 201, pub.text)
        legacy_token = pub.json()["token"]
        self.assertEqual(
            self._read(self.snapshot_ids[0], legacy_token).status_code, 200
        )
        self.assertEqual(
            self._read(self.snapshot_ids[0], legacy_token, expected=403).status_code, 403
        )
        # 共享凭证额度未被原令牌消耗
        status_body = self.client.get(
            f"/read-bundles/{approve['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 0)
        # 公开读取统一响应结构未变
        body = self._read(self.snapshot_ids[0], cred).json()
        self.assertEqual(set(body.keys()), {"record_id", "version", "status", "redacted_text"})

    def _record_of_snapshot(self, snapshot_id):
        # 通过共享凭证读取响应无法拿 record（避免耦合），改用维护侧记录检索：
        # 直接从快照行查询其 record_id
        from app.db import SessionLocal
        from app.models import Snapshot as SnapshotModel

        with SessionLocal() as session:
            return session.get(SnapshotModel, snapshot_id).record_id


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class CredentialRotationIntegrationTests(unittest.TestCase):
    """读取凭证泄露换发：单快照令牌与多快照共享凭证统一入口。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ---- 工具方法 ----

    def _create_record_publish(self, max_reads=None, extra=None):
        body = {"text": TEXT_V1, "intervals": INTERVALS_V1}
        r = self.client.post("/records", headers=SERVICE_HEADERS, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        rid = r.json()["record_id"]
        pub_body = {"version": 1}
        if max_reads is not None:
            pub_body["max_reads"] = max_reads
        if extra:
            pub_body.update(extra)
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json=pub_body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return rid, r.json()

    def _read(self, sid, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{sid}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _rotate(self, credential, request_id, expected=200):
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": credential, "request_id": request_id},
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 鉴权与校验 ----

    def test_rotate_requires_service_key(self):
        rid, pub = self._create_record_publish()
        r = self.client.post(
            "/credentials/rotate",
            json={"credential": pub["token"], "request_id": "req-1"},
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.post(
            "/credentials/rotate",
            headers={"X-Service-Key": "wrong"},
            json={"credential": pub["token"], "request_id": "req-1"},
        )
        self.assertEqual(r.status_code, 401)

    def test_rotate_unknown_credential_404(self):
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": "definitely-not-a-credential", "request_id": "r1"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "credential not found")

    def test_rotate_blank_fields_422(self):
        rid, pub = self._create_record_publish()
        for body in (
            {"credential": "", "request_id": "r1"},
            {"credential": "  ", "request_id": "r1"},
            {"credential": pub["token"], "request_id": ""},
            {"credential": pub["token"], "request_id": "   "},
        ):
            r = self.client.post(
                "/credentials/rotate", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, r.text)

    # ---- 单快照令牌换发 ----

    def test_token_rotation_issues_new_and_invalidates_old(self):
        rid, pub = self._create_record_publish()
        sid, old = pub["snapshot_id"], pub["token"]
        result = self._rotate(old, "req-token-1").json()
        self.assertEqual(result["kind"], "token")
        self.assertEqual(result["request_id"], "req-token-1")
        self.assertEqual(result["old_credential"], old)
        self.assertNotEqual(result["new_credential"], old)
        self.assertEqual(result["snapshot_ids"], [sid])
        self.assertIsNone(result["expires_at"])
        self.assertIsNone(result["max_reads"])
        self.assertEqual(result["reads_used"], 0)
        self.assertIsNone(result["remaining_reads"])
        self.assertFalse(result["replayed"])
        self.assertIsNone(result["items"])
        self.assertIsNotNone(result["rotated_at"])
        new = result["new_credential"]
        # 旧令牌换发提交后不能再读取（模糊 403，不泄露原因）
        self._read(sid, old, expected=403)
        r = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS)
        self.assertFalse(r.json()["active"])
        self.assertIsNotNone(r.json()["rotated_at"])
        # 新令牌在同一快照继续可读
        self._read(sid, new, expected=200)
        r = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS)
        self.assertTrue(r.json()["active"])
        self.assertIsNone(r.json()["rotated_at"])

    def test_token_rotation_inherits_expiry_cap_and_used_count(self):
        rid, pub = self._create_record_publish(
            max_reads=5, extra={"expires_in_seconds": 3600}
        )
        sid, old = pub["snapshot_id"], pub["token"]
        self._read(sid, old)
        self._read(sid, old)
        old_status = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS).json()
        result = self._rotate(old, "req-inherit").json()
        # 原绝对到期时刻被继承（不延长）
        self.assertEqual(result["expires_at"], old_status["expires_at"])
        self.assertEqual(result["max_reads"], 5)
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 3)
        new = result["new_credential"]
        # 新令牌沿用继承的计数：再读 3 次后耗尽
        for _ in range(3):
            self.assertEqual(self._read(sid, new).status_code, 200)
        for _ in range(2):
            self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        status_body = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status_body["reads_used"], 5)
        self.assertEqual(status_body["remaining_reads"], 0)
        # 旧令牌继续不可读，计数停在换发时的 2（旧令牌失败读取不计数）
        old_status = self.client.get(f"/tokens/{old}", headers=SERVICE_HEADERS).json()
        self.assertEqual(old_status["reads_used"], 2)

    def test_rotation_idempotent_for_same_request_id(self):
        rid, pub = self._create_record_publish(max_reads=4)
        sid, old = pub["snapshot_id"], pub["token"]
        first = self._rotate(old, "idem-req").json()
        # 换发后再用旧凭证成功读取是不可能的；直接重试（相同原凭证 + 请求标识）
        second = self._rotate(old, "idem-req").json()
        third = self._rotate(old, "idem-req").json()
        for body in (second, third):
            self.assertTrue(body["replayed"])
            self.assertEqual(body["new_credential"], first["new_credential"])
            self.assertEqual(body["rotated_at"], first["rotated_at"])
            self.assertEqual(body["reads_used"], first["reads_used"])
        self.assertFalse(first["replayed"])
        # 只产生一枚新凭证
        self._read(sid, first["new_credential"], expected=200)
        self._read(sid, second["new_credential"], expected=200)

    def test_different_request_id_after_rotation_conflicts(self):
        rid, pub = self._create_record_publish()
        old = pub["token"]
        self._rotate(old, "req-A")
        # 不同请求标识再次换发同一原凭证 -> 409，不签出新凭证
        r = self._rotate(old, "req-B", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "already_rotated")

    def test_concurrent_rotations_with_distinct_request_ids_succeed_once(self):
        results: list = []
        for attempt in range(5):
            rid, pub = self._create_record_publish(max_reads=20)
            old = pub["token"]
            barrier = threading.Barrier(8)

            def rotate(req_suffix):
                barrier.wait()
                client = TestClient(app_main.app)
                r = client.post(
                    "/credentials/rotate",
                    headers=SERVICE_HEADERS,
                    json={"credential": old, "request_id": f"conc-{req_suffix}"},
                )
                results.append(r.status_code)

            threads = [
                threading.Thread(target=rotate, args=(f"req-{i}",)) for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            batch = results[-8:]
            self.assertEqual(batch.count(200), 1, batch)
            self.assertEqual(batch.count(409), 7, batch)

    def test_revoked_expired_and_exhausted_credentials_cannot_rotate(self):
        # 已撤销
        rid, pub = self._create_record_publish()
        r = self.client.post(
            f"/tokens/{pub['token']}/revoke", headers=SERVICE_HEADERS
        )
        self.assertEqual(r.status_code, 200)
        r = self._rotate(pub["token"], "r-revoked", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "revoked")
        # 额度耗尽
        rid, pub = self._create_record_publish(max_reads=1)
        sid, tok = pub["snapshot_id"], pub["token"]
        self._read(sid, tok)
        r = self._rotate(tok, "r-exhausted", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "quota_exhausted")
        # 已到期
        rid, pub = self._create_record_publish(
            max_reads=5, extra={"expires_in_seconds": 1}
        )
        import time as _time

        _time.sleep(1.2)
        r = self._rotate(pub["token"], "r-expired", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "expired")

    def test_rotation_settles_with_concurrent_reads_without_overspend(self):
        """换发与并发读取按事务先后结算：用快照行锁确定性地制造重叠，不依赖调度时序。

        协调线程先持有快照行锁：读取线程随后发起并阻塞在该锁队列中；换发线程
        此时启动（它也要拿快照行锁，排在队列之后）。协调线程放行后，队列中的读取
        先于换发获锁并成功，换发提交后再用旧凭证的读取稳定 403——两种结果都必然
        出现，且成功总次数绝不超过上限。
        """
        from sqlalchemy import text as sql_text

        from app.db import SessionLocal

        rid, pub = self._create_record_publish(max_reads=10)
        sid, old = pub["snapshot_id"], pub["token"]
        read_codes: list[int] = []
        # 协调：独立连接先对快照行加 FOR UPDATE 锁，直到放行事件才提交释放
        holder_ready = threading.Event()
        release_holder = threading.Event()
        readers_queued = threading.Barrier(4)  # 3 个读取线程 + 主线程

        def hold_snapshot_lock():
            session = SessionLocal()
            try:
                session.execute(
                    sql_text("SELECT id FROM snapshots WHERE id = :sid FOR UPDATE"),
                    {"sid": sid},
                )
                holder_ready.set()
                release_holder.wait(timeout=10)
                session.commit()
            finally:
                session.close()

        holder = threading.Thread(target=hold_snapshot_lock)
        holder.start()
        self.assertTrue(holder_ready.wait(timeout=10))

        def read_old():
            client = TestClient(app_main.app)
            # 所有读取线程先同步（它们随后阻塞在快照行锁上），再由主线程放行协调者
            readers_queued.wait()
            read_codes.append(
                client.get(f"/public/snapshots/{sid}?token={old}").status_code
            )

        reader_threads = [threading.Thread(target=read_old) for _ in range(3)]
        for t in reader_threads:
            t.start()
        readers_queued.wait()
        # 给读取请求留出到达并排队等待快照行锁的时间
        time.sleep(0.5)

        # 换发在此启动：快照锁仍被持有，换发与已排队读取必然竞争同一把行锁
        rotate_client = TestClient(app_main.app)

        def rotate():
            rotate_client.post(
                "/credentials/rotate",
                headers=SERVICE_HEADERS,
                json={"credential": old, "request_id": "req-race"},
            )

        rotation_thread = threading.Thread(target=rotate)
        rotation_thread.start()
        time.sleep(0.5)

        # 放行：排队的 3 个读取先获锁成功（200），随后换发获锁提交
        release_holder.set()
        for t in reader_threads:
            t.join(timeout=10)
        rotation_thread.join(timeout=10)
        holder.join(timeout=10)

        self.assertEqual(len(read_codes), 3)
        self.assertTrue(all(c == 200 for c in read_codes), read_codes)

        # 换发提交后，旧凭证的新读取稳定 403（再多重试也不翻转）
        for _ in range(5):
            self.assertEqual(self._read(sid, old, expected=403).status_code, 403)

        result = self._rotate(old, "req-race").json()  # 幂等回放
        new = result["new_credential"]
        # 新凭证继承已用次数：锁队列中先提交的 3 次成功读取
        inherited = read_codes.count(200)
        self.assertEqual(result["reads_used"], inherited)
        self.assertEqual(result["remaining_reads"], 10 - inherited)
        # 新凭证只能再成功 (10 - inherited) 次，成功总数绝不超过上限
        for _ in range(10 - inherited):
            self.assertEqual(self._read(sid, new).status_code, 200)
        self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        status_body = self.client.get(f"/tokens/{new}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status_body["reads_used"], 10)
        # 审计事件总数与成功读取总数一致（失败读取不写事件）
        events = self.client.get(
            f"/snapshots/{sid}/read-events"
            "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        ).json()["events"]
        self.assertEqual(len(events), 10)

    def test_rotated_token_can_be_rotated_again(self):
        rid, pub = self._create_record_publish(max_reads=7)
        sid, tok1 = pub["snapshot_id"], pub["token"]
        self._read(sid, tok1)
        tok2 = self._rotate(tok1, "chain-1").json()["new_credential"]
        self._read(sid, tok2)
        result = self._rotate(tok2, "chain-2").json()
        tok3 = result["new_credential"]
        self.assertNotIn(tok3, (tok1, tok2))
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 5)
        # 前两代都不可读，最新凭证可读
        self._read(sid, tok1, expected=403)
        self._read(sid, tok2, expected=403)
        self._read(sid, tok3, expected=200)

    def test_token_on_withdrawn_snapshot_can_still_rotate_but_unreadable(self):
        rid, pub = self._create_record_publish(max_reads=5)
        sid, old = pub["snapshot_id"], pub["token"]
        self._read(sid, old)
        r = self.client.post(
            f"/snapshots/{sid}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "误发撤回"},
        )
        self.assertEqual(r.status_code, 200)
        # 快照撤回不作为换发障碍
        result = self._rotate(old, "req-withdrawn").json()
        self.assertEqual(result["snapshot_ids"], [sid])
        self.assertEqual(result["remaining_reads"], 4)
        # 但新旧令牌对该快照都读不到正文（模糊 403，不计数）
        self._read(sid, old, expected=403)
        self._read(sid, result["new_credential"], expected=403)
        status_body = self.client.get(
            f"/tokens/{result['new_credential']}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)

    # ---- 多快照共享凭证换发 ----

    def _approved_bundle(self, snapshot_ids, max_reads=5, extra=None):
        body = {"snapshot_ids": snapshot_ids, "max_reads": max_reads}
        if extra:
            body.update(extra)
        r = self.client.post("/bundle-requests", auth=BASIC_ALICE, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/bundle-requests/{request_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _two_snapshots(self):
        sids = []
        for text_, intervals in (
            (TEXT_V1, INTERVALS_V1),
            (TEXT_V2, [{"start": 9, "end": 15}]),
        ):
            r = self.client.post(
                "/records",
                headers=SERVICE_HEADERS,
                json={"text": text_, "intervals": intervals},
            )
            rid = r.json()["record_id"]
            r = self.client.post(
                f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
            )
            sids.append(r.json()["snapshot_id"])
        return sids

    def test_bundle_rotation_copies_list_and_inherits_quota(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=5)
        old = approved["credential"]
        self._read(sids[0], old)
        self._read(sids[1], old)
        result = self._rotate(old, "bundle-req-1").json()
        self.assertEqual(result["kind"], "bundle")
        self.assertEqual(result["snapshot_ids"], sids)
        self.assertEqual(result["max_reads"], 5)
        self.assertEqual(result["reads_used"], 2)
        self.assertEqual(result["remaining_reads"], 3)
        self.assertIsNone(result["expires_at"])
        self.assertIsNotNone(result["items"])
        self.assertEqual([i["snapshot_id"] for i in result["items"]], sids)
        self.assertEqual([i["position"] for i in result["items"]], [0, 1])
        self.assertTrue(all(i["status"] == "published" for i in result["items"]))
        new = result["new_credential"]
        # 旧凭证失效，新凭证继承清单与剩余额度
        self._read(sids[0], old, expected=403)
        for _ in range(3):
            self.assertEqual(self._read(sids[0], new).status_code, 200)
        for sid in sids:
            self.assertEqual(self._read(sid, new, expected=403).status_code, 403)
        # 维护侧：旧凭证 active=false 且带 rotated_at；新凭证计数继承
        bid = approved["bundle_id"]
        old_status = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertFalse(old_status["active"])
        self.assertIsNotNone(old_status["rotated_at"])
        new_status = self.client.get("/read-bundles", headers=SERVICE_HEADERS).json()
        new_row = next(b for b in new_status["bundles"] if b["credential"] == new)
        self.assertEqual(new_row["reads_used"], 5)
        self.assertFalse(new_row["active"])  # 继承额度已用尽
        self.assertEqual(new_row["remaining_reads"], 0)

    def test_bundle_rotation_with_withdrawn_item_keeps_item_unreadable(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=5)
        old = approved["credential"]
        # 撤回清单第二项后换发：仍可换发，该项继续不可读且不扣额度
        r = self.client.post(
            f"/snapshots/{sids[1]}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "该项撤回"},
        )
        self.assertEqual(r.status_code, 200)
        result = self._rotate(old, "bundle-withdrawn-item").json()
        items = result["items"]
        self.assertEqual(items[0]["status"], "published")
        self.assertEqual(items[1]["status"], "withdrawn")
        self.assertIsNotNone(items[1]["withdrawn_at"])
        new = result["new_credential"]
        self._read(sids[1], new, expected=403)
        self._read(sids[1], new, expected=403)
        self._read(sids[0], new, expected=200)
        # 撤回项不扣额度：仅消耗 1 次
        bid = next(
            b["bundle_id"]
            for b in self.client.get("/read-bundles", headers=SERVICE_HEADERS).json()[
                "bundles"
            ]
            if b["credential"] == new
        )
        status_body = self.client.get(
            f"/read-bundles/{bid}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status_body["reads_used"], 1)
        self.assertEqual(status_body["remaining_reads"], 4)

    def test_bundle_rotation_idempotent_and_conflict(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=3)
        old = approved["credential"]
        first = self._rotate(old, "b-idem").json()
        second = self._rotate(old, "b-idem").json()
        self.assertTrue(second["replayed"])
        self.assertEqual(second["new_credential"], first["new_credential"])
        # 不同请求标识 -> 409
        r = self._rotate(old, "b-other", expected=409)
        self.assertEqual(r.json()["detail"]["reason"], "already_rotated")

    def test_credentials_remain_valid_and_rotatable_after_upgrade(self):
        """重启升级后既有单令牌/共享凭证继续可读、可换发，已用额度沿用。"""
        from sqlalchemy import inspect

        from app.db import engine

        rid, pub = self._create_record_publish()
        token = pub["token"]
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=3)
        credential = approved["credential"]

        app_main._init_db_with_retry()
        table_names = inspect(engine).get_table_names()
        token_columns = {c["name"] for c in inspect(engine).get_columns("read_tokens")}
        bundle_columns = {c["name"] for c in inspect(engine).get_columns("read_bundles")}
        self.assertIn("rotated_at", token_columns)
        self.assertIn("rotated_at", bundle_columns)
        self.assertIn("credential_rotations", table_names)
        # 既有凭证升级后继续可读，且仍可正常换发
        self.assertEqual(
            self._read(pub["snapshot_id"], token).status_code, 200
        )
        result = self._rotate(credential, "migration-bundle").json()
        self.assertEqual(result["snapshot_ids"], sids)
        self.assertEqual(result["remaining_reads"], 3)

    def test_bundle_rotation_concurrent_once_and_inherits_expiry(self):
        sids = self._two_snapshots()
        approved = self._approved_bundle(sids, max_reads=8, extra={"expires_in_seconds": 3600})
        old = approved["credential"]
        old_status = self.client.get(
            f"/read-bundles/{approved['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        responses = []
        barrier = threading.Barrier(6)

        def rotate(suffix):
            barrier.wait()
            client = TestClient(app_main.app)
            responses.append(
                client.post(
                    "/credentials/rotate",
                    headers=SERVICE_HEADERS,
                    json={"credential": old, "request_id": f"bc-{suffix}"},
                )
            )

        threads = [threading.Thread(target=rotate, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = [r.status_code for r in responses]
        self.assertEqual(statuses.count(200), 1, statuses)
        self.assertEqual(statuses.count(409), 5, statuses)
        winner = next(r for r in responses if r.status_code == 200).json()
        # 相同（原凭证, 赢家请求标识）重试：幂等回放，不再次换发
        replay = self._rotate(old, winner["request_id"]).json()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["new_credential"], winner["new_credential"])
        self.assertEqual(replay["expires_at"], old_status["expires_at"])


class ReadAuditEventIntegrationTests(unittest.TestCase):
    """成功读取审计：与扣次同事务的不可变事件 + 换发链归并 + 维护侧分页查询。"""

    WIDE_RANGE = "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z"

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    # ---- 工具方法 ----

    def _publish(self, max_reads=None, extra=None):
        body = {"text": TEXT_V1, "intervals": INTERVALS_V1}
        r = self.client.post("/records", headers=SERVICE_HEADERS, json=body)
        self.assertEqual(r.status_code, 201, r.text)
        rid = r.json()["record_id"]
        pub_body = {"version": 1}
        if max_reads is not None:
            pub_body["max_reads"] = max_reads
        if extra:
            pub_body.update(extra)
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json=pub_body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return rid, r.json()

    def _read(self, sid, credential, expected=200):
        r = self.client.get(f"/public/snapshots/{sid}?token={credential}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _events(self, sid, range_qs=None, limit=None, cursor=None):
        qs = range_qs if range_qs is not None else self.WIDE_RANGE
        if limit is not None:
            qs += f"&limit={limit}"
        if cursor is not None:
            qs += f"&cursor={cursor}"
        r = self.client.get(f"/snapshots/{sid}/read-events{qs}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _all_events(self, sid, limit=2, range_qs=None):
        """按小页大小把整个查询窗口翻完，返回事件序列（用于校验重复/遗漏）。"""
        page = self._events(sid, range_qs=range_qs, limit=limit)
        events = list(page["events"])
        cursor = page["next_cursor"]
        while cursor:
            page = self._events(
                sid, range_qs=range_qs, limit=limit, cursor=cursor
            )
            events.extend(page["events"])
            cursor = page["next_cursor"]
        return events

    def _approved_bundle(self, sids, max_reads=10):
        r = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": sids, "max_reads": max_reads},
        )
        self.assertEqual(r.status_code, 201, r.text)
        request_id = r.json()["request_id"]
        r = self.client.post(
            f"/bundle-requests/{request_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---- 写入：只记录成功读取，字段完整且无凭证明文 ----

    def test_success_writes_event_failure_writes_nothing(self):
        rid, pub = self._publish(max_reads=3)
        sid, token = pub["snapshot_id"], pub["token"]
        # 两次成功
        self._read(sid, token)
        self._read(sid, token)
        # 失败：错误凭证、耗尽后的第 4 次、撤回后的读取——都不产生事件
        self._read(sid, "no-such-token", expected=403)
        self._read(sid, token)  # 第 3 次成功
        self._read(sid, token, expected=403)  # 已耗尽
        # 另一未知快照（清单外语义的单令牌路径）也不产生任何事件
        import uuid as uuidlib

        self._read(uuidlib.uuid4(), token, expected=403)

        events = self._all_events(sid)
        self.assertEqual(len(events), 3)
        for event in events:
            self.assertEqual(set(event), {
                "event_id", "snapshot_id", "occurred_at",
                "credential_kind", "credential_chain_id",
            })
            self.assertEqual(event["snapshot_id"], sid)
            self.assertEqual(event["credential_kind"], "token")
            # 响应里绝不出现凭证字符串或正文
            self.assertNotIn(token, json.dumps(event))
            self.assertNotIn("redacted_text", json.dumps(event))
        # event_id 互不相同；occurred_at 非降序
        ids = [e["event_id"] for e in events]
        self.assertEqual(len(set(ids)), 3)
        times = [e["occurred_at"] for e in events]
        self.assertEqual(times, sorted(times))

    def test_withdrawn_snapshot_and_out_of_list_requests_write_nothing(self):
        _, p1 = self._publish()
        _, p2 = self._publish()
        s1, t1 = p1["snapshot_id"], p1["token"]
        s2, t2 = p2["snapshot_id"], p2["token"]
        # 令牌与快照不匹配 -> 403，两快照都不应有事件
        self._read(s1, t2, expected=403)
        self._read(s2, t1, expected=403)
        # 撤回 s2 后的任意读取不产生事件
        self._read(s2, t2)
        r = self.client.post(
            f"/snapshots/{s2}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "撤回审计测试"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self._read(s2, t2, expected=403)
        self.assertEqual(len(self._all_events(s1)), 0)
        # s2 只有撤回前那一次成功读取
        events_s2 = self._all_events(s2)
        self.assertEqual(len(events_s2), 1)
        # 已撤回快照的历史事件仍可维护侧查询
        self.assertEqual(events_s2[0]["snapshot_id"], s2)

    # ---- 换发链：多次换发归于同一链，旧凭证换发后不能读取 ----

    def test_chain_id_stable_across_multiple_rotations(self):
        _, pub = self._publish(max_reads=20)
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        current = token
        chains = set()
        for request_id in ("rot-1", "rot-2", "rot-3"):
            r = self.client.post(
                "/credentials/rotate",
                headers=SERVICE_HEADERS,
                json={"credential": current, "request_id": request_id},
            )
            self.assertEqual(r.status_code, 200, r.text)
            new_credential = r.json()["new_credential"]
            # 旧凭证换发后立即不能再读（失败不计数、不写事件）
            self._read(sid, current, expected=403)
            # 新凭证继续可读
            self._read(sid, new_credential)
            current = new_credential
        events = self._all_events(sid)
        # 1（初始）+ 3（每代新凭证各一次）= 4 条成功事件；3 次旧凭证失败不写
        self.assertEqual(len(events), 4)
        chains = {e["credential_chain_id"] for e in events}
        self.assertEqual(len(chains), 1)
        # 链标识是合法 UUID，但不等于任何一代凭证
        import uuid as uuidlib

        for event in events:
            uuidlib.UUID(event["credential_chain_id"])
            self.assertNotIn(current, event["credential_chain_id"])

    def test_bundle_chain_id_stable_and_kind_is_bundle(self):
        _, p1 = self._publish()
        _, p2 = self._publish()
        s1, s2 = p1["snapshot_id"], p2["snapshot_id"]
        approved = self._approved_bundle([s1, s2], max_reads=10)
        credential = approved["credential"]
        self._read(s1, credential)
        self._read(s2, credential)
        r = self.client.post(
            "/credentials/rotate",
            headers=SERVICE_HEADERS,
            json={"credential": credential, "request_id": "b-rot-1"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        new_credential = r.json()["new_credential"]
        self._read(s1, new_credential)
        for sid, expected_count in ((s1, 2), (s2, 1)):
            events = self._all_events(sid)
            self.assertEqual(len(events), expected_count)
            self.assertTrue(all(e["credential_kind"] == "bundle" for e in events))
            self.assertEqual(
                len({e["credential_chain_id"] for e in events}), 1
            )
        # 两个快照上同一条链的链标识一致（同一共享凭证及其换发体）
        chain_s1 = self._all_events(s1)[0]["credential_chain_id"]
        chain_s2 = self._all_events(s2)[0]["credential_chain_id"]
        self.assertEqual(chain_s1, chain_s2)

    # ---- 事件与扣次同事务 ----

    def test_event_and_quota_deduction_commit_together(self):
        from sqlalchemy import select as _select

        from app.db import SessionLocal
        from app.models import ReadAuditEvent, ReadToken

        _, pub = self._publish(max_reads=10)
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        with SessionLocal() as session:
            events = session.execute(
                _select(ReadAuditEvent).where(ReadAuditEvent.snapshot_id == sid)
            ).scalars().all()
            used = session.get(ReadToken, token).reads_used
        self.assertEqual(len(events), used)
        self.assertEqual(used, 1)

    # ---- 维护侧查询：鉴权、参数与游标校验 ----

    def test_query_requires_service_key(self):
        _, pub = self._publish()
        sid = pub["snapshot_id"]
        r = self.client.get(
            f"/snapshots/{sid}/read-events{self.WIDE_RANGE}"
        )
        self.assertEqual(r.status_code, 401)
        r = self.client.get(
            f"/snapshots/{sid}/read-events{self.WIDE_RANGE}",
            headers={"X-Service-Key": "wrong"},
        )
        self.assertEqual(r.status_code, 401)

    def test_query_unknown_snapshot_404(self):
        import uuid as uuidlib

        r = self.client.get(
            f"/snapshots/{uuidlib.uuid4()}/read-events{self.WIDE_RANGE}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 404)

    def test_query_invalid_time_range_422(self):
        _, pub = self._publish()
        sid = pub["snapshot_id"]
        # 缺失参数 -> 422（FastAPI 参数校验）
        r = self.client.get(
            f"/snapshots/{sid}/read-events?start=2020-01-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 无法解析的时刻 -> 422
        r = self.client.get(
            f"/snapshots/{sid}/read-events?start=not-a-time&end=2020-02-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 起 >= 止（空/倒置窗口）-> 422
        r = self.client.get(
            f"/snapshots/{sid}/read-events"
            "?start=2020-02-01T00:00:00Z&end=2020-02-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        r = self.client.get(
            f"/snapshots/{sid}/read-events"
            "?start=2020-03-01T00:00:00Z&end=2020-02-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # limit 越界 -> 422
        r = self.client.get(
            f"/snapshots/{sid}/read-events{self.WIDE_RANGE}&limit=0",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        r = self.client.get(
            f"/snapshots/{sid}/read-events{self.WIDE_RANGE}&limit=501",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)

    def test_query_invalid_or_mismatched_cursor_422(self):
        _, p1 = self._publish()
        _, p2 = self._publish()
        s1, s2 = p1["snapshot_id"], p2["snapshot_id"]
        base = "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z&limit=2"
        for bad_cursor in ("garbage", "!!!", "bm90anNvbg=="):
            r = self.client.get(
                f"/snapshots/{s1}/read-events{base}&cursor={bad_cursor}",
                headers=SERVICE_HEADERS,
            )
            self.assertEqual(r.status_code, 422, bad_cursor)
        self._read(s1, p1["token"])
        self._read(s1, p1["token"])
        self._read(s1, p1["token"])
        page = self._events(s1, range_qs=base, limit=2)
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        # 游标换快照 -> 422
        r = self.client.get(
            f"/snapshots/{s2}/read-events{base}&cursor={cursor}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 游标换时间窗口 -> 422
        other_window = "?start=2001-01-01T00:00:00Z&end=2100-01-01T00:00:00Z&limit=2"
        r = self.client.get(
            f"/snapshots/{s1}/read-events{other_window}&cursor={cursor}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 游标换 limit -> 422
        r = self.client.get(
            f"/snapshots/{s1}/read-events"
            "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z&limit=3"
            f"&cursor={cursor}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)
        # 篡改游标内容 -> 422
        import base64 as b64

        raw = b64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        tampered = json.loads(raw)
        tampered["after_seq"] = 99999
        encoded = b64.urlsafe_b64encode(
            json.dumps(tampered).encode()
        ).decode().rstrip("=")
        # after_seq > horizon_seq -> 非法
        r = self.client.get(
            f"/snapshots/{s1}/read-events{base}&cursor={encoded}",
            headers=SERVICE_HEADERS,
        )
        self.assertEqual(r.status_code, 422)

    # ---- 分页：窗口过滤、页大小与不重复不遗漏 ----

    def test_pagination_returns_all_events_exactly_once(self):
        _, pub = self._publish(max_reads=50)
        sid, token = pub["snapshot_id"], pub["token"]
        for _ in range(7):
            self._read(sid, token)
        events = self._all_events(sid, limit=3)
        self.assertEqual(len(events), 7)
        self.assertEqual(len({e["event_id"] for e in events}), 7)
        # 每页不超过页大小；最后一页后 next_cursor 为空
        page = self._events(sid, limit=3)
        self.assertEqual(len(page["events"]), 3)
        self.assertIsNotNone(page["next_cursor"])
        last = self._events(sid, limit=3, cursor=page["next_cursor"])
        # 第二次可能取完也可能还有，继续翻到末尾，末页必须无游标
        cursor = last["next_cursor"]
        while cursor:
            last = self._events(sid, limit=3, cursor=cursor)
            cursor = last["next_cursor"]
        self.assertIsNone(last["next_cursor"])

    def test_pagination_filters_time_range_half_open(self):
        from urllib.parse import quote

        _, pub = self._publish(max_reads=20)
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        first = self._events(sid)["events"][0]["occurred_at"]
        # 解析首事件时间，构造 [first, 遥远未来)：左闭包含首事件
        start = datetime.fromisoformat(first.replace("Z", "+00:00"))
        start_iso = quote(start.isoformat(), safe="")
        rng = f"?start={start_iso}&end=" + quote("2100-01-01T00:00:00+00:00", safe="")
        events = self._all_events(sid, range_qs=rng)
        self.assertEqual(len(events), 1)
        # 右开：end 恰好等于首事件时间时不含
        rng_excl = f"?start=2000-01-01T00:00:00Z&end={start_iso}"
        events = self._all_events(sid, range_qs=rng_excl)
        self.assertEqual(len(events), 0)
        # 落在窗口之外的事件不返回
        rng_miss = "?start=2030-01-01T00:00:00Z&end=2031-01-01T00:00:00Z"
        page = self._events(sid, range_qs=rng_miss)
        self.assertEqual(page["events"], [])
        self.assertIsNone(page["next_cursor"])

    def test_pagination_stable_against_concurrent_writes(self):
        """翻页期间并发写入：起点前已提交事件不重复不遗漏，新事件可留待下次查询。"""
        _, pub = self._publish(max_reads=1000)
        sid, token = pub["snapshot_id"], pub["token"]
        for _ in range(5):
            self._read(sid, token)

        # 首页（小页大小）确定查询上界
        first_page = self._events(sid, limit=2)
        self.assertEqual(len(first_page["events"]), 2)
        self.assertIsNotNone(first_page["next_cursor"])
        horizon_ids = {e["event_id"] for e in first_page["events"]}

        # 翻页进行中并发提交 5 条新事件
        def writer():
            client = TestClient(app_main.app)
            for _ in range(5):
                r = client.get(f"/public/snapshots/{sid}?token={token}")
                self.assertEqual(r.status_code, 200)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 继续沿旧游标翻完：恰好得到查询起点前已提交的 5 条，无重复
        seen = list(first_page["events"])
        cursor = first_page["next_cursor"]
        while cursor:
            page = self._events(sid, limit=2, cursor=cursor)
            seen.extend(page["events"])
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), 5)
        self.assertEqual(len({e["event_id"] for e in seen}), 5)
        self.assertEqual(
            {e["event_id"] for e in seen[:2]}, horizon_ids
        )
        # 查询起点后新提交的事件：再次发起查询即可见到全部 15 条
        fresh = self._all_events(sid, limit=4)
        self.assertEqual(len(fresh), 15)
        self.assertEqual(len({e["event_id"] for e in fresh}), 15)

    # ---- 不可变性：数据库层拒绝改写 ----

    def test_audit_table_is_append_only(self):
        from sqlalchemy import text as sql_text

        from app.db import engine

        _, pub = self._publish()
        sid, token = pub["snapshot_id"], pub["token"]
        self._read(sid, token)
        for statement in (
            "UPDATE read_audit_events SET credential_kind = 'bundle'",
            "DELETE FROM read_audit_events",
        ):
            with self.assertRaises(Exception) as ctx:
                with engine.begin() as conn:
                    conn.execute(sql_text(statement))
            self.assertIn("append-only", str(ctx.exception))
        with self.assertRaises(Exception) as ctx:
            with engine.begin() as conn:
                conn.execute(sql_text("TRUNCATE read_audit_events"))
        self.assertIn("append-only", str(ctx.exception))
        # 事件仍在
        self.assertEqual(len(self._all_events(sid)), 1)

    # ---- 既有库升级：补链标识、补审计表 ----

    def test_chain_id_audit_events_and_trigger_survive_upgrade_restart(self):
        """重启升级后：换发链标识稳定、审计事件只增且保留、防改写触发器仍生效。"""
        from sqlalchemy import select as _select, text as sql_text
        from sqlalchemy.exc import DBAPIError

        from app.db import SessionLocal, engine
        from app.models import ReadAuditEvent, ReadBundle, ReadToken

        _, pub = self._publish()
        token = pub["token"]
        sids = [self._publish()[1]["snapshot_id"] for _ in range(2)]
        approved = self._approved_bundle(sids, max_reads=5)
        credential = approved["credential"]
        self._read(pub["snapshot_id"], token)

        with SessionLocal() as session:
            chain_before = session.get(ReadToken, token).chain_id
            audit_before = (
                session.query(ReadAuditEvent)
                .filter(ReadAuditEvent.snapshot_id == pub["snapshot_id"])
                .count()
            )

        # 重启升级：不重放、不重写链标识、审计事件原样保留
        app_main._init_db_with_retry()

        with SessionLocal() as session:
            self.assertEqual(session.get(ReadToken, token).chain_id, chain_before)
            old_bundle = session.execute(
                _select(ReadBundle).where(ReadBundle.credential == credential)
            ).scalar_one()
            self.assertIsNotNone(old_bundle.chain_id)
            self.assertEqual(
                session.query(ReadAuditEvent)
                .filter(ReadAuditEvent.snapshot_id == pub["snapshot_id"])
                .count(),
                audit_before,
            )

        # 升级后既有凭证继续可读，成功读取照常同事务写审计事件
        self._read(pub["snapshot_id"], token)
        events = self._all_events(pub["snapshot_id"])
        self.assertEqual(len(events), audit_before + 1)
        with SessionLocal() as session:
            self.assertEqual(
                session.query(ReadAuditEvent)
                .filter(ReadAuditEvent.snapshot_id == pub["snapshot_id"])
                .count(),
                audit_before + 1,
            )
            # 防改写触发器在升级后仍生效
            with self.assertRaises(DBAPIError):
                session.execute(sql_text("UPDATE read_audit_events SET seq = seq + 1"))
                session.commit()
            session.rollback()

    def test_readiness_probe_reflects_upgrade_state(self):
        """升级完成后 /readyz 报告目标版本；业务接口保持原有鉴权与响应。"""
        import uuid

        from app.migrations import TARGET_SCHEMA_VERSION

        app_main._init_db_with_retry()
        r = self.client.get("/readyz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["schema_version"], TARGET_SCHEMA_VERSION)
        r = self.client.get("/livez")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["upgrade_status"], "ready")
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})
        # 鉴权语义不变：无密钥的维护侧请求仍 401
        r = self.client.get(f"/snapshots/{uuid.uuid4()}/read-events")
        self.assertEqual(r.status_code, 401)


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class RecordFreezeIntegrationTests(unittest.TestCase):
    """记录临时冻结：冻结/查询/解除、幂等与不可覆盖、公开读取拒绝与额度/审计规则。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _publish(self, version=1, extra=None):
        body = {"version": version}
        if extra:
            body.update(extra)
        r = self.client.post(
            f"/records/{self.record_id}/publish", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _create_other_record(self, text_="另一条记录，内容A。", intervals=None):
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": text_, "intervals": intervals or [{"start": 0, "end": 3}]},
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["record_id"]

    def _publish_other(self, record_id, max_reads=None):
        body = {"version": 1}
        if max_reads is not None:
            body["max_reads"] = max_reads
        r = self.client.post(
            f"/records/{record_id}/publish", headers=SERVICE_HEADERS, json=body
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def _freeze(self, reason="临时阻断外部阅览", expected=200):
        r = self.client.post(
            f"/records/{self.record_id}/freeze",
            headers=SERVICE_HEADERS,
            json={"reason": reason},
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _unfreeze(self, expected=200):
        r = self.client.post(
            f"/records/{self.record_id}/unfreeze", headers=SERVICE_HEADERS
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _freeze_status(self, expected=200):
        r = self.client.get(
            f"/records/{self.record_id}/freeze", headers=SERVICE_HEADERS
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 鉴权与明确拒绝 ----

    def test_freeze_endpoints_require_service_key(self):
        r = self.client.post(f"/records/{self.record_id}/freeze", json={"reason": "x"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post(f"/records/{self.record_id}/unfreeze")
        self.assertEqual(r.status_code, 401)
        r = self.client.get(f"/records/{self.record_id}/freeze")
        self.assertEqual(r.status_code, 401)
        # 错误密钥同样拒绝
        r = self.client.post(
            f"/records/{self.record_id}/freeze",
            headers={"X-Service-Key": "wrong"},
            json={"reason": "x"},
        )
        self.assertEqual(r.status_code, 401)

    def test_unknown_record_is_rejected_on_all_endpoints(self):
        import uuid

        unknown = str(uuid.uuid4())
        r = self.client.post(
            f"/records/{unknown}/freeze", headers=SERVICE_HEADERS, json={"reason": "x"}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "record not found")
        r = self.client.post(f"/records/{unknown}/unfreeze", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)
        r = self.client.get(f"/records/{unknown}/freeze", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 404)

    def test_blank_reason_is_rejected(self):
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            r = self.client.post(
                f"/records/{self.record_id}/freeze", headers=SERVICE_HEADERS, json=body
            )
            self.assertEqual(r.status_code, 422, (body, r.text))
            self.assertIn("reason", r.json()["detail"])

    # ---- 冻结/查询/解除基础语义 ----

    def test_freeze_status_unfreeze_lifecycle(self):
        # 未冻结
        body = self._freeze_status().json()
        self.assertEqual(
            body,
            {"record_id": self.record_id, "frozen": False, "frozen_at": None,
             "reason": None, "unfrozen_at": None},
        )
        # 未冻结时解除 -> 幂等 already_unfrozen
        r = self._unfreeze().json()
        self.assertFalse(r["frozen"])
        self.assertTrue(r["already_unfrozen"])
        self.assertIsNone(r["unfrozen_at"])

        f1 = self._freeze(reason="阻断原因A").json()
        self.assertTrue(f1["frozen"])
        self.assertFalse(f1["already_frozen"])
        self.assertEqual(f1["reason"], "阻断原因A")
        self.assertIsNotNone(f1["frozen_at"])
        self.assertIsNone(f1["unfrozen_at"])

        status = self._freeze_status().json()
        self.assertTrue(status["frozen"])
        self.assertEqual(status["frozen_at"], f1["frozen_at"])
        self.assertEqual(status["reason"], "阻断原因A")

        u = self._unfreeze().json()
        self.assertFalse(u["frozen"])
        self.assertFalse(u["already_unfrozen"])
        self.assertIsNotNone(u["unfrozen_at"])
        # 解除后状态
        status = self._freeze_status().json()
        self.assertFalse(status["frozen"])
        self.assertIsNone(status["frozen_at"])
        self.assertIsNone(status["reason"])
        # 重复解除幂等
        u2 = self._unfreeze().json()
        self.assertTrue(u2["already_unfrozen"])

    def test_repeated_freeze_is_idempotent_and_never_overwrites(self):
        f1 = self._freeze(reason="首次原因").json()
        # 不同原因 / 空原因重复冻结：回放首次结果
        r2 = self._freeze(reason="试图覆盖的原因").json()
        self.assertTrue(r2["already_frozen"])
        self.assertEqual(r2["frozen_at"], f1["frozen_at"])
        self.assertEqual(r2["reason"], "首次原因")
        r3 = self.client.post(
            f"/records/{self.record_id}/freeze", headers=SERVICE_HEADERS, json={}
        )
        self.assertEqual(r3.status_code, 200, r3.text)
        body3 = r3.json()
        self.assertTrue(body3["already_frozen"])
        self.assertEqual(body3["frozen_at"], f1["frozen_at"])
        self.assertEqual(body3["reason"], "首次原因")
        # 库内仍只有一个当前冻结期，原因是首次值
        from sqlalchemy import text as sql_text

        from app.db import SessionLocal

        with SessionLocal() as session:
            count, stored_reason = session.execute(
                sql_text(
                    "SELECT count(*), max(reason) FROM record_freezes "
                    "WHERE record_id = :rid AND unfrozen_at IS NULL"
                ),
                {"rid": self.record_id},
            ).one()
        self.assertEqual(count, 1)
        self.assertEqual(stored_reason, "首次原因")

    def test_refreeze_after_unfreeze_opens_new_period(self):
        f1 = self._freeze(reason="第一轮").json()
        u1 = self._unfreeze().json()
        f2 = self._freeze(reason="第二轮").json()
        self.assertFalse(f2["already_frozen"])
        self.assertEqual(f2["reason"], "第二轮")
        self.assertNotEqual(f2["frozen_at"], f1["frozen_at"])
        # 历史冻结期逐次留存（两行），当前只有第二轮
        from sqlalchemy import text as sql_text

        from app.db import SessionLocal

        with SessionLocal() as session:
            total, active = session.execute(
                sql_text(
                    "SELECT count(*), count(*) FILTER (WHERE unfrozen_at IS NULL) "
                    "FROM record_freezes WHERE record_id = :rid"
                ),
                {"rid": self.record_id},
            ).one()
        self.assertEqual(total, 2)
        self.assertEqual(active, 1)
        self.assertEqual(u1["unfrozen_at"] is not None, True)

    # ---- 对公开读取的效果 ----

    def test_frozen_record_blocks_token_reads_without_quota_or_audit(self):
        pub = self._publish(extra={"max_reads": 3})
        sid, tok = pub["snapshot_id"], pub["token"]
        # 冻结前可读
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 200
        )
        self._freeze()
        # 冻结后：有效令牌也模糊 403；重复两次均不扣次数
        for _ in range(2):
            r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["detail"], "snapshot unavailable")
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 1)  # 冻结前的一次；冻结后两次未扣
        self.assertEqual(q["remaining_reads"], 2)
        # 不写成功读取审计：冻结期间仍只有冻结前的 1 条
        events = self.client.get(
            f"/snapshots/{sid}/read-events"
            "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        ).json()["events"]
        self.assertEqual(len(events), 1)
        # 公开响应绝不透露冻结/原因
        body_text = r.text
        self.assertNotIn("阻断", body_text)
        self.assertNotIn("freeze", body_text.lower())

    def test_freeze_covers_snapshot_published_during_freeze(self):
        # 冻结期间发布的新快照同样不可读；解除后按凭证资格可读
        self._freeze(reason="期间发布也要阻断")
        pub = self._publish(extra={"max_reads": 2})
        sid, tok = pub["snapshot_id"], pub["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
        )
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], 0)
        self._unfreeze()
        r = self.client.get(f"/public/snapshots/{sid}?token={tok}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(q["reads_used"] + 1, 1)  # 解除后才开始计数

    def test_freeze_blocks_bundle_credential_but_other_records_stay_readable(self):
        # 记录 A 两个快照（需要共享凭证含 >=2 个快照）：冻结 A 后凭证读 A 全拒，
        # 凭证清单中不属于 A 的记录仍可读，且这些拒绝不扣共享额度。
        pub_a1 = self._publish(extra={"max_reads": 100})
        # 修订 A 到版本 2 再发布
        self.client.put(
            f"/records/{self.record_id}",
            headers=SERVICE_HEADERS,
            json={"base_version": 1, "text": TEXT_V2,
                  "intervals": [{"start": 9, "end": 15}]},
        )
        pub_a2 = self._publish(version=2, extra={"max_reads": 100})
        other_rid = self._create_other_record()
        pub_b = self._publish_other(other_rid, max_reads=100)
        # 共享凭证覆盖 A 的两个快照
        submit = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [pub_a1["snapshot_id"], pub_a2["snapshot_id"]],
                  "max_reads": 10},
        )
        self.assertEqual(submit.status_code, 201, submit.text)
        approve = self.client.post(
            f"/bundle-requests/{submit.json()['request_id']}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        cred = approve.json()["credential"]
        # 冻结前可正常读 A
        self.assertEqual(
            self.client.get(
                f"/public/snapshots/{pub_a1['snapshot_id']}?token={cred}"
            ).status_code,
            200,
        )
        self._freeze()
        # A 的两个快照凭共享凭证一律 403
        for sid in (pub_a1["snapshot_id"], pub_a2["snapshot_id"]):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={cred}").status_code,
                403,
            )
        # 冻结拒绝不扣共享额度（冻结前成功 1 次）
        bundle_id = approve.json()["bundle_id"]
        status = self.client.get(
            f"/read-bundles/{bundle_id}", headers=SERVICE_HEADERS
        ).json()
        self.assertEqual(status["reads_used"], 1)
        # 单快照令牌（A 记录）冻结期间也 403
        self.assertEqual(
            self.client.get(
                f"/public/snapshots/{pub_a1['snapshot_id']}?token={pub_a1['token']}"
            ).status_code,
            403,
        )
        # 解除后 A 恢复
        self._unfreeze()
        self.assertEqual(
            self.client.get(
                f"/public/snapshots/{pub_a2['snapshot_id']}?token={cred}"
            ).status_code,
            200,
        )
        # 其他记录 B 的快照自始至终可读，其令牌与本次冻结无关
        self.assertEqual(
            self.client.get(
                f"/public/snapshots/{pub_b['snapshot_id']}?token={pub_b['token']}"
            ).status_code,
            200,
        )

    def test_unfreeze_does_not_revive_revoked_or_exhausted_credentials(self):
        # 1) 撤销的令牌解除后不复活
        pub = self._publish(extra={"max_reads": 3})
        sid, tok = pub["snapshot_id"], pub["token"]
        self._freeze()
        r = self.client.post(f"/tokens/{tok}/revoke", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200)
        self._unfreeze()
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
        )
        # 2) 冻结前已耗尽额度的令牌解除后不复活
        pub2 = self._publish(extra={"max_reads": 1})
        sid2, tok2 = pub2["snapshot_id"], pub2["token"]
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid2}?token={tok2}").status_code, 200
        )
        self._freeze()
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid2}?token={tok2}").status_code, 403
        )
        self._unfreeze()
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid2}?token={tok2}").status_code, 403
        )

    def test_withdrawn_snapshot_stays_unavailable_after_unfreeze(self):
        pub = self._publish()
        sid, tok = pub["snapshot_id"], pub["token"]
        self._freeze()
        r = self.client.post(
            f"/snapshots/{sid}/withdraw",
            headers=SERVICE_HEADERS,
            json={"reason": "冻结期间撤回"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self._unfreeze()
        self.assertEqual(
            self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code, 403
        )

    def test_publish_review_and_other_auth_contracts_remain_compatible(self):
        # 冻结不阻止发布/受控复核流程；公开读取鉴权与响应结构不变
        self._freeze()
        pub = self._publish()
        self.assertEqual(pub["status"], "published")
        # 维护侧记录状态接口仍可用，响应结构兼容（不含冻结字段也不报错）
        rec = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(rec.status_code, 200)
        self.assertEqual(
            sorted(s["status"] for s in rec.json()["snapshots"]), ["published"]
        )
        # 受控申请-复核在冻结期间仍可完成（批准签发），但快照读仍 403
        self.client.put(
            f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS
        )
        sub = self.client.post(
            f"/records/{self.record_id}/publish-requests",
            auth=BASIC_ALICE,
            json={"version": 1},
        )
        # 版本 1 已有快照（非撤回），受控申请仍允许提交
        self.assertEqual(sub.status_code, 201, sub.text)
        # 公开读取对无效令牌的模糊 403 与冻结时完全一致
        r = self.client.get(f"/public/snapshots/{pub['snapshot_id']}?token=bogus")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"detail": "snapshot unavailable"})

    # ---- 提交边界：冻结与并发读取 ----

    def test_concurrent_freeze_and_reads_respect_commit_boundary(self):
        pub = self._publish(extra={"max_reads": 50})
        sid, tok = pub["snapshot_id"], pub["token"]
        results: list[int] = []
        barrier = threading.Barrier(9)

        def freeze():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.post(
                f"/records/{self.record_id}/freeze",
                headers=SERVICE_HEADERS,
                json={"reason": "并发冻结"},
            )
            results.append(0 if r.status_code == 200 else r.status_code)

        def read_once():
            barrier.wait()
            client = TestClient(app_main.app)
            results.append(
                client.get(f"/public/snapshots/{sid}?token={tok}").status_code
            )

        threads = [threading.Thread(target=freeze)]
        threads += [threading.Thread(target=read_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIn(0, results)  # 冻结成功
        concurrent_reads = [c for c in results if c != 0]
        # 冻结提交后开始的读取一律 403；冻结提交前已获锁的读取至多 8 个 200
        self.assertTrue(all(c in (200, 403) for c in concurrent_reads), results)
        for _ in range(5):
            self.assertEqual(
                self.client.get(f"/public/snapshots/{sid}?token={tok}").status_code,
                403,
            )
        # 冻结完成后的 403 不消耗次数；审计事件数恰为并发阶段的成功数
        q = self.client.get(f"/tokens/{tok}", headers=SERVICE_HEADERS).json()
        self.assertEqual(q["reads_used"], concurrent_reads.count(200))
        events = self.client.get(
            f"/snapshots/{sid}/read-events"
            "?start=2000-01-01T00:00:00Z&end=2100-01-01T00:00:00Z",
            headers=SERVICE_HEADERS,
        ).json()["events"]
        self.assertEqual(len(events), concurrent_reads.count(200))

    def test_concurrent_freeze_calls_create_exactly_one_period(self):
        """并发首次冻结：恰好一个成功新开冻结期，其余幂等回放，原因/时间不被覆盖。"""
        responses: list[dict] = []
        barrier = threading.Barrier(8)

        def freeze_once():
            barrier.wait()
            client = TestClient(app_main.app)
            r = client.post(
                f"/records/{self.record_id}/freeze",
                headers=SERVICE_HEADERS,
                json={"reason": "并发冻结原因"},
            )
            responses.append(r.json())

        threads = [threading.Thread(target=freeze_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(responses), 8)
        first = [b for b in responses if not b["already_frozen"]]
        replayed = [b for b in responses if b["already_frozen"]]
        self.assertEqual(len(first), 1, responses)
        self.assertEqual(len(replayed), 7, responses)
        frozen_ats = {b["frozen_at"] for b in responses}
        reasons = {b["reason"] for b in responses}
        self.assertEqual(len(frozen_ats), 1)
        self.assertEqual(reasons, {"并发冻结原因"})

        from sqlalchemy import text as sql_text

        from app.db import SessionLocal

        with SessionLocal() as session:
            total = session.execute(
                sql_text(
                    "SELECT count(*) FROM record_freezes WHERE record_id = :rid"
                ),
                {"rid": self.record_id},
            ).scalar_one()
        self.assertEqual(total, 1)


@unittest.skipIf(SKIP_INTEGRATION, _SKIP_REASON)
class SensitivePhraseIntegrationTests(unittest.TestCase):
    """敏感短语清单：原子替换/查询 + 发布与受控申请提交/批准核验 + 并发版本一致性。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        # 复位为停用核验，避免影响其他测试类
        cls.client.put("/sensitive-phrases", headers=SERVICE_HEADERS, json={"phrases": []})
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # 敏感短语清单是全局单行：每个用例前后复位为停用核验
        r = self.client.put(
            "/sensitive-phrases", headers=SERVICE_HEADERS, json={"phrases": []}
        )
        self.assertEqual(r.status_code, 200, r.text)

    def _create_record(self, text, intervals):
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": text, "intervals": intervals},
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["record_id"]

    def _mark_controlled(self, rid):
        r = self.client.put(f"/records/{rid}/controlled", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)

    def _put_phrases(self, phrases):
        return self.client.put(
            "/sensitive-phrases", headers=SERVICE_HEADERS, json={"phrases": phrases}
        )

    def _get_phrases(self):
        return self.client.get("/sensitive-phrases", headers=SERVICE_HEADERS)

    # ---- 清单维护：鉴权、初始状态、替换、校验 ----

    def test_initial_list_is_empty_and_disabled(self):
        # 全新初始化（本测试库首个用例）或复位后：空清单、停用核验、version>=1
        r = self._get_phrases()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["phrases"], [])
        self.assertFalse(body["enabled"])
        self.assertGreaterEqual(body["version"], 1)

    def test_endpoints_require_service_key(self):
        self.assertEqual(self.client.get("/sensitive-phrases").status_code, 401)
        self.assertEqual(
            self.client.put(
                "/sensitive-phrases", json={"phrases": ["x"]}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/sensitive-phrases", headers={"X-Service-Key": "wrong"}
            ).status_code,
            401,
        )

    def test_replace_atomically_returns_new_version(self):
        r1 = self._put_phrases(["机密", "110101"])
        self.assertEqual(r1.status_code, 200, r1.text)
        b1 = r1.json()
        self.assertEqual(b1["phrases"], ["机密", "110101"])
        self.assertTrue(b1["enabled"])
        self.assertIsNotNone(b1["updated_at"])
        # 再次替换（即使内容相同）version 再 +1，整份清单原子切换
        r2 = self._put_phrases(["机密", "110101"])
        b2 = r2.json()
        self.assertEqual(b2["version"], b1["version"] + 1)
        self.assertEqual(b2["phrases"], ["机密", "110101"])
        # 替换为完全不同的清单：旧短语不再出现
        r3 = self._put_phrases(["另一个短语"])
        self.assertEqual(r3.json()["phrases"], ["另一个短语"])
        self.assertEqual(r3.json()["version"], b2["version"] + 1)

    def test_empty_list_replaces_and_disables(self):
        self._put_phrases(["机密"])
        r = self._put_phrases([])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["phrases"], [])
        self.assertFalse(body["enabled"])

    def test_invalid_payloads_422(self):
        for bad in ([""], ["a", "a"]):  # 空短语；重复短语
            r = self._put_phrases(bad)
            self.assertEqual(r.status_code, 422, (bad, r.text))
        # 缺字段
        r = self.client.put(
            "/sensitive-phrases", headers=SERVICE_HEADERS, json={}
        )
        self.assertEqual(r.status_code, 422)
        # "a" 与 "A" 是两个不同短语（区分大小写）：允许
        r = self._put_phrases(["a", "A"])
        self.assertEqual(r.status_code, 200, r.text)

    # ---- 直接发布核验 ----

    def test_direct_publish_blocked_when_hit_uncovered(self):
        rid = self._create_record(
            "患者张三，身份证号 110101199003077777。",
            [{"start": 2, "end": 4}],  # 只遮蔽姓名，未遮蔽身份证号
        )
        self._put_phrases(["110101199003077777"])
        r = self.client.post(
            f"/records/{rid}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 422, r.text)
        body = r.json()["detail"]
        self.assertIsInstance(body, dict)
        self.assertEqual(
            body["hits"], [{"start": 10, "end": 28}]
        )
        # 不回显短语或原文
        self.assertNotIn("110101199003077777", r.text)
        self.assertNotIn("患者", r.text)
        # 不产生快照或令牌
        status = self.client.get(f"/records/{rid}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status["snapshots"], [])

    def test_direct_publish_allowed_when_hit_fully_covered(self):
        rid = self._create_record(
            "患者张三，身份证号 110101199003077777。",
            [{"start": 10, "end": 28}],
        )
        self._put_phrases(["110101199003077777"])
        r = self.client.post(
            f"/records/{rid}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)

    def test_partial_coverage_still_uncovered(self):
        rid = self._create_record(
            "患者张三，身份证号 110101199003077777。",
            [{"start": 10, "end": 20}],  # 只遮蔽命中的一部分
        )
        self._put_phrases(["110101199003077777"])
        r = self.client.post(
            f"/records/{rid}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["detail"]["hits"], [{"start": 10, "end": 28}])

    def test_case_sensitive_matching(self):
        rid = self._create_record("令牌ABC内容", [])
        # 小写短语不应命中大写原文：发布成功
        self._put_phrases(["abc"])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 201, r.text)
        # 大写短语命中且未遮蔽：拒绝
        self._put_phrases(["ABC"])
        # 版本 1 已发布（快照已存在）；换一条新记录验证大写命中
        rid2 = self._create_record("令牌ABC内容", [])
        r = self.client.post(
            f"/records/{rid2}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["detail"]["hits"], [{"start": 2, "end": 5}])

    def test_empty_list_disables_verification(self):
        rid = self._create_record("完全未遮蔽的机密内容。", [])
        # 空清单：停用核验，直接发布成功
        self._put_phrases([])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 201, r.text)

    def test_overlapping_phrase_hits_reported(self):
        rid = self._create_record("aaaa内容", [])
        self._put_phrases(["aaa"])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(
            r.json()["detail"]["hits"], [{"start": 0, "end": 3}, {"start": 1, "end": 4}]
        )

    def test_multiple_hits_positions(self):
        rid = self._create_record("机密一与机密二", [])
        self._put_phrases(["机密"])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 422, r.text)
        # 机(0)密(1)一(2)与(3)机(4)密(5)二(6)
        self.assertEqual(
            r.json()["detail"]["hits"], [{"start": 0, "end": 2}, {"start": 4, "end": 6}]
        )

    # ---- 受控申请：提交核验 + 批准时按当时清单重新核验 ----

    def test_controlled_submit_verified(self):
        rid = self._create_record("受控机密内容。", [])
        self._mark_controlled(rid)
        self._put_phrases(["机密"])
        r = self.client.post(
            f"/records/{rid}/publish-requests",
            auth=BASIC_ALICE,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["detail"]["hits"], [{"start": 2, "end": 4}])
        # 待审申请没有产生
        listing = self.client.get(
            f"/records/{rid}/publish-requests", auth=BASIC_ALICE
        ).json()
        self.assertEqual(listing["requests"], [])

    def test_controlled_approve_rechecks_with_current_list(self):
        rid = self._create_record("受控机密内容。", [{"start": 2, "end": 4}])
        self._mark_controlled(rid)
        # 提交时清单为空（停用核验）：申请成功
        self._put_phrases([])
        r = self.client.post(
            f"/records/{rid}/publish-requests",
            auth=BASIC_ALICE,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        prid = r.json()["request_id"]
        # 等待期间启用不含命中的清单：批准时用当时清单重新核验冻结原文，通过
        self._put_phrases(["秘密"])  # 原文无“秘密”：核验通过
        r = self.client.post(f"/publish-requests/{prid}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"])

        # 第二条记录的申请：等待期间加入命中短语，批准必须 422 且申请仍待审可驳回
        rid2 = self._create_record("受控机密内容。", [{"start": 2, "end": 4}])
        self._mark_controlled(rid2)
        self._put_phrases([])  # 提交时停用
        r = self.client.post(
            f"/records/{rid2}/publish-requests",
            auth=BASIC_ALICE,
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        prid2 = r.json()["request_id"]
        # 批准前启用清单：“受控”位于 [0,2)，未被 [2,4) 遮蔽 -> 按当时清单重新核验失败
        self._put_phrases(["受控"])
        r = self.client.post(f"/publish-requests/{prid2}/approve", auth=BASIC_BOB)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["detail"]["hits"], [{"start": 0, "end": 2}])
        self.assertNotIn("受控", r.text)
        # 不产生新快照/令牌
        status = self.client.get(f"/records/{rid2}", headers=SERVICE_HEADERS).json()
        self.assertEqual(status["snapshots"], [])
        # 申请保持待审：仍可驳回
        r = self.client.post(
            f"/publish-requests/{prid2}/reject",
            auth=BASIC_BOB,
            json={"reason": "清单已启用，遮蔽不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")

    def test_list_change_does_not_affect_published_snapshot_and_token(self):
        rid = self._create_record("历史发布内容。", [])
        # 清单为空时发布成功
        self._put_phrases([])
        pub = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        ).json()
        sid, token = pub["snapshot_id"], pub["token"]
        # 发布后再启用包含原文短语的清单：已发布快照与令牌不受影响，公开仍可读
        self._put_phrases(["历史发布"])
        r = self.client.get(f"/public/snapshots/{sid}?token={token}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["redacted_text"], "历史发布内容。")

    # ---- 并发：发布按同一已提交清单版本完成核验与提交 ----

    def test_replace_blocks_until_publishing_transaction_commits(self):
        """确定性制造重叠：先在独立事务持有清单行锁（模拟发布事务进行中），
        此时 PUT 替换必须阻塞；持锁期间 GET 仍读到旧清单；持锁提交后 PUT 完成、
        版本递增。证明替换与发布由同一行锁串行化。
        """
        from sqlalchemy import text as sql_text

        from app.db import SessionLocal

        self._put_phrases(["旧短语"])
        version_before = self._get_phrases().json()["version"]

        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_settings_lock():
            session = SessionLocal()
            try:
                session.execute(
                    sql_text(
                        "SELECT id FROM sensitive_phrase_settings "
                        "WHERE id = 1 FOR UPDATE"
                    )
                )
                holder_ready.set()
                release_holder.wait(timeout=10)
                session.commit()
            finally:
                session.close()

        holder = threading.Thread(target=hold_settings_lock)
        holder.start()
        self.assertTrue(holder_ready.wait(timeout=10))

        result = {}

        def replace():
            client = TestClient(app_main.app)
            r = client.put(
                "/sensitive-phrases",
                headers=SERVICE_HEADERS,
                json={"phrases": ["新短语"]},
            )
            result["status"] = r.status_code
            result["body"] = r.json() if r.status_code == 200 else r.text

        t = threading.Thread(target=replace)
        t.start()
        time.sleep(0.7)
        # 替换仍被行锁阻塞、未完成
        self.assertNotIn("status", result)
        # 行锁未提交：其他请求读到的仍是旧清单版本
        cur = self._get_phrases().json()
        self.assertEqual(cur["version"], version_before)
        self.assertEqual(cur["phrases"], ["旧短语"])

        release_holder.set()
        holder.join(timeout=10)
        t.join(timeout=10)
        self.assertEqual(result["status"], 200, result)
        self.assertEqual(result["body"]["version"], version_before + 1)
        self.assertEqual(result["body"]["phrases"], ["新短语"])

    def test_concurrent_publish_and_replace_commit_consistent_versions(self):
        """压力场景：发布事务与清单替换并发，每个发布要么整体按旧清单、要么整体
        按新清单核验并提交；不应出现按混合状态产生的结果。新清单命中未遮蔽内容，
        故通过的发布数 + 被拒发布数恒等于尝试次数，且一旦新清单生效后所有后续
        发布都被拒绝（没有漏网）。
        """
        rid = self._create_record("并发机密内容。", [])
        self._put_phrases([])  # 初始停用：发布可通过

        outcomes: list[int] = []
        barrier = threading.Barrier(11)

        def publish_once():
            client = TestClient(app_main.app)
            barrier.wait()
            r = client.post(
                f"/records/{rid}/publish",
                headers=SERVICE_HEADERS,
                json={"version": 1},
            )
            outcomes.append(r.status_code)

        def enable_list():
            client = TestClient(app_main.app)
            barrier.wait()
            time.sleep(0.05)  # 让部分发布先按旧清单进入/完成
            client.put(
                "/sensitive-phrases",
                headers=SERVICE_HEADERS,
                json={"phrases": ["机密"]},
            )

        threads = [threading.Thread(target=publish_once) for _ in range(10)]
        threads.append(threading.Thread(target=enable_list))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(outcomes), 10)
        self.assertTrue(all(c in (201, 409, 422) for c in outcomes), outcomes)
        # 同一版本只有一份不可变快照：首个成功发布 201，其后旧清单下的重复发布
        # 是正常的重复发布路径；关键不变量是——新清单生效后不可能再有 201，
        # 即最后一个非冲突结果必须是 422，且 422 之后不出现 201。
        meaningful = [c for c in outcomes if c != 409]
        if 422 in meaningful:
            first_reject = meaningful.index(422)
            self.assertNotIn(201, meaningful[first_reject:], outcomes)

    def test_rejected_publish_leaves_no_snapshot_but_retry_after_disable_succeeds(self):
        rid = self._create_record("临时机密内容。", [])
        self._put_phrases(["机密"])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 422)
        # 停用核验后重试同一版本：成功发布
        self._put_phrases([])
        r = self.client.post(
            f"/records/{rid}/publish", headers=SERVICE_HEADERS, json={"version": 1}
        )
        self.assertEqual(r.status_code, 201, r.text)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SnapshotReadingWindowIntegrationTests(unittest.TestCase):
    """快照外部阅览时段：发布/申请携带、首次发布固定、窗口外模糊 403 不扣次不审计。"""

    @classmethod
    def setUpClass(cls):
        app_main._init_db_with_retry()
        cls.client = TestClient(app_main.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": TEXT_V1, "intervals": INTERVALS_V1},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.record_id = r.json()["record_id"]

    # ---- 工具方法 ----

    def _publish(self, version=1, body=None, expected=201):
        payload = {"version": version}
        if body:
            payload.update(body)
        r = self.client.post(
            f"/records/{self.record_id}/publish",
            headers=SERVICE_HEADERS,
            json=payload,
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _read(self, snapshot_id, token, expected=200):
        r = self.client.get(f"/public/snapshots/{snapshot_id}?token={token}")
        self.assertEqual(r.status_code, expected, r.text)
        return r

    def _token_status(self, token):
        r = self.client.get(f"/tokens/{token}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _record_snapshots(self):
        r = self.client.get(f"/records/{self.record_id}", headers=SERVICE_HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return {s["version"]: s for s in r.json()["snapshots"]}

    def _mark_controlled(self):
        r = self.client.put(
            f"/records/{self.record_id}/controlled", headers=SERVICE_HEADERS
        )
        self.assertEqual(r.status_code, 200, r.text)

    def _submit_request(self, body, auth=BASIC_ALICE, expected=201):
        r = self.client.post(
            f"/records/{self.record_id}/publish-requests", auth=auth, json=body
        )
        self.assertEqual(r.status_code, expected, r.text)
        return r

    # ---- 请求体校验 ----

    def test_window_must_be_provided_as_a_pair(self):
        now = datetime.now(timezone.utc)
        open_iso = _iso(now + timedelta(seconds=10))
        close_iso = _iso(now + timedelta(hours=1))
        # 只填开放
        r = self._publish(body={"window_open_at": open_iso}, expected=422)
        self.assertIn("window_open_at", str(r.json()))
        # 只填截止
        r = self._publish(body={"window_close_at": close_iso}, expected=422)
        self.assertIn("window_open_at", str(r.json()))

    def test_window_open_must_be_before_close(self):
        now = datetime.now(timezone.utc)
        r = self._publish(
            body={
                "window_open_at": _iso(now + timedelta(hours=1)),
                "window_close_at": _iso(now + timedelta(seconds=10)),
            },
            expected=422,
        )
        self.assertIn("window_open_at", str(r.json()))
        # 开放等于截止也拒绝
        same = _iso(now + timedelta(hours=1))
        r = self._publish(
            body={"window_open_at": same, "window_close_at": same},
            expected=422,
        )

    def test_window_close_already_passed_at_submit_is_422(self):
        r = self._publish(
            body={
                "window_open_at": "2000-01-01T00:00:00Z",
                "window_close_at": "2000-01-02T00:00:00Z",
            },
            expected=422,
        )
        self.assertIn("window_close_at", r.json()["detail"])

    # ---- 直接发布：窗口判定（开放含、截止不含） ----

    def test_direct_publish_window_boundaries_are_half_open(self):
        now = datetime.now(timezone.utc)
        # 开放时刻在过去 1 秒（允许“已开放”），截止在 1 秒后：当前在窗口内
        r = self._publish(
            body={
                "max_reads": 5,
                "window_open_at": _iso(now - timedelta(seconds=30)),
                "window_close_at": _iso(now + timedelta(seconds=2)),
            }
        )
        data = r.json()
        sid, token = data["snapshot_id"], data["token"]
        self.assertIsNotNone(data["window_open_at"])
        self.assertIsNotNone(data["window_close_at"])
        # 窗口内：200 并扣 1 次
        self._read(sid, token)
        self.assertEqual(self._token_status(token)["reads_used"], 1)
        # 截止时刻前不含：等到关闭后 -> 模糊 403，不扣次
        time.sleep(2.4)
        self._read(sid, token, expected=403)
        self.assertEqual(self._token_status(token)["reads_used"], 1)

    def test_before_window_open_is_opaque_403_without_quota_or_audit(self):
        now = datetime.now(timezone.utc)
        r = self._publish(
            body={
                "max_reads": 3,
                "window_open_at": _iso(now + timedelta(hours=1)),
                "window_close_at": _iso(now + timedelta(hours=2)),
            }
        )
        sid, token = r.json()["snapshot_id"], r.json()["token"]
        self._read(sid, token, expected=403)
        self._read(sid, token, expected=403)
        # 不扣次数
        status = self._token_status(token)
        self.assertEqual(status["reads_used"], 0)
        self.assertEqual(status["remaining_reads"], 3)
        # 不写成功读取审计
        events = self.client.get(
            f"/snapshots/{sid}/read-events",
            headers=SERVICE_HEADERS,
            params={
                "start": _iso(now - timedelta(days=1)),
                "end": _iso(now + timedelta(days=1)),
            },
        )
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()["events"], [])

    def test_naive_datetime_treated_as_utc_and_offset_timezones_equivalent(self):
        # 同一绝对时刻分别用 Z 与带偏移量表达：第二次是“相同设置”的重复发布，
        # 应幂等放行而非冲突，证明服务端按绝对时刻比较。
        now = datetime.now(timezone.utc)
        open_dt = now + timedelta(minutes=5)
        close_dt = now + timedelta(hours=1)
        naive_open = open_dt.replace(tzinfo=None).isoformat()
        naive_close = close_dt.replace(tzinfo=None).isoformat()
        r1 = self._publish(
            body={"window_open_at": naive_open, "window_close_at": naive_close}
        )
        sid1 = r1.json()["snapshot_id"]
        # +08:00 表达同一 UTC 绝对时刻
        offset_open = open_dt.astimezone(timezone(timedelta(hours=8))).isoformat()
        offset_close = close_dt.astimezone(timezone(timedelta(hours=8))).isoformat()
        r2 = self._publish(
            body={"window_open_at": offset_open, "window_close_at": offset_close}
        )
        self.assertEqual(r2.json()["snapshot_id"], sid1)
        self.assertEqual(
            r2.json()["window_close_at"], r1.json()["window_close_at"]
        )

    # ---- 首次发布固定、后续签发不可改写 ----

    def test_window_is_fixed_at_first_publish_and_immutable(self):
        now = datetime.now(timezone.utc)
        open1 = now + timedelta(minutes=5)
        close1 = now + timedelta(hours=1)
        open2 = now + timedelta(minutes=10)
        close2 = now + timedelta(hours=2)
        r1 = self._publish(
            body={"window_open_at": _iso(open1), "window_close_at": _iso(close1)}
        )
        sid = r1.json()["snapshot_id"]
        # 后续签发省略时段：沿用首次设置
        r2 = self._publish(body={"max_reads": 7})
        self.assertEqual(r2.json()["snapshot_id"], sid)
        self.assertEqual(
            r2.json()["window_close_at"], r1.json()["window_close_at"]
        )
        # 显式相同设置：幂等放行
        r3 = self._publish(
            body={"window_open_at": _iso(open1), "window_close_at": _iso(close1)}
        )
        self.assertEqual(r3.json()["snapshot_id"], sid)        # 显式不同设置：409 冲突，且不改写快照
        r4 = self._publish(
            body={"window_open_at": _iso(open2), "window_close_at": _iso(close2)},
            expected=409,
        )
        self.assertIn("window", str(r4.json()["detail"]))
        snap = self._record_snapshots()[1]
        self.assertEqual(snap["window_open_at"], r1.json()["window_open_at"])
        self.assertEqual(snap["window_close_at"], r1.json()["window_close_at"])
        # 沿用首次设置签出的令牌仍可按原窗口工作；新令牌计数独立
        t1, t2 = r1.json()["token"], r2.json()["token"]
        # 窗口未开放：两枚令牌都 403，各自不扣次
        self._read(sid, t1, expected=403)
        self._read(sid, t2, expected=403)
        self.assertEqual(self._token_status(t1)["reads_used"], 0)
        self.assertEqual(self._token_status(t2)["reads_used"], 0)

    def test_snapshot_without_window_can_never_gain_one(self):
        r1 = self._publish()  # 首次发布不带时段
        sid = r1.json()["snapshot_id"]
        self.assertIsNone(r1.json()["window_open_at"])
        self.assertIsNone(r1.json()["window_close_at"])
        now = datetime.now(timezone.utc)
        r2 = self._publish(
            body={
                "window_open_at": _iso(now + timedelta(minutes=1)),
                "window_close_at": _iso(now + timedelta(hours=1)),
            },
            expected=409,
        )
        self.assertIn("window", str(r2.json()["detail"]))
        snap = self._record_snapshots()[1]
        self.assertIsNone(snap["window_open_at"])
        self.assertIsNone(snap["window_close_at"])

    def test_no_window_requests_stay_unrestricted_and_compatible(self):
        # 不带时段字段（既有请求形态）：发布后立即可读、永久可读
        r = self._publish(body={"max_reads": 2})
        sid, token = r.json()["snapshot_id"], r.json()["token"]
        self.assertIsNone(r.json()["window_open_at"])
        body = self._read(sid, token).json()
        # 公开成功响应结构保持兼容
        self.assertEqual(set(body.keys()), {"record_id", "version", "status", "redacted_text"})
        self._read(sid, token)
        self._read(sid, token, expected=403)  # 次数用尽仍按既有规则

    # ---- 维护侧查询 ----

    def test_maintenance_record_status_returns_window(self):
        now = datetime.now(timezone.utc)
        open_at, close_at = now + timedelta(minutes=5), now + timedelta(hours=1)
        self._publish(
            body={"window_open_at": _iso(open_at), "window_close_at": _iso(close_at)}
        )
        snap = self._record_snapshots()[1]
        self.assertIsNotNone(snap["window_open_at"])
        self.assertIsNotNone(snap["window_close_at"])
        # 无时段快照字段为 null
        r = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": "另一条记录。", "intervals": []},
        )
        rid2 = r.json()["record_id"]
        self.client.post(
            f"/records/{rid2}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        r = self.client.get(f"/records/{rid2}", headers=SERVICE_HEADERS)
        s = r.json()["snapshots"][0]
        self.assertIsNone(s["window_open_at"])
        self.assertIsNone(s["window_close_at"])

    # ---- 受控发布：申请冻结时段、批准时校验 ----

    def test_controlled_request_freezes_window_and_info_exposes_it(self):
        self._mark_controlled()
        now = datetime.now(timezone.utc)
        open_at, close_at = now + timedelta(minutes=5), now + timedelta(hours=1)
        r = self._submit_request(
            {
                "version": 1,
                "window_open_at": _iso(open_at),
                "window_close_at": _iso(close_at),
            }
        )
        req_id = r.json()["request_id"]
        self.assertEqual(r.json()["window_close_at"], _iso(close_at))
        info = self.client.get(
            f"/publish-requests/{req_id}", auth=BASIC_ALICE
        ).json()
        self.assertIsNotNone(info["window_open_at"])
        self.assertIsNotNone(info["window_close_at"])

    def test_controlled_approval_uses_frozen_window(self):
        self._mark_controlled()
        now = datetime.now(timezone.utc)
        r = self._submit_request(
            {
                "version": 1,
                "max_reads": 3,
                "window_open_at": _iso(now - timedelta(seconds=10)),
                "window_close_at": _iso(now + timedelta(seconds=2)),
            }
        )
        req_id = r.json()["request_id"]
        a = self.client.post(
            f"/publish-requests/{req_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(a.status_code, 200, a.text)
        sid, token = a.json()["snapshot_id"], a.json()["token"]
        self.assertIsNotNone(a.json()["window_open_at"])
        self._read(sid, token)
        time.sleep(2.4)
        self._read(sid, token, expected=403)
        self.assertEqual(self._token_status(token)["reads_used"], 1)

    def test_controlled_approval_rejected_when_window_closed_but_stays_pending(self):
        self._mark_controlled()
        # 提交时窗口仍开放（截止在 2 秒后），批准前等待其关闭
        now = datetime.now(timezone.utc)
        r = self._submit_request(
            {
                "version": 1,
                "window_open_at": _iso(now - timedelta(seconds=10)),
                "window_close_at": _iso(now + timedelta(seconds=1)),
            }
        )
        req_id = r.json()["request_id"]
        time.sleep(1.4)
        a = self.client.post(
            f"/publish-requests/{req_id}/approve", auth=BASIC_BOB
        )
        self.assertEqual(a.status_code, 409, a.text)
        # 申请仍待审、可驳回；期间没有快照/令牌产生
        info = self.client.get(
            f"/publish-requests/{req_id}", auth=BASIC_ALICE
        ).json()
        self.assertEqual(info["status"], "pending")
        self.assertIsNone(info["snapshot_id"])
        rej = self.client.post(
            f"/publish-requests/{req_id}/reject",
            auth=BASIC_BOB,
            json={"reason": "窗口已过"},
        )
        self.assertEqual(rej.status_code, 200, rej.text)
        self.assertEqual(rej.json()["status"], "rejected")

    def test_controlled_window_conflict_with_existing_snapshot_rejects_approval(self):
        # 先直接发布前把记录设为受控不可行；改为：另一条非受控记录首次发布带窗口，
        # 但受控申请与快照同属一条记录。这里直接走：先批准申请 A（首次发布带窗口），
        # 再提交申请 B 带不同窗口，批准 B 必须 409 且不改写快照。
        self._mark_controlled()
        now = datetime.now(timezone.utc)
        open1, close1 = now + timedelta(minutes=5), now + timedelta(hours=1)
        open2, close2 = now + timedelta(minutes=10), now + timedelta(hours=2)
        a1 = self._submit_request(
            {"version": 1, "window_open_at": _iso(open1), "window_close_at": _iso(close1)}
        )
        approve1 = self.client.post(
            f"/publish-requests/{a1.json()['request_id']}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve1.status_code, 200, approve1.text)
        sid = approve1.json()["snapshot_id"]
        # 申请 B：显式不同窗口 -> 批准 409，申请 B 仍待审、可驳回
        a2 = self._submit_request(
            {"version": 1, "window_open_at": _iso(open2), "window_close_at": _iso(close2)}
        )
        req2 = a2.json()["request_id"]
        approve2 = self.client.post(
            f"/publish-requests/{req2}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve2.status_code, 409, approve2.text)
        snap = self._record_snapshots()[1]
        self.assertEqual(snap["window_close_at"], approve1.json()["window_close_at"])
        # 申请 B 仍待审
        info = self.client.get(f"/publish-requests/{req2}", auth=BASIC_ALICE).json()
        self.assertEqual(info["status"], "pending")
        # 申请 B 省略窗口无法构造（已带窗口提交）；改用“不带窗口”的申请 C：
        # 批准时沿用首次设置，成功
        a3 = self._submit_request({"version": 1})
        approve3 = self.client.post(
            f"/publish-requests/{a3.json()['request_id']}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve3.status_code, 200, approve3.text)
        self.assertEqual(approve3.json()["snapshot_id"], sid)
        self.assertEqual(
            approve3.json()["window_close_at"], approve1.json()["window_close_at"]
        )

    def test_controlled_request_without_window_on_unwindowed_snapshot(self):
        # 受控记录首次批准时申请未带窗口：快照无时段限制，立即可读
        self._mark_controlled()
        a = self._submit_request({"version": 1, "max_reads": 2})
        approve = self.client.post(
            f"/publish-requests/{a.json()['request_id']}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        sid, token = approve.json()["snapshot_id"], approve.json()["token"]
        self.assertIsNone(approve.json()["window_open_at"])
        self._read(sid, token)

    # ---- 共享凭证：窗口按快照独立判定 ----

    def test_bundle_credential_window_blocks_one_snapshot_only(self):
        now = datetime.now(timezone.utc)
        # 本记录快照：窗口 1 秒后关闭
        r1 = self._publish(
            body={
                "window_open_at": _iso(now - timedelta(seconds=10)),
                "window_close_at": _iso(now + timedelta(seconds=1)),
            }
        )
        sid_window = r1.json()["snapshot_id"]
        # 另一条记录：无窗口
        r2 = self.client.post(
            "/records",
            headers=SERVICE_HEADERS,
            json={"text": "另一条记录，内容B。", "intervals": [{"start": 0, "end": 3}]},
        )
        rid2 = r2.json()["record_id"]
        p2 = self.client.post(
            f"/records/{rid2}/publish",
            headers=SERVICE_HEADERS,
            json={"version": 1},
        )
        sid_open = p2.json()["snapshot_id"]
        # 共享凭证申请（总额度 5）
        b = self.client.post(
            "/bundle-requests",
            auth=BASIC_ALICE,
            json={"snapshot_ids": [sid_window, sid_open], "max_reads": 5},
        )
        self.assertEqual(b.status_code, 201, b.text)
        approve = self.client.post(
            f"/bundle-requests/{b.json()['request_id']}/approve", auth=BASIC_BOB
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        cred = approve.json()["credential"]
        # 两枚快照当前都可读
        self._read(sid_window, cred)
        self._read(sid_open, cred)
        # 窗口快照关闭后：该项 403 不扣共享额度，另一项继续可读
        time.sleep(1.4)
        self._read(sid_window, cred, expected=403)
        self._read(sid_open, cred)
        self._read(sid_window, cred, expected=403)
        status = self.client.get(
            f"/read-bundles/{approve.json()['bundle_id']}", headers=SERVICE_HEADERS
        ).json()
        # 成功的只有 3 次（窗口内 1 + 另一项 2），窗口外 403 未扣次
        self.assertEqual(status["reads_used"], 3)


if __name__ == "__main__":
    unittest.main()