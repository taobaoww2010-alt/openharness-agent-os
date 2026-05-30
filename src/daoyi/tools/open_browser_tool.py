"""Tool for opening URLs in browser."""

from __future__ import annotations

import webbrowser
from typing import Any

from pydantic import BaseModel, Field

from daoyi.tools.base import BaseTool, ToolExecutionContext, ToolResult


class OpenBrowserInput(BaseModel):
    """Arguments for opening a URL in browser."""
    
    url: str = Field(description="URL to open in browser")


class OpenBrowserTool(BaseTool):
    """Open a URL in the default web browser."""
    
    name = "open_browser"
    description = "Open a URL in the default web browser. Use this when the user wants to visit a website."
    input_model = OpenBrowserInput
    
    def is_read_only(self, arguments: OpenBrowserInput) -> bool:
        del arguments
        return True
    
    async def execute(
        self,
        arguments: OpenBrowserInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        
        url = arguments.url
        
        if not url.startswith(('http://', 'https://')):
            url = f'https://{url}'
        
        try:
            webbrowser.open(url)
            return ToolResult(output=f"已在浏览器中打开：{url}", is_error=False)
        except Exception as e:
            return ToolResult(output=f"打开浏览器失败：{str(e)}", is_error=True)
