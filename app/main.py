"""X12 810 发票核验、采购单核销、997 功能回执生成与 997 状态解析 HTTP API。

另提供幂等收件库：POST /v1/inbox 接收单份 810，核验通过后按收件键
（ISA06/GS02/BIG02）与业务指纹原子入库，重发返回原 receipt_id；
GET /v1/inbox/{receipt_id} 返回首次保存的原文与核验结果。
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .ack_status import parse_ack_status
from .acknowledger import acknowledge
from .inbox import (
    InboxConflict,
    InboxStore,
    extract_key_and_fingerprint,
    make_receipt_id,
)
from .reconciler import (
    MAX_EDI_TEXTS,
    OrderError,
    milli_to_str,
    reconcile,
    validate_order_lines,
)
from .verifier import Invoice, X12Error, verify_text

app = FastAPI(
    title="X12 810 Invoice Verification API",
    version="1.4.0",
    description="核验 ASC X12 004010 单份 810 发票报文的外壳结构与发票金额，"
                "按采购单对多份发票逐张核销，对单份 810 生成 997 功能回执，"
                "按发送清单解析 997 回执中各交易集的接受状态，"
                "并将单份 810 按收件键幂等收入收件库。",
)

# 收件库 SQLite 路径：容器内经 INBOX_DB_PATH 指向持久卷，本地默认当前目录
inbox_store = InboxStore(os.environ.get("INBOX_DB_PATH", "inbox.db"))


class VerifyRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")


class LineOut(BaseModel):
    line_id: str = Field(..., description="IT101 行号")
    quantity: str = Field(..., description="IT102 数量（原文）")
    unit_price: str = Field(..., description="IT104 单价（原文）")
    line_amount: int = Field(..., description="行金额，整数分（ROUND_HALF_UP）")


class VerifyResponse(BaseModel):
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


@app.exception_handler(X12Error)
async def x12_error_handler(_request, exc: X12Error) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "seg": exc.seg,
                "segment": exc.segment,
            }
        },
    )


@app.exception_handler(OrderError)
async def order_error_handler(_request, exc: OrderError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.post(
    "/v1/verify",
    response_model=VerifyResponse,
    summary="核验一份 X12 810 发票报文",
)
async def verify_invoice(req: VerifyRequest) -> VerifyResponse:
    invoice = verify_text(req.edi_text)
    return VerifyResponse(
        invoice_number=invoice.invoice_number,
        lines=[
            LineOut(
                line_id=line.line_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                line_amount=line.line_amount,
            )
            for line in invoice.lines
        ],
        total_amount=invoice.total_amount,
    )


# ---------- POST /v1/reconcile ----------


class OrderLineInModel(BaseModel):
    line_id: str = Field(..., description="采购单行号，须唯一")
    ordered_quantity: str = Field(
        ..., description="订购数量（十进制字符串，正，至多 3 位小数）")
    unit_price: str = Field(
        ..., description="单价（十进制字符串，非负，至多 4 位小数）")


class ReconcileRequest(BaseModel):
    order_lines: list[OrderLineInModel] = Field(..., description="采购单行")
    edi_texts: list[str] = Field(
        ..., min_length=1, max_length=MAX_EDI_TEXTS,
        description=f"按输入顺序核销的 1～{MAX_EDI_TEXTS} 份 X12 810 报文")


class RejectOut(BaseModel):
    code: str = Field(..., description="拒绝原因错误码（核验失败时为原核验错误码）")
    message: str
    seg: Optional[int] = Field(None, description="原核验错误的段序号（仅核验失败时）")
    segment: Optional[str] = Field(None, description="原核验错误的段标识（仅核验失败时）")
    line_id: Optional[str] = Field(None, description="触发拒绝的采购单行号（核销失败时）")


class InvoiceResultOut(BaseModel):
    index: int = Field(..., description="edi_texts 中的输入序号（从 0 开始）")
    accepted: bool
    invoice_number: Optional[str] = Field(
        None, description="BIG02 发票号；核验失败时为 null")
    error: Optional[RejectOut] = None


class RemainingOut(BaseModel):
    line_id: str = Field(..., description="采购单行号")
    remaining_quantity: str = Field(..., description="剩余数量（十进制字符串）")


class ReconcileResponse(BaseModel):
    results: list[InvoiceResultOut] = Field(..., description="逐张核销结果")
    remaining: list[RemainingOut] = Field(
        ..., description="各采购单行剩余数量（按采购单顺序）")


@app.post(
    "/v1/reconcile",
    response_model=ReconcileResponse,
    summary="按采购单对多份 X12 810 发票逐张核销",
)
async def reconcile_invoices(req: ReconcileRequest) -> ReconcileResponse:
    # 采购单非法 -> 整单 422
    order_lines = validate_order_lines(req.order_lines)
    outcome = reconcile(order_lines, req.edi_texts)
    return ReconcileResponse(
        results=[
            InvoiceResultOut(
                index=r.index,
                accepted=r.accepted,
                invoice_number=r.invoice_number,
                error=RejectOut(
                    code=r.error.code,
                    message=r.error.message,
                    seg=r.error.seg,
                    segment=r.error.segment,
                    line_id=r.error.line_id,
                ) if r.error is not None else None,
            )
            for r in outcome.results
        ],
        remaining=[
            RemainingOut(line_id=line_id,
                         remaining_quantity=milli_to_str(milli))
            for line_id, milli in outcome.remaining
        ],
    )


# ---------- POST /v1/acknowledge ----------


class AcknowledgeRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")
    isa13: str = Field(..., description="回执 ISA13 交换控制号（恰好 9 位数字）")
    gs06: str = Field(..., description="回执 GS06 组控制号（非空，不含分隔符）")
    st02: str = Field(..., description="回执 ST02 交易集控制号（非空，不含分隔符）")


class AcknowledgeResponse(BaseModel):
    edi_text: str = Field(..., description="ASC X12 004010 997 功能回执报文")


@app.post(
    "/v1/acknowledge",
    response_model=AcknowledgeResponse,
    summary="对单份 X12 810 报文生成 997 功能回执",
)
async def acknowledge_invoice(req: AcknowledgeRequest) -> AcknowledgeResponse:
    return AcknowledgeResponse(
        edi_text=acknowledge(req.edi_text, req.isa13, req.gs06, req.st02))


# ---------- POST /v1/ack-status ----------


class AckStatusRequest(BaseModel):
    gs06: str = Field(..., description="发送清单的组控制号（非空）")
    st02: list[str] = Field(
        ..., min_length=1, max_length=100,
        description="发送清单的 1～100 个唯一非空交易集控制号")
    edi_text: str = Field(
        ..., description="完整 ASC X12 004010 997 功能回执报文（ASCII，≤64 KiB）")


class AckStatusItemOut(BaseModel):
    st02: str = Field(..., description="发送清单中的交易集控制号")
    status: Literal["A", "R", "U"] = Field(
        ..., description="A=接受（AK5=A 或未列项接受数恰好覆盖全部未列项）；"
                         "R=拒绝（AK5=R 或未列项无接受数）；"
                         "U=未知（未列项，剩余接受数介于两者之间）")


class AckStatusResponse(BaseModel):
    items: list[AckStatusItemOut] = Field(
        ..., description="按发送清单顺序排列的各项状态")


@app.post(
    "/v1/ack-status",
    response_model=AckStatusResponse,
    summary="按发送清单解析 997 功能回执中各 810 交易集的接受状态",
)
async def acknowledge_status(req: AckStatusRequest) -> AckStatusResponse:
    items = parse_ack_status(req.edi_text, req.gs06, req.st02)
    return AckStatusResponse(
        items=[AckStatusItemOut(st02=item.st02, status=item.status)
               for item in items])


# ---------- POST /v1/inbox、GET /v1/inbox/{receipt_id} ----------


class InboxRequest(BaseModel):
    edi_text: str = Field(..., description="完整 X12 810 报文（ASCII，≤64 KiB）")


class InboxResponse(BaseModel):
    receipt_id: str = Field(
        ..., description="收件回执标识（由收件键与业务指纹确定性导出，固定不变）")
    replayed: bool = Field(
        ..., description="false=首次提交；true=同键同指纹重发，返回首次保存的记录")
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


class InboxEntryResponse(BaseModel):
    receipt_id: str = Field(..., description="收件回执标识")
    edi_text: str = Field(..., description="首次保存的报文原文")
    invoice_number: str = Field(..., description="BIG02 发票号")
    lines: list[LineOut]
    total_amount: int = Field(..., description="TDS01 总额，整数分")


class InboxNotFound(Exception):
    """receipt_id 不在收件库中。"""

    def __init__(self, receipt_id: str) -> None:
        super().__init__(receipt_id)
        self.receipt_id = receipt_id


@app.exception_handler(InboxConflict)
async def inbox_conflict_handler(_request, exc: InboxConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "INBOX_KEY_CONFLICT",
                "message": "同一收件键（ISA06/GS02/BIG02）已保存业务内容不同的报文，"
                           "原记录未改动",
                "receipt_id": exc.receipt_id,
            }
        },
    )


@app.exception_handler(InboxNotFound)
async def inbox_not_found_handler(_request, exc: InboxNotFound) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "INBOX_NOT_FOUND",
                "message": f"receipt_id 不存在: {exc.receipt_id}",
            }
        },
    )


def _verify_result(invoice: Invoice) -> dict:
    """核验结果的可 JSON 化结构（与 /v1/verify 响应一致），用于入库与回放。"""
    return {
        "invoice_number": invoice.invoice_number,
        "lines": [
            {
                "line_id": line.line_id,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "line_amount": line.line_amount,
            }
            for line in invoice.lines
        ],
        "total_amount": invoice.total_amount,
    }


@app.post(
    "/v1/inbox",
    response_model=InboxResponse,
    summary="接收单份 X12 810 报文并按收件键幂等入库",
)
def submit_to_inbox(req: InboxRequest) -> InboxResponse:
    # 核验失败 -> 原样 422，且不写入收件库
    invoice = verify_text(req.edi_text)
    key, fingerprint = extract_key_and_fingerprint(req.edi_text, invoice)
    receipt_id = make_receipt_id(key, fingerprint)
    outcome = inbox_store.submit(
        key=key,
        fingerprint=fingerprint,
        receipt_id=receipt_id,
        edi_text=req.edi_text,
        result=_verify_result(invoice),
    )
    return InboxResponse(
        receipt_id=outcome.receipt_id,
        replayed=outcome.replayed,
        **outcome.result,
    )


@app.get(
    "/v1/inbox/{receipt_id}",
    response_model=InboxEntryResponse,
    summary="按 receipt_id 查询首次保存的报文原文与核验结果",
)
def get_inbox_entry(receipt_id: str) -> InboxEntryResponse:
    entry = inbox_store.get(receipt_id)
    if entry is None:
        raise InboxNotFound(receipt_id)
    return InboxEntryResponse(
        receipt_id=entry.receipt_id, edi_text=entry.edi_text, **entry.result)


@app.get("/health", summary="存活探针")
async def health() -> dict:
    return {"status": "ok"}
