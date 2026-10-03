"""有版本记录的数据库升级流程集成测试（需要真实 PostgreSQL）。

默认用 pgserver 拉起独立的临时实例，每个场景再建独立数据库，互不污染主测试库：
- 空库：从 v1 依次建表、补齐、装触发器，只执行一次、重复启动不改变数据；
- 升级框架出现前的各代旧库：被识别为旧库、补登 v1 基线后增量升级，
  记录/快照/凭证有效性、已用额度、换发链标识与审计事件完整保留；
- 未知的较新版本：拒绝服务（业务路径 503）；
- 多实例同时启动：只有一个执行升级，其余等待并核对版本；
- 升级失败：事务回滚、不登记版本，修复后重试成功。
"""

import json
import os
import threading
import unittest

os.environ.setdefault("SERVICE_KEY", "dev-service-key-change-me")
os.environ.setdefault(
    "REVIEW_CREDENTIALS",
    json.dumps(
        [
            {"username": "alice", "password": "alice-pw", "roles": ["operator"]},
            {"username": "bob", "password": "bob-pw", "roles": ["reviewer"]},
        ]
    ),
)

_PG_SERVER = None
_SKIP_REASON = ""
if "TEST_DATABASE_URL" not in os.environ:
    try:
        import pgserver

        _PG_SERVER = pgserver.get_server("/tmp/pgdata-migration-test")
        _ADMIN_URL = (
            "postgresql+psycopg2://postgres@/postgres?host=/tmp/pgdata-migration-test"
        )
    except Exception as exc:  # pragma: no cover
        _SKIP_REASON = f"pgserver unavailable: {exc}"
else:  # 用指定库的连接信息推导一个管理员入口（测试内部自建临时库）
    _ADMIN_URL = os.environ["TEST_DATABASE_URL"]

SKIP_MIGRATIONS = _PG_SERVER is None and "TEST_DATABASE_URL" not in os.environ

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from app import migrations  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.migrations import (  # noqa: E402
    MIGRATION_LOCK_KEY,
    TARGET_SCHEMA_VERSION,
    MigrationFailedError,
    NewerSchemaVersionError,
)
from app.models import (  # noqa: E402
    ReadAuditEvent,
    ReadBundle,
    ReadToken,
    Record,
)

_admin_engine = None
_db_counter = 0
_db_counter_lock = threading.Lock()


def _new_database() -> str:
    """在临时实例里创建一个全新数据库，返回指向它的连接串。"""
    global _db_counter, _admin_engine
    if _admin_engine is None:
        _admin_engine = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with _db_counter_lock:
        _db_counter += 1
        name = f"migr_{os.getpid()}_{_db_counter}"
    with _admin_engine.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    # 形如 postgresql+psycopg2://postgres@/postgres?host=/tmp/...
    # 只需把路径里的库名 postgres 换成新库名
    return _ADMIN_URL.replace("/postgres?", f"/{name}?", 1)


def _make_legacy_v1_schema(engine_) -> None:
    """用当前模型建出全套表，再逐层拆除各代新增结构，模拟“升级框架出现前”的旧库。

    拆成几代由各用例按需保留；这里提供最旧的形态：无 controlled、无撤回列、
    无次数/换发字段、无受控申请策略列、无审计触发器，并写入代表性存量数据。
    """
    from app.db import Base
    import app.models  # noqa: F401  注册全部模型到 metadata

    with engine_.begin() as conn:
        Base.metadata.create_all(bind=conn)
        conn.execute(text("ALTER TABLE records DROP COLUMN controlled"))
        conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdrawn_at"))
        conn.execute(text("ALTER TABLE snapshots DROP COLUMN withdraw_reason"))
        conn.execute(text("ALTER TABLE read_tokens DROP COLUMN max_reads"))
        conn.execute(text("ALTER TABLE read_tokens DROP COLUMN reads_used"))
        conn.execute(text("ALTER TABLE read_tokens DROP COLUMN rotated_at"))
        conn.execute(text("ALTER TABLE read_tokens DROP COLUMN chain_id"))
        conn.execute(text("ALTER TABLE read_bundles DROP COLUMN rotated_at"))
        conn.execute(text("ALTER TABLE read_bundles DROP COLUMN chain_id"))
        conn.execute(
            text("ALTER TABLE publish_requests DROP COLUMN token_expires_in_seconds")
        )
        conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_expires_at"))
        conn.execute(text("ALTER TABLE publish_requests DROP COLUMN token_max_reads"))
        conn.execute(text("ALTER TABLE snapshots DROP COLUMN opens_at"))
        conn.execute(text("ALTER TABLE snapshots DROP COLUMN closes_at"))
        conn.execute(text("ALTER TABLE publish_requests DROP COLUMN opens_at"))
        conn.execute(text("ALTER TABLE publish_requests DROP COLUMN closes_at"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_read_audit_events_no_update ON read_audit_events"))
        conn.execute(text("DROP TRIGGER IF EXISTS trg_read_audit_events_no_truncate ON read_audit_events"))
        conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))


