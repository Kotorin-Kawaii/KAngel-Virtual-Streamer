"""Token 审计接口的响应模型。

刻意用宽松模型（`extra` 默认忽略、字段可选），因为花费字段在未配价目表时是
`None`，而聚合列在没有数据的日子是 0——前端按存在性判断即可。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TokenDayRow(BaseModel):
    """某一自然日的总量与折算金额；未配价的部分单独计入 unpriced_tokens。"""

    day: str
    calls: int = 0
    failed_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_missing_calls: int = 0
    total_tokens: int = 0
    latency_ms_sum: int = 0
    cost_amount: float = 0.0
    unpriced_tokens: int = 0
    fully_priced: bool = True


class TokenDailyTotals(BaseModel):
    calls: int = 0
    failed_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_missing_calls: int = 0
    total_tokens: int = 0
    latency_ms_sum: int = 0
    cost_amount: float = 0.0
    unpriced_tokens: int = 0
    distinct_models: int = 0


class TokenDailyResponse(BaseModel):
    start_day: str
    end_day: str
    timezone: str
    currency: Optional[str] = None
    pricing_configured: bool = False
    days: List[TokenDayRow] = Field(default_factory=list)
    totals: TokenDailyTotals


class TokenGroupRow(BaseModel):
    """role / provider / model 三种视图共用一种行结构，key 是分组值。"""

    key: str
    calls: int = 0
    failed_calls: int = 0
    usage_missing_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_missing_calls: int = 0
    total_tokens: int = 0
    latency_ms_sum: int = 0
    avg_latency_ms: int = 0
    cost_amount: float = 0.0
    unpriced_tokens: int = 0
    fully_priced: bool = True


class TokenBreakdownResponse(BaseModel):
    start_day: str
    end_day: str
    currency: Optional[str] = None
    by_role: List[TokenGroupRow] = Field(default_factory=list)
    by_provider: List[TokenGroupRow] = Field(default_factory=list)
    by_model: List[TokenGroupRow] = Field(default_factory=list)


class TokenRecordRow(BaseModel):
    """逐次调用明细：只有元数据与计数，没有正文、账号或 IP。"""

    record_id: str
    day: str
    created_at: str
    role: str
    provider: str
    model: str
    status: str
    usage_reported: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: Optional[int] = None
    reasoning_tokens_reported: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    error_kind: Optional[str] = None
    cost_amount: Optional[float] = None
    priced: bool = False


class TokenRecordsResponse(BaseModel):
    records: List[TokenRecordRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 100
    offset: int = 0
    currency: Optional[str] = None
    detail_enabled: bool = True
    detail_retention_days: int = 14


class TokenAuditStatsResponse(BaseModel):
    recorder: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    pricing: Dict[str, Any] = Field(default_factory=dict)
    timezone: str
    today: str


__all__ = [
    "TokenDayRow", "TokenDailyTotals", "TokenDailyResponse",
    "TokenGroupRow", "TokenBreakdownResponse",
    "TokenRecordRow", "TokenRecordsResponse", "TokenAuditStatsResponse",
]
