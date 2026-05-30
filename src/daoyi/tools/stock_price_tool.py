"""Stock price lookup tool (A-share market via Sina API)."""

from __future__ import annotations

import logging
import re

import httpx
from pydantic import BaseModel, Field

from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult

_logger = logging.getLogger(__name__)


class StockPriceToolInput(BaseModel):
    code: str = Field(description="Stock code, e.g. 600410 or sh600410 or 000001")


class StockPriceTool(BaseTool):
    """Get real-time A-share stock price from Sina Finance API."""

    name = "stock_price"
    description = "Get real-time stock price for A-share stocks (沪深两市). Returns current price, open, high, low, previous close, change."
    input_model = StockPriceToolInput

    def is_read_only(self, arguments: StockPriceToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: StockPriceToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        try:
            raw = arguments.code.strip()
            # Normalize code: strip exchange prefix, determine exchange
            code, exchange = self._parse_code(raw)
            url = f"http://hq.sinajs.cn/list={exchange}{code}"
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(url, headers={"Referer": "https://finance.sina.com.cn"})
                r.raise_for_status()
            text = r.text.strip()
            if not text or "=" not in text:
                return ToolResult(output=f"No data for stock code: {raw}", is_error=True)
            # Parse: var hq_str_sh600410="field1,field2,...";
            m = re.search(r'"([^"]+)"', text)
            if not m:
                return ToolResult(output=f"Unexpected response format: {text[:200]}", is_error=True)
            fields = m.group(1).split(",")
            return self._format_result(code, exchange, fields)
        except httpx.HTTPError as e:
            return ToolResult(output=f"Failed to fetch stock price: {e}", is_error=True)
        except Exception as e:
            _logger.exception("stock_price error")
            return ToolResult(output=f"Internal error: {e}", is_error=True)

    @staticmethod
    def _parse_code(raw: str) -> tuple[str, str]:
        raw = raw.upper().strip()
        if raw.startswith("SH"):
            return raw[2:].strip(), "sh"
        if raw.startswith("SZ"):
            return raw[2:].strip(), "sz"
        if raw.startswith("SH.") or raw.startswith("SH,"):
            return raw[3:].strip(), "sh"
        if raw.startswith("SZ.") or raw.startswith("SZ,"):
            return raw[3:].strip(), "sz"
        if raw.endswith(".SH") or raw.endswith(".SS"):
            return raw[:-3].strip(), "sh"
        if raw.endswith(".SZ"):
            return raw[:-3].strip(), "sz"
        # Plain code: 6xxxxx → Shanghai, 0xxxxx/3xxxxx → Shenzhen
        if re.match(r"^\d{6}$", raw):
            if raw.startswith(("6", "9")):
                return raw, "sh"
            return raw, "sz"
        return raw, "sh"

    @staticmethod
    def _format_result(code: str, exchange: str, fields: list[str]) -> ToolResult:
        name = fields[0] if len(fields) > 0 else ""
        try:
            open_price = float(fields[1]) if fields[1] else 0.0
        except (ValueError, IndexError):
            open_price = 0.0
        try:
            prev_close = float(fields[2]) if fields[2] else 0.0
        except (ValueError, IndexError):
            prev_close = 0.0
        try:
            current = float(fields[3]) if fields[3] else 0.0
        except (ValueError, IndexError):
            current = 0.0
        try:
            high = float(fields[4]) if fields[4] else 0.0
        except (ValueError, IndexError):
            high = 0.0
        try:
            low = float(fields[5]) if fields[5] else 0.0
        except (ValueError, IndexError):
            low = 0.0
        date = fields[30] if len(fields) > 30 else ""
        time_str = fields[31] if len(fields) > 31 else ""

        change = current - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        prefix = "sh" if exchange == "sh" else "sz"
        output = (
            f"{name} ({prefix.upper()}{code})\n"
            f"日期: {date} {time_str}\n"
            f"当前价: {current:.2f}\n"
            f"涨跌幅: {change:+.2f} ({change_pct:+.2f}%)\n"
            f"开盘: {open_price:.2f}\n"
            f"最高: {high:.2f}\n"
            f"最低: {low:.2f}\n"
            f"昨收: {prev_close:.2f}"
        )
        return ToolResult(output=output)