@unittest.skipIf(SKIP_MIGRATIONS, _SKIP_REASON)
class FreshDatabaseUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.url = _new_database()
        self.engine = create_engine(self.url)

    def tearDown(self):
        self.engine.dispose()

    def _versions(self):
        with self.engine.connect() as conn:
            return {
                row[0]
                for row in conn.execute(
                    text("SELECT version FROM schema_migrations ORDER BY version")
                )
            }

    def test_fresh_database_applies_every_version_in_order(self):
        previous, applied = migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        self.assertEqual(previous, 0)
        self.assertEqual(applied, [1, 2, 3, 4, 5, 6])
        self.assertEqual(self._versions(), {1, 2, 3, 4, 5, 6})
        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("schema_migrations", tables)
        for expected in (
            "records",
            "record_versions",
            "snapshots",
            "read_tokens",
            "publish_requests",
            "read_bundle_requests",
            "read_bundles",
            "read_bundle_items",
            "credential_rotations",
            "read_audit_events",
            "record_freezes",
            "sensitive_phrase_settings",
        ):
            self.assertIn(expected, tables)
        # “每条记录至多一个当前冻结期”的部分唯一索引随 v1 建表即存在
        indexes = {ix["name"] for ix in inspect(self.engine).get_indexes("record_freezes")}
        self.assertIn("uq_record_freezes_active", indexes)
        # v5 幂等插入固定单行：空清单（停用核验）、version=1
        with self.engine.connect() as conn:
            sid, phrases, version = conn.execute(
                text("SELECT id, phrases, version FROM sensitive_phrase_settings")
            ).one()
        self.assertEqual((sid, list(phrases), version), (1, [], 1))

    def test_migrations_module_is_self_contained_for_metadata(self):
        """只导入 app.migrations（不导入 app.models）也能建出全部业务表。

        用独立子进程隔离已被本测试模块导入的 models，防止“metadata 为空、
        v1 只登记不建表”的回归。
        """
        import subprocess
        import sys

        code = (
            "import os; os.environ['SERVICE_KEY']='k';"
            "from sqlalchemy import create_engine, inspect;"
            "from app import migrations;"
            f"eng = create_engine({self.url!r});"
            "prev, applied = migrations.run_upgrade(eng, connect_retries=1, connect_retry_delay=0);"
            "names = set(inspect(eng).get_table_names());"
            "assert applied == [1, 2, 3, 4, 5, 6], applied;"
            "assert 'records' in names and 'read_audit_events' in names, names;"
            "assert 'sensitive_phrase_settings' in names, names;"
            "print('SELF_CONTAINED_OK')"
        )
        env = dict(os.environ)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=repo_root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SELF_CONTAINED_OK", result.stdout)

    def test_running_again_applies_nothing_and_changes_no_data(self):
        migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        with self.engine.connect() as conn:
            before = conn.execute(
                text(
                    "SELECT version, name, applied_at FROM schema_migrations "
                    "ORDER BY version"
                )
            ).all()
        previous, applied = migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        self.assertEqual(previous, TARGET_SCHEMA_VERSION)
        self.assertEqual(applied, [])
        with self.engine.connect() as conn:
            after = conn.execute(
                text(
                    "SELECT version, name, applied_at FROM schema_migrations "
                    "ORDER BY version"
                )
            ).all()
        self.assertEqual(before, after)

    def test_audit_append_only_trigger_installed(self):
        migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO read_audit_events "
                    "(id, snapshot_id, occurred_at, credential_kind, credential_chain_id) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), now(), 'token', "
                    "gen_random_uuid())"
                )
            )
        with self.engine.begin() as conn:
            with self.assertRaises(DBAPIError):
                conn.execute(text("UPDATE read_audit_events SET seq = seq + 1"))
        with self.engine.begin() as conn:
            with self.assertRaises(DBAPIError):
                conn.execute(text("DELETE FROM read_audit_events"))
        with self.engine.begin() as conn:
            with self.assertRaises(DBAPIError):
                conn.execute(text("TRUNCATE read_audit_events"))


