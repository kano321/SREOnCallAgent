import os
import random
import time
from openai import OpenAI
from dotenv import load_dotenv
from typing import List, Dict, Any

load_dotenv()


class LLMError(RuntimeError):
    """Provider error after retries are exhausted or retries are not allowed."""

    def __init__(self, info: Dict[str, str], original: Exception = None):
        self.info = info
        self.category = info.get("category", "unknown")
        self.original = original
        message = info.get("message", "大模型请求失败")
        hint = info.get("hint", "")
        super().__init__(f"{message} ({self.category})" + (f"：{hint}" if hint else ""))


def classify_llm_error(exc: Exception) -> Dict[str, str]:
    """Map a provider/network exception to a short Chinese label plus a hint.

    Lets the UI tell apart bad config, bad key, empty balance, rate limits,
    wrong model names, network failures and provider-side outages.
    """
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    text = f"{type(exc).__name__}: {exc}".lower()

    if status in (401, 403) or "invalid api key" in text or "unauthorized" in text \
            or "authentication" in text or "api key" in text and "invalid" in text:
        return {"category": "auth_error", "message": "API Key 无效或无权限",
                "hint": "检查 .env 中 LLM_API_KEY 是否正确、是否已过期"}
    if status == 402 or "insufficient balance" in text or "insufficient quota" in text \
            or "no credit" in text or "quota exceeded" in text or "余额" in text:
        return {"category": "insufficient_balance", "message": "账户余额不足",
                "hint": "前往模型服务商控制台充值，或更换有余额的 API Key"}
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return {"category": "rate_limit", "message": "请求被限流",
                "hint": "稍后重试；若持续出现，检查账户余额、配额或并发量"}
    if status == 404 or "model not found" in text or "model_not_found" in text \
            or "does not exist" in text:
        return {"category": "model_not_found", "message": "模型名不存在或无权限",
                "hint": "检查 .env 中 LLM_MODEL_ID 是否为当前账号可用的模型"}
    if status is not None and 500 <= status < 600:
        return {"category": "server_error", "message": "模型服务端错误",
                "hint": "模型服务商暂时异常，稍后重试"}
    if "timeout" in text or "timed out" in text or "connection" in text \
            or "connect" in text or "network" in text or "dns" in text:
        return {"category": "network_error", "message": "网络不通或连接超时",
                "hint": "检查本机网络/代理，以及 LLM_BASE_URL 是否可访问"}
    if "session" in text and "missing" in text:
        return {"category": "gateway_session", "message": "网关要求额外会话头",
                "hint": "该 LLM_BASE_URL 是专用网关，建议改用标准 OpenAI 兼容接口"}
    return {"category": "unknown", "message": "未知错误", "hint": "查看下方原始错误信息"}


class HelloAgentsLLM:
    """OpenAI-compatible LLM client (works with AIHubmix, ModelScope, OpenAI)."""

    def __init__(
        self,
        model: str = None,
        api_key: str = None,
        base_url: str = None,
        timeout: int = None,
        verbose: bool = True,
    ):
        self.model = model or os.getenv("LLM_MODEL_ID")
        api_key = api_key or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        timeout = timeout or int(os.getenv("LLM_TIMEOUT", "60"))
        self.timeout = timeout
        self.max_retries = max(0, int(os.getenv("LLM_MAX_RETRIES", "2")))
        self.retry_base_delay = max(
            0.0,
            float(os.getenv("LLM_RETRY_BASE_SECONDS", "0.5")),
        )
        self.request_budget = max(
            1.0,
            float(os.getenv("LLM_REQUEST_BUDGET_SECONDS", "120")),
        )
        self.verbose = verbose
        self.base_url = base_url

        if not all([self.model, api_key, base_url]):
            raise ValueError(
                "LLM_MODEL_ID, LLM_API_KEY, and LLM_BASE_URL must be set "
                "(via constructor args or .env file)."
            )

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def check(self, timeout: int = 45) -> Dict[str, Any]:
        """Verify the model works using the same call shape as think().

        Uses a streaming request (exactly like the production pipeline, whose
        think() streams tokens) so the diagnostic never disagrees with real
        usage. No max_tokens cap: reasoning models spend tokens on
        reasoning_content first, and a small cap would truncate the answer to
        empty. Never raises: returns a diagnostic dict instead.
        """
        started = time.time()
        try:
            response = self.client.with_options(timeout=timeout).chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "Reply with exactly: OK"}],
                temperature=0,
                stream=True,
            )
            collected = []
            for chunk in response:
                if not chunk.choices:
                    continue
                collected.append(getattr(chunk.choices[0].delta, "content", None) or "")
            reply = "".join(collected).strip()
            latency = int((time.time() - started) * 1000)
            if reply:
                return {
                    "ok": True,
                    "latency_ms": latency,
                    "reply": reply[:100],
                    "error": None,
                    "category": "ok",
                    "message": "连接正常",
                    "hint": "",
                }
            return {
                "ok": False,
                "latency_ms": latency,
                "reply": "",
                "error": "stream finished without any content",
                "category": "empty_response",
                "message": "模型返回空内容",
                "hint": "模型可能只输出了推理内容，或响应被截断；可更换模型或检查参数",
            }
        except Exception as e:
            latency = int((time.time() - started) * 1000)
            info = classify_llm_error(e)
            return {
                "ok": False,
                "latency_ms": latency,
                "reply": "",
                "error": f"{type(e).__name__}: {e}",
                **info,
            }

    def _create_stream(self, messages: List[Dict[str, str]], temperature: float, timeout: float):
        client = self.client.with_options(timeout=timeout)
        return client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            stream=True,
        )

    def think(self, messages: List[Dict[str, str]], temperature: float = 0) -> str:
        started = time.monotonic()
        last_error = None
        failure_info = None

        for attempt in range(self.max_retries + 1):
            remaining = self.request_budget - (time.monotonic() - started)
            if remaining <= 0:
                failure_info = {
                    "category": "budget_exhausted",
                    "message": "大模型请求总预算已耗尽",
                    "hint": "请检查上游延迟，或适当提高 LLM_REQUEST_BUDGET_SECONDS",
                }
                break

            if self.verbose:
                print(f"🧠 Calling {self.model}... (attempt {attempt + 1})")
            try:
                response = self._create_stream(
                    messages,
                    temperature,
                    min(self.timeout, remaining),
                )
                collected = []
                for chunk in response:
                    content = chunk.choices[0].delta.content or ""
                    if self.verbose:
                        print(content, end="", flush=True)
                    collected.append(content)
                if self.verbose:
                    print()

                reply = "".join(collected).strip()
                if reply:
                    return reply
                failure_info = {
                    "category": "empty_response",
                    "message": "模型返回空内容",
                    "hint": "稍后重试；若持续出现，请更换模型或检查接口参数",
                }
            except Exception as exc:
                last_error = exc
                failure_info = classify_llm_error(exc)
                if self.verbose:
                    print(f"❌ LLM API error: {exc}")

            retryable = failure_info and failure_info["category"] in {
                "rate_limit",
                "server_error",
                "network_error",
                "empty_response",
            }
            if not retryable or attempt >= self.max_retries:
                break

            remaining = self.request_budget - (time.monotonic() - started)
            if remaining <= 0:
                break
            delay = min(
                self.retry_base_delay * (2 ** attempt)
                + random.uniform(0, self.retry_base_delay * 0.25),
                remaining,
            )
            if delay > 0:
                time.sleep(delay)

        info = failure_info or {
            "category": "unknown",
            "message": "大模型请求失败",
            "hint": "检查模型配置和服务状态",
        }
        raise LLMError(info, last_error)
