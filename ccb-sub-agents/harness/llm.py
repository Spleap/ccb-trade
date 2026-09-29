"""LLMClient 抽象：provider 可换，业务代码不绑死（ARCHITECTURE §10.1）。

消息格式直接用 OpenAI 那一套（system / user / assistant.tool_calls / tool）——
它是事实标准，把抽象定在"协议"而不是"厂商 SDK"上，换 provider 只需换 client。

四条纪律（§3.5）
----------------
1. **失败必须抛 `LLMError`，不能返回一句"我做不到"。** 上层要据此降级本 tick，
   而不是把一个失败的回复当成正常判断去执行。
2. **不重试模型输出不合法** —— 那由 loop 的 ⑤ 拒绝后带着原因重试一次，
   位置不同、语义不同（§3.5 "可重试与不可重试分开"）。
3. **不在这里做任何交易判断。** 本模块只负责"把对话发出去、把回复拿回来"。
4. **但连接层要重试。** 断网、超时、流式中途被掐断都只是传输问题，
   原样重发一次多半就好了；把这种偶发故障直接判成"本 tick 降级"太亏 ——
   长回测里它是 degraded 的最大来源。**只重试传输类异常**（见 `_retryable`），
   我们自己的 `LLMError`（缺 key / 剧本用尽 / 离线）一律不重试。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMError(RuntimeError):
    """调用失败 / 超时 / 缺 key。上层一律降级为"本 tick 什么都不做"。"""


# ============================================================
# 连接层重试的判据
# ============================================================
#
# 哪些异常值得原样重发一次：**网络抖动 / 服务端临时故障**。
# 判据是「异常类型名 + HTTP 状态码」，不是消息文本 —— 文本里出现 "error"
# 之类的词太容易误伤，把"参数写错了"也当成可重试，白等三倍时间。
#
# 必须在这一层兜的原因：SDK 自己的 `max_retries` 只覆盖**建连阶段**，
# 而**流式接收中途被掐断**（`RemoteProtocolError: peer closed connection...`）
# 不在其列 —— 那正是长回测里最常撞的一种。
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_RETRYABLE_NAMES = (
    "Connection",        # APIConnectionError / ConnectError …
    "Timeout",           # APITimeoutError / ReadTimeout / ConnectTimeout …
    "ProtocolError",     # httpx.RemoteProtocolError（流式断连）
    "ReadError",         # httpx.ReadError
    "WriteError",
    "SSL",
    "RateLimit",         # 429
    "InternalServer",    # 500
    "ServiceUnavailable",  # 503
    "ServerError",
    "Temporarily",
    "overloaded",
)


def _retryable(exc: Exception) -> bool:
    """这个异常值不值得原样重发一次？"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUS:
        return True
    name = type(exc).__name__
    return any(hint.lower() in name.lower() for hint in _RETRYABLE_NAMES)


# ============================================================
# 数据结构
# ============================================================


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None

    def as_message(self) -> dict:
        """转成对话里的 assistant 消息（回灌给下一轮）。"""
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in self.tool_calls
            ]
        return msg


def tool_message(call_id: str, name: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}


class LLMClient(Protocol):
    """只要实现 `chat` 就能当 LLM 用。

    `on_text` 给定时走**流式**：模型每吐出一小段文字就回调一次。
    它只影响"看得见"，不影响拿到的结果 —— 关掉它行为完全一样。
    """

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0,
             on_text: Callable[[str], None] | None = None) -> LLMReply: ...


# ============================================================
# OpenAI 兼容实现（也覆盖 DeepSeek / Moonshot / 本地 vLLM 等）
# ============================================================


