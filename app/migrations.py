"""有版本记录的数据库升级流程。

服务启动时在 PostgreSQL 会话级咨询锁（advisory lock）保护下，把数据库结构
单向、可重入地升级到当前代码支持的版本：

- 空库：从 v1 开始依次执行“初始建表 -> 历史缺列补齐 -> 审计防改写约束”。
- 升级框架出现之前的旧库（无 ``schema_migrations`` 但已有业务表）：
  把既有结构补登为 v1 基线，再依次执行 v2/v3；所有语句均为幂等的
  ``IF NOT EXISTS``，且数据补齐只作用于仍为 NULL 的行，绝不重写已有数据。
- 已是目标版本的库：只核对版本，不执行任何变更语句。
- 库版本高于代码支持版本（未知的较新版本）：抛 ``NewerSchemaVersionError``，
  调用方据此拒绝对外提供业务服务。

多个实例同时启动时，咨询锁保证全集群只有一个实例执行升级；其余实例在锁上
等待，拿到锁后升级已结束，仅做版本核对即放行。每个版本与其版本记录在同一
事务提交（PostgreSQL 的 DDL 可事务化），因此已完成的步骤不会被重复执行，
中途失败则整体回滚，修复后重启可安全重试。
"""

import logging
import time

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .db import Base, engine

# 确保所有模型表已注册到 Base.metadata（不能依赖调用方先导入 app.models）。
from . import models  # noqa: E402,F401

logger = logging.getLogger("app.migrations")

# 版本记录表：每成功执行一个版本就在同事务写入一行，此后不再执行该版本。
MIGRATIONS_TABLE = "schema_migrations"

# 升级咨询锁的固定键（'REDACT' 的十六进制，落在 int8 范围内）。
MIGRATION_LOCK_KEY = 0x524544414354

# 任一这些表存在，且尚无 schema_migrations，即判定为“升级框架出现前的旧库”，
# 需先把既有结构补登为 v1 基线，再增量升级后续版本。
LEGACY_MARKER_TABLES = (
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
)
_BASELINE_NOTE = "初始建表（升级框架出现前旧库的基线补登，未重放任何变更）"


class NewerSchemaVersionError(RuntimeError):
    """数据库版本高于当前代码支持的版本：不得继续服务，需先升级代码。"""


class MigrationFailedError(RuntimeError):
    """某个版本升级执行失败：事务已回滚，修复后可重新运行。"""


# ---- 各版本升级步骤（按版本号顺序执行；每个步骤整体在一个事务内） ----


def _migrate_1_initial_schema(conn) -> None:
    """v1：初始建表，创建当前全部业务表（空库路径）。"""
    Base.metadata.create_all(bind=conn, checkfirst=True)


