"""POST /v1/inbox 与 GET /v1/inbox/{receipt_id} 端到端及存储测试。"""

from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.inbox import (
    InboxConflict,
    InboxStore,
    extract_key_and_fingerprint,
    make_receipt_id,
)
from app.main import app
from app.verifier import verify_text


# ---------- 报文构造工具 ----------

def _cents(qty: str, price: str) -> int:
    return int((Decimal(qty) * Decimal(price)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _isa(isa06: str, isa13: str) -> str:
    fields = [
        "00", "          ",  # ISA01/02
        "00", "          ",  # ISA03/04
        "ZZ", isa06.ljust(15),          # ISA05/06（定长 15 字符）
        "ZZ", "RECEIVER1234567",        # ISA07/08
        "000830", "1200",     # ISA09/10
        "U", "00401",         # ISA11/12
        isa13,                # ISA13
        "0", "P", ":",
    ]
    out = "ISA*" + "*".join(fields)
    assert len(out) == 105, len(out)
    return out + "~"


def make_edi(*, isa06="SENDER123456789", gs02="SENDERGS",
             big01="20200830", big02="INV-1",
             lines=(("1", "10", "19.99"), ("2", "3", "4.25")),
             tds=None, isa13="000000001", gs06="42", st02="0001"):
    """组装一份可通过核验的 810；未显式给出的 tds 取各行整数分之和。"""
    if tds is None:
        tds = str(sum(_cents(qty, price) for _lid, qty, price in lines))
    segs = [
        f"GS*IN*{gs02}*RECVGS*20200830*1200*{gs06}*X*004010",
        f"ST*810*{st02}",
        f"BIG*{big01}*{big02}",
        *[f"IT1*{lid}*{qty}*EA*{price}" for lid, qty, price in lines],
        f"TDS*{tds}",
    ]
    segs.append(f"SE*{len(segs)}*{st02}")  # SE01 = ST..SE 段数
    segs.append(f"GE*1*{gs06}")
    segs.append(f"IEA*1*{isa13}")
    return _isa(isa06, isa13) + "~".join(segs) + "~"


# ---------- 测试装置 ----------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """把 app.main 的收件库替换为临时目录中的全新 SQLite。"""
    s = InboxStore(str(tmp_path / "inbox.db"))
    monkeypatch.setattr(main_module, "inbox_store", s)
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(app)


def post_inbox(client, edi):
    return client.post("/v1/inbox", json={"edi_text": edi})


def _submit(store, edi):
    """与端点一致的提交流程（存储层并发测试用）。"""
    invoice = verify_text(edi)
    key, fingerprint = extract_key_and_fingerprint(edi, invoice)
    receipt_id = make_receipt_id(key, fingerprint)
    return store.submit(key=key, fingerprint=fingerprint,
                        receipt_id=receipt_id, edi_text=edi, result={})


# ---------- 首次提交与重发 ----------

def test_first_submit(client):
    r = post_inbox(client, make_edi())
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is False
    assert body["receipt_id"]
    assert body["invoice_number"] == "INV-1"
    assert body["lines"] == [
        {"line_id": "1", "quantity": "10", "unit_price": "19.99",
         "line_amount": 19990},
        {"line_id": "2", "quantity": "3", "unit_price": "4.25",
         "line_amount": 1275},
    ]
    assert body["total_amount"] == 21265


def test_replay_same_message(client):
    edi = make_edi()
    first = post_inbox(client, edi).json()
    second = post_inbox(client, edi).json()
    assert second["replayed"] is True
    assert second["receipt_id"] == first["receipt_id"]
    assert second["lines"] == first["lines"]


def test_replay_ignores_envelope_line_order_and_number_format(client):
    """信封控制号、IT1 行顺序、IT102/IT104/TDS 的等价写法不参与指纹。"""
    edi_a = make_edi()
    edi_b = make_edi(
        lines=(("2", "3.00", "4.2500"), ("1", "10.0", "19.990")),  # 逆序+等价数值
        tds="212.65",                       # 显式小数，等价于 21265 分
        isa13="000000009", gs06="99", st02="7777",  # 信封控制号全换
    )
    first = post_inbox(client, edi_a).json()
    second = post_inbox(client, edi_b)
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["receipt_id"] == first["receipt_id"]
    # 返回首次保存的核验结果（原始数量写法 "10"/"3"，而非 "10.0"/"3.00"）
    assert body["lines"] == first["lines"]
    # GET 同样返回首次保存的原文
    got = client.get(f"/v1/inbox/{first['receipt_id']}").json()
    assert got["edi_text"] == edi_a


def test_receipt_id_is_deterministic(client, store):
    """同一内容在任何时刻（包括清空重算）都导出同一 receipt_id。"""
    edi = make_edi()
    invoice = verify_text(edi)
    key, fingerprint = extract_key_and_fingerprint(edi, invoice)
    expected = make_receipt_id(key, fingerprint)
    assert post_inbox(client, edi).json()["receipt_id"] == expected


# ---------- 收件键 ----------

def test_key_components_each_define_distinct_inbox(client):
    base = post_inbox(client, make_edi()).json()
    # ISA06 不同 -> 新收件键
    assert post_inbox(client, make_edi(
        isa06="OTHERSENDER123")).json()["replayed"] is False
    # GS02 不同 -> 新收件键
    assert post_inbox(client, make_edi(
        gs02="OTHERGS")).json()["replayed"] is False
    # BIG02 不同 -> 新收件键
    assert post_inbox(client, make_edi(
        big02="INV-2")).json()["replayed"] is False
    # 三者的 receipt_id 互不相同，也不同于 base
    ids = {base["receipt_id"]}
    for kwargs in ({"isa06": "OTHERSENDER123"}, {"gs02": "OTHERGS"},
                   {"big02": "INV-2"}):
        ids.add(post_inbox(client, make_edi(**kwargs)).json()["receipt_id"])
    assert len(ids) == 4


# ---------- 同键不同指纹 -> 409 ----------

def test_conflict_on_different_quantity(client):
    first = post_inbox(client, make_edi()).json()
    r = post_inbox(client, make_edi(lines=(("1", "11", "19.99"),
                                          ("2", "3", "4.25"))))
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "INBOX_KEY_CONFLICT"
    assert body["error"]["receipt_id"] == first["receipt_id"]
    # 原记录未改动：重发原报文仍为 replay，GET 仍为原文
    assert post_inbox(client, make_edi()).json()["replayed"] is True
    assert client.get(f"/v1/inbox/{first['receipt_id']}").json()[
        "edi_text"] == make_edi()


def test_conflict_on_different_big01(client):
    """BIG 段整体参与指纹：仅改 BIG01 也构成不同业务内容。"""
    post_inbox(client, make_edi())
    r = post_inbox(client, make_edi(big01="20200901"))
    assert r.status_code == 409


def test_conflict_on_different_tds(client):
    post_inbox(client, make_edi())
    # 行不变、总额不同的另一份合法报文（另配一组行使 TDS 自洽）
    r = post_inbox(client, make_edi(lines=(("1", "10", "19.99"),)))
    assert r.status_code == 409


# ---------- 核验失败：原样 422 且不写入 ----------

def test_verify_failure_returns_original_422_and_not_stored(client):
    bad = make_edi(tds="1")  # TDS_MISMATCH
    r_inbox = post_inbox(client, bad)
    r_verify = client.post("/v1/verify", json={"edi_text": bad})
    assert r_inbox.status_code == 422
    assert r_inbox.json() == r_verify.json()
    # 未写入收件库：同键合法报文随后仍按首次提交处理
    assert post_inbox(client, make_edi()).json()["replayed"] is False


def test_non_ascii_failure_not_stored(client):
    r = post_inbox(client, make_edi() + "é")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NOT_ASCII"
    assert post_inbox(client, make_edi()).json()["replayed"] is False


# ---------- GET /v1/inbox/{receipt_id} ----------

def test_get_returns_first_saved_text_and_result(client):
    edi = make_edi()
    first = post_inbox(client, edi).json()
    r = client.get(f"/v1/inbox/{first['receipt_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["receipt_id"] == first["receipt_id"]
    assert body["edi_text"] == edi
    assert body["invoice_number"] == "INV-1"
    assert body["lines"] == first["lines"]
    assert body["total_amount"] == 21265


def test_get_unknown_receipt_id(client):
    r = client.get("/v1/inbox/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "INBOX_NOT_FOUND"


# ---------- 持久化与并发 ----------

def test_persistence_across_restart(tmp_path, monkeypatch):
    """模拟容器重启：重新打开同一 SQLite 文件后仍能识别重发。"""
    path = str(tmp_path / "inbox.db")
    edi = make_edi()

    store1 = InboxStore(path)
    monkeypatch.setattr(main_module, "inbox_store", store1)
    first = post_inbox(TestClient(app), edi).json()
    assert first["replayed"] is False
    store1.close()

    store2 = InboxStore(path)  # “重启”后重新打开同一数据库
    monkeypatch.setattr(main_module, "inbox_store", store2)
    try:
        second = post_inbox(TestClient(app), edi).json()
        assert second["replayed"] is True
        assert second["receipt_id"] == first["receipt_id"]
        got = TestClient(app).get(f"/v1/inbox/{first['receipt_id']}")
        assert got.status_code == 200
        assert got.json()["edi_text"] == edi
    finally:
        store2.close()


def test_concurrent_same_message(store):
    """并发提交同一报文：恰好一个首次，其余全部重发同一 receipt_id。"""
    edi = make_edi()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: _submit(store, edi), range(32)))
    assert sum(1 for o in outcomes if not o.replayed) == 1
    assert len({o.receipt_id for o in outcomes}) == 1


def test_concurrent_conflicting_fingerprints(store):
    """并发提交同键不同指纹：恰好一种指纹入库，另一种全部冲突。"""
    edi_a = make_edi()
    edi_b = make_edi(lines=(("1", "11", "19.99"), ("2", "3", "4.25")))
    edis = [edi_a, edi_b] * 16

    def work(edi):
        try:
            out = _submit(store, edi)
            return ("ok", out.receipt_id, out.replayed)
        except InboxConflict:
            return ("conflict", None, None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, edis))

    ok = [r for r in results if r[0] == "ok"]
    conflict = [r for r in results if r[0] == "conflict"]
    assert len(ok) == 16 and len(conflict) == 16
    # 胜出指纹的 16 个提交共享同一 receipt_id，且恰好一个为首次
    assert len({r[1] for r in ok}) == 1
    assert sum(1 for r in ok if not r[2]) == 1