def _load_dotenv(env_file: str | None = None) -> None:
    """把 `.env` 灌进 `os.environ`（dotenv 是可选的，没装就跳过）。

    **必须在读 key 之前调用。** 否则"key 只写在 .env 里"这种最常见的用法
    会因为"读 key 时 .env 还没加载"而被判成"没有 key"。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(env_file) if env_file else load_dotenv()


class OpenAIClient:
    """走官方 `openai` SDK。

    通过 `base_url` 复用同一份代码接所有 OpenAI 兼容端点（README 里那串
    `*_API_KEY` 大多能用这种方式接上）。key 从环境变量读，**不落任何配置文件**。

    两个"重试"不是一回事，别混：
    - `max_retries` 交给 SDK，管建连阶段；
    - `retry_attempts` 是本层自己做的，**管整个调用（含流式接收）的中途断连**。
    """

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 60.0, max_retries: int = 2, env_file: str | None = None,
                 retry_attempts: int = 3, retry_backoff: float = 1.5):
        _load_dotenv(env_file)

        self.model = model
        self._retry_attempts = max(0, int(retry_attempts))     # 失败后的**额外**重试次数
        self._retry_backoff = max(0.0, float(retry_backoff))   # 首次退避秒数，之后翻倍
        key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        if not key:
            raise LLMError("没有可用的 OPENAI_API_KEY（缺 key 不重试，重试也没用）")

        try:
            from openai import OpenAI
        except ImportError as exc:                # pragma: no cover - 环境问题
            raise LLMError(f"未安装 openai SDK：{exc}") from exc

        self._client = OpenAI(api_key=key,
                              base_url=base_url or os.environ.get("OPENAI_BASE_URL") or None,
                              timeout=timeout, max_retries=max_retries)

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0,
             on_text: Callable[[str], None] | None = None) -> LLMReply:
        payload: dict[str, Any] = {"model": self.model, "messages": messages,
                                   "temperature": temperature}
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]

        attempt = 0
        while True:
            try:
                if on_text is None:
                    return _parse(self._client.chat.completions.create(**payload))
                return self._consume(payload, on_text)
            except LLMError:
                # 我们自己的错误（缺 key / 未知 provider / 上层主动抛的）：重试没意义
                raise
            except Exception as exc:
                # 非传输类错误（参数不合法、模型不存在…）也不重试 —— 重发一次结果一样
                if attempt >= self._retry_attempts or not _retryable(exc):
                    raise LLMError(f"{type(exc).__name__}: {exc}") from exc
                # 断的是连接，不是判断。**原样重发**，别改 payload。
                if on_text is not None:      # 有人看着就说一声，否则静默重试
                    try:
                        on_text(f"\n（连接中断（{type(exc).__name__}），"
                                f"正在重试 {attempt + 1}/{self._retry_attempts}…）\n")
                    except Exception:
                        pass
                time.sleep(self._retry_backoff * (2 ** attempt))
                attempt += 1

    def _consume(self, payload: dict, on_text: Callable[[str], None]) -> LLMReply:
        """流式接收：边收边把文字丢给 `on_text`，最后拼回和一次性调用一样的 `LLMReply`。

        工具调用在流式里是**碎片**（name 一次、arguments 分很多次），
        所以按 `index` 攒起来再拼；`_loads` 负责兜住半截 JSON。
        """
        stream = self._client.chat.completions.create(**payload, stream=True)
        texts: list[str] = []
        slots: dict[int, dict] = {}

        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta

            piece = getattr(delta, "content", None)
            if piece:
                texts.append(piece)
                try:
                    on_text(piece)              # 打印失败不该影响决策
                except Exception:
                    pass

            for call in (getattr(delta, "tool_calls", None) or []):
                slot = slots.setdefault(getattr(call, "index", 0) or 0,
                                        {"id": "", "name": "", "args": ""})
                if getattr(call, "id", None):
                    slot["id"] = call.id
                fn = getattr(call, "function", None)
                if fn is None:
                    continue
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments

        calls = [ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"],
                          arguments=_loads(slot["args"]))
                 for i, slot in sorted(slots.items())]
        return LLMReply(content="".join(texts), tool_calls=calls, raw=None)


def _parse(resp) -> LLMReply:
    """把一次性的（非流式）回复翻成 `LLMReply`。和 `_consume` 的出口保持同形。"""
    choice = resp.choices[0].message
    calls: list[ToolCall] = []
    for c in (getattr(choice, "tool_calls", None) or []):
        calls.append(ToolCall(id=c.id, name=c.function.name,
                              arguments=_loads(c.function.arguments)))
    return LLMReply(content=choice.content or "", tool_calls=calls, raw=resp)


def _loads(raw: str | None) -> dict:
    """模型偶尔会给出空串或半截 JSON —— 转成一个"没参数"的调用比抛异常好，
    因为参数缺失会由工具自己报错并被记进 inputs_summary（绝不静默失败）。"""
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


# ============================================================
# 剧本实现：给测试用
# ============================================================


class ScriptedClient:
    """按剧本返回回复，**不联网、完全确定**。

    它不只是测试替身 —— "同样的输入跑两遍结果一致"这条可测试性前提，
    靠的就是把 LLM 的回复固定住。
    """

    def __init__(self, replies: list[LLMReply | str] | None = None):
        self._replies = list(replies or [])
        self.seen: list[list[dict]] = []          # 每次调用收到的 messages（留痕）

    def push(self, *replies: LLMReply | str) -> "ScriptedClient":
        self._replies.extend(replies)
        return self

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0,
             on_text: Callable[[str], None] | None = None) -> LLMReply:
        self.seen.append(list(messages))
        if not self._replies:
            raise LLMError("剧本用尽：本 tick 视为 LLM 不可用")
        nxt = self._replies.pop(0)
        reply = nxt if isinstance(nxt, LLMReply) else LLMReply(content=nxt)
        if on_text and reply.content:
            on_text(reply.content)
        return reply


def reply(text: str = "", *calls: tuple[str, dict]) -> LLMReply:
    """写剧本用的小糖：`reply("看多", ("get_candles", {...}))`。"""
    return LLMReply(content=text,
                    tool_calls=[ToolCall(id=f"call_{i}_{n}", name=n, arguments=a)
                                for i, (n, a) in enumerate(calls)])


class NullClient:
    """永远不可用。预检 / 无 key 时用。

    它让每个醒来的 Agent 走一遍**降级路径**，从而证明 §3.5 那句
    "LLM 挂掉 = 什么都不做，而这是安全的" —— 止损照跑、账本照记，只是不下新单。
    """

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.0,
             on_text: Callable[[str], None] | None = None) -> LLMReply:
        raise LLMError("LLM 离线（NullClient）：本 tick 降级")


# ============================================================
# provider 工厂：按名字挑一家，读对应的环境变量
# ============================================================

# 全都是 OpenAI 兼容端点，所以实现只有一个 `OpenAIClient`，差异只在 base_url / key / 默认模型。
PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "key_env": ("DEEPSEEK_API_KEY",),
        "model": "deepseek-chat",          # 支持 function calling
    },
    "openai": {
        "base_url": None,                  # 用 SDK 默认
        "key_env": ("OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY"),
        "model": "gpt-4o-mini",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ("OPENROUTER_API_KEY",),
        "model": "openai/gpt-4o-mini",
    },
}


def build_client(provider: str | None = None, model: str | None = None,
                 **kwargs) -> LLMClient:
    """按 provider 名造一个 client（§10.1：业务代码不绑死厂商）。

    provider 取 `参数 > CCB_LLM_PROVIDER > "deepseek"`；key 只从环境变量读，
    **不落任何配置文件**。缺 key 抛 `LLMError` —— 上层据此降级为"本 tick 不动"。
    """
    name = (provider or os.environ.get("CCB_LLM_PROVIDER") or "deepseek").lower()
    cfg = PROVIDERS.get(name)
    if cfg is None:
        raise LLMError(f"未知的 LLM provider {name!r}（可选：{', '.join(PROVIDERS)}）")

    _load_dotenv()          # 必须在读 key 之前：key 可能只写在 .env 里
    key = next((os.environ.get(env) for env in cfg["key_env"] if os.environ.get(env)), None)
    if not key and not kwargs.get("api_key"):
        raise LLMError(
            f"provider {name!r} 没有可用的 key（请设置 {'/'.join(cfg['key_env'])}）")
    return OpenAIClient(model=model or cfg["model"], api_key=key,
                        base_url=cfg["base_url"], **kwargs)