def _migrate_2_historical_columns(conn) -> None:
    """v2：历史缺列补齐 + 换发链标识回填。

    先 create_all 补齐旧库可能整体缺失的表（如多快照凭证、换发记录、审计表），
    再按列幂等补齐历史版本缺失的字段。补列一律 IF NOT EXISTS，回填只更新
    NULL 行：已完成升级的库再次运行时不触碰任何已有数据。
    """
    # 旧库可能停留在更早发布：缺失的整张表按当前结构补齐。
    Base.metadata.create_all(bind=conn, checkfirst=True)

    statements: list[str] = [
        # 受控记录标记：既有记录默认非受控，直接发布语义不变
        "ALTER TABLE records ADD COLUMN IF NOT EXISTS controlled BOOLEAN NOT NULL DEFAULT false",
        # 快照永久撤回：既有快照默认未撤回，继续公开可读
        "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMP",
        "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS withdraw_reason TEXT",
        # 单快照读取令牌的次数上限 / 已用次数
        "ALTER TABLE read_tokens ADD COLUMN IF NOT EXISTS max_reads INTEGER",
        "ALTER TABLE read_tokens "
        "ADD COLUMN IF NOT EXISTS reads_used INTEGER NOT NULL DEFAULT 0",
        # 泄露换发：既有令牌未经过换发，保持可读
        "ALTER TABLE read_tokens ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP",
        "ALTER TABLE read_tokens ADD COLUMN IF NOT EXISTS chain_id UUID",
        # 多快照共享凭证的换发字段
        "ALTER TABLE read_bundles ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP",
        "ALTER TABLE read_bundles ADD COLUMN IF NOT EXISTS chain_id UUID",
        # 受控发布申请上冻结的令牌策略
        "ALTER TABLE publish_requests "
        "ADD COLUMN IF NOT EXISTS token_expires_in_seconds INTEGER",
        "ALTER TABLE publish_requests ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMP",
        "ALTER TABLE publish_requests ADD COLUMN IF NOT EXISTS token_max_reads INTEGER",
        # 早期版本曾冗余存放令牌；统一以 read_tokens 为准，删除冗余列
        "ALTER TABLE publish_requests DROP COLUMN IF EXISTS published_token",
    ]
    for statement in statements:
        conn.execute(text(statement))

    # 换发链标识回填：只补仍为 NULL 的既有凭证，逐行生成独立链标识；
    # 已有链标识的行（完成过升级或已在同换发链上）绝不重写。
    conn.execute(
        text(
            "UPDATE read_tokens SET chain_id = gen_random_uuid() "
            "WHERE chain_id IS NULL"
        )
    )
    conn.execute(text("ALTER TABLE read_tokens ALTER COLUMN chain_id SET NOT NULL"))
    conn.execute(
        text(
            "UPDATE read_bundles SET chain_id = gen_random_uuid() "
            "WHERE chain_id IS NULL"
        )
    )
    conn.execute(text("ALTER TABLE read_bundles ALTER COLUMN chain_id SET NOT NULL"))


def _migrate_3_audit_append_only(conn) -> None:
    """v3：成功读取审计事件的防改写约束（触发器拒绝 UPDATE/DELETE/TRUNCATE）。

    CREATE OR REPLACE 函数 + DROP TRIGGER IF EXISTS 后重建，保证旧库缺触发器
    或触发器定义过旧时都收敛到当前定义；不触碰任何已有审计事件行。
    """
    conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION read_audit_events_block_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION
                    'read_audit_events is append-only; % is not permitted', TG_OP
                    USING ERRCODE = 'check_violation';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    conn.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_read_audit_events_no_update "
            "ON read_audit_events"
        )
    )
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_read_audit_events_no_update
            BEFORE UPDATE OR DELETE ON read_audit_events
            FOR EACH ROW EXECUTE FUNCTION read_audit_events_block_mutation()
            """
        )
    )
    conn.execute(
        text(
            "DROP TRIGGER IF EXISTS trg_read_audit_events_no_truncate "
            "ON read_audit_events"
        )
    )
    conn.execute(
        text(
            """
            CREATE TRIGGER trg_read_audit_events_no_truncate
            BEFORE TRUNCATE ON read_audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION read_audit_events_block_mutation()
            """
        )
    )


def _migrate_4_record_freezes(conn) -> None:
    """v4：记录临时冻结表与“每条记录至多一个当前冻结期”的部分唯一索引。

    空库路径下 v1 的 create_all 已按当前模型建出完整表与索引，此处幂等；
    旧库路径先按当前模型补齐可能缺失的整张表，再以 CREATE INDEX IF NOT
    EXISTS 收敛索引定义。只新增结构，不改动任何既有数据：既有记录默认未冻结，
    全部快照继续按既有资格公开可读。
    """
    Base.metadata.create_all(bind=conn, checkfirst=True)
    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_record_freezes_active
            ON record_freezes (record_id) WHERE unfrozen_at IS NULL
            """
        )
    )
    conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_record_freezes_record
            ON record_freezes (record_id, frozen_at)
            """
        )
    )


def _migrate_5_sensitive_phrases(conn) -> None:
    """v5：敏感短语清单的单行配置表与初始“停用核验”清单行。

    空库路径下 v1 的 create_all 已按当前模型建出该表（含单行 CHECK），此处
    幂等补建并幂等插入固定单行：初始 phrases 为空数组（停用核验）、version=1，
    既有数据没有任何敏感短语配置，全部既有发布/读取语义保持不变。已完成 v5
    的库再次启动时单行已存在，ON CONFLICT 不重写任何内容。
    """
    Base.metadata.create_all(bind=conn, checkfirst=True)
    conn.execute(
        text(
            """
            INSERT INTO sensitive_phrase_settings (id, phrases, version, created_at)
            VALUES (1, '[]'::jsonb, 1, now())
            ON CONFLICT (id) DO NOTHING
            """
        )
    )


def _migrate_6_snapshot_read_windows(conn) -> None:
    """v6：快照统一外部阅览时段（开放/截止时刻）列与成对校验约束。

    空库路径下 v1 的 create_all 已按当前模型建出全部新列与 CHECK 约束，此处
    幂等；旧库路径幂等补列：既有快照与既有待审申请的新列一律为 NULL/NULL
    （无时段限制），公开读取与发布语义完全不变。CHECK 约束以 DO 块判定存在性，
    保证“空库建表已含约束”与“旧库补列后补约束”两条路径收敛到同一定义。
    """
    Base.metadata.create_all(bind=conn, checkfirst=True)
    statements: list[str] = [
        # 快照阅览时段：NULL/NULL 表示无时段限制（既有快照默认如此）
        "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS opens_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS closes_at TIMESTAMP WITH TIME ZONE",
        # 受控申请在提交时冻结的阅览时段
        "ALTER TABLE publish_requests ADD COLUMN IF NOT EXISTS opens_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE publish_requests ADD COLUMN IF NOT EXISTS closes_at TIMESTAMP WITH TIME ZONE",
    ]
    for statement in statements:
        conn.execute(text(statement))
    # 成对且有序的校验约束：两列同空（无时段限制）或同非空且开放早于截止
    for table, constraint in (
        ("snapshots", "ck_snapshots_read_window"),
        ("publish_requests", "ck_publish_requests_read_window"),
    ):
        conn.execute(
            text(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = '{constraint}'
                          AND conrelid = '{table}'::regclass
                    ) THEN
                        ALTER TABLE {table} ADD CONSTRAINT {constraint}
                        CHECK (
                            (opens_at IS NULL) = (closes_at IS NULL)
                            AND (opens_at IS NULL OR opens_at < closes_at)
                        );
                    END IF;
                END $$
                """
            )
        )


