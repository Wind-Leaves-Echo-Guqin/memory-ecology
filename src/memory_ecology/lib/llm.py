"""LLM 调用公共函数（memory-ecology lib）。

complete() 纯函数：默认走 deepseek API（生产现行为：urllib + .env 密钥），
client 可注入（测试用 fake）。不做类层次——13 脚本中真调 LLM 的仅 3-5 个，
不值得客户端框架。周熔断逻辑留各脚本（成本护栏属业务配置）。
"""
import json
import urllib.request

from . import config

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_TIMEOUT = 90


def complete(prompt: str, *, client=None, model: str = DEFAULT_MODEL,
             base_url: str = DEFAULT_BASE_URL, api_key: str | None = None,
             max_tokens: int = 800, temperature: float = 0.1,
             timeout: int = DEFAULT_TIMEOUT) -> str:
    """单轮 LLM 补全，返回文本内容。client 可注入（测试 fake 签名：
    client(prompt, model=..., max_tokens=..., temperature=...) -> str）。"""
    if client is not None:
        return client(prompt, model=model, max_tokens=max_tokens, temperature=temperature)
    key = api_key or config.env_key("DEEPSEEK_API_KEY")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"]
