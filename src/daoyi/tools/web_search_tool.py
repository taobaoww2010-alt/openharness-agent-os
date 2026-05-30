"""Web search tool using cua-driver (real Chrome, no CAPTCHAs)."""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from daoyi.tools._chrome_session import ChromeSession
from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult

_logger = logging.getLogger(__name__)


class WebSearchToolInput(BaseModel):
    """Arguments for a web search."""

    query: str = Field(description="Search query")
    max_results: int = Field(default=5, ge=1, le=20, description="Maximum number of results")


class WebSearchTool(BaseTool):
    """Run a web search using your real Chrome browser (backgrounded, no CAPTCHAs)."""

    name = "web_search"
    description = "Search the web using your real Chrome browser (backgrounded, no focus steal, no CAPTCHAs). Returns titles, URLs, and snippets."
    input_model = WebSearchToolInput

    def is_read_only(self, arguments: WebSearchToolInput) -> bool:
        del arguments
        return True

    async def execute(
        self,
        arguments: WebSearchToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            query = arguments.query

            # Try wttr.in for weather queries first (no browser needed)
            if any(kw in query for kw in ["天气", "temperature", "weather", "温度", "气温"]):
                weather = await self._try_wttrin(query)
                if weather:
                    return ToolResult(output=weather)

            # Primary: cua-driver with real Chrome
            try:
                return await self._search_via_chrome(query, arguments.max_results)
            except Exception as e:
                _logger.warning("cua-driver search failed: %s", e)

            # Fallback: Tavily API
            key = self._get_tavily_api_key()
            if key:
                try:
                    return await self._tavily_search(key, query, arguments.max_results)
                except Exception:
                    _logger.exception("Tavily search failed too")

            return ToolResult(output="web_search failed: all search methods exhausted", is_error=True)
        except Exception as e:
            _logger.exception("Unexpected error in web_search tool")
            return ToolResult(output=f"Internal error: {e}", is_error=True)

    async def _search_via_chrome(self, query: str, max_results: int) -> ToolResult:
        # Check cua-driver daemon
        try:
            out = await ChromeSession._cua(["status"])
            if "running" not in out:
                return ToolResult(
                    output="cua-driver daemon not running. Start with:\n  open -n -g -a CuaDriver --args serve",
                    is_error=True,
                )
        except Exception as e:
            return ToolResult(output=f"cua-driver check failed: {e}", is_error=True)

        # Use Baidu as the default search engine
        search_url = f"https://www.baidu.com/s?wd={quote(query)}"

        # Get or start the shared Chrome session (one window reused across calls)
        try:
            session = await ChromeSession.get()
        except Exception as e:
            return ToolResult(output=f"Failed to start Chrome session: {e}", is_error=True)

        # Navigate the shared tab to the search URL
        try:
            await session.navigate(search_url)
        except Exception as e:
            return ToolResult(output=f"Navigation to Baidu failed: {e}", is_error=True)

        # Extract structured search results via JavaScript (Baidu DOM selectors)
        js = """(() => {
  const out = [];
  for (const el of document.querySelectorAll('div.result, div.c-container')) {
    if (out.length >= %d) break;
    const a = el.querySelector('h3 > a');
    if (!a || !a.href) continue;
    const snip = el.querySelector('.c-abstract, .c-span-last, .c-row');
    out.push({
      title: (a.textContent || '').trim(),
      url: a.href,
      snippet: snip ? snip.textContent.trim() : ''
    });
  }
  return JSON.stringify(out);
})()""" % max_results

        try:
            js_out = await session.execute_js(js)
        except Exception as e:
            return ToolResult(output=f"JavaScript extraction failed: {e}", is_error=True)

        # Parse JS result from markdown code block
        raw = _extract_js_output(js_out)
        if raw:
            try:
                results = json.loads(raw)
                if results:
                    lines = [f"Search results for: {query}"]
                    for idx, r in enumerate(results, 1):
                        lines.append(f"{idx}. {r['title']}")
                        lines.append(f"   URL: {r['url']}")
                        if r.get("snippet"):
                            lines.append(f"   {r['snippet']}")
                    return ToolResult(output="\n".join(lines))
            except json.JSONDecodeError:
                pass

        # Fallback: parse from page text
        try:
            text = await session.get_text()
        except Exception as e:
            return ToolResult(output=f"Failed to read page text: {e}", is_error=True)

        results = _parse_google_text(text, max_results)
        if not results:
            return ToolResult(output="No search results found.", is_error=True)

        lines = [f"Search results for: {query}"]
        for idx, r in enumerate(results, 1):
            lines.append(f"{idx}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
        return ToolResult(output="\n".join(lines))

    @staticmethod
    def _get_tavily_api_key() -> str | None:
        key = os.environ.get("DAOYI_TAVILY_API_KEY")
        if key:
            return key
        try:
            from daoyi.config.paths import get_config_dir
            creds_path = get_config_dir() / "credentials.json"
            if creds_path.exists():
                data = json.loads(creds_path.read_text())
                return data.get("tavily", {}).get("api_key") or None
        except Exception:
            pass
        return None

    async def _tavily_search(self, api_key: str, query: str, max_results: int) -> ToolResult:
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "include_answer": False,
            "search_depth": "basic",
        }
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post("https://api.tavily.com/search", json=payload)
            if r.status_code != 200:
                return ToolResult(output=f"Tavily returned {r.status_code}", is_error=True)
            data = r.json()
        results = data.get("results", [])
        if not results:
            return ToolResult(output="No results from Tavily", is_error=True)
        lines = [f"Search results for: {query}"]
        for idx, item in enumerate(results, 1):
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            content = item.get("content", "").strip()
            lines.append(f"{idx}. {title}")
            lines.append(f"   URL: {url}")
            if content:
                lines.append(f"   {content[:300]}")
        return ToolResult(output="\n".join(lines))

    async def _try_wttrin(self, query: str) -> str | None:
        location = query
        for kw in ["天气", "temperature", "weather", "温度", "气温", "today", "今天", "当前", "现在", "多少度", "怎么样"]:
            location = location.replace(kw, "").strip()
        location = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", location).strip()
        if not location:
            location = "Beijing"
        city_map = {
            "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
            "杭州": "Hangzhou", "成都": "Chengdu", "武汉": "Wuhan", "南京": "Nanjing",
            "天津": "Tianjin", "重庆": "Chongqing", "苏州": "Suzhou", "西安": "Xi'an",
            "长沙": "Changsha", "青岛": "Qingdao", "大连": "Dalian", "厦门": "Xiamen",
            "郑州": "Zhengzhou", "沈阳": "Shenyang", "宁波": "Ningbo", "东莞": "Dongguan",
            "佛山": "Foshan", "合肥": "Hefei", "福州": "Fuzhou", "昆明": "Kunming",
            "哈尔滨": "Harbin", "济南": "Jinan", "温州": "Wenzhou", "太原": "Taiyuan",
            "贵阳": "Guiyang", "珠海": "Zhuhai", "南昌": "Nanchang", "长春": "Changchun",
            "无锡": "Wuxi", "南宁": "Nanning", "兰州": "Lanzhou", "石家庄": "Shijiazhuang",
            "呼和浩特": "Hohhot", "乌鲁木齐": "Urumqi", "西宁": "Xining", "银川": "Yinchuan",
            "拉萨": "Lhasa", "海口": "Haikou", "台北": "Taipei", "香港": "Hong Kong",
            "澳门": "Macau",
        }
        for cn, en in city_map.items():
            if cn in location:
                location = en
                break
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"https://wttr.in/{location}",
                    params={"format": "%l:+%C,+%t,+%w,+%h,+%p"},
                    headers={"User-Agent": "curl/8.0"},
                )
                if r.status_code == 200 and r.text.strip():
                    return f"Current weather: {r.text.strip()}"
        except httpx.HTTPError:
            pass
        return None


