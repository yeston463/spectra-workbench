"""路径与远程/VSI 辅助函数（P8-02）。

统一管理 GDAL /vsi 路径规范、远程 URL 与本地归档路径。
保证在 Windows 环境下不被 pathlib.Path.resolve() 破坏，
同时对归档文件（.zip, .tar, .tar.gz 等）和 HTTP/HTTPS URL 进行标准 VSI 前缀包装。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

VSI_PREFIXES: tuple[str, ...] = (
    "/vsi",
    "http://",
    "https://",
    "s3://",
    "gs://",
    "az://",
    "zip://",
    "tar://",
)

ARCHIVE_EXTENSIONS: tuple[str, ...] = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".gz",
)


def is_vsi_or_remote(path: Path | str | None) -> bool:
    """判断路径是否为 GDAL VSI 虚拟文件系统路径、远程 URL 或复合子数据集 URI。"""
    if path is None:
        return False
    s = str(path).strip()
    if not s:
        return False
    norm = s.replace("\\", "/").lower()
    if any(norm.startswith(p) for p in ("/vsi", "http://", "https://", "s3://", "gs://", "az://", "zip://", "tar://")):
        return True
    return bool(re.match(r"^[A-Za-z0-9_]+:[\"'].*[\"']", s))


def is_archive(path: Path | str | None) -> bool:
    """判断路径是否指向压缩归档包（.zip, .tar 等）。"""
    if path is None:
        return False
    s = str(path).strip().lower()
    norm = s.replace("\\", "/")
    if norm.startswith(("/vsizip/", "/vsitar/", "/vsigzip/")):
        return True
    return any(norm.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def normalize_vsi_path(path: Path | str) -> str:
    """将普通文件/归档/URL 标准化为 GDAL 可识别的 VSI 路径字符串。

    规则：
    1. http:// 或 https:// -> /vsicurl/{url}
    2. s3:// -> /vsis3/{url[5:]}
    3. gs:// -> /vsigs/{url[5:]}
    4. az:// -> /vsiaz/{url[5:]}
    5. zip:// -> /vsizip/{url[6:]}
    6. tar:// -> /vsitar/{url[6:]}
    7. 带有 /vsi 前缀的路径 -> 保留前缀，反斜杠统一替换为正斜杠
    8. 本地 .zip 归档文件 -> /vsizip/{posix_path}
    9. 本地 .tar / .tar.gz / .tgz 归档文件 -> /vsitar/{posix_path}
    10. 其它普通文件 -> 绝对路径并转为 posix 格式字符串
    """
    s = str(path) if isinstance(path, Path) else str(path).strip()

    norm = s.replace("\\", "/")

    if norm.lower().startswith(("http://", "https://")):
        return f"/vsicurl/{s}"
    if norm.lower().startswith("s3://"):
        return f"/vsis3/{s[5:]}"
    if norm.lower().startswith("gs://"):
        return f"/vsigs/{s[5:]}"
    if norm.lower().startswith("az://"):
        return f"/vsiaz/{s[5:]}"
    if norm.lower().startswith("zip://"):
        return f"/vsizip/{s[6:]}"
    if norm.lower().startswith("tar://"):
        return f"/vsitar/{s[6:]}"
    if norm.lower().startswith("/vsi"):
        return norm

    if re.match(r"^[A-Za-z0-9_]+:[\"'].*[\"']", s):
        return s

    lowered = norm.lower()
    p = Path(s)
    if lowered.endswith(".zip"):
        abs_p = p.resolve().as_posix()
        return f"/vsizip/{abs_p}"
    if lowered.endswith((".tar", ".tar.gz", ".tgz")):
        abs_p = p.resolve().as_posix()
        return f"/vsitar/{abs_p}"

    try:
        return p.resolve().as_posix()
    except Exception:
        return norm


def get_display_name(path: Path | str) -> str:
    """获取适用于 UI 标题和状态栏的人类可读显示名。"""
    s = str(path).strip()
    norm = s.replace("\\", "/")
    if "://" in norm:
        try:
            parsed = urlparse(norm)
            norm = parsed.path
        except Exception:
            pass
    part = norm.rstrip("/").split("/")[-1]
    return part or s


def sanitize_url(url: str) -> str:
    """脱敏 URL 中的敏感凭据（如 query 中的 token、key、sig 等）。"""
    s = str(url).strip()
    try:
        parsed = urlparse(s)
        if not parsed.query:
            return s
        sensitive_keys = {"token", "key", "sig", "signature", "secret", "auth", "access_token", "apikey"}
        pairs = []
        for item in parsed.query.split("&"):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.lower() in sensitive_keys or any(sk in k.lower() for sk in sensitive_keys):
                    pairs.append(f"{k}=***")
                else:
                    pairs.append(item)
            else:
                pairs.append(item)
        new_query = "&".join(pairs)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return s


def extract_local_path_from_vsi(path: Path | str | None) -> Path | None:
    """如果路径是本地文件或本地归档中的文件，返回其在本地文件系统中的宿主文件 Path；
    如果是纯远程 URL（http/s3/gs 等）或内存数据集，返回 None。
    """
    if path is None:
        return None
    if isinstance(path, Path):
        return path
    s = str(path).strip()
    if not s:
        return None
    norm = s.replace("\\", "/")
    lowered = norm.lower()

    # 纯远程或内存
    if any(lowered.startswith(p) for p in ("http://", "https://", "s3://", "gs://", "az://", "/vsicurl/", "/vsimem/")):
        return None

    # 本地归档前缀：/vsizip/, /vsitar/, /vsigzip/
    for prefix in ("/vsizip/", "/vsitar/", "/vsigzip/"):
        if lowered.startswith(prefix):
            inner = s[len(prefix):]
            # 查找压缩包扩展名
            for ext in (".tar.gz", ".tgz", ".zip", ".tar", ".gz"):
                idx = inner.lower().find(ext)
                if idx != -1:
                    archive_part = inner[:idx + len(ext)]
                    return Path(archive_part)
            return Path(inner)

    # 驱动器盘符或绝对/相对路径
    try:
        return Path(s)
    except Exception:
        return None

