"""凭据 Profile 与脱敏管理（P8-03）。

提供：
1. CredentialProfile：统一管理远程数据访问凭据（HTTP Bearer, HTTP Basic, S3, Azure, GCS 等），
   支持转换为 GDAL VSI 配置选项与环境变量。
2. CredentialStore：本地独立凭据存储库（默认 ~/.spectra/credentials.json），
   与项目文件 (.spectra.json) 物理隔离，凭据绝不写入项目文件。
3. 脱敏工具：URL 脱敏 (sanitize_url)、文本脱敏 (sanitize_text)、字典与诊断报告脱敏 (sanitize_dict)。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "token",
        "key",
        "secret",
        "password",
        "passwd",
        "auth",
        "authorization",
        "apikey",
        "api_key",
        "access_key",
        "secret_key",
        "signature",
        "sig",
        "credential",
        "credentials",
        "private_key",
        "session_token",
        "bearer",
    }
)

SAFE_IDENTIFIER_KEYS: frozenset[str] = frozenset(
    {
        "profile_id",
        "credential_profile",
        "credential_id",
        "profile_name",
        "credential_name",
        "active_layer_id",
        "layer_id",
    }
)

SENSITIVE_REGEXES: tuple[re.Pattern[str], ...] = (
    # Bearer token
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-\.=]+)"),
    # Basic auth base64
    re.compile(r"(?i)\b(basic\s+)([A-Za-z0-9+/=]+)"),
    # URL inline credentials (http://user:password@host)
    re.compile(r"(https?://)([^:/@\s]+):([^@/\s]+)@"),
    # Key=Value pairs
    re.compile(
        r"(?i)\b(access_key|secret_key|api_key|apikey|token|password|secret|auth|signature|sig)\s*[:=]\s*([\"\']?)([^\s,\"\'&;]+)\3"
    ),
    # AWS Secret Key pattern (typically 40 chars base64-like)
    re.compile(r"(?i)\b(aws_secret_access_key\s*=\s*)([A-Za-z0-9/+=]{20,})"),
)


def sanitize_url(url: str) -> str:
    """脱敏 URL 中的敏感信息（query 参数与 basic auth）。"""
    s = str(url).strip()
    try:
        parsed = urlparse(s)
        # 1. 检查 username/password
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            if ":" in userinfo:
                user, _ = userinfo.split(":", 1)
                netloc = f"{user}:***@{host}"
            else:
                netloc = f"***@{host}"

        # 2. 检查 query 参数
        query = parsed.query
        if query:
            pairs = []
            for item in query.split("&"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    k_lower = k.lower()
                    if k_lower in SENSITIVE_KEY_NAMES or any(
                        sk in k_lower for sk in SENSITIVE_KEY_NAMES
                    ):
                        pairs.append(f"{k}=***")
                    else:
                        pairs.append(item)
                else:
                    pairs.append(item)
            query = "&".join(pairs)

        return urlunparse(parsed._replace(netloc=netloc, query=query))
    except Exception:
        return s


def sanitize_text(text: str) -> str:
    """脱敏任意日志或消息文本中的凭据、Token 与敏感键值。"""
    if not text:
        return ""
    result = str(text)

    # 1. Bearer token
    result = re.sub(
        r"(?i)\b(bearer\s+)[A-Za-z0-9_\-\.=]+",
        r"\g<1>***",
        result,
    )
    # 2. Basic token
    result = re.sub(
        r"(?i)\b(basic\s+)[A-Za-z0-9+/=]+",
        r"\g<1>***",
        result,
    )
    # 3. URL inline user:pass
    result = re.sub(
        r"(https?://)([^:/@\s]+):([^@/\s]+)@",
        r"\g<1>\g<2>:***@",
        result,
    )
    # 4. Sensitive key=value
    result = re.sub(
        r"(?i)\b(access_key|secret_key|api_key|apikey|token|password|secret|auth|signature|sig)\s*([:=])\s*([\"\']?)([^\s,\"\'&;]+)\3",
        r"\1\2\3***\3",
        result,
    )
    # 5. AWS secret
    result = re.sub(
        r"(?i)\b(aws_secret_access_key\s*[:=]\s*)([A-Za-z0-9/+=]{10,})",
        r"\g<1>***",
        result,
    )
    # 6. URL query string in text
    urls = re.findall(r"https?://[^\s<>\"\']+", result)
    for u in urls:
        sanitized_u = sanitize_url(u)
        if sanitized_u != u:
            result = result.replace(u, sanitized_u)

    return result


def sanitize_dict(data: Any) -> Any:
    """深度递归脱敏 dict/list 结构中的凭据字段与 URL。"""
    if isinstance(data, dict):
        out_dict = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if k_lower in SAFE_IDENTIFIER_KEYS:
                out_dict[k] = sanitize_dict(v)
            elif any(sk in k_lower for sk in SENSITIVE_KEY_NAMES):
                out_dict[k] = "***"
            else:
                out_dict[k] = sanitize_dict(v)
        return out_dict
    elif isinstance(data, (list, tuple)):
        return [sanitize_dict(item) for item in data]
    elif isinstance(data, str):
        return sanitize_text(data)
    return data


@dataclass
class CredentialProfile:
    """凭据 Profile。

    kind 可选:
    - "http_bearer": {"token": "..."}
    - "http_basic": {"username": "...", "password": "..."}
    - "s3": {"aws_access_key_id": "...", "aws_secret_access_key": "...", "aws_region": "...", "aws_endpoint": "..."}
    - "azure": {"azure_storage_account": "...", "azure_storage_key": "...", "azure_storage_connection_string": "..."}
    - "gcs": {"gs_secret_key": "...", "gs_access_key_id": "..."}
    - "header": {"header_name": "...", "header_value": "..."}
    - "custom": {"ENV_VAR_NAME": "value"}
    """

    profile_id: str
    kind: str = "http_bearer"
    description: str = ""
    credentials: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_env_vars(self) -> dict[str, str]:
        """将凭据配置转换为 GDAL VSI 及进程环境变量映射。"""
        env: dict[str, str] = {}
        kind = self.kind.lower()
        creds = self.credentials

        if kind == "s3":
            if "aws_access_key_id" in creds:
                env["AWS_ACCESS_KEY_ID"] = creds["aws_access_key_id"]
            if "aws_secret_access_key" in creds:
                env["AWS_SECRET_ACCESS_KEY"] = creds["aws_secret_access_key"]
            if "aws_session_token" in creds:
                env["AWS_SESSION_TOKEN"] = creds["aws_session_token"]
            if "aws_region" in creds:
                env["AWS_DEFAULT_REGION"] = creds["aws_region"]
            if "aws_endpoint" in creds:
                env["AWS_S3_ENDPOINT"] = creds["aws_endpoint"]

        elif kind == "azure":
            if "azure_storage_account" in creds:
                env["AZURE_STORAGE_ACCOUNT"] = creds["azure_storage_account"]
            if "azure_storage_key" in creds:
                env["AZURE_STORAGE_ACCESS_KEY"] = creds["azure_storage_key"]
            if "azure_storage_connection_string" in creds:
                env["AZURE_STORAGE_CONNECTION_STRING"] = creds["azure_storage_connection_string"]

        elif kind == "gcs":
            if "gs_access_key_id" in creds:
                env["GS_ACCESS_KEY_ID"] = creds["gs_access_key_id"]
            if "gs_secret_key" in creds:
                env["GS_SECRET_ACCESS_KEY"] = creds["gs_secret_key"]

        elif kind == "http_bearer":
            token = creds.get("token") or creds.get("access_token") or ""
            if token:
                env["GDAL_HTTP_HEADERS"] = f"Authorization: Bearer {token}"

        elif kind == "http_basic":
            user = creds.get("username", "")
            pwd = creds.get("password", "")
            env["GDAL_HTTP_USERPWD"] = f"{user}:{pwd}"

        elif kind == "header":
            name = creds.get("header_name", "")
            val = creds.get("header_value", "")
            if name and val:
                env["GDAL_HTTP_HEADERS"] = f"{name}: {val}"

        elif kind == "custom":
            for k, v in creds.items():
                env[k] = v

        return env

    def to_safe_dict(self) -> dict[str, Any]:
        """导出安全脱敏字典（供项目文件、诊断与 UI 展示），绝不输出密钥明文。"""
        return {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "configured_keys": list(self.credentials.keys()),
        }

    def to_full_dict(self) -> dict[str, Any]:
        """导出完整字典（仅用于凭据存储库持久化）。"""
        return {
            "profile_id": self.profile_id,
            "kind": self.kind,
            "description": self.description,
            "credentials": dict(self.credentials),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CredentialProfile:
        return cls(
            profile_id=str(data.get("profile_id", "")),
            kind=str(data.get("kind", "http_bearer")),
            description=str(data.get("description", "")),
            credentials=dict(data.get("credentials", {})),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


class CredentialStore:
    """凭据存储库。

    独立管理本地凭据配置文件 (~/.spectra/credentials.json)。
    完全隔离于项目文件与工作区。
    """

    DEFAULT_STORE_DIR = Path.home() / ".spectra"
    DEFAULT_STORE_FILE = DEFAULT_STORE_DIR / "credentials.json"

    def __init__(self, store_path: Path | str | None = None) -> None:
        if store_path is not None:
            self.store_path = Path(store_path).resolve()
        else:
            env_path = os.environ.get("SPECTRA_CREDENTIALS_FILE")
            if env_path:
                self.store_path = Path(env_path).resolve()
            else:
                self.store_path = self.DEFAULT_STORE_FILE
        self._lock = threading.Lock()

    def list_profiles(self) -> list[CredentialProfile]:
        """列出所有已保存的凭据 profile。若文件不存在则安全返回空列表。"""
        with self._lock:
            if not self.store_path.exists():
                return []
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                profiles = data.get("profiles", {})
                return [
                    CredentialProfile.from_dict(p_data)
                    for p_data in profiles.values()
                    if isinstance(p_data, dict)
                ]
            except Exception:
                return []

    def get_profile(self, profile_id: str) -> CredentialProfile | None:
        """根据 profile_id 获取凭据 profile。若不存在返回 None。"""
        if not profile_id:
            return None
        with self._lock:
            if not self.store_path.exists():
                return None
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                profiles = data.get("profiles", {})
                p_data = profiles.get(profile_id)
                if isinstance(p_data, dict):
                    return CredentialProfile.from_dict(p_data)
                return None
            except Exception:
                return None

    def save_profile(self, profile: CredentialProfile) -> None:
        """保存或更新凭据 profile。原子写且限制文件权限。"""
        with self._lock:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict[str, Any] = {"schema_version": "1.0", "profiles": {}}
            if self.store_path.exists():
                try:
                    loaded = json.loads(self.store_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and "profiles" in loaded:
                        data = loaded
                except Exception:
                    data = {"schema_version": "1.0", "profiles": {}}

            profiles = data.setdefault("profiles", {})
            profile.updated_at = datetime.now(UTC).isoformat()
            profiles[profile.profile_id] = profile.to_full_dict()

            # 原子写入
            tmp_file = self.store_path.with_suffix(f".tmp.{os.getpid()}")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # POSIX 文件权限保护
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(tmp_file, 0o600)
            os.replace(tmp_file, self.store_path)

    def delete_profile(self, profile_id: str) -> bool:
        """删除指定 profile。成功返回 True，不存在返回 False。"""
        with self._lock:
            if not self.store_path.exists():
                return False
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                profiles = data.get("profiles", {})
                if profile_id not in profiles:
                    return False
                del profiles[profile_id]
                tmp_file = self.store_path.with_suffix(f".tmp.{os.getpid()}")
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_file, self.store_path)
                return True
            except Exception:
                return False

    def resolve_env_for_profile(self, profile_id: str) -> dict[str, str]:
        """根据 profile_id 解析出可直接赋给 worker 进程的环境变量映射。"""
        prof = self.get_profile(profile_id)
        if prof is None:
            return {}
        return prof.to_env_vars()