@unittest.skipIf(SKIP_MIGRATIONS, _SKIP_REASON)
class LegacyDatabaseUpgradeTests(unittest.TestCase):
    """升级框架出现前的旧库：补登基线 + 增量升级，存量数据语义全部保留。"""

    def setUp(self):
        self.url = _new_database()
        self.engine = create_engine(self.url)

    def tearDown(self):
        self.engine.dispose()

    def _seed_legacy_data(self):
        """在旧结构里写入：受控前记录、已发布快照、有已用次数的令牌与共享凭证、审计事件。"""
        with self.engine.begin() as conn:
            record_id = "11111111-1111-1111-1111-111111111111"
            snapshot_id = "22222222-2222-2222-2222-222222222222"
            token = "legacy-token-abcdef"
            conn.execute(
                text(
                    "INSERT INTO records (id, current_version, created_at, updated_at) "
                    "VALUES (:rid, 1, now(), now())"
                ),
                {"rid": record_id},
            )
            conn.execute(
                text(
                    "INSERT INTO record_versions "
                    "(record_id, version, original_text, intervals, created_at) "
                    "VALUES (:rid, 1, '原文', '[]'::jsonb, now())"
                ),
                {"rid": record_id},
            )
            conn.execute(
                text(
                    "INSERT INTO snapshots (id, record_id, version, redacted_text, published_at) "
                    "VALUES (:sid, :rid, 1, '脱敏文', now())"
                ),
                {"sid": snapshot_id, "rid": record_id},
            )
            # 旧令牌：无次数上限概念（读计数列也是新补的，应得 0）
            conn.execute(
                text(
                    "INSERT INTO read_tokens (token, snapshot_id, created_at) "
                    "VALUES (:token, :sid, now())"
                ),
                {"token": token, "sid": snapshot_id},
            )
            # 共享凭证旧行：直接写 reads_used=2 模拟已用额度（旧表有这两列）
            bundle_id = "33333333-3333-3333-3333-333333333333"
            conn.execute(
                text(
                    "INSERT INTO read_bundles (id, credential, max_reads, reads_used, created_at) "
                    "VALUES (:bid, 'legacy-credential', 5, 2, now())"
                ),
                {"bid": bundle_id},
            )
            conn.execute(
                text(
                    "INSERT INTO read_bundle_items (bundle_id, snapshot_id, position) "
                    "VALUES (:bid, :sid, 0)"
                ),
                {"bid": bundle_id, "sid": snapshot_id},
            )
            # 旧审计事件（无触发器保护）：必须原样保留
            conn.execute(
                text(
                    "INSERT INTO read_audit_events "
                    "(id, snapshot_id, occurred_at, credential_kind, credential_chain_id) "
                    "VALUES (gen_random_uuid(), :sid, now(), 'bundle', gen_random_uuid())"
                ),
                {"sid": snapshot_id},
            )
        return record_id, snapshot_id, token

    def test_oldest_legacy_database_is_baselined_then_upgraded(self):
        _make_legacy_v1_schema(self.engine)
        record_id, snapshot_id, token = self._seed_legacy_data()

        with self.engine.connect() as conn:
            self.assertFalse(
                bool(
                    conn.execute(
                        text("SELECT to_regclass('schema_migrations') IS NOT NULL")
                    ).scalar()
                )
            )

        previous, applied = migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        # 旧库补登 v1 基线：v1 不重放，只增量执行 v2/v3/v4/v5/v6
        self.assertEqual(previous, 1)
        self.assertEqual(applied, [2, 3, 4, 5, 6])

        with self.engine.connect() as conn:
            baseline_name = conn.execute(
                text("SELECT name FROM schema_migrations WHERE version = 1")
            ).scalar_one()
            self.assertIn("基线", baseline_name)
            columns = {c["name"] for c in inspect(self.engine).get_columns("records")}
            self.assertIn("controlled", columns)
            # v6 在旧库幂等补出阅览时段列：既有快照/申请一律 NULL（无时段限制）
            snapshot_columns = {
                c["name"] for c in inspect(self.engine).get_columns("snapshots")
            }
            self.assertIn("opens_at", snapshot_columns)
            self.assertIn("closes_at", snapshot_columns)
            request_columns = {
                c["name"] for c in inspect(self.engine).get_columns("publish_requests")
            }
            self.assertIn("opens_at", request_columns)
            self.assertIn("closes_at", request_columns)
            # v5 在旧库幂等补出固定单行：空清单停用核验，既有语义不变
            sid, phrases, sp_version = conn.execute(
                text("SELECT id, phrases, version FROM sensitive_phrase_settings")
            ).one()
            self.assertEqual((sid, list(phrases), sp_version), (1, [], 1))
            # 既有快照升级后无时段限制（NULL/NULL），公开读取语义不变
            opens, closes = conn.execute(
                text("SELECT opens_at, closes_at FROM snapshots WHERE id = :sid"),
                {"sid": snapshot_id},
            ).one()
            self.assertIsNone(opens)
            self.assertIsNone(closes)

        # 记录与快照保留：旧记录默认非受控，旧快照默认未撤回、仍可读取
        SessionLocal.configure(bind=self.engine)
        try:
            with SessionLocal() as session:
                record = session.get(Record, record_id)
                self.assertEqual(record.current_version, 1)
                self.assertFalse(record.controlled)
                stored_token = session.get(ReadToken, token)
                self.assertIsNone(stored_token.max_reads)
                self.assertEqual(stored_token.reads_used, 0)
                self.assertIsNone(stored_token.rotated_at)
                self.assertIsNotNone(stored_token.chain_id)
                bundle = session.query(ReadBundle).filter_by(
                    credential="legacy-credential"
                ).one()
                # 已用额度原样保留，换发链标识补齐且稳定
                self.assertEqual(bundle.max_reads, 5)
                self.assertEqual(bundle.reads_used, 2)
                self.assertIsNotNone(bundle.chain_id)
                chain_id_before = bundle.chain_id
                audit_count = session.query(ReadAuditEvent).count()
                self.assertEqual(audit_count, 1)
        finally:
            SessionLocal.configure(bind=migrations.engine)

        # 再跑一次：不重放、不重写链标识、审计事件不增不减
        migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        SessionLocal.configure(bind=self.engine)
        try:
            with SessionLocal() as session:
                bundle = session.query(ReadBundle).filter_by(
                    credential="legacy-credential"
                ).one()
                self.assertEqual(bundle.chain_id, chain_id_before)
                self.assertEqual(bundle.reads_used, 2)
                self.assertEqual(session.query(ReadAuditEvent).count(), 1)
        finally:
            SessionLocal.configure(bind=migrations.engine)

    def test_legacy_upgrade_preserves_token_used_quota_and_rotation_chain(self):
        """接近当前结构的旧库：已有 chain_id 与 reads_used 的行绝不被重写。"""
        from app.db import Base
        import app.models  # noqa: F401

        fixed_chain = "44444444-4444-4444-4444-444444444444"
        with self.engine.begin() as conn:
            Base.metadata.create_all(bind=conn)
            # 模拟 v2 已应用、v3 之前的库：结构齐全，但无版本表、无防改写触发器
            conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))
            conn.execute(text("DROP TRIGGER IF EXISTS trg_read_audit_events_no_update ON read_audit_events"))
            conn.execute(text("DROP TRIGGER IF EXISTS trg_read_audit_events_no_truncate ON read_audit_events"))
            conn.execute(
                text(
                    "INSERT INTO records (id, current_version, controlled, created_at, updated_at) "
                    "VALUES ('55555555-5555-5555-5555-555555555555', 1, true, now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO snapshots (id, record_id, version, redacted_text, published_at) "
                    "VALUES ('66666666-6666-6666-6666-666666666666', "
                    "'55555555-5555-5555-5555-555555555555', 1, 'x', now())"
                )
            )
            # 已换发过的旧令牌：已用 7 次、有固定换发链标识
            conn.execute(
                text(
                    "INSERT INTO read_tokens "
                    "(token, snapshot_id, created_at, max_reads, reads_used, rotated_at, chain_id) "
                    "VALUES ('fixed-chain-token', '66666666-6666-6666-6666-666666666666', "
                    "now(), 10, 7, now(), :chain)"
                ),
                {"chain": fixed_chain},
            )

        previous, applied = migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        self.assertEqual((previous, applied), (1, [2, 3, 4, 5, 6]))
        with self.engine.connect() as conn:
            reads_used, chain_id, rotated = conn.execute(
                text(
                    "SELECT reads_used, chain_id, rotated_at IS NOT NULL "
                    "FROM read_tokens WHERE token = 'fixed-chain-token'"
                )
            ).one()
        self.assertEqual(reads_used, 7)
        self.assertEqual(str(chain_id), fixed_chain)
        self.assertTrue(rotated)

    def test_upgraded_legacy_token_remains_functional_end_to_end(self):
        """旧令牌升级后经真实 HTTP 路径读取：成功读取同事务扣次并写审计事件。"""
        from fastapi.testclient import TestClient

        from app import main as app_main

        _make_legacy_v1_schema(self.engine)
        record_id, snapshot_id, token = self._seed_legacy_data()

        # 让应用使用这个旧库执行启动升级
        SessionLocal.configure(bind=self.engine)
        import app.db as dbmod

        original_engine = dbmod.engine
        dbmod.engine = self.engine
        migrations.engine = self.engine
        try:
            app_main._set_upgrade_state(app_main.UPGRADE_STATUS_UPGRADING)
            app_main._init_db_with_retry()
            self.assertEqual(
                app_main.get_upgrade_state()["status"], app_main.UPGRADE_STATUS_READY
            )
            client = TestClient(app_main.app)
            r = client.get(f"/public/snapshots/{snapshot_id}?token={token}")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["redacted_text"], "脱敏文")
            with self.engine.connect() as conn:
                reads_used = conn.execute(
                    text("SELECT reads_used FROM read_tokens WHERE token = :t"),
                    {"t": token},
                ).scalar_one()
                audit_count = conn.execute(
                    text(
                        "SELECT count(*) FROM read_audit_events WHERE snapshot_id = :s"
                    ),
                    {"s": snapshot_id},
                ).scalar_one()
            self.assertEqual(reads_used, 1)
            # 旧库预置 1 条 + 本次成功读取 1 条
            self.assertEqual(audit_count, 2)
        finally:
            dbmod.engine = original_engine
            migrations.engine = original_engine
            SessionLocal.configure(bind=original_engine)


