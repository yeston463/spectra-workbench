"""P1-02 验收：分包、粘包、中文、非法消息、超限转文件引用、世代守卫。"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from spectra_workbench.protocol.messages import (
    MAX_CONTROL_MESSAGE_BYTES,
    Envelope,
    GenerationGuard,
    JsonlStreamParser,
    Kind,
    ProtocolError,
    encode_message,
    new_message,
)

pytestmark = pytest.mark.unit

WORKER = "sw-gdal-0"


def _log(text: str) -> bytes:
    return encode_message(
        new_message(Kind.log, WORKER, {"level": "info", "message": text}, generation=3)
    )


def test_single_complete_line():
    p = JsonlStreamParser()
    got = p.feed(_log("hello"))
    assert len(got) == 1 and got[0].payload["message"] == "hello"
    assert got[0].generation == 3


def test_sticky_packets_two_messages_in_one_read():
    p = JsonlStreamParser()
    data = _log("a") + _log("b") + _log("c")
    got = p.feed(data)
    assert [m.payload["message"] for m in got] == ["a", "b", "c"]


def test_partial_line_is_buffered_not_parsed_prematurely():
    data = _log("半行消息")
    p = JsonlStreamParser()
    assert p.feed(data[:-5]) == []
    got = p.feed(data[-5:])
    assert len(got) == 1 and got[0].payload["message"] == "半行消息"


def test_utf8_multibyte_split_across_chunks():
    """中文按字节切在字符中间也必须还原，不能出现替换符或崩溃。"""
    data = _log("栅格重投影与波段堆叠")
    p = JsonlStreamParser()
    out = []
    for i in range(0, len(data), 3):  # 每 3 字节喂一次，必然截断多字节字符
        out.extend(p.feed(data[i : i + 3]))
    out.extend(p.close())
    assert len(out) == 1
    assert out[0].payload["message"] == "栅格重投影与波段堆叠"


def test_last_line_without_newline_is_flushed_on_close():
    data = _log("no trailing newline")[:-1]
    p = JsonlStreamParser()
    assert p.feed(data) == []
    got = p.close()
    assert len(got) == 1 and got[0].payload["message"] == "no trailing newline"


def test_blank_lines_ignored():
    p = JsonlStreamParser()
    got = p.feed(b"\n\n" + _log("x") + b"\n")
    assert len(got) == 1 and got[0].payload["message"] == "x"


def test_invalid_json_line_raises_with_raw_preview():
    p = JsonlStreamParser()
    with pytest.raises(ProtocolError) as ei:
        p.feed(b"{not json}\n")
    assert "JSON" in ei.value.reason
    assert ei.value.raw and "not json" in str(ei.value.raw)


def test_json_scalar_line_rejected():
    p = JsonlStreamParser()
    with pytest.raises(ProtocolError):
        p.feed(b"123\n")


def test_unknown_kind_rejected():
    p = JsonlStreamParser()
    bad = (
        json.dumps(
            {
                "protocol_version": "1.0",
                "kind": "telepathy",
                "message_id": "m",
                "request_id": None,
                "task_id": None,
                "worker_id": WORKER,
                "generation": 0,
                "payload": {},
            }
        ).encode()
        + b"\n"
    )
    with pytest.raises(ProtocolError) as ei:
        p.feed(bad)
    assert "telepathy" in ei.value.reason


def test_unsupported_protocol_version_rejected():
    p = JsonlStreamParser()
    bad = (
        json.dumps(
            {
                "protocol_version": "9.9",
                "kind": "heartbeat",
                "message_id": "m",
                "request_id": None,
                "task_id": None,
                "worker_id": WORKER,
                "generation": 0,
                "payload": {"monotonic_seconds": 1.0},
            }
        ).encode()
        + b"\n"
    )
    with pytest.raises(ProtocolError):
        p.feed(bad)


def test_missing_envelope_field_is_reported():
    p = JsonlStreamParser()
    bad = (
        json.dumps(
            {
                "protocol_version": "1.0",
                "kind": "log",
                "message_id": "m",
                "request_id": None,
                "task_id": None,
                "generation": 0,
                "payload": {"level": "info", "message": "x"},
            }
        ).encode()
        + b"\n"
    )
    with pytest.raises(ProtocolError) as ei:
        p.feed(bad)
    assert "worker_id" in ei.value.reason


def test_kind_specific_payload_requirements():
    with pytest.raises(ValueError):
        new_message(Kind.hello, WORKER, {"provider_id": "gdal"})  # 缺 worker_package_version
    env = new_message(
        Kind.hello, WORKER, {"provider_id": "gdal", "worker_package_version": "0.0.1"}
    )
    assert env.kind is Kind.hello


def test_oversized_control_message_spills_to_file(tmp_path: Path):
    big = "x" * (MAX_CONTROL_MESSAGE_BYTES + 1024)
    env = new_message(Kind.log, WORKER, {"level": "info", "message": big})
    line = encode_message(env, spill_dir=tmp_path / "spill")
    assert len(line) < MAX_CONTROL_MESSAGE_BYTES
    p = JsonlStreamParser()
    got = p.feed(line)
    assert len(got) == 1
    art = got[0]
    assert art.kind is Kind.artifact
    assert art.payload["original_kind"] == "log"
    spilled = Path(url2pathname(urlparse(art.payload["uri"]).path))
    assert spilled.exists(), spilled
    restored = json.loads(spilled.read_text(encoding="utf-8"))
    assert restored["payload"]["message"] == big
    assert restored["message_id"] == env.message_id


def test_oversized_without_spill_dir_is_an_error(tmp_path: Path):
    env = new_message(
        Kind.log, WORKER, {"level": "info", "message": "y" * (MAX_CONTROL_MESSAGE_BYTES + 1)}
    )
    with pytest.raises(ProtocolError):
        encode_message(env)


def test_buffer_cap_prevents_unbounded_memory():
    p = JsonlStreamParser(max_buffer_bytes=1024)
    with pytest.raises(ProtocolError):
        p.feed(b"a" * 2048)


def test_stderr_like_text_never_enters_parser():
    """worker 的 stderr 必须走独立通道；混进 stdout 的普通日志要报错而不是被当成消息。"""
    p = JsonlStreamParser()
    with pytest.raises(ProtocolError):
        p.feed(b"Traceback (most recent call last):\n")


def test_generation_guard_rejects_stale_responses():
    guard = GenerationGuard(current_generation=1)
    fresh = new_message(Kind.heartbeat, WORKER, {"monotonic_seconds": 1.0}, generation=1)
    stale = new_message(Kind.heartbeat, WORKER, {"monotonic_seconds": 2.0}, generation=0)
    assert guard.accept(fresh) is True
    assert guard.accept(stale) is False
    assert guard.rejected_stale == 1
    assert guard.restart() == 2
    assert guard.accept(fresh) is False  # 旧世代 1 现在也算过期


def test_round_trip_keeps_chinese_and_paths(tmp_path: Path):
    env = new_message(
        Kind.request,
        WORKER,
        {
            "action": "gdal.translate",
            "arguments": {"input": "C:/数据/影像 1.tif", "bands": [1, 4, 7]},
        },
        request_id="r-9",
        task_id="t-9",
        generation=7,
    )
    p = JsonlStreamParser()
    got = p.feed(encode_message(env))
    assert got[0].payload["arguments"]["input"] == "C:/数据/影像 1.tif"
    assert got[0].request_id == "r-9" and got[0].generation == 7


def test_envelope_forbids_extra_fields():
    with pytest.raises(ValueError):
        Envelope.model_validate(
            {
                "protocol_version": "1.0",
                "kind": "heartbeat",
                "message_id": "m",
                "request_id": None,
                "task_id": None,
                "worker_id": WORKER,
                "generation": 0,
                "payload": {"monotonic_seconds": 0.1},
                "extra": 1,
            }
        )
