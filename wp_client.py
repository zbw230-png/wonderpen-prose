"""妙笔（WonderPen）本地 API 客户端。

妙笔桌面版 v3.1.3+ 在本机提供 MCP 服务（streamableHttp），
本模块通过它读写文档库：列出文档库、列文档树、读文档、建文档。

官方文档: https://docs.wonderpen.app/zh/guides/api/
仅使用 Python 标准库。
"""

import json
import urllib.error
import urllib.request


class WonderPenError(Exception):
    """妙笔 API 相关错误，message 为面向用户的可读信息。"""


class WonderPenClient:
    def __init__(self, port: int = 8022, token: str = ""):
        self._url = f"http://127.0.0.1:{port}/mcp"
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------ MCP 基础

    def _call(self, tool: str, arguments: dict, timeout: float = 20):
        """调用一个 MCP 工具并返回其数据（自动解包 content[0].text 的双层 JSON）。"""
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=payload, headers=self._headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise WonderPenError(
                    "鉴权失败：API Token 不正确。请到 妙笔 设置 → 高级 → API 处核对 Token。"
                ) from e
            raise WonderPenError(f"妙笔 API 返回 HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise WonderPenError(
                "连不上妙笔：请确认妙笔已打开，且已在 设置 → 高级 → API 中开启服务。"
            ) from e
        except TimeoutError:
            raise WonderPenError("妙笔 API 响应超时，请重试。") from None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise WonderPenError(f"妙笔 API 返回了无法解析的内容：{raw[:200]}") from None

        if "error" in data:
            raise WonderPenError(f"妙笔 API 报错：{data['error'].get('message', data['error'])}")

        result = data.get("result", {})
        content = result.get("content", [])
        if not content or content[0].get("type") != "text":
            raise WonderPenError(f"妙笔 API 返回了意外的结构：{raw[:200]}")
        text = content[0]["text"]
        # 工具数据本身是 JSON 字符串，需要二次解析；失败则原样返回文本
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    # ------------------------------------------------------------------ 工具封装

    def list_libraries(self) -> list[dict]:
        """列出可访问的文档库，每项含 libKey / title / type。"""
        data = self._call("list_libraries", {})
        if isinstance(data, list):
            return data
        raise WonderPenError("list_libraries 返回结构异常")

    def list_items(self, lib_key: str = None) -> dict:
        """列出文档树（嵌套结构），节点含 id / title / rendered_title / children。"""
        args = {"type": "doc"}
        if lib_key:
            args["libKey"] = lib_key
        data = self._call("list_items", args)
        if isinstance(data, dict) and "items" in data:
            return data
        raise WonderPenError("list_items 返回结构异常")

    def get_item(self, item_id: str, lib_key: str = None, fmt: str = "markdown") -> dict:
        """读取单个文档，返回含 title / renderedTitle / content（Markdown）。"""
        args = {"itemId": item_id, "format": fmt}
        if lib_key:
            args["libKey"] = lib_key
        data = self._call("get_item", args)
        if isinstance(data, dict):
            return data
        raise WonderPenError("get_item 返回结构异常")

    def create_item(
        self,
        title: str,
        content: str,
        fmt: str = "markdown",
        related_item_id: str = None,
        where: str = "after",
        lib_key: str = None,
    ) -> dict:
        """在文档库中新建文档。

        related_item_id + where 决定位置：
        "in" = 作为其子文档；"after"/"before" = 作为其前/后的同级文档。
        不传 related_item_id 则加在顶层。
        """
        args = {"type": "doc", "title": title, "content": content, "format": fmt}
        if related_item_id:
            args["relatedItemId"] = related_item_id
            args["where"] = where
        if lib_key:
            args["libKey"] = lib_key
        data = self._call("create_item", args)
        if isinstance(data, dict):
            return data
        raise WonderPenError("create_item 返回结构异常")

    def trash_item(self, item_id: str, lib_key: str = None) -> dict:
        """把文档移入回收站（软删除，可恢复）。"""
        args = {"itemId": item_id}
        if lib_key:
            args["libKey"] = lib_key
        data = self._call("trash_item", args)
        if isinstance(data, dict):
            return data
        raise WonderPenError("trash_item 返回结构异常")
