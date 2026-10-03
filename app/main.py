import base64
import binascii
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from .db import get_db
from .migrations import (
    TARGET_SCHEMA_VERSION,
    MigrationFailedError,
    NewerSchemaVersionError,
    run_upgrade,
)
from .models import (
    AUDIT_KIND_BUNDLE,
    AUDIT_KIND_TOKEN,
    BUNDLE_REQUEST_APPROVED,
    BUNDLE_REQUEST_PENDING,
    BUNDLE_REQUEST_REJECTED,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    CredentialRotation,
    PublishRequest as PublishRequestModel,
    ReadAuditEvent,
    ReadBundle,
    ReadBundleItem,
    ReadBundleRequest,
    ReadToken,
    Record,
    RecordFreeze,
    RecordVersion,
    ROTATION_KIND_BUNDLE,
    ROTATION_KIND_TOKEN,
    SENSITIVE_PHRASE_SETTINGS_ID,
    SensitivePhraseSettings,
    Snapshot,
)
from .redaction import (
    IntervalOutOfBoundsError,
    find_uncovered_phrase_hits,
    redact_text,
    validate_intervals,
)
from .schemas import (
    ApproveResponse,
    BundleApproveResponse,
    BundleItemInfo,
    BundleRequestCreate,
    BundleRequestInfo,
    BundleRequestListResponse,
    BundleRevokeResponse,
    BundleSubmitResponse,
    ControlledFlagResponse,
    ControlledPublishRequest,
    ControlledSubmitResponse,
    CredentialRotateRequest,
    CredentialRotationResponse,
    Interval,
    PublicSnapshotResponse,
    PublishRequest as PublishPayload,
    PublishRequestInfo,
    PublishRequestListResponse,
    PublishResponse,
    ReadAuditEventInfo,
    ReadAuditEventPage,
    ReadBundleListResponse,
    ReadBundleStatusResponse,
    RecordCreateRequest,
    RecordFreezeRequest,
    RecordFreezeResponse,
    RecordFreezeStatus,
    RecordResponse,
    RecordReviseRequest,
    RecordStatusResponse,
    RecordUnfreezeResponse,
    ReviewRejectRequest,
    RevokeResponse,
    RotationBundleItem,
    SensitivePhraseListResponse,
    SensitivePhraseReplaceRequest,
    SnapshotInfo,
    SnapshotWithdrawRequest,
    SnapshotWithdrawResponse,
    TokenStatusResponse,
)
from .security import Operator, require_operator, require_role, require_service_key

logger = logging.getLogger("app.main")

SNAPSHOT_STATUS = "published"
SNAPSHOT_WITHDRAWN_STATUS = "withdrawn"


def _snapshot_status(snapshot: Snapshot) -> str:
    """快照维护状态：撤回为终态，撤回前沿用 published。"""
    return SNAPSHOT_WITHDRAWN_STATUS if snapshot.withdrawn_at is not None else SNAPSHOT_STATUS


# ---- 启动期数据库升级状态（门禁中间件据此放行或拒绝业务请求） ----

# upgrading：启动升级进行中（此时连接被锁阻塞，不应接受业务请求）
# ready：版本核对通过，可对外服务
# failed：升级失败或库版本高于代码支持版本，拒绝一切业务请求（仅 /livez 可探活）
UPGRADE_STATUS_UPGRADING = "upgrading"
UPGRADE_STATUS_READY = "ready"
UPGRADE_STATUS_FAILED = "failed"

_upgrade_lock = threading.Lock()
_upgrade_state: dict[str, object] = {
    "status": UPGRADE_STATUS_UPGRADING,
    "schema_version": None,
    "error": None,
}


def get_upgrade_state() -> dict[str, object]:
    with _upgrade_lock:
        return dict(_upgrade_state)


def _set_upgrade_state(status: str, *, error: str | None = None) -> None:
    with _upgrade_lock:
        _upgrade_state["status"] = status
        _upgrade_state["error"] = error
        if status == UPGRADE_STATUS_READY:
            _upgrade_state["schema_version"] = TARGET_SCHEMA_VERSION


def _init_db_with_retry() -> None:
    """启动时执行有版本记录的数据库升级（等待库就绪 + 咨询锁内升级 + 核对）。

    兼容测试与运维脚本中对该入口的直接调用：升级成功后把进程内状态置为 ready；
    失败则置为 failed，由门禁中间件保证不对外提供业务服务。
    """
    try:
        previous, applied = run_upgrade()
        _set_upgrade_state(UPGRADE_STATUS_READY)
        logger.info(
            "startup database upgrade complete: %s -> v%s (applied: %s)",
            previous,
            TARGET_SCHEMA_VERSION,
            applied or "none",
        )
    except NewerSchemaVersionError as exc:
        # 未知的较新版本：绝不降级、绝不服务
        logger.error("refusing to start serving: %s", exc)
        _set_upgrade_state(UPGRADE_STATUS_FAILED, error=str(exc))
    except MigrationFailedError as exc:
        # 升级失败：失败版本已整体回滚；修复并重启后可重试
        logger.error("database upgrade failed: %s", exc)
        _set_upgrade_state(UPGRADE_STATUS_FAILED, error=str(exc))
    except RuntimeError as exc:
        # 数据库持续不可达
        logger.error("database upgrade aborted: %s", exc)
        _set_upgrade_state(UPGRADE_STATUS_FAILED, error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="Redacted Records API",
    version="1.8.0",
    summary="机构公开脱敏记录服务（受控发布 + 读取次数上限 + 快照撤回 + 多快照共享凭证 + 泄露换发 + 成功读取审计 + 记录临时冻结 + 敏感短语清单核验 + 版本化数据库升级）",
    lifespan=lifespan,
)

_LIVEZ_PATH = "/livez"


@app.middleware("http")
async def block_traffic_unless_schema_ready(request: Request, call_next):
    """数据库升级未完成、失败或版本不匹配时，拒绝一切业务请求。

    - /livez 始终可用（进程存活探针，不代表可服务）；
    - 其余路径在非 ready 状态统一返回 503，不执行任何业务逻辑，
      鉴权与错误细节一律不暴露；编排系统据此摘流/重启。
    """
    if request.url.path == _LIVEZ_PATH:
        return await call_next(request)
    state = get_upgrade_state()
    if state["status"] != UPGRADE_STATUS_READY:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "service unavailable: database schema is not ready"},
        )
    return await call_next(request)


def _validated_intervals(text: str, intervals: list[Interval]) -> list[tuple[int, int]]:
    """越界区间直接 422，且发生在任何写库之前，保证整次拒绝。"""
    try:
        return validate_intervals(len(text), [(iv.start, iv.end) for iv in intervals])
    except IntervalOutOfBoundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


# ---- 敏感短语清单核验（直接发布与受控申请提交/批准均须通过） ----


def _lock_sensitive_phrase_settings(db: Session) -> SensitivePhraseSettings:
    """锁定敏感短语清单单行并返回当前已提交版本。

    发布/申请提交/批准事务在整个核验与提交期间持有该行的 FOR UPDATE 行锁；
    与清单的原子替换串行化，保证核验与提交对应同一已提交清单版本：
    - 替换先提交：等待行锁的核验在获锁后看到新清单，按新版本核验；
    - 核验先提交：替换在其后生效，本次发布仍按旧清单完成，不受影响。
    """
    settings_row = db.execute(
        select(SensitivePhraseSettings)
        .where(SensitivePhraseSettings.id == SENSITIVE_PHRASE_SETTINGS_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if settings_row is None:
        # 理论上 v5 迁移已幂等插入固定单行；缺失说明结构异常，不猜测、不放行
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="sensitive phrase settings are not initialized",
        )
    return settings_row


def _verify_against_settings(
    settings_row: SensitivePhraseSettings, text: str, intervals: list[tuple[int, int]]
) -> None:
    """在已锁定的清单版本上核验：空清单停用核验；命中未完整覆盖则 422。

    失败时只抛错，由调用方回滚。错误体只含未覆盖命中的码点区间，绝不回显
    任何短语或原文。
    """
    phrases = list(settings_row.phrases or [])
    if not phrases:
        return
    uncovered = find_uncovered_phrase_hits(text, phrases, intervals)
    if uncovered:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "sensitive phrase hits are not fully covered by redaction intervals",
                "hits": [{"start": s, "end": e} for s, e in uncovered],
            },
        )