@unittest.skipIf(SKIP_MIGRATIONS, _SKIP_REASON)
class NewerAndFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        self.url = _new_database()
        self.engine = create_engine(self.url)

    def tearDown(self):
        self.engine.dispose()

    def test_unknown_newer_version_is_rejected(self):
        # 先正常升级到目标版本，再伪造一个更高版本记录
        migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"
                ),
                {"v": TARGET_SCHEMA_VERSION + 1, "n": "future_unknown"},
            )
        with self.assertRaises(NewerSchemaVersionError):
            migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)

    def test_failed_step_rolls_back_and_can_be_retried(self):
        """v2 执行期间被失败注入：整个版本回滚（不登记），释放阻塞后重试成功。"""
        from sqlalchemy.exc import OperationalError

        migrations.run_upgrade(self.engine, connect_retries=1, connect_retry_delay=0)
        # 回到“只完成 v1”的状态，让升级重新尝试 v2
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM schema_migrations WHERE version IN (2, 3, 4, 5, 6)"))
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS trg_read_audit_events_no_update "
                    "ON read_audit_events"
                )
            )
            conn.execute(
                text(
                    "DROP TRIGGER IF EXISTS trg_read_audit_events_no_truncate "
                    "ON read_audit_events"
                )
            )

        # 另一会话持有 records 的 ACCESS EXCLUSIVE 锁并保持事务打开：
        # v2 的 ALTER TABLE records 需要该锁；设极短 lock_timeout 使其确定性失败。
        locker = self.engine.connect()
        try:
            locker.execute(text("BEGIN"))
            locker.execute(text("LOCK TABLE records IN ACCESS EXCLUSIVE MODE"))
            locked_engine = create_engine(
                self.url,
                connect_args={"options": "-c lock_timeout=200"},
            )
            with self.assertRaises(MigrationFailedError) as cm:
                migrations.run_upgrade(
                    locked_engine, connect_retries=1, connect_retry_delay=0
                )
            self.assertIsInstance(cm.exception.__cause__, OperationalError)
            locked_engine.dispose()

            # v2/v3 均未登记；触发器也未安装（事务整体回滚）
            with self.engine.connect() as conn:
                versions = {
                    row[0]
                    for row in conn.execute(text("SELECT version FROM schema_migrations"))
                }
                trigger_count = conn.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger WHERE tgrelid = "
                        "'read_audit_events'::regclass AND tgname LIKE "
                        "'trg_read_audit_events_%'"
                    )
                ).scalar_one()
            self.assertEqual(versions, {1})
            self.assertEqual(trigger_count, 0)
        finally:
            # 修复：释放阻塞锁
            locker.execute(text("ROLLBACK"))
            locker.close()

        # 释放阻塞后重试：v2/v3/v4/v5/v6 依次成功
        previous, applied = migrations.run_upgrade(
            self.engine, connect_retries=1, connect_retry_delay=0
        )
        self.assertEqual(previous, 1)
        self.assertEqual(applied, [2, 3, 4, 5, 6])
        with self.engine.connect() as conn:
            versions = {
                row[0]
                for row in conn.execute(text("SELECT version FROM schema_migrations"))
            }
        self.assertEqual(versions, {1, 2, 3, 4, 5, 6})
        # v4 收敛出冻结表与部分唯一索引
        with self.engine.connect() as conn:
            freeze_table = conn.execute(
                text("SELECT to_regclass('record_freezes') IS NOT NULL")
            ).scalar()
            freeze_index = conn.execute(
                text(
                    "SELECT to_regclass('uq_record_freezes_active') IS NOT NULL"
                )
            ).scalar()
        self.assertTrue(freeze_table)
        self.assertTrue(freeze_index)

    def test_http_traffic_blocked_when_schema_not_ready(self):
        from fastapi.testclient import TestClient

        from app import main as app_main

        original = app_main.get_upgrade_state()
        try:
            app_main._set_upgrade_state(app_main.UPGRADE_STATUS_FAILED, error="boom")
            client = TestClient(app_main.app)
            for path in ("/healthz", "/records", "/readyz"):
                r = client.get(path)
                self.assertEqual(r.status_code, 503, path)
                self.assertEqual(r.json()["detail"],
                                 "service unavailable: database schema is not ready")
            # livez 始终可用，供编排系统探活
            r = client.get("/livez")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["upgrade_status"], "failed")
        finally:
            app_main._set_upgrade_state(original["status"], error=original["error"])


