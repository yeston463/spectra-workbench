"""本地 HTTP Range 测试服务与归档工具（P8-02 / A13）。

用于在 127.0.0.1 闭环测试：
1. HTTP 范围读取（Range: bytes=start-end，返回 206 Partial Content）。
2. 可控超时（注入可配置延迟）。
3. 临时故障注入与重试（注入 503 错误，前 N 次失败后恢复）。
4. 请求头与传输字节数审计。
"""

from __future__ import annotations

import http.server
import socketserver
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any


class LocalHttpRangeServer:
    """本地 HTTP Range 与故障注入测试服务器。"""

    def __init__(
        self,
        files: dict[str, bytes | Path] | None = None,
        *,
        delay_seconds: float = 0.0,
        fail_count: int = 0,
        fail_status: int = 503,
    ) -> None:
        self.files: dict[str, bytes] = {}
        if files:
            for k, v in files.items():
                clean_key = k.lstrip("/").replace("\\", "/")
                if isinstance(v, (str, Path)):
                    self.files[clean_key] = Path(v).read_bytes()
                else:
                    self.files[clean_key] = bytes(v)

        self.delay_seconds = delay_seconds
        self.fail_count = fail_count
        self.fail_status = fail_status
        self.requests: list[dict[str, Any]] = []
        self.total_bytes_sent = 0
        self._lock = threading.Lock()

        outer = self

        class RangeHandler(http.server.BaseHTTPRequestHandler):
            def _record_request(self, method: str) -> None:
                with outer._lock:
                    outer.requests.append({
                        "method": method,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "range": self.headers.get("Range"),
                    })

            def _handle_failure(self) -> bool:
                with outer._lock:
                    if outer.fail_count > 0:
                        outer.fail_count -= 1
                        self.send_response(outer.fail_status)
                        self.end_headers()
                        return True
                return False

            def _apply_delay(self) -> None:
                if outer.delay_seconds > 0:
                    time.sleep(outer.delay_seconds)

            def do_HEAD(self) -> None:
                self._record_request("HEAD")
                self._apply_delay()
                if self._handle_failure():
                    return
                key = self.path.split("?")[0].lstrip("/")
                data = outer.files.get(key)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "image/tiff")
                self.end_headers()

            def do_GET(self) -> None:
                self._record_request("GET")
                self._apply_delay()
                if self._handle_failure():
                    return
                key = self.path.split("?")[0].lstrip("/")
                data = outer.files.get(key)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return

                total_len = len(data)
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    specs = rng[6:].strip().split(",")
                    spec = specs[0].strip()
                    parts = spec.split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if len(parts) > 1 and parts[1] else total_len - 1
                    end = min(end, total_len - 1)
                    if start > end or start >= total_len:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total_len}")
                        self.end_headers()
                        return

                    chunk = data[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total_len}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", "image/tiff")
                    self.end_headers()
                    self.wfile.write(chunk)
                    with outer._lock:
                        outer.total_bytes_sent += len(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(total_len))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", "image/tiff")
                    self.end_headers()
                    self.wfile.write(data)
                    with outer._lock:
                        outer.total_bytes_sent += len(data)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = socketserver.TCPServer(("127.0.0.1", 0), RangeHandler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def add_file(self, filename: str, content: bytes | Path) -> None:
        key = filename.lstrip("/").replace("\\", "/")
        if isinstance(content, (str, Path)):
            self.files[key] = Path(content).read_bytes()
        else:
            self.files[key] = bytes(content)

    def url_for(self, filename: str) -> str:
        key = filename.lstrip("/").replace("\\", "/")
        return f"http://127.0.0.1:{self.port}/{key}"

    def vsi_url_for(self, filename: str) -> str:
        return f"/vsicurl/{self.url_for(filename)}"

    def range_requests_count(self) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r.get("range"))

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=2.0)

    def __enter__(self) -> LocalHttpRangeServer:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


def create_zip_archive(zip_path: Path, files: dict[str, Path | bytes]) -> Path:
    """创建测试用的 ZIP 压缩归档文件。"""
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in files.items():
            if isinstance(content, (str, Path)):
                zf.write(Path(content), arcname=arcname)
            else:
                zf.writestr(arcname, content)
    return zip_path


def create_tar_archive(tar_path: Path, files: dict[str, Path | bytes]) -> Path:
    """创建测试用的 TAR 压缩归档文件。"""
    tar_path = Path(tar_path)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w:gz" if str(tar_path).endswith((".tar.gz", ".tgz")) else "w"
    with tarfile.open(tar_path, mode) as tf:
        for arcname, content in files.items():
            if isinstance(content, (str, Path)):
                tf.add(Path(content), arcname=arcname)
            else:
                import io

                data = bytes(content)
                ti = tarfile.TarInfo(name=arcname)
                ti.size = len(data)
                ti.mtime = int(time.time())
                tf.addfile(ti, io.BytesIO(data))
    return tar_path