def _parse_google_text(text: str, limit: int) -> list[dict[str, str]]:
    """Parse Google search results from page text (document.body.innerText)."""
    lines = text.split("\n")

    skip = {
        "跳到主要内容", "无障碍功能帮助", "全部", "新闻", "图片", "视频",
        "短视频", "网页", "图书", "更多", "工具", "搜索结果",
        "包含站点链接的网页搜索结果", "广告", "相关搜索",
    }

    # Find URL anchors
    url_indices: list[int] = []
    for i, raw in enumerate(lines):
        l = raw.strip()
        if l.startswith(("http://", "https://")) and " " not in l[:40]:
            url_indices.append(i)

    results: list[dict[str, str]] = []
    for idx, url_idx in enumerate(url_indices):
        if len(results) >= limit:
            break
        url = lines[url_idx].strip()

        # Header: lines between previous section end and this URL
        prev_end = url_indices[idx - 1] + 1 if idx > 0 else 0
        header: list[str] = []
        for j in range(prev_end, url_idx):
            l = lines[j].strip()
            if not l or l in skip:
                continue
            header.append(l)
        title = " ".join(header)
        if not title:
            continue

        # Snippet: next few non-blank lines after URL (up to ~5 lines max)
        snippet_parts: list[str] = []
        for j in range(url_idx + 1, min(url_idx + 20, len(lines))):
            l = lines[j].strip()
            if not l or l in skip:
                if snippet_parts:  # blank after snippet → stop
                    break
                continue
            if l.startswith(("http://", "https://")):
                break
            snippet_parts.append(l)
        snippet = " ".join(snippet_parts)[:400]

        results.append({"title": title, "url": url, "snippet": snippet})

    return results


def _extract_js_output(output: str) -> str | None:
    """Extract the result from cua-driver's execute_javascript markdown output."""
    # Try markdown code-block format with optional leading whitespace
    for pat in (
        r"^## Result\s*\n\s*```\s*\n(.*?)\n\s*```",
        r"## Result\s*\n\s*```\s*\n(.*?)\n\s*```",
    ):
        m = re.search(pat, output, re.DOTALL | re.MULTILINE)
        if m:
            return m.group(1).strip()
    # Fallback: find a JSON array or object anywhere in the output
    m = re.search(r"\s*(\[.*\]|\{.*\})\s*", output, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None
