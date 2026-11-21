"""同步核心的线程隔离观测上下文。"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from gitwarden.i18n import msg

MAX_TAIL_BYTES = 128 * 1024
_CHUNK_SLICE_BYTES = 4096


class TailBuffer:
    """Bounded UTF-8 byte tail buffer with O(1) amortized appends."""

    def __init__(self, max_bytes: int = MAX_TAIL_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self.truncated = False

    def append(self, text: str) -> None:
        if not text:
            return
        data = text.encode("utf-8")
        if len(data) >= self.max_bytes:
            if len(data) > self.max_bytes or self._size > 0:
                self.truncated = True
            self._chunks.clear()
            data = data[-self.max_bytes :]
            self._size = len(data)
            for offset in range(0, len(data), _CHUNK_SLICE_BYTES):
                self._chunks.append(data[offset : offset + _CHUNK_SLICE_BYTES])
            return
        if len(data) > _CHUNK_SLICE_BYTES:
            for offset in range(0, len(data), _CHUNK_SLICE_BYTES):
                self._chunks.append(data[offset : offset + _CHUNK_SLICE_BYTES])
        else:
            self._chunks.append(data)
        self._size += len(data)
        while self._size > self.max_bytes and len(self._chunks) > 1:
            if self._size - len(self._chunks[0]) < self.max_bytes:
                break
            removed = self._chunks.popleft()
            self._size -= len(removed)
            self.truncated = True

    def __bool__(self) -> bool:
        return self._size > 0

    def text(self) -> str:
        if not self._chunks:
            return ""
        raw = b"".join(self._chunks)
        if len(raw) > self.max_bytes:
            self.truncated = True
            raw = raw[-self.max_bytes :]
        if self.truncated:
            # Drop leading UTF-8 continuation bytes left by byte-level slicing.
            idx = 0
            while idx < len(raw) and (raw[idx] & 0xC0) == 0x80:
                idx += 1
            if idx:
                raw = raw[idx:]
        return raw.decode("utf-8", errors="replace")


@dataclass
class StepRecord:
    name: str
    duration_ms: int = 0
    failed: bool = False
    error_message: str | None = None
    _buffer: TailBuffer = field(default_factory=TailBuffer)

    @property
    def raw(self) -> str:
        return self._buffer.text()

    def add_output(self, text: str) -> None:
        if text:
            self._buffer.append(text)

    def add_command_output(self, text: str) -> None:
        if not text:
            return
        current = self.raw
        if current and (
            current.endswith(text) or (self._buffer.truncated and text.endswith(current))
        ):
            return
        self._buffer.append(text)


_stack: ContextVar[tuple[StepRecord, ...]] = ContextVar("gitwarden_telemetry_stack", default=())
_collector: ContextVar[list[StepRecord] | None] = ContextVar(
    "gitwarden_telemetry_collector", default=None
)


@contextmanager
def collect() -> Iterator[list[StepRecord]]:
    records: list[StepRecord] = []
    token = _collector.set(records)
    try:
        yield records
    finally:
        _collector.reset(token)


@contextmanager
def step(name: str) -> Iterator[StepRecord]:
    record = StepRecord(name)
    token = _stack.set((*_stack.get(), record))
    started = time.monotonic()
    try:
        yield record
    finally:
        record.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        _stack.reset(token)
        records = _collector.get()
        if records is not None:
            records.append(record)


def output(text: str) -> None:
    """接收 git 流式输出，并归入当前所有嵌套步骤。"""
    for record in _stack.get():
        record.add_output(text)


def command_output(proc: object, args: list[str] | None = None) -> None:
    """把一次 git 命令的完整输出补入步骤，兼容无流式回调的命令。"""
    stdout = getattr(proc, "stdout", None) or ""
    stderr = getattr(proc, "stderr", None) or ""
    command = None
    if args:
        index = 0
        while index < len(args):
            value = args[index]
            if value in {"-c", "--config"}:
                index += 2
                continue
            if value.startswith("-"):
                index += 1
                continue
            command = value
            break
    if getattr(proc, "returncode", 0) != 0 and command in {"fetch", "push", "ls-remote"}:
        for record in _stack.get():
            record.failed = True
            record.error_message = stderr or msg("run.git_failed", command=command)
    for text in (stdout, stderr):
        if not isinstance(text, str):
            continue
        for record in _stack.get():
            record.add_command_output(text)