@unittest.skipIf(SKIP_MIGRATIONS, _SKIP_REASON)
class ConcurrentStartupTests(unittest.TestCase):
    def setUp(self):
        self.url = _new_database()
        self.engine = create_engine(self.url)

    def tearDown(self):
        self.engine.dispose()

    def test_only_one_instance_runs_migrations(self):
        """多个实例并发启动：恰好一个执行全部步骤，其余等待后核对版本放行。"""
        num_instances = 5
        results: list[tuple[int, list[int]] | Exception] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(num_instances)

        def worker():
            barrier.wait()
            engine_ = create_engine(self.url)
            try:
                outcome = migrations.run_upgrade(engine_, connect_retries=2, connect_retry_delay=0)
                with results_lock:
                    results.append(outcome)
            except Exception as exc:
                with results_lock:
                    results.append(exc)
            finally:
                engine_.dispose()

        threads = [threading.Thread(target=worker) for _ in range(num_instances)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), num_instances)
        self.assertTrue(all(isinstance(r, tuple) for r in results), results)
        # 恰好一个实例真正执行了 [1,2,3,4,5,6]，其余 applied 为空
        applied_lists = [applied for _, applied in results]
        self.assertEqual(sorted(applied_lists, key=len, reverse=True)[0], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(applied == [] for applied in applied_lists[1:]), applied_lists)
        with self.engine.connect() as conn:
            versions = [
                row[0]
                for row in conn.execute(
                    text("SELECT version FROM schema_migrations ORDER BY version")
                )
            ]
        self.assertEqual(versions, [1, 2, 3, 4, 5, 6])

    def test_advisory_lock_key_is_stable(self):
        # 锁键固定：各实例竞争的是同一把集群级锁
        self.assertEqual(MIGRATION_LOCK_KEY, 0x524544414354)
        with self.engine.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": MIGRATION_LOCK_KEY},
            ).scalar()
            self.assertTrue(acquired)
            still = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": MIGRATION_LOCK_KEY},
            ).scalar()
            self.assertTrue(still)  # 同会话可重入
            conn.execute(text("SELECT pg_advisory_unlock_all()"))
            conn.commit()


if __name__ == "__main__":
    unittest.main()