# (版本号, 版本名称, 升级函数)。追加新版本时只允许在末尾追加，永不改写历史版本。
MIGRATIONS: tuple[tuple[int, str, callable], ...] = (
    (1, "initial_schema", _migrate_1_initial_schema),
    (2, "historical_columns_and_rotation_chain", _migrate_2_historical_columns),
    (3, "audit_events_append_only", _migrate_3_audit_append_only),
    (4, "record_freezes", _migrate_4_record_freezes),
    (5, "sensitive_phrases", _migrate_5_sensitive_phrases),
    (6, "snapshot_read_windows", _migrate_6_snapshot_read_windows),
)

TARGET_SCHEMA_VERSION = MIGRATIONS[-1][0]


def _connect_with_retry(
    db_engine: Engine, retries: int, retry_delay: float
):
    """等待数据库就绪后返回一条（已不在事务中的）连接。"""
    last_exc: Exception | None = None
    for _ in range(max(retries, 1)):
        conn = None
        try:
            conn = db_engine.connect()
            conn.execute(text("SELECT 1"))
            conn.commit()
            return conn
        except Exception as exc:  # 连接尚未建立或库不可达
            last_exc = exc
            if conn is not None:
                conn.close()
            time.sleep(retry_delay)
    raise RuntimeError("database is not reachable") from last_exc


def _table_exists(conn, table_name: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar()
    )


def _applied_versions(conn) -> set[int]:
    rows = conn.execute(
        text(f"SELECT version FROM {MIGRATIONS_TABLE}")
    ).all()
    return {row[0] for row in rows}