def _verify_phrases_cover_text(
    db: Session, text: str, intervals: list[tuple[int, int]]
) -> SensitivePhraseSettings:
    """按当前启用清单核验原文：每个区分大小写的字面量命中都须被遮蔽区间完整覆盖。

    先锁定清单单行（固定锁序的第一把锁）再在锁内核验，行锁持有至本次事务
    提交/回滚，保证核验与提交对应同一已提交清单版本。核验失败统一 422 并回滚：
    绝不回显任何短语或原文，且不产生快照、令牌或改变申请状态。返回锁定的
    清单行，供需要在同一把锁后继续加其他行锁的调用方复用。
    """
    settings_row = _lock_sensitive_phrase_settings(db)
    try:
        _verify_against_settings(settings_row, text, intervals)
    except HTTPException:
        db.rollback()
        raise
    return settings_row


def _future_expires_at(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> datetime | None:
    """解析令牌到期时间；显式到期时刻必须晚于当前时间，否则 422。"""
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )
        return expires_at
    if expires_in_seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    return None


def _request_info(request: PublishRequestModel) -> PublishRequestInfo:
    """申请详情映射：只输出元数据，冻结原文绝不离开服务端。"""
    return PublishRequestInfo(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        requested_by=request.requested_by,
        reviewed_by=request.reviewed_by,
        reason=request.reason,
        token_expires_in_seconds=request.token_expires_in_seconds,
        token_expires_at=request.token_expires_at,
        token_max_reads=request.token_max_reads,
        snapshot_id=request.snapshot_id,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def _validate_expiry_policy(
    expires_in_seconds: int | None, expires_at: datetime | None
) -> None:
    """校验提交时选择的令牌到期策略：绝对时刻必须晚于提交时刻。

    相对时长（expires_in_seconds）在批准签发时才换算，复核耗时不占用令牌寿命。
    """
    if expires_at is None:
        return
    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(
        tzinfo=timezone.utc
    )
    if normalized <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expires_at must be in the future",
        )


def _bundle_request_info(request: ReadBundleRequest) -> BundleRequestInfo:
    """共享凭证申请详情映射：只输出元数据，快照原文绝不离开服务端。"""
    return BundleRequestInfo(
        request_id=request.id,
        status=request.status,
        snapshot_ids=[UUID(sid) for sid in request.frozen_snapshot_ids],
        requested_by=request.requested_by,
        reviewed_by=request.reviewed_by,
        reason=request.reason,
        credential_expires_in_seconds=request.credential_expires_in_seconds,
        credential_expires_at=request.credential_expires_at,
        max_reads=request.max_reads,
        bundle_id=request.bundle_id,
        created_at=request.created_at,
        decided_at=request.decided_at,
    )


def _bundle_active(bundle: ReadBundle, now: datetime) -> bool:
    """凭证是否仍可成功读取：未撤销、未换发、未到期且共享额度未耗尽。"""
    if bundle.revoked_at is not None or bundle.rotated_at is not None:
        return False
    if bundle.expires_at is not None and bundle.expires_at <= now:
        return False
    return bundle.reads_used < bundle.max_reads


def _bundle_status_response(
    bundle: ReadBundle, snapshots: dict[UUID, Snapshot]
) -> ReadBundleStatusResponse:
    """维护侧凭证状态映射：清单按提交顺序排列，绝不返回快照原文。"""
    now = datetime.now(timezone.utc)
    items = [
        BundleItemInfo(
            snapshot_id=item.snapshot_id,
            record_id=snapshots[item.snapshot_id].record_id,
            version=snapshots[item.snapshot_id].version,
            position=item.position,
            status=_snapshot_status(snapshots[item.snapshot_id]),
            withdrawn_at=snapshots[item.snapshot_id].withdrawn_at,
        )
        for item in sorted(bundle.items, key=lambda item: item.position)
    ]
    return ReadBundleStatusResponse(
        bundle_id=bundle.id,
        credential=bundle.credential,
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,
        remaining_reads=max(bundle.max_reads - bundle.reads_used, 0),
        active=_bundle_active(bundle, now),
        created_at=bundle.created_at,
        expires_at=bundle.expires_at,
        revoked_at=bundle.revoked_at,
        rotated_at=bundle.rotated_at,
        items=items,
    )


@app.get("/livez")
def livez():
    """存活探针：进程在运行即 200，不代表数据库就绪；升级失败时仍可用。"""
    state = get_upgrade_state()
    body = {"status": "alive", "upgrade_status": state["status"]}
    if state["status"] == UPGRADE_STATUS_FAILED:
        body["error"] = "database schema upgrade failed or version mismatch"
    return body


@app.get("/readyz")
def readyz():
    """就绪探针：仅当数据库升级完成且版本核对通过时可达（中间件门禁）。"""
    state = get_upgrade_state()
    return {
        "status": "ready",
        "schema_version": state["schema_version"],
        "supported_schema_version": TARGET_SCHEMA_VERSION,
    }


@app.get("/healthz")
def healthz():
    """兼容原有健康检查：升级未就绪时由中间件统一返回 503。"""
    return {"status": "ok"}


@app.post(
    "/records",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def create_record(payload: RecordCreateRequest, db: Session = Depends(get_db)):
    """创建记录（版本 1）：提交原文与敏感区间。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    record = Record(current_version=1)
    db.add(record)
    db.flush()
    db.add(
        RecordVersion(
            record_id=record.id,
            version=1,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record.id, version=1)


@app.put(
    "/records/{record_id}",
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def revise_record(record_id: UUID, payload: RecordReviseRequest, db: Session = Depends(get_db)):
    """修订记录：必须携带当前版本号作为基准，过期则 409 并返回当前版本。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    # 条件更新是原子的：并发修订时只有一个能基于同一基准版本成功，
    # 其余因 current_version 已变化而失败，不会产生分叉版本。
    new_version = db.execute(
        update(Record)
        .where(Record.id == record_id, Record.current_version == payload.base_version)
        .values(current_version=payload.base_version + 1)
        .returning(Record.current_version)
    ).scalar_one_or_none()
    if new_version is None:
        db.rollback()
        record = db.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "base_version is stale",
                "current_version": record.current_version,
            },
        )
    db.add(
        RecordVersion(
            record_id=record_id,
            version=new_version,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record_id, version=new_version)


@app.put(
    "/records/{record_id}/controlled",
    response_model=ControlledFlagResponse,
    dependencies=[Depends(require_service_key)],
)
def mark_record_controlled(record_id: UUID, db: Session = Depends(get_db)):
    """将记录设为受控发布（幂等：已是受控状态时直接返回当前状态）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        record.controlled = True
        db.commit()
    return ControlledFlagResponse(record_id=record_id, controlled=True)


@app.post(
    "/records/{record_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishResponse,
    dependencies=[Depends(require_service_key)],
)
def publish_record(record_id: UUID, payload: PublishPayload, db: Session = Depends(get_db)):
    """直接发布指定版本：合并区间生成不可变快照，并签发随机读取令牌。

    受控记录的直接发布请求一律 409 拒绝，必须走申请-复核流程。
    """
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    if record.controlled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is controlled; submit a publish request for review instead",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    expires_at = _future_expires_at(payload.expires_in_seconds, payload.expires_at)
    version_intervals = [(iv["start"], iv["end"]) for iv in version.intervals]
    # 敏感短语核验：先锁清单单行并在锁内核验，行锁持有至本次发布提交，
    # 保证核验与提交对应同一已提交清单版本（与清单替换并发时按事务先后串行）。
    # 不通过则整次拒绝：不产生快照或令牌。
    _verify_phrases_cover_text(db, version.original_text, version_intervals)
    redacted = redact_text(version.original_text, version_intervals)
    # 快照按 (record_id, version) 唯一：并发发布时唯一约束保证只落库一份，
    # 且快照与令牌在同一事务提交，不会出现部分发布。
    db.execute(
        pg_insert(Snapshot)
        .values(record_id=record_id, version=payload.version, redacted_text=redacted)
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    # 锁定快照行后再判定撤回状态：与撤回操作串行化，
    # 已被永久撤回的版本不能通过再次直接发布重新公开。
    snapshot = db.execute(
        select(Snapshot)
        .where(Snapshot.record_id == record_id, Snapshot.version == payload.version)
        .with_for_update()
    ).scalar_one()
    if snapshot.withdrawn_at is not None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot has been withdrawn and cannot be published again",
        )
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=expires_at,
        max_reads=payload.max_reads,
    )
    db.add(token)
    db.commit()
    return {
        "snapshot_id": snapshot.id,
        "record_id": record_id,
        "version": payload.version,
        "status": SNAPSHOT_STATUS,
        "token": token.token,
        "expires_at": expires_at,
        "max_reads": payload.max_reads,
    }


@app.post(
    "/records/{record_id}/publish-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=ControlledSubmitResponse,
)
def submit_publish_request(
    record_id: UUID,
    payload: ControlledPublishRequest,
    operator: Operator = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """提交受控发布申请：用申请人的个人凭据，冻结提交时刻的历史版本内容。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if not record.controlled:
        # 非受控记录应使用直接发布，明确拒绝以免误用流程
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is not controlled; use the direct publish endpoint",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    existing_snapshot = db.execute(
        select(Snapshot).where(
            Snapshot.record_id == record_id, Snapshot.version == payload.version
        )
    ).scalar_one_or_none()
    if existing_snapshot is not None and existing_snapshot.withdrawn_at is not None:
        # 已永久撤回的版本不能再提交受控申请（冻结内容也不会重新公开）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot of this version has been withdrawn",
        )
    _validate_expiry_policy(payload.expires_in_seconds, payload.expires_at)
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    frozen_intervals = [dict(iv) for iv in version.intervals]
    # 提交即须按当前启用清单核验拟发布版本原文：不通过整次拒绝，不产生待审申请。
    _verify_phrases_cover_text(
        db, version.original_text, [(iv["start"], iv["end"]) for iv in frozen_intervals]
    )
    # 待审内容在此冻结：后续修订只产生新版本，不改变本申请绑定的原文与区间
    request = PublishRequestModel(
        record_id=record_id,
        version=payload.version,
        frozen_text=version.original_text,
        frozen_intervals=frozen_intervals,
        status=REQUEST_PENDING,
        requested_by=operator.username,
        token_expires_in_seconds=payload.expires_in_seconds,
        token_expires_at=expires_at,
        token_max_reads=payload.max_reads,
    )
    db.add(request)
    db.commit()
    return ControlledSubmitResponse(
        request_id=request.id,
        record_id=record_id,
        version=payload.version,
        status=request.status,
        requested_by=request.requested_by,
        created_at=request.created_at,
    )


