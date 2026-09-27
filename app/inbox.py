"""810 收件库：收件键提取、业务指纹计算与 SQLite 幂等存储。

收件键为 (ISA06, GS02, BIG02)。业务指纹覆盖 BIG 段全部元素、按 IT101
排序的 IT1 段（IT102 规范为千分位整数、IT104 规范为万分位整数）以及
TDS 整数分；信封控制号（ISA13/GS06/ST02/SE/GE/IEA）与 IT1 行顺序不参与。

同一收件键首次提交时，在 SQLite 中原子保存报文原文与核验结果，返回固定
receipt_id（由收件键与业务指纹确定性导出的 UUID5）；同键同指纹重发返回
原 receipt_id 与首次保存的核验结果；同键不同指纹抛 InboxConflict（409）
且不改动原记录。并发提交由进程内锁与 SQLite 唯一约束共同保证上述规则。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .verifier import Invoice

# receipt_id 的 UUID5 命名空间（固定值，同一内容永远导出同一 receipt_id）
_RECEIPT_NAMESPACE = uuid.UUID("810ed1a8-10b0-4f1c-9b01-810ed1a810ed")


@dataclass(frozen=True)
class InboxKey:
    """收件键：发送方 + 应用发送方代码 + 发票号。"""

    isa06: str   # ISA06 发送方标识（定长 15 字符，按原值参与）
    gs02: str    # GS02 应用发送方代码
    big02: str   # BIG02 发票号


class InboxConflict(Exception):
    """同一收件键已保存不同业务指纹的报文；携带已存记录的 receipt_id。"""

    def __init__(self, receipt_id: str) -> None:
        super().__init__(receipt_id)
        self.receipt_id = receipt_id


def _to_scaled_int(raw: str, scale: int) -> int:
    """把已通过核验的十进制字符串规范为 10**scale 整数。

    例：scale=3 时 "2" -> 2000、"2.5" -> 2500；调用前报文已通过核验，
    小数位不超过 scale，因此换算精确无舍入。
    """
    whole, dot, frac = raw.partition(".")
    frac = (frac + "0" * scale)[:scale] if dot else "0" * scale
    return int(whole + frac)


def extract_key_and_fingerprint(
        edi_text: str, invoice: Invoice) -> tuple[InboxKey, str]:
    """从已通过 /v1/verify 核验的报文提取收件键并计算业务指纹（SHA-256 hex）。

    前置条件：edi_text 已通过 verify_text 核验（ISA 定长、分隔符合法、外壳
    完整），此处仅做轻量切段，不重复校验。
    """
    raw = edi_text.encode("latin-1")
    element_sep = chr(raw[3])
    segment_term = chr(raw[105])

    isa_fields = raw[:105].decode("ascii").split(element_sep)
    isa06 = isa_fields[6]  # fields[0] 为段标识，ISA0n 即 fields[n]

    remainder = raw[106:].decode("ascii")
    fragments = remainder.split(segment_term)
    # 与 verifier 一致：CR/LF 仅在不充当段终止符时作为排版空白剥离
    if segment_term not in ("\r", "\n"):
        fragments = [f.strip("\r\n") for f in fragments]

    gs02: Optional[str] = None
    big_elems: Optional[list[str]] = None
    it1_elems: list[list[str]] = []
    for fragment in fragments:
        if not fragment:
            continue
        fields = fragment.split(element_sep)
        tag = fields[0]
        if tag == "GS":
            gs02 = fields[2]
        elif tag == "BIG":
            big_elems = fields[1:]
        elif tag == "IT1":
            it1_elems.append(fields[1:])

    assert gs02 is not None and big_elems is not None  # 已通过核验

    norm_lines: list[list[object]] = []
    for elems in it1_elems:
        norm: list[object] = list(elems)
        norm[1] = _to_scaled_int(elems[1], 3)   # IT102 -> 千分位整数
        norm[3] = _to_scaled_int(elems[3], 4)   # IT104 -> 万分位整数
        norm_lines.append(norm)
    norm_lines.sort(key=lambda e: e[0])  # 按 IT101 排序，行顺序不参与指纹

    payload = {
        "big": big_elems,
        "it1": norm_lines,
        "tds": invoice.total_amount,     # TDS 整数分
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return (InboxKey(isa06=isa06, gs02=gs02, big02=invoice.invoice_number),
            fingerprint)


def make_receipt_id(key: InboxKey, fingerprint: str) -> str:
    """由收件键与业务指纹确定性导出固定 receipt_id（UUID5）。"""
    canonical = json.dumps(
        {"key": [key.isa06, key.gs02, key.big02], "fp": fingerprint},
        ensure_ascii=True, separators=(",", ":"))
    return str(uuid.uuid5(_RECEIPT_NAMESPACE, canonical))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    receipt_id  TEXT PRIMARY KEY,
    isa06       TEXT NOT NULL,
    gs02        TEXT NOT NULL,
    big02       TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    edi_text    TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_key_uidx ON inbox (isa06, gs02, big02);
"""


@dataclass(frozen=True)
class InboxEntry:
    """收件库中保存的一条记录（首次提交的原文与核验结果）。"""

    receipt_id: str
    edi_text: str
    result: dict          # /v1/verify 响应结构：invoice_number/lines/total_amount


@dataclass(frozen=True)
class SubmitOutcome:
    receipt_id: str
    replayed: bool        # True 表示同键同指纹的重发
    result: dict          # 首次保存的核验结果（重发时同样返回首次内容）


class InboxStore:
    """SQLite 收件库。进程内以锁串行化写入，唯一约束兜底跨进程并发。"""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    def _select_by_key(self, key: InboxKey):
        return self._conn.execute(
            "SELECT receipt_id, fingerprint, result_json FROM inbox "
            "WHERE isa06 = ? AND gs02 = ? AND big02 = ?",
            (key.isa06, key.gs02, key.big02),
        ).fetchone()

    def submit(self, *, key: InboxKey, fingerprint: str, receipt_id: str,
               edi_text: str, result: dict) -> SubmitOutcome:
        """原子落地首次提交；同键同指纹返回原记录，同键不同指纹抛 InboxConflict。"""
        result_json = json.dumps(result, ensure_ascii=False)
        with self._lock:
            row = self._select_by_key(key)
            if row is None:
                try:
                    with self._conn:  # 单条 INSERT 事务，失败自动回滚
                        self._conn.execute(
                            "INSERT INTO inbox (receipt_id, isa06, gs02, big02,"
                            " fingerprint, edi_text, result_json, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (receipt_id, key.isa06, key.gs02, key.big02,
                             fingerprint, edi_text, result_json,
                             datetime.now(timezone.utc).isoformat()),
                        )
                except sqlite3.IntegrityError:
                    # 其他连接抢先写入同一收件键：按已存记录判定
                    row = self._select_by_key(key)
                else:
                    return SubmitOutcome(receipt_id, False, result)
            if row[1] == fingerprint:
                return SubmitOutcome(row[0], True, json.loads(row[2]))
            raise InboxConflict(row[0])

    def get(self, receipt_id: str) -> Optional[InboxEntry]:
        """按 receipt_id 取首次保存的原文与核验结果；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT edi_text, result_json FROM inbox WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        return InboxEntry(receipt_id, row[0], json.loads(row[1]))