def run_upgrade(
    db_engine: Engine | None = None,
    *,
    connect_retries: int | None = None,
    connect_retry_delay: float | None = None,
):
    """执行启动升级，返回 ``(previous_version, applied_versions)``。

    - 连接阶段按配置重试；拿到连接后，获取咨询锁、确定基线、逐版本升级、核对。
    - 每个版本独立事务：升级语句与版本记录同生共死；已记录的版本永不重放。
    - 库版本新于代码支持版本时抛 :class:`NewerSchemaVersionError`。
    """
    from .config import settings

    db_engine = db_engine or engine
    retries = settings.db_connect_retries if connect_retries is None else connect_retries
    delay = (
        settings.db_connect_retry_delay_seconds
        if connect_retry_delay is None
        else connect_retry_delay
    )

    conn = _connect_with_retry(db_engine, retries, delay)
    try:
        # 会话级咨询锁：领导者升级期间，其余启动实例阻塞在此；持有者进程退出
        # 时锁随会话自动释放，不会永久卡死后来者。
        conn.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": MIGRATION_LOCK_KEY},
        )
        conn.commit()

        migration_table_preexisting = _table_exists(conn, MIGRATIONS_TABLE)
        is_legacy_database = not migration_table_preexisting and any(
            _table_exists(conn, table_name) for table_name in LEGACY_MARKER_TABLES
        )
        conn.commit()

        # 版本记录表本身的创建不算业务版本：先确保它存在。
        with conn.begin():
            conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
                    )
                    """
                )
            )
            if is_legacy_database:
                # 旧库：把既有结构视作 v1 基线补登（ON CONFLICT 防御极端重入），
                # 随后仅增量执行 v2+，绝不重放建表、不重写业务数据。
                conn.execute(
                    text(
                        f"INSERT INTO {MIGRATIONS_TABLE} (version, name) "
                        "VALUES (1, :note) ON CONFLICT (version) DO NOTHING"
                    ),
                    {"note": _BASELINE_NOTE},
                )
                logger.info(
                    "detected pre-framework legacy database; "
                    "stamped baseline version 1 without replaying it"
                )

        applied = _applied_versions(conn)
        conn.commit()
        previous_version = max(applied, default=0)

        if previous_version > TARGET_SCHEMA_VERSION:
            raise NewerSchemaVersionError(
                f"database schema version {previous_version} is newer than this "
                f"binary supports (version {TARGET_SCHEMA_VERSION}); "
                "refusing to serve"
            )

        applied_versions: list[int] = []
        for version, name, upgrade in MIGRATIONS:
            if version in applied:
                continue
            logger.info("applying database migration v%s (%s)", version, name)
            try:
                # 单个版本一个事务：DDL、数据回填与版本记录原子提交，
                # 任一步失败整体回滚，不会留下“执行了但未登记”的半成品版本。
                with conn.begin():
                    upgrade(conn)
                    conn.execute(
                        text(
                            f"INSERT INTO {MIGRATIONS_TABLE} (version, name) "
                            "VALUES (:version, :name)"
                        ),
                        {"version": version, "name": name},
                    )
            except NewerSchemaVersionError:
                raise
            except Exception as exc:
                raise MigrationFailedError(
                    f"database migration v{version} ({name}) failed and was rolled "
                    f"back; fix the cause and restart: {exc}"
                ) from exc
            applied_versions.append(version)

        # 升级后核对：只允许停在代码精确支持的版本。
        final_applied = _applied_versions(conn)
        conn.commit()
        latest = max(final_applied, default=0)
        if latest > TARGET_SCHEMA_VERSION:
            raise NewerSchemaVersionError(
                f"database schema version {latest} is newer than this binary "
                f"supports (version {TARGET_SCHEMA_VERSION}); refusing to serve"
            )
        if latest != TARGET_SCHEMA_VERSION:
            raise MigrationFailedError(
                f"database ended at schema version {latest}, expected "
                f"{TARGET_SCHEMA_VERSION}; refusing to serve"
            )

        logger.info(
            "database schema at version %s (newly applied: %s)",
            latest,
            applied_versions or "none",
        )
        return previous_version, applied_versions
    finally:
        try:
            conn.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": MIGRATION_LOCK_KEY},
            )
            conn.commit()
        finally:
            conn.close()