@app.post(
    "/publish-requests/{request_id}/approve",
    response_model=ApproveResponse,
)
def approve_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """批准申请：沿用既有脱敏与令牌规则，同事务生成快照与新读取令牌。

    申请人不得复核；已终态（批准/驳回）的申请一律 409；
    行锁串行化并发复核，保证同一申请只有一个终态。
    """
    # SELECT ... FOR UPDATE：并发复核在此排队，后进入者会看到已终态并被拒绝
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )

    # 快照与令牌在同一事务内生成：任一步失败整体回滚，不会留下可读令牌。
    # 相对到期时长在签发时刻换算为绝对时刻；显式到期时刻沿用申请时的选择。
    if request.token_expires_in_seconds is not None:
        token_expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=request.token_expires_in_seconds
        )
    else:
        token_expires_at = request.token_expires_at
    frozen_intervals = [(iv["start"], iv["end"]) for iv in request.frozen_intervals]
    # 批准时用“当时启用”的清单重新核验申请冻结的原文与区间（清单可能已在等待
    # 期间变更）。先锁清单单行再锁快照行，锁序与直接发布一致、不会死锁；不通过
    # 则整次拒绝：不产生快照或令牌，申请保持待审且仍可驳回。
    _verify_phrases_cover_text(db, request.frozen_text, frozen_intervals)
    redacted = redact_text(request.frozen_text, frozen_intervals)
    db.execute(
        pg_insert(Snapshot)
        .values(
            record_id=request.record_id,
            version=request.version,
            redacted_text=redacted,
        )
        .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
    )
    snapshot = db.execute(
        select(Snapshot)
        .where(
            Snapshot.record_id == request.record_id,
            Snapshot.version == request.version,
        )
        .with_for_update()
    ).scalar_one()
    if snapshot.withdrawn_at is not None:
        # 申请等待期间该版本快照被永久撤回：不能批准重新公开（仍可驳回本申请）
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="snapshot of this version has been withdrawn",
        )
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot.id,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )
    db.add(token)
    db.flush()  # 确保令牌已在事务内落库后再终结申请
    request.status = REQUEST_APPROVED
    request.reviewed_by = operator.username
    request.snapshot_id = snapshot.id
    request.token_expires_at = token_expires_at
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return ApproveResponse(
        request_id=request.id,
        record_id=request.record_id,
        version=request.version,
        status=request.status,
        reviewed_by=operator.username,
        snapshot_id=snapshot.id,
        token=token.token,
        expires_at=token_expires_at,
        max_reads=request.token_max_reads,
    )


