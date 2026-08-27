"""智谱 GLM API 客户端（OpenAI 兼容接口，仅标准库）。

API Key 申请: https://open.bigmodel.cn/
接口文档: https://docs.bigmodel.cn/api/reference/模型-api/对话补全
"""

import json
import urllib.error
import urllib.request


class GLMError(Exception):
    """GLM API 相关错误，message 为面向用户的可读信息。"""


class GLMClient:
    def __init__(
        self,
        api_key: str,
        model: str = "glm-5.2",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        timeout: float = 300,
    ):
        if not api_key:
            raise GLMError("还没有填写智谱 API Key，请先在「设置」里填写。")
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout

    def chat(self, system: str, user: str, temperature: float = 0.7, max_tokens: int = 8192) -> str:
        """发送一次对话补全，返回模型生成的文本。"""
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code in (401, 403):
                raise GLMError("智谱 API Key 无效或没有权限，请核对后重新填写。")
            msg = _extract_error(body)
            raise GLMError(f"智谱 API 返回 HTTP {e.code}：{msg or body[:200]}")
        except urllib.error.URLError as e:
            raise GLMError(f"请求智谱 API 失败（网络问题）：{e.reason}") from e
        except TimeoutError:
            raise GLMError("智谱 API 响应超时，随笔可能太长，请重试。") from None

        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise GLMError(f"智谱 API 返回了意外的内容：{raw[:200]}") from None


def _extract_error(body: str) -> str:
    """从错误响应体里尽量提取出 message 字段。"""
    try:
        data = json.loads(body)
        err = data.get("error") or data
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or "")
    except json.JSONDecodeError:
        pass
    return ""
