"""JSONL 消息封套与增量解析（P1-02）。

每行一个 UTF-8 JSON 对象。本模块只负责协议层：分帧、校验、超限转文件引用、世代守卫。
不启动进程、不执行计算，因此可被 GUI（无原生依赖）与 worker 共用。

必须扛住的真实情况（都是验收点，见 tests/unit/test_protocol_jsonl.py）：
- 一次 read 只给半行；一次 read 给多行（粘包）。
- UTF-8 多字节字符被从中间字节截断。
- 非法 JSON 行、未知 kind、协议版本不匹配 → 明确错误，不静默丢弃。
- 控制消息超过上限 → 落文件并改为 artifact 引用，而不是撑爆管道。
- worker 重启后的旧世代响应必须被拒。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from spectra_workbench.domain.models import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS

# 协议设计值，双方一致；超限内容改写为文件引用。
MAX_CONTROL_MESSAGE_BYTES = 1 * 1024 * 1024


class Kind(StrEnum):
    hello = "hello"
    ready = "ready"
    request = "request"
    accepted = "accepted"
    progress = "progress"
    log = "log"
    artifact = "artifact"
    result = "result"
    error = "error"
    cancel = "cancel"
    heartbeat = "heartbeat"


class ProtocolError(Exception):
    """协议层错误。携带原始片段便于诊断，但绝不把内容当指令执行。"""

    def __init__(self, reason: str, raw: str | bytes | None = None, line_no: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw
        self.line_no = line_no

    def to_payload(self) -> dict[str, Any]:
        preview = (
            self.raw
            if isinstance(self.raw, str)
            else (self.raw.decode("utf-8", "replace") if self.raw else "")
        )
        return {
            "code": "protocol_error",
            "message": self.reason,
            "line_no": self.line_no,
            "preview": preview[:400],
        }


class Envelope(BaseModel):
    """与 contracts/message.schema.json 对齐：所有字段必填，未知字段禁止。"""

    model_config = ConfigDict(extra="forbid")

    protocol_version: str = PROTOCOL_VERSION
    template_only: bool = False
    kind: Kind
    message_id: str = Field(min_length=1)
    request_id: str | None = None
    task_id: str | None = None
    worker_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("protocol_version")
    @classmethod
    def _supported(cls, v: str):
        if v not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(f"不支持的 protocol_version：{v}")
        return v

    @field_validator("request_id", "task_id")
    @classmethod
    def _blank_not_allowed(cls, v):
        if v is not None and not v.strip():
            raise ValueError("id 字段不得为空白")
        return v

    @model_validator(mode="after")
    def _validate_payload_model(self) -> Envelope:
        if not isinstance(self.payload, dict):
            raise ValueError(f"payload 必须为字典，收到 {type(self.payload).__name__}")
        expected = _KIND_REQUIRED_PAYLOAD.get(self.kind)
        if expected:
            missing = [k for k in expected if k not in self.payload]
            if missing:
                raise ValueError(f"kind={self.kind.value} 的 payload 缺少必填字段：{missing}")
        model_cls = PAYLOAD_MODELS.get(self.kind)
        if model_cls is not None:
            try:
                validated = model_cls.model_validate(self.payload)
                # 保持归一化后的数据（如 level 大写），同时保留用户开放扩展键
                self.payload.update(validated.model_dump(exclude_unset=True))
            except Exception as exc:
                raise ValueError(f"kind={self.kind.value} 的 payload 校验失败: {exc}") from exc
        return self


class HelloPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider_id: str = Field(min_length=1)
    worker_package_version: str = Field(min_length=1)


class ReadyPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    runtime_id: str = Field(min_length=1)
    features: list[str] | None = None

    @field_validator("features", mode="before")
    @classmethod
    def _validate_features(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("features 允许省略，但禁止显式传入 null")
        if not isinstance(v, list):
            raise ValueError("features 必须为列表")
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("features 列表项必须为非空字符串")
        return v


class RequestPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str = Field(min_length=1)
    arguments: dict[str, Any]

    @field_validator("arguments", mode="before")
    @classmethod
    def _validate_args(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            raise ValueError("arguments 必须为字典对象")
        return v


class AcceptedPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    attempt: int = Field(ge=1)

    @field_validator("attempt", mode="before")
    @classmethod
    def _not_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("attempt 必须为整数，不能为布尔值")
        return v


class ProgressPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    ratio: float | None = None
    phase: str
    message: str | None = None

    @field_validator("ratio", mode="before")
    @classmethod
    def _validate_ratio(cls, v: Any) -> Any:
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("ratio 必须为数字或 null")
            if not (0.0 <= float(v) <= 1.0):
                raise ValueError(f"ratio 必须在 [0.0, 1.0] 范围内，收到 {v}")
        return v

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("message 允许省略，但禁止显式传入 null")
        if not isinstance(v, str):
            raise ValueError("message 必须为字符串")
        return v


class LogPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    level: str
    message: str
    stream: str | None = Field(default=None, pattern=r"^(stdout|stderr|engine|application)$")

    @field_validator("level", mode="before")
    @classmethod
    def _validate_level(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("level 必须为字符串")
        v_upper = v.upper()
        if v_upper not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(f"level 必须为 DEBUG/INFO/WARNING/ERROR，收到 {v}")
        return v_upper

    @field_validator("stream", mode="before")
    @classmethod
    def _validate_stream(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("stream 允许省略，但禁止显式传入 null")
        if not isinstance(v, str):
            raise ValueError("stream 必须为字符串")
        if v not in {"stdout", "stderr", "engine", "application"}:
            raise ValueError(f"stream 必须为 stdout/stderr/engine/application，收到 {v}")
        return v


class ArtifactPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    artifact_id: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    commit_state: str = Field(pattern=r"^(staging|committed|partial|failed)$")


class ResultPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str = Field(pattern=r"^(succeeded|failed|cancelled|interrupted)$")
    artifact_ids: list[str]
    warnings: list[str] | None = None

    @field_validator("artifact_ids", mode="before")
    @classmethod
    def _validate_artifacts(cls, v: Any) -> Any:
        if not isinstance(v, list):
            raise ValueError("artifact_ids 必须为列表")
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("artifact_ids 列表项必须为非空字符串")
        return v

    @field_validator("warnings", mode="before")
    @classmethod
    def _validate_warnings(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("warnings 允许省略，但禁止显式传入 null")
        if not isinstance(v, list):
            raise ValueError("warnings 必须为列表")
        for item in v:
            if not isinstance(item, str):
                raise ValueError("warnings 列表项必须为字符串")
        return v


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] | None = None

    @field_validator("retryable", mode="before")
    @classmethod
    def _strict_bool(cls, v: Any) -> Any:
        if not isinstance(v, bool):
            raise ValueError("retryable 必须为布尔值")
        return v

    @field_validator("details", mode="before")
    @classmethod
    def _validate_details(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("details 允许省略，但禁止显式传入 null")
        if not isinstance(v, dict):
            raise ValueError("details 必须为字典对象")
        return v


class CancelPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    reason: str | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def _validate_reason(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("reason 允许省略，但禁止显式传入 null")
        if not isinstance(v, str):
            raise ValueError("reason 必须为字符串")
        return v


class HeartbeatPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    monotonic_seconds: float = Field(ge=0)
    active_task_id: str | None = None

    @field_validator("monotonic_seconds", mode="before")
    @classmethod
    def _not_bool(cls, v: Any) -> Any:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("monotonic_seconds 必须为非负数值，不能为布尔值")
        return float(v)

    @field_validator("active_task_id", mode="before")
    @classmethod
    def _validate_active_task_id(cls, v: Any) -> Any:
        if v is not None and not isinstance(v, str):
            raise ValueError("active_task_id 必须为字符串或 null")
        return v


PAYLOAD_MODELS: dict[Kind, type[BaseModel]] = {
    Kind.hello: HelloPayload,
    Kind.ready: ReadyPayload,
    Kind.request: RequestPayload,
    Kind.accepted: AcceptedPayload,
    Kind.progress: ProgressPayload,
    Kind.log: LogPayload,
    Kind.artifact: ArtifactPayload,
    Kind.result: ResultPayload,
    Kind.error: ErrorPayload,
    Kind.cancel: CancelPayload,
    Kind.heartbeat: HeartbeatPayload,
}


_KIND_REQUIRED_PAYLOAD: dict[Kind, tuple[str, ...]] = {
    Kind.hello: ("provider_id", "worker_package_version"),
    Kind.ready: ("runtime_id",),
    Kind.request: ("action", "arguments"),
    Kind.accepted: ("attempt",),
    Kind.progress: ("ratio", "phase"),
    Kind.log: ("level", "message"),
    Kind.artifact: ("artifact_id", "uri", "kind", "commit_state"),
    Kind.result: ("status", "artifact_ids"),
    Kind.error: ("code", "message", "retryable"),
    Kind.heartbeat: ("monotonic_seconds",),
}


def new_message(
    kind: Kind,
    worker_id: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    task_id: str | None = None,
    generation: int = 0,
    message_id: str | None = None,
) -> Envelope:
    return Envelope(
        kind=kind,
        message_id=message_id or uuid.uuid4().hex,
        request_id=request_id,
        task_id=task_id,
        worker_id=worker_id,
        generation=generation,
        payload=payload,
    )


def encode_message(
    env: Envelope,
    spill_dir: Path | None = None,
    max_bytes: int = MAX_CONTROL_MESSAGE_BYTES,
) -> bytes:
    """序列化成一行 JSONL。payload 超限时落文件并把行内内容替换为引用。"""
    raw = json.dumps(env.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    data = raw.encode("utf-8")
    if len(data) <= max_bytes:
        return data + b"\n"
    if spill_dir is None:
        raise ProtocolError(
            f"控制消息 {len(data)} 字节超过上限 {max_bytes}，且未提供 spill_dir；应改走 artifact 文件引用"
        )
    spill_dir = Path(spill_dir)
    spill_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    target = spill_dir / f"{env.message_id}-{digest[:12]}.json"
    target.write_bytes(data)
    ref = Envelope(
        protocol_version=env.protocol_version,
        kind=Kind.artifact,
        message_id=env.message_id,
        request_id=env.request_id,
        task_id=env.task_id,
        worker_id=env.worker_id,
        generation=env.generation,
        payload={
            "artifact_id": env.message_id,
            "uri": target.as_uri(),
            "kind": "inline_payload_spill",
            "commit_state": "committed",
            "sha256": digest,
            "original_kind": env.kind.value,
            "original_bytes": len(data),
        },
    )
    return (
        json.dumps(ref.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


@dataclass
class JsonlStreamParser:
    """增量分帧器：喂任意字节切分，产出完整消息或抛出协议错误。

    `feed()` 返回本次新完成的消息列表；未收完的尾部留在内部缓冲区。
    单条未完成行超过 `max_buffer_bytes` 视为流失控，报错而不是无限吃内存。
    """

    max_buffer_bytes: int = 8 * 1024 * 1024
    _buf: bytearray = field(default_factory=bytearray)
    _line_no: int = 0
    dropped_lines: list[dict[str, Any]] = field(default_factory=list)

    def feed(self, chunk: bytes) -> list[Envelope]:
        if not isinstance(chunk, (bytes, bytearray)):
            raise ProtocolError("feed 只接受 bytes", raw=None)
        self._buf.extend(chunk)
        if len(self._buf) > self.max_buffer_bytes:
            raise ProtocolError(
                f"未闭合的缓冲达到 {len(self._buf)} 字节，超过上限 {self.max_buffer_bytes}",
                raw=bytes(self._buf[:200]),
            )
        out: list[Envelope] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            self._line_no += 1
            env = self._parse_line(line)
            if env is not None:
                out.append(env)
        return out

    def close(self) -> list[Envelope]:
        """流结束：处理没有换行结尾的最后一行。"""
        out: list[Envelope] = []
        if self._buf:
            line = bytes(self._buf)
            self._buf.clear()
            self._line_no += 1
            env = self._parse_line(line)
            if env is not None:
                out.append(env)
        return out

    def _parse_line(self, line: bytes) -> Envelope | None:
        if not line.strip():
            return None
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"第 {self._line_no} 行不是合法 UTF-8：{exc}", raw=line) from exc
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(
                f"第 {self._line_no} 行 JSON 解析失败：{exc}", raw=text, line_no=self._line_no
            ) from exc
        if not isinstance(obj, dict):
            raise ProtocolError(
                f"第 {self._line_no} 行不是 JSON 对象", raw=text, line_no=self._line_no
            )
        try:
            return Envelope.model_validate(obj)
        except Exception as exc:
            raise ProtocolError(
                f"第 {self._line_no} 行消息校验失败：{exc}", raw=text, line_no=self._line_no
            ) from exc


class GenerationGuard:
    """worker 重启后旧世代的响应不得应用到新任务。"""

    def __init__(self, current_generation: int = 0):
        self.current_generation = current_generation
        self.rejected_stale = 0

    def restart(self) -> int:
        self.current_generation += 1
        return self.current_generation

    def accept(self, env: Envelope) -> bool:
        if env.generation != self.current_generation:
            self.rejected_stale += 1
            return False
        return True