@app.post(
    "/publish-requests/{request_id}/reject",
    response_model=PublishRequestInfo,
)
def reject_publish_request(
    request_id: UUID,
    payload: ReviewRejectRequest = Body(default_factory=ReviewRejectRequest),
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """驳回申请：不生成快照与令牌；驳回是终态，此后不能再批准。"""
    request = db.execute(
        select(PublishRequestModel)
        .where(PublishRequestModel.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    if request.status != REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "publish request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own publish request",
        )
    request.status = REQUEST_REJECTED
    request.reviewed_by = operator.username
    request.reason = payload.reason
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _request_info(request)


@app.get(
    "/publish-requests/{request_id}",
    response_model=PublishRequestInfo,
)
def get_publish_request(
    request_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """查看单个申请状态（任何已认证操作者可查；不返回冻结原文）。"""
    request = db.get(PublishRequestModel, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    return _request_info(request)


@app.get(
    "/records/{record_id}/publish-requests",
    response_model=PublishRequestListResponse,
)
def list_publish_requests(
    record_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """列出某条记录的全部受控发布申请（不返回冻结原文）。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    requests = (
        db.execute(
            select(PublishRequestModel)
            .where(PublishRequestModel.record_id == record_id)
            .order_by(PublishRequestModel.created_at)
        )
        .scalars()
        .all()
    )
    return PublishRequestListResponse(
        record_id=record_id, requests=[_request_info(request) for request in requests]
    )


# ---- 多快照共享凭证（read bundle）：申请-复核 + 共用读取额度 ----


@app.post(
    "/bundle-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=BundleSubmitResponse,
)
def submit_bundle_request(
    payload: BundleRequestCreate,
    operator: Operator = Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """提交多快照共享凭证申请：冻结清单、可选到期策略与正整数共享总额度。

    清单在提交时定形：至少两个互不重复的快照 ID，且全部为已存在且未撤回
    的快照；未知快照 404、已撤回快照 409。批准时会再次重新核对全部快照。
    """
    snapshot_ids = list(dict.fromkeys(payload.snapshot_ids))  # 去重保序（重复已在 schema 拒绝）
    snapshots = (
        db.execute(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    missing = [sid for sid in snapshot_ids if sid not in by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    withdrawn = [sid for sid in snapshot_ids if by_id[sid].withdrawn_at is not None]
    if withdrawn:
        # 已撤回快照不能进入共享凭证；批准时还会重新核对
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="one or more snapshots have been withdrawn",
        )
    _validate_expiry_policy(payload.expires_in_seconds, payload.expires_at)
    expires_at = payload.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    # 清单在此冻结：JSONB 以字符串数组存放 UUID，之后快照撤回等变化不改变本申请清单
    request = ReadBundleRequest(
        frozen_snapshot_ids=[str(sid) for sid in snapshot_ids],
        status=BUNDLE_REQUEST_PENDING,
        requested_by=operator.username,
        credential_expires_in_seconds=payload.expires_in_seconds,
        credential_expires_at=expires_at,
        max_reads=payload.max_reads,
    )
    db.add(request)
    db.commit()
    return BundleSubmitResponse(
        request_id=request.id,
        status=request.status,
        snapshot_ids=snapshot_ids,
        requested_by=request.requested_by,
        max_reads=request.max_reads,
        created_at=request.created_at,
    )


@app.post(
    "/bundle-requests/{request_id}/approve",
    response_model=BundleApproveResponse,
)
def approve_bundle_request(
    request_id: UUID,
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """批准共享凭证申请：重新核对冻结清单全部快照，全部有效才一次性签发凭证。

    - 申请人不得复核自己的申请 -> 403；
    - 已终态的申请再次操作 -> 409；并发复核由申请行行锁串行化；
    - 任一快照已不存在或已撤回 -> 整次批准失败（409），不签发凭证，申请仍可驳回；
    - 凭证、清单项与申请终态在同一事务提交，任一异常整体回滚。
    """
    request = db.execute(
        select(ReadBundleRequest)
        .where(ReadBundleRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    if request.status != BUNDLE_REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "bundle request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own bundle request",
        )

    snapshot_ids = [UUID(sid) for sid in request.frozen_snapshot_ids]
    # 批准时重新核对：对全部快照行加锁，任一项失效则整次批准失败、不签发凭证。
    # FOR UPDATE 与撤回操作串行化；撤回提交后才进入核对的批准必定看到失效项。
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.id.in_(snapshot_ids)).with_for_update()
        )
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    if len(by_id) != len(snapshot_ids) or any(
        sid not in by_id or by_id[sid].withdrawn_at is not None for sid in snapshot_ids
    ):
        # 申请保持 pending：不签发凭证，本次批准失败，但申请仍可被驳回
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="one or more snapshots in the frozen list are no longer available",
        )

    if request.credential_expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=request.credential_expires_in_seconds
        )
    else:
        expires_at = request.credential_expires_at
    # 一次性签发随机凭证并绑定冻结清单：凭证、清单项与申请终态同事务提交
    bundle = ReadBundle(
        credential=secrets.token_urlsafe(32),
        max_reads=request.max_reads,
        expires_at=expires_at,
    )
    db.add(bundle)
    db.flush()  # 取得 bundle.id 后写入清单项
    for position, snapshot_id in enumerate(snapshot_ids):
        db.add(
            ReadBundleItem(
                bundle_id=bundle.id,
                snapshot_id=snapshot_id,
                position=position,
            )
        )
    request.status = BUNDLE_REQUEST_APPROVED
    request.reviewed_by = operator.username
    request.bundle_id = bundle.id
    request.credential_expires_at = expires_at
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return BundleApproveResponse(
        request_id=request.id,
        status=request.status,
        reviewed_by=operator.username,
        bundle_id=bundle.id,
        credential=bundle.credential,
        snapshot_ids=snapshot_ids,
        expires_at=expires_at,
        max_reads=request.max_reads,
    )


@app.post(
    "/bundle-requests/{request_id}/reject",
    response_model=BundleRequestInfo,
)
def reject_bundle_request(
    request_id: UUID,
    payload: ReviewRejectRequest = Body(default_factory=ReviewRejectRequest),
    operator: Operator = Depends(require_role("reviewer")),
    db: Session = Depends(get_db),
):
    """驳回共享凭证申请：不签发凭证；驳回是终态，此后不能再批准。"""
    request = db.execute(
        select(ReadBundleRequest)
        .where(ReadBundleRequest.id == request_id)
        .with_for_update()
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    if request.status != BUNDLE_REQUEST_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "bundle request already decided",
                "status": request.status,
            },
        )
    if request.requested_by == operator.username:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the requester may not review their own bundle request",
        )
    request.status = BUNDLE_REQUEST_REJECTED
    request.reviewed_by = operator.username
    request.reason = payload.reason
    request.decided_at = datetime.now(timezone.utc)
    db.commit()
    return _bundle_request_info(request)


@app.get(
    "/bundle-requests/{request_id}",
    response_model=BundleRequestInfo,
)
def get_bundle_request(
    request_id: UUID,
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """查看单个共享凭证申请（任何已认证操作者可查；不返回快照原文）。"""
    request = db.get(ReadBundleRequest, request_id)
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="bundle request not found"
        )
    return _bundle_request_info(request)


@app.get(
    "/bundle-requests",
    response_model=BundleRequestListResponse,
)
def list_bundle_requests(
    operator: Operator = Depends(require_operator),
    db: Session = Depends(get_db),
):
    """列出全部共享凭证申请（任何已认证操作者可查；只含元数据）。"""
    requests = (
        db.execute(
            select(ReadBundleRequest).order_by(ReadBundleRequest.created_at)
        )
        .scalars()
        .all()
    )
    return BundleRequestListResponse(
        requests=[_bundle_request_info(request) for request in requests]
    )


@app.get(
    "/read-bundles",
    response_model=ReadBundleListResponse,
    dependencies=[Depends(require_service_key)],
)
def list_read_bundles(db: Session = Depends(get_db)):
    """维护侧查询全部共享凭证的清单、额度与状态（绝不返回快照原文）。"""
    bundles = (
        db.execute(
            select(ReadBundle)
            .options(selectinload(ReadBundle.items))
            .order_by(ReadBundle.created_at)
        )
        .scalars()
        .all()
    )
    snapshots = _load_bundle_snapshots(db, bundles)
    return ReadBundleListResponse(
        bundles=[_bundle_status_response(bundle, snapshots) for bundle in bundles]
    )


@app.get(
    "/read-bundles/{bundle_id}",
    response_model=ReadBundleStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_read_bundle(bundle_id: UUID, db: Session = Depends(get_db)):
    """维护侧查询单个共享凭证的清单、额度与状态（绝不返回快照原文）。"""
    bundle = db.get(ReadBundle, bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="read bundle not found"
        )
    snapshots = _load_bundle_snapshots(db, [bundle])
    return _bundle_status_response(bundle, snapshots)


@app.post(
    "/read-bundles/{bundle_id}/revoke",
    response_model=BundleRevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_read_bundle(bundle_id: UUID, db: Session = Depends(get_db)):
    """撤销共享凭证：原子条件 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadBundle)
        .where(ReadBundle.id == bundle_id, ReadBundle.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return BundleRevokeResponse(
            bundle_id=bundle_id, revoked=True, already_revoked=False
        )
    if db.get(ReadBundle, bundle_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="read bundle not found"
        )
    return BundleRevokeResponse(
        bundle_id=bundle_id, revoked=True, already_revoked=True
    )


def _load_bundle_snapshots(
    db: Session, bundles: list[ReadBundle]
) -> dict[UUID, Snapshot]:
    """批量取清单项引用的快照（维护侧需要其 record/version 与撤回状态）。"""
    snapshot_ids = {
        item.snapshot_id
        for bundle in bundles
        for item in bundle.items
    }
    if not snapshot_ids:
        return {}
    snapshots = (
        db.execute(select(Snapshot).where(Snapshot.id.in_(snapshot_ids)))
        .scalars()
        .all()
    )
    return {snapshot.id: snapshot for snapshot in snapshots}


# ---- 读取凭证泄露换发（单快照令牌 / 多快照共享凭证统一入口） ----


def _rotation_conflict(reason: str) -> None:
    """换发资格不满足或并发落选：统一 409，不把可换发凭证换成新凭证。"""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"message": "credential is not eligible for rotation", "reason": reason},
    )


def _rotation_payload(
    *,
    kind: str,
    request_id: str,
    old_credential: str,
    new_credential: str,
    snapshot_ids: list[UUID],
    expires_at: datetime | None,
    max_reads: int | None,
    reads_used: int,
    rotated_at: datetime,
    items: list[RotationBundleItem] | None = None,
) -> CredentialRotationResponse:
    """组装换发响应：剩余额度按换发时已用次数结算（不限次时为 null）。"""
    remaining = None if max_reads is None else max(max_reads - reads_used, 0)
    return CredentialRotationResponse(
        kind=kind,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_credential,
        snapshot_ids=snapshot_ids,
        expires_at=expires_at,
        max_reads=max_reads,
        reads_used=reads_used,
        remaining_reads=remaining,
        rotated_at=rotated_at,
        items=items,
    )


def _replay_rotation(record: CredentialRotation) -> CredentialRotationResponse:
    """相同（原凭证, 请求标识）重试：原样回放首次换发结果，不再次换发。"""
    response = CredentialRotationResponse.model_validate(record.response_payload)
    response.replayed = True
    return response


def _rotate_token(
    db: Session, snapshot_id: UUID, request_id: str, old_credential: str
) -> CredentialRotationResponse:
    """单快照令牌换发：新令牌继承快照范围、原绝对到期时刻与已用次数。"""
    # 锁序与公开读取一致：先快照行、再凭证行，避免与并发读取形成死锁；
    # 换发等待快照锁期间，先提交的读取按既有规则计入旧令牌并被新令牌继承。
    snapshot = db.execute(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()
    ).scalar_one_or_none()
    read_token = db.execute(
        select(ReadToken)
        .where(ReadToken.token == old_credential)
        .with_for_update()
        # 锁可能在队列中等待并发读取/换发提交：必须刷新为当前已提交版本，
        # 否则会读到定位阶段缓存的旧 rotated_at/reads_used（identity map）。
        .execution_options(populate_existing=True)
    ).scalar_one()

    prior = db.execute(
        select(CredentialRotation).where(
            CredentialRotation.old_credential == old_credential,
            CredentialRotation.request_id == request_id,
        )
    ).scalar_one_or_none()
    if prior is not None:
        db.rollback()
        return _replay_rotation(prior)

    # 仅未撤销、未换发、未到期且额度未耗尽的凭证可换发。
    # 快照是否已撤回不作为换发条件（撤回快照的令牌仍可换发，只是继续读不到正文）。
    if read_token.revoked_at is not None:
        _rotation_conflict("revoked")
    if read_token.rotated_at is not None:
        # 不同请求标识对同一原凭证的换发：仅一次成功，落选方在此冲突
        _rotation_conflict("already_rotated")
    now = datetime.now(timezone.utc)
    if read_token.expires_at is not None and read_token.expires_at <= now:
        _rotation_conflict("expired")
    if (
        read_token.max_reads is not None
        and read_token.reads_used >= read_token.max_reads
    ):
        _rotation_conflict("quota_exhausted")

    rotated_at = now
    new_token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=read_token.snapshot_id,
        expires_at=read_token.expires_at,  # 继承原绝对到期时刻，不延长
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,  # 继承已用次数，不重置
        chain_id=read_token.chain_id,  # 继承稳定换发链标识：多代读取归于同一链
    )
    db.add(new_token)
    db.flush()
    read_token.rotated_at = rotated_at  # 旧令牌换发提交后不能再读取
    payload = _rotation_payload(
        kind=ROTATION_KIND_TOKEN,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_token.token,
        snapshot_ids=[read_token.snapshot_id],
        expires_at=read_token.expires_at,
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,
        rotated_at=rotated_at,
    )
    db.add(
        CredentialRotation(
            kind=ROTATION_KIND_TOKEN,
            old_credential=old_credential,
            new_credential=new_token.token,
            request_id=request_id,
            response_payload=payload.model_dump(mode="json"),
        )
    )
    db.commit()
    return payload


def _rotate_bundle(
    db: Session, bundle_id: UUID, request_id: str, old_credential: str
) -> CredentialRotationResponse:
    """多快照共享凭证换发：新凭证继承冻结清单、原到期时刻与已用共享额度。

    清单中个别快照已撤回时仍可换发：新凭证复制冻结清单，撤回项继续不可读、
    读取该项仍不扣共享额度，其他项继续可读。
    """
    frozen_items = (
        db.execute(
            select(ReadBundleItem)
            .where(ReadBundleItem.bundle_id == bundle_id)
            .order_by(ReadBundleItem.position)
        )
        .scalars()
        .all()
    )
    snapshot_ids = [item.snapshot_id for item in frozen_items]
    # 先按清单快照行加锁（排序保证加锁顺序一致），再锁凭证行：
    # 与公开读取的“快照行锁 → 共享额度条件 UPDATE”同序，串行化而不死锁。
    snapshots = (
        db.execute(
            select(Snapshot)
            .where(Snapshot.id.in_(snapshot_ids))
            .order_by(Snapshot.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    by_id = {snapshot.id: snapshot for snapshot in snapshots}
    bundle = db.execute(
        select(ReadBundle)
        .where(ReadBundle.id == bundle_id)
        .with_for_update()
        # 获得行锁后刷新为当前已提交版本（等待锁期间可能有读取扣次或换发完成）
        .execution_options(populate_existing=True)
    ).scalar_one()

    prior = db.execute(
        select(CredentialRotation).where(
            CredentialRotation.old_credential == old_credential,
            CredentialRotation.request_id == request_id,
        )
    ).scalar_one_or_none()
    if prior is not None:
        db.rollback()
        return _replay_rotation(prior)

    if bundle.revoked_at is not None:
        _rotation_conflict("revoked")
    if bundle.rotated_at is not None:
        _rotation_conflict("already_rotated")
    now = datetime.now(timezone.utc)
    if bundle.expires_at is not None and bundle.expires_at <= now:
        _rotation_conflict("expired")
    if bundle.reads_used >= bundle.max_reads:
        _rotation_conflict("quota_exhausted")

    rotated_at = now
    new_bundle = ReadBundle(
        credential=secrets.token_urlsafe(32),
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,  # 继承已用共享额度，不重置
        expires_at=bundle.expires_at,  # 继承原绝对到期时刻
        chain_id=bundle.chain_id,  # 继承稳定换发链标识：多代共享凭证归于同一链
    )
    db.add(new_bundle)
    db.flush()
    for item in frozen_items:
        db.add(
            ReadBundleItem(
                bundle_id=new_bundle.id,
                snapshot_id=item.snapshot_id,
                position=item.position,
            )
        )
    bundle.rotated_at = rotated_at
    response_items = [
        RotationBundleItem(
            snapshot_id=item.snapshot_id,
            record_id=by_id[item.snapshot_id].record_id,
            version=by_id[item.snapshot_id].version,
            position=item.position,
            status=_snapshot_status(by_id[item.snapshot_id]),
            withdrawn_at=by_id[item.snapshot_id].withdrawn_at,
        )
        for item in frozen_items
    ]
    payload = _rotation_payload(
        kind=ROTATION_KIND_BUNDLE,
        request_id=request_id,
        old_credential=old_credential,
        new_credential=new_bundle.credential,
        snapshot_ids=snapshot_ids,
        expires_at=bundle.expires_at,
        max_reads=bundle.max_reads,
        reads_used=bundle.reads_used,
        rotated_at=rotated_at,
        items=response_items,
    )
    db.add(
        CredentialRotation(
            kind=ROTATION_KIND_BUNDLE,
            old_credential=old_credential,
            new_credential=new_bundle.credential,
            request_id=request_id,
            response_payload=payload.model_dump(mode="json"),
        )
    )
    db.commit()
    return payload


@app.post(
    "/credentials/rotate",
    response_model=CredentialRotationResponse,
    dependencies=[Depends(require_service_key)],
)
def rotate_credential(
    payload: CredentialRotateRequest, db: Session = Depends(get_db)
):
    """读取凭证疑似泄露时换发：单快照令牌或多快照共享凭证同一入口。

    维护人员凭服务密钥提交（原凭证, 请求标识）：
    - 仅未撤销、未到期且额度未耗尽的凭证可换发；新凭证继承原范围（适用快照/
      冻结清单）、原绝对到期时刻与已用次数，旧凭证换发提交后不能再读取；
    - 相同原凭证与请求标识重试 -> 原样返回首次换发结果（replayed=true），
      不再次换发；不同请求标识并发/重复换发仅一次成功，其余 409；
    - 换发与同时进行的成功读取按事务先后结算（快照行锁串行化），
      不重置也不超支额度；
    - 未知凭证 404；共享清单中个别快照已撤回仍可换发，该项继续不可读。
    """
    old_credential = payload.credential
    request_id = payload.request_id
    # 先以普通读定位凭证及其类型（随后在类型分支内按“先快照后凭证”顺序加锁）
    read_token = db.execute(
        select(ReadToken).where(ReadToken.token == old_credential)
    ).scalar_one_or_none()
    if read_token is not None:
        return _rotate_token(db, read_token.snapshot_id, request_id, old_credential)
    bundle = db.execute(
        select(ReadBundle).where(ReadBundle.credential == old_credential)
    ).scalar_one_or_none()
    if bundle is not None:
        return _rotate_bundle(db, bundle.id, request_id, old_credential)
    db.rollback()
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="credential not found"
    )


@app.post(
    "/tokens/{token}/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_token(token: str, db: Session = Depends(get_db)):
    """撤销令牌：单条原子 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadToken)
        .where(ReadToken.token == token, ReadToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return RevokeResponse(token=token, revoked=True, already_revoked=False)
    if db.get(ReadToken, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return RevokeResponse(token=token, revoked=True, already_revoked=True)


@app.get(
    "/tokens/{token}",
    response_model=TokenStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_token_status(token: str, db: Session = Depends(get_db)):
    """维护侧查询令牌的读取次数上限、已用与剩余次数（绝不返回快照原文）。"""
    read_token = db.get(ReadToken, token)
    if read_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    snapshot = db.get(Snapshot, read_token.snapshot_id)
    now = datetime.now(timezone.utc)
    expired = read_token.expires_at is not None and read_token.expires_at <= now
    exhausted = (
        read_token.max_reads is not None and read_token.reads_used >= read_token.max_reads
    )
    remaining = (
        None
        if read_token.max_reads is None
        else max(read_token.max_reads - read_token.reads_used, 0)
    )
    return TokenStatusResponse(
        token=read_token.token,
        snapshot_id=read_token.snapshot_id,
        record_id=snapshot.record_id,
        version=snapshot.version,
        created_at=read_token.created_at,
        expires_at=read_token.expires_at,
        revoked_at=read_token.revoked_at,
        rotated_at=read_token.rotated_at,
        max_reads=read_token.max_reads,
        reads_used=read_token.reads_used,
        remaining_reads=remaining,
        active=(
            read_token.revoked_at is None
            and read_token.rotated_at is None
            and not expired
            and not exhausted
        ),
    )


@app.post(
    "/snapshots/{snapshot_id}/withdraw",
    response_model=SnapshotWithdrawResponse,
    dependencies=[Depends(require_service_key)],
)
def withdraw_snapshot(
    snapshot_id: UUID,
    payload: SnapshotWithdrawRequest = Body(default_factory=SnapshotWithdrawRequest),
    db: Session = Depends(get_db),
):
    """将快照永久撤回：记录首次撤回时间与原因，重复撤回返回首次结果。

    - 未知快照 -> 404；缺失/错误服务密钥 -> 401；
    - 首次撤回必须提供非空白原因 -> 422；
    - 条件 UPDATE 只命中未撤回行：并发撤回恰好一个成功，
      落选方随后读回首次撤回时间与状态，结果幂等。
    """
    snapshot = db.get(Snapshot, snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    if snapshot.withdrawn_at is None and not (payload.reason and payload.reason.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reason is required when withdrawing a snapshot",
        )
    # 原子条件更新：只有未撤回行能命中，首次撤回时间由此固定，永不被覆盖。
    withdrawn_at = db.execute(
        update(Snapshot)
        .where(Snapshot.id == snapshot_id, Snapshot.withdrawn_at.is_(None))
        .values(withdrawn_at=func.now(), withdraw_reason=payload.reason)
        .returning(Snapshot.withdrawn_at)
    ).scalar_one_or_none()
    if withdrawn_at is None:
        # 已被（并发的）首次撤回终结：回滚本次更新，读回首次结果
        db.rollback()
        snapshot = db.get(Snapshot, snapshot_id)
        withdrawn_at = snapshot.withdrawn_at
    else:
        db.commit()
    return SnapshotWithdrawResponse(
        snapshot_id=snapshot_id,
        withdrawn_at=withdrawn_at,
        status=SNAPSHOT_WITHDRAWN_STATUS,
    )


@app.get(
    "/records/{record_id}",
    response_model=RecordStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_record(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查看记录当前版本、受控标记与已发布快照列表。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.record_id == record_id).order_by(Snapshot.version)
        )
        .scalars()
        .all()
    )
    return RecordStatusResponse(
        record_id=record.id,
        current_version=record.current_version,
        controlled=record.controlled,
        snapshots=[
            SnapshotInfo(
                snapshot_id=s.id,
                version=s.version,
                status=_snapshot_status(s),
                published_at=s.published_at,
                withdrawn_at=s.withdrawn_at,
                withdraw_reason=s.withdraw_reason,
            )
            for s in snapshots
        ],
    )


# ---- 记录临时冻结（维护侧；覆盖该记录现有及期间新发布的全部快照） ----


def _freeze_status_response(freeze: RecordFreeze | None, record_id: UUID) -> RecordFreezeStatus:
    """组装当前冻结期状态：无当前冻结期时各字段为 null（原因仅维护侧可见）。"""
    if freeze is None:
        return RecordFreezeStatus(record_id=record_id, frozen=False)
    return RecordFreezeStatus(
        record_id=record_id,
        frozen=True,
        frozen_at=freeze.frozen_at,
        reason=freeze.reason,
        unfrozen_at=None,
    )


@app.post(
    "/records/{record_id}/freeze",
    response_model=RecordFreezeResponse,
    dependencies=[Depends(require_service_key)],
)
def freeze_record(
    record_id: UUID,
    payload: RecordFreezeRequest = Body(default_factory=RecordFreezeRequest),
    db: Session = Depends(get_db),
):
    """临时冻结记录：冻结覆盖该记录现有及冻结期间新发布的全部快照。

    - 凭维护密钥提交记录标识与非空白原因：未知记录 404、空白原因 422、
      缺失/错误密钥 401；
    - 幂等：记录已在冻结期时重复冻结（无论是否带原因）原样回放当前冻结期，
      返回 already_frozen=true，当前冻结期的原因与开始时间绝不被覆盖；
    - 记录行行锁 + “每条记录至多一个未解除冻结期”的部分唯一索引串行化并发
      冻结，恰好一个首次结果；
    - 冻结提交后开始的公开读取（即使持有效单快照令牌或共享凭证）一律按既有
      “snapshot unavailable”拒绝，不扣次数、不写成功读取审计；冻结原因只在
      维护侧响应中出现，公开侧的模糊 403 绝不透露它。
    """
    # 先锁记录行再判定当前冻结期：与并发冻结/解除及公开读取的记录锁串行化，
    # 保证提交边界一致——冻结提交后才拿到记录锁的读取必定看到冻结。
    record = db.execute(
        select(Record).where(Record.id == record_id).with_for_update()
    ).scalar_one_or_none()
    if record is None:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    active = db.execute(
        select(RecordFreeze)
        .where(RecordFreeze.record_id == record_id, RecordFreeze.unfrozen_at.is_(None))
        .with_for_update()
    ).scalar_one_or_none()
    if active is not None:
        # 已在冻结期：幂等回放，绝不覆盖原因与开始时间（不校验本次原因）。
        response = RecordFreezeResponse(
            **_freeze_status_response(active, record_id).model_dump(),
            already_frozen=True,
        )
        db.rollback()  # 释放记录行锁，状态未被改变
        return response
    if not (payload.reason and payload.reason.strip()):
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reason is required when freezing a record",
        )
    freeze = RecordFreeze(reason=payload.reason, record_id=record_id)
    db.add(freeze)
    db.commit()
    db.refresh(freeze)
    return RecordFreezeResponse(
        **_freeze_status_response(freeze, record_id).model_dump(),
        already_frozen=False,
    )


@app.post(
    "/records/{record_id}/unfreeze",
    response_model=RecordUnfreezeResponse,
    dependencies=[Depends(require_service_key)],
)
def unfreeze_record(record_id: UUID, db: Session = Depends(get_db)):
    """解除记录冻结：只恢复原本仍有效的读取资格，幂等且不复活任何失效凭证。

    - 未知记录 404；记录当前未冻结时重复解除返回 frozen=false、
      already_unfrozen=true（状态不被改变）；
    - 条件 UPDATE 只命中未解除的冻结行：与并发解除串行化，恰好一个首次结果；
    - 解除不复活冻结期间已撤回的快照、已过期/撤销/换发的凭证或已耗尽的额度：
      公开读取仍按既有规则各自判定。
    """
    if db.get(Record, record_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    unfrozen_at = db.execute(
        update(RecordFreeze)
        .where(RecordFreeze.record_id == record_id, RecordFreeze.unfrozen_at.is_(None))
        .values(unfrozen_at=func.now())
        .returning(RecordFreeze.unfrozen_at)
    ).scalar_one_or_none()
    if unfrozen_at is None:
        # 当前没有冻结期：重复解除幂等，不改变状态
        db.rollback()
        return RecordUnfreezeResponse(
            record_id=record_id, frozen=False, unfrozen_at=None, already_unfrozen=True
        )
    db.commit()
    return RecordUnfreezeResponse(
        record_id=record_id,
        frozen=False,
        unfrozen_at=unfrozen_at,
        already_unfrozen=False,
    )


@app.get(
    "/records/{record_id}/freeze",
    response_model=RecordFreezeStatus,
    dependencies=[Depends(require_service_key)],
)
def get_record_freeze(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查询记录当前冻结状态：未冻结时 frozen=false、其余字段为 null。

    只返回当前冻结期的开始时间、原因与（冻结中恒为 null 的）解除时间；
    历史冻结期在库内留存但不展开。未知记录 404，缺失/错误密钥 401。
    """
    if db.get(Record, record_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    active = db.execute(
        select(RecordFreeze)
        .where(RecordFreeze.record_id == record_id, RecordFreeze.unfrozen_at.is_(None))
        .order_by(RecordFreeze.frozen_at.desc())
    ).scalar_one_or_none()
    return _freeze_status_response(active, record_id)


# ---- 敏感短语清单（机构维护侧：原子替换 + 查询；发布与申请提交/批准均核验） ----


def _sensitive_phrase_list_response(row: SensitivePhraseSettings) -> SensitivePhraseListResponse:
    """组装清单响应：空清单 enabled=false（停用核验）；只返回清单本身与版本元数据。"""
    phrases = list(row.phrases or [])
    return SensitivePhraseListResponse(
        version=row.version,
        phrases=phrases,
        enabled=bool(phrases),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@app.get(
    "/sensitive-phrases",
    response_model=SensitivePhraseListResponse,
    dependencies=[Depends(require_service_key)],
)
def get_sensitive_phrases(db: Session = Depends(get_db)):
    """查询当前启用的敏感短语清单（服务密钥）。

    返回清单版本、短语数组、enabled 标志与时间元数据；空清单（[]）且
    enabled=false 表示当前停用核验。缺/错密钥 401。
    """
    row = db.get(SensitivePhraseSettings, SENSITIVE_PHRASE_SETTINGS_ID)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="sensitive phrase settings are not initialized",
        )
    return _sensitive_phrase_list_response(row)


@app.put(
    "/sensitive-phrases",
    response_model=SensitivePhraseListResponse,
    dependencies=[Depends(require_service_key)],
)
def replace_sensitive_phrases(
    payload: SensitivePhraseReplaceRequest, db: Session = Depends(get_db)
):
    """凭服务密钥原子替换当前启用的敏感短语清单。

    - 短语须非空、清单内不得重复（请求体 422）；空清单 [] 表示停用核验；
    - 整份清单在同一事务替换并把 version +1：并发替换由固定单行的行锁串行化，
      后提交者在前一已提交版本上继续递增，每次成功替换都是一个新已提交版本；
    - 与发布并发时，发布事务持有同一行的 FOR UPDATE 锁完成核验与提交，本替换
      在其提交后才生效，发布按其锁定的同一版本完成，不受本次替换影响；
    - 缺/错密钥 401。清单变更不改变已发布快照与既有读取凭证。
    """
    row = db.execute(
        select(SensitivePhraseSettings)
        .where(SensitivePhraseSettings.id == SENSITIVE_PHRASE_SETTINGS_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="sensitive phrase settings are not initialized",
        )
    row.phrases = list(payload.phrases)
    row.version = row.version + 1
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _sensitive_phrase_list_response(row)


@app.get("/public/snapshots/{snapshot_id}", response_model=PublicSnapshotResponse)
def read_snapshot(
    snapshot_id: UUID,
    token: str = Query(..., description="发布时签发的读取令牌或共享凭证"),
    db: Session = Depends(get_db),
):
    """公开读取：凭有效令牌或多快照共享凭证返回脱敏正文。

    先对快照行加锁再判定有效性，使读取与永久撤回由数据库行锁串行化：
    撤回提交后才开始的读取在此看到 withdrawn_at 非空，直接返回模糊 403，
    既不返回正文也不执行计数 UPDATE（不消耗任何额度）。

    随后对该快照所属记录的行加锁并核对当前冻结期，使读取与临时冻结/解除
    同样串行化：冻结提交后才开始的读取在此看到未解除冻结期，沿用与撤回完全
    一致的模糊 403（不透露冻结或原因），不扣次数、不写成功读取审计；解除提交
    后开始的读取按各自凭证的既有资格继续判定。冻结覆盖该记录现有及冻结期间
    新发布的全部快照；共享凭证中属于其他记录的快照不受影响、仍可读取。

    单快照令牌：有效性判定与次数消耗由同一条条件 UPDATE 原子完成
    （未撤销、未到期、(不限次或 reads_used < max_reads)）。

    多快照共享凭证：额外要求该快照在凭证绑定清单内，且凭证未撤销、未到期、
    共享额度未耗尽；计数条件合并在同一条 JOIN 条件 UPDATE 中原子完成：
    - 凭证不存在、与清单不匹配、快照不在清单内、已撤销、已到期、额度耗尽 -> rowcount=0，
      统一返回模糊 403，且这些请求不消耗共享额度；
    - 清单内某项快照后来被撤回：只拒绝该项（快照行锁阶段即 403），不扣额度，
      其他项仍可凭同一凭证读取；
    - 到期、撤销或额度耗尽时全部拒绝。

    每次成功读取（无论令牌还是共享凭证）都在本次额度扣减的同一事务内追加一条
    不可变审计事件（事件标识、快照、时间、凭证类型与稳定换发链标识，绝无凭证明文）：
    事件与计数同生共死，任一失败整体回滚；上述所有失败路径在写事件之前即结束，
    不产生任何审计事件。
    """
    snapshot = db.execute(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()
    ).scalar_one_or_none()
    if snapshot is None or snapshot.withdrawn_at is not None:
        # 不区分“快照不存在 / 已撤回 / 已冻结 / 令牌或凭证无效 / 已撤销 / 已过期 / 次数用尽 / 不在清单”，
        # 快照不存在或已撤回时直接拒绝，不触碰任何读取计数，也不写审计事件。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    # 再锁快照所属记录行并在锁内核对当前冻结期：冻结操作先锁记录行再插入冻结行，
    # 与读取在此串行化；冻结提交后才开始的读取必定看到未解除冻结期并模糊拒绝，
    # 不扣任何额度、不写成功读取审计（解除只恢复原本仍有效的读取资格）。
    # 锁序恒为“快照行 -> 记录行 -> 冻结行”，与冻结/换发等事务保持同序，不会死锁。
    db.execute(
        select(Record.id).where(Record.id == snapshot.record_id).with_for_update()
    )
    active_freeze = db.execute(
        select(RecordFreeze.id)
        .where(
            RecordFreeze.record_id == snapshot.record_id,
            RecordFreeze.unfrozen_at.is_(None),
        )
        .with_for_update()
        .limit(1)
    ).first()
    if active_freeze is not None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    now = datetime.now(timezone.utc)
    # 优先按既有单快照令牌判定（行为与响应完全保持兼容）。
    # RETURNING chain_id：只有真正完成扣次的成功读取才取回换发链标识并写事件。
    token_chain_id = db.execute(
        update(ReadToken)
        .where(
            ReadToken.token == token,
            ReadToken.snapshot_id == snapshot.id,
            ReadToken.revoked_at.is_(None),
            ReadToken.rotated_at.is_(None),
            or_(ReadToken.expires_at.is_(None), ReadToken.expires_at > now),
            or_(
                ReadToken.max_reads.is_(None),
                ReadToken.reads_used < ReadToken.max_reads,
            ),
        )
        .values(reads_used=ReadToken.reads_used + 1)
        .returning(ReadToken.chain_id)
    ).scalar_one_or_none()
    credential_kind = None
    chain_id = None
    if token_chain_id is not None:
        credential_kind, chain_id = AUDIT_KIND_TOKEN, token_chain_id
    else:
        # 不是有效的单快照令牌：按多快照共享凭证判定（凭证只能读取清单内快照）。
        # 成员关系、撤销/换发/到期/额度判定与共享计数递增合并为同一条条件 UPDATE，
        # 命中行即 reads_used = reads_used + 1：行锁串行化并发读取，
        # 跨清单的成功次数绝不超过共享上限。
        bundle_chain_id = db.execute(
            update(ReadBundle)
            .where(
                ReadBundle.credential == token,
                ReadBundle.revoked_at.is_(None),
                ReadBundle.rotated_at.is_(None),
                or_(ReadBundle.expires_at.is_(None), ReadBundle.expires_at > now),
                ReadBundle.reads_used < ReadBundle.max_reads,
                ReadBundle.id.in_(
                    select(ReadBundleItem.bundle_id).where(
                        ReadBundleItem.snapshot_id == snapshot.id
                    )
                ),
            )
            .values(reads_used=ReadBundle.reads_used + 1)
            .returning(ReadBundle.chain_id)
        ).scalar_one_or_none()
        if bundle_chain_id is None:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
            )
        credential_kind, chain_id = AUDIT_KIND_BUNDLE, bundle_chain_id
    # 成功读取：审计事件与本次额度扣减在同一事务提交（原子）。
    # 只记录稳定换发链标识，不记录令牌/共享凭证字符串本身。
    db.add(
        ReadAuditEvent(
            snapshot_id=snapshot.id,
            occurred_at=now,
            credential_kind=credential_kind,
            credential_chain_id=chain_id,
        )
    )
    db.commit()
    return {
        "record_id": snapshot.record_id,
        "version": snapshot.version,
        "status": SNAPSHOT_STATUS,
        "redacted_text": snapshot.redacted_text,
    }


# ---- 成功读取审计事件的维护侧分页查询（只追加日志，无修改/删除入口） ----


# 单次查询最多返回的事件数（防止一次拉取过多）
AUDIT_MAX_PAGE_LIMIT = 500
AUDIT_DEFAULT_PAGE_LIMIT = 100
# 游标体积很小（一个 JSON），但仍限制请求头/查询串的滥用长度
AUDIT_MAX_CURSOR_CHARS = 2000


def _bad_cursor() -> HTTPException:
    """非法游标一律 422，不透露编码或内容细节。"""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor"
    )


def _normalize_time_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """统一为 UTC  aware 时间；左闭右开，且 start < end，否则 422。"""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if start >= end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start must be earlier than end",
        )
    return start, end


def _encode_cursor(
    *,
    snapshot_id: UUID,
    start: datetime,
    end: datetime,
    limit: int,
    after_seq: int,
    horizon_seq: int,
) -> str:
    """游标承载查询窗口与分页位置；只允许在同一查询的后续页之间流转。"""
    payload = {
        "snapshot_id": str(snapshot_id),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": limit,
        "after_seq": after_seq,
        "horizon_seq": horizon_seq,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    """解析并做结构校验；任何不符都视为非法游标（422）。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        )
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _bad_cursor() from exc
    required = ("snapshot_id", "start", "end", "limit", "after_seq", "horizon_seq")
    if not isinstance(payload, dict) or any(key not in payload for key in required):
        raise _bad_cursor()
    try:
        snapshot_id = UUID(str(payload["snapshot_id"]))
        start = datetime.fromisoformat(payload["start"])
        end = datetime.fromisoformat(payload["end"])
        limit = int(payload["limit"])
        after_seq = int(payload["after_seq"])
        horizon_seq = int(payload["horizon_seq"])
    except (ValueError, TypeError) as exc:
        raise _bad_cursor() from exc
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or limit < 1
        or limit > AUDIT_MAX_PAGE_LIMIT
        or after_seq < 0
        or horizon_seq < 0
        or after_seq > horizon_seq
    ):
        raise _bad_cursor()
    return {
        "snapshot_id": snapshot_id,
        "start": start,
        "end": end,
        "limit": limit,
        "after_seq": after_seq,
        "horizon_seq": horizon_seq,
    }


@app.get(
    "/snapshots/{snapshot_id}/read-events",
    response_model=ReadAuditEventPage,
    dependencies=[Depends(require_service_key)],
)
def list_snapshot_read_events(
    snapshot_id: UUID,
    start: datetime = Query(..., description="窗口起点（含），ISO 8601"),
    end: datetime = Query(..., description="窗口终点（不含），ISO 8601"),
    limit: int = Query(default=AUDIT_DEFAULT_PAGE_LIMIT, ge=1, le=AUDIT_MAX_PAGE_LIMIT),
    cursor: str | None = Query(default=None, max_length=AUDIT_MAX_CURSOR_CHARS),
    db: Session = Depends(get_db),
):
    """维护侧分页查询某快照在时间范围内的成功读取审计事件。

    - 鉴权：服务密钥（缺失/错误 401）；未知快照明确拒绝（404）；
      非法时间范围或游标 422；
    - 时间窗口左闭右开 [start, end)，naive 时间按 UTC 处理；
    - 分页按库内单调序号（提交顺序）推进，游标不透明且只能在同一查询内续用；
    - 并发安全：首页以查询开始时该快照“已提交事件的最大序号”为上界，
      此后各页沿用该上界——窗口内、查询起点前已提交的事件跨页既不重复也不遗漏；
      查询期间新提交的事件可能落在上界之后，留待下次查询，不会造成重排；
    - 响应只含 event_id / snapshot_id / occurred_at / credential_kind /
      credential_chain_id，绝不返回原文、脱敏正文或任何凭证明文。
    """
    if db.get(Snapshot, snapshot_id) is None:
        # 未知快照明确拒绝，避免对不存在资源返回空页
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="snapshot not found"
        )
    window_start, window_end = _normalize_time_range(start, end)

    if cursor is not None:
        position = _decode_cursor(cursor)
        # 游标只允许在同一快照、同一时间窗口与同一页大小的查询间流转
        if (
            position["snapshot_id"] != snapshot_id
            or position["start"] != window_start
            or position["end"] != window_end
            or position["limit"] != limit
        ):
            raise _bad_cursor()
        after_seq = position["after_seq"]
        horizon_seq = position["horizon_seq"]
    else:
        after_seq = 0
        # 首页上界：语句开始时可见的、该快照已提交事件的最大 seq。
        # seq 为 IDENTITY 单调递增且公开读取按快照行锁串行提交，
        # 故所有 <= 上界的事件在本次查询中必然稳定可见（无重复/遗漏）。
        horizon_seq = db.execute(
            select(func.coalesce(func.max(ReadAuditEvent.seq), 0)).where(
                ReadAuditEvent.snapshot_id == snapshot_id
            )
        ).scalar_one()

    rows = (
        db.execute(
            select(ReadAuditEvent)
            .where(
                ReadAuditEvent.snapshot_id == snapshot_id,
                ReadAuditEvent.seq > after_seq,
                ReadAuditEvent.seq <= horizon_seq,
                ReadAuditEvent.occurred_at >= window_start,
                ReadAuditEvent.occurred_at < window_end,
            )
            .order_by(ReadAuditEvent.seq)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if page_rows and has_more:
        last_seq = page_rows[-1].seq
        next_cursor = _encode_cursor(
            snapshot_id=snapshot_id,
            start=window_start,
            end=window_end,
            limit=limit,
            after_seq=last_seq,
            horizon_seq=horizon_seq,
        )
    return ReadAuditEventPage(
        snapshot_id=snapshot_id,
        start=window_start,
        end=window_end,
        events=[
            ReadAuditEventInfo(
                event_id=row.id,
                snapshot_id=row.snapshot_id,
                occurred_at=row.occurred_at,
                credential_kind=row.credential_kind,
                credential_chain_id=row.credential_chain_id,
            )
            for row in page_rows
        ],
        next_cursor=next_cursor,
    )
