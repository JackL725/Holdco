"""Client for the local model server (Ollama native API, or any OpenAI-compatible server).

Messages use a neutral format:
  {"role": "system|user|assistant|tool", "content": str,
   "tool_calls": [{"id": str, "name": str, "arguments": dict}],   # assistant only
   "tool_call_id": str, "name": str}                               # tool only
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import db

# Local model servers must never go through a system proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_json_mode_models: set[str] = set()   # models that rejected native tool calling
_no_think_models: set[str] = set()    # models (or Ollama versions) that rejected the think parameter

_bg_lock = threading.Lock()
_bg_sem: threading.BoundedSemaphore | None = None
_bg_size = 0
_chat_sem = threading.BoundedSemaphore(1)  # owner chat never waits behind background work queues


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = ""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0
    mode: str = "native"
    thinking: str = ""


def background_slot() -> threading.BoundedSemaphore:
    """Semaphore limiting how many agents use the GPU at once."""
    global _bg_sem, _bg_size
    size = max(1, int(db.get_settings().get("max_concurrent_runs", 2)))
    with _bg_lock:
        if _bg_sem is None or size != _bg_size:
            _bg_sem = threading.BoundedSemaphore(size)
            _bg_size = size
        return _bg_sem


def chat_slot() -> threading.BoundedSemaphore:
    return _chat_sem


# ---------------- HTTP ----------------

def _request(url: str, body: dict | None, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:800]
        raise LLMError(f"HTTP {e.code} from model server: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"Can't reach the model server at {url} ({e.reason}). Is Ollama running?") from None
    except TimeoutError:
        raise LLMError(f"The model server took longer than {timeout}s to reply.") from None
    except (http.client.HTTPException, ConnectionError, OSError) as e:
        raise LLMError(f"The model server dropped the connection ({type(e).__name__}: {e}).") from None
    except ValueError as e:
        raise LLMError(f"Bad response from model server: {e}") from None


def _openai_base(base: str) -> str:
    base = base.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _headers(s: dict) -> dict:
    return {"Authorization": f"Bearer {s['llm_api_key']}"} if s.get("llm_api_key") else {}


def list_models(settings: dict | None = None) -> list[str]:
    s = settings or db.get_settings()
    base = s["llm_base_url"].rstrip("/")
    if s["llm_backend"] == "ollama":
        data = _request(base + "/api/tags", None, _headers(s), 10)
        return sorted(m.get("name", "") for m in data.get("models", []))
    data = _request(_openai_base(base) + "/models", None, _headers(s), 10)
    return sorted(m.get("id", "") for m in data.get("data", []))


def loaded_models(settings: dict | None = None) -> list[dict]:
    """Models Ollama has loaded right now (GET /api/ps): [{"name", "size", "size_vram"}]. A model whose
    size_vram is below its size is partly in system RAM, which makes it several times slower. Other backends
    do not report this, so they return an empty list."""
    s = settings or db.get_settings()
    if s["llm_backend"] != "ollama":
        return []
    data = _request(s["llm_base_url"].rstrip("/") + "/api/ps", None, _headers(s), 10)
    return [{"name": m.get("name") or m.get("model") or "", "size": int(m.get("size") or 0),
             "size_vram": int(m.get("size_vram") or 0)} for m in data.get("models", [])]


def tool_ping(model: str | None = None, settings: dict | None = None) -> tuple[bool, str]:
    """Ask the model to call one tiny tool. (ok, message): ok is False only when the server failed; the message
    says whether the model really called the tool ("can call tools") or only answered in prose."""
    s = settings or db.get_settings()
    model = model or s["default_model"]
    try:
        t = chat([{"role": "system", "content": "Reply briefly."},
                  {"role": "user", "content": "Call the ping tool with message 'ok'."}],
                 tools=[{"name": "ping", "description": "Reply to a ping",
                         "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                                        "required": ["message"]}}], model=model, settings=s)
    except LLMError as e:
        return False, str(e)
    if t.tool_calls:
        return True, f"{model} is working and can call tools ({t.mode} mode)."
    return True, (f"{model} replied, but didn't call the test tool. Agents may struggle; "
                  "a model with stronger tool use (e.g. gpt-oss:20b) is recommended.")


# ---------------- format conversion ----------------

def _tool_schema(t: dict) -> dict:
    return {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                             "parameters": t["parameters"]}}


def _to_ollama(messages: list[dict], keep_thinking: bool = False) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            msg = {"role": "assistant", "content": m.get("content") or "",
                   "tool_calls": [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                  for tc in m["tool_calls"]]}
            # gpt-oss reasons best when it sees its own earlier reasoning between tool calls of one session.
            if keep_thinking and m.get("thinking"):
                msg["thinking"] = m["thinking"]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _to_openai(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"id": tc.get("id") or f"call_{i}", "type": "function",
                                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                                       for i, tc in enumerate(m["tool_calls"])]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id") or "call_0", "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


def _json_mode_instructions(tools: list[dict]) -> str:
    lines = ["", "# How to act",
             "You act by calling tools. To call a tool, reply with exactly one JSON object and nothing else:",
             '{"tool": "<tool name>", "args": {<arguments>}}',
             "After each call you will receive the result. Available tools:"]
    for t in tools:
        props = t["parameters"].get("properties", {})
        req = set(t["parameters"].get("required", []))
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'string')}" for k, v in props.items())
        lines.append(f"- {t['name']}({args}): {t['description']}")
    return "\n".join(lines)


def _to_json_mode(messages: list[dict], tools: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "system":
            out.append({"role": "system", "content": (m.get("content") or "") + _json_mode_instructions(tools)})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            tc = m["tool_calls"][0]
            out.append({"role": "assistant", "content": json.dumps({"tool": tc["name"], "args": tc["arguments"]})})
        elif m["role"] == "tool":
            out.append({"role": "user", "content": f"Result of {m.get('name', 'tool')}:\n{m['content']}"})
        else:
            out.append({"role": m["role"], "content": m.get("content") or ""})
    return out


# ---------------- parsing ----------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


def split_thinking(text: str) -> tuple[str, str]:
    """Separate a model's <think> block from its answer. Returns (answer, thoughts)."""
    text = text or ""
    thoughts = [m.strip() for m in re.findall(r"<think>(.*?)</think>", text, re.S | re.I)]
    text = _THINK_RE.sub("", text)
    if "<think>" in text:  # unterminated: everything after the tag is still thinking
        head, _, tail = text.partition("<think>")
        thoughts.append(tail.strip())
        text = head
    elif "</think>" in text:  # the chat template already opened the tag
        head, _, tail = text.rpartition("</think>")
        thoughts.append(head.strip())
        text = tail
    return text.strip(), "\n\n".join(t for t in thoughts if t)


def strip_thinking(text: str) -> str:
    return split_thinking(text)[0]


def extract_json(text: str):
    """Find the first JSON object/array in text (handles ```json fences)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for cand in candidates:
        cand = cand.strip()
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            while start != -1:
                depth, in_str, esc = 0, False, False
                for i in range(start, len(cand)):
                    ch = cand[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    elif ch == '"':
                        in_str = True
                    elif ch == opener:
                        depth += 1
                    elif ch == closer:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(cand[start:i + 1])
                            except ValueError:
                                break
                start = cand.find(opener, start + 1)
    return None


def _text_tool_call(text: str, tool_names: set[str]) -> ToolCall | None:
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return None
    name = obj.get("tool") or obj.get("name") or obj.get("function")
    args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
    if isinstance(args, str):
        args = extract_json(args) or {}
    if isinstance(name, str) and name in tool_names and isinstance(args, dict):
        return ToolCall(name=name, arguments=args)
    return None


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except ValueError:
            v = extract_json(raw)
            return v if isinstance(v, dict) else {}
    return {}


# ---------------- sampling ----------------

def is_gpt_oss(model: str) -> bool:
    return "gpt-oss" in (model or "").lower()


CREATIVITY_TEMPS = {"precise": 0.3, "balanced": 0.6, "creative": 0.8}


def sampling_for(model: str, creativity: str, s: dict) -> dict:
    """Sampling settings. In auto mode, follow each model maker's published recommendation."""
    if s.get("sampling", "auto") == "manual":
        return {"temperature": float(s.get("temperature", 0.4))}
    m = (model or "").lower()
    if is_gpt_oss(m):            # OpenAI's gpt-oss model card: temperature 1.0, top_p 1.0
        return {"temperature": 1.0, "top_p": 1.0}
    if "qwen3" in m:             # Qwen3 model card: thinking 0.6/0.95/20, non-thinking 0.7/0.8/20; never greedy
        if "instruct" in m or creativity == "creative":
            return {"temperature": 0.7, "top_p": 0.8, "top_k": 20}
        return {"temperature": 0.6, "top_p": 0.95, "top_k": 20}
    return {"temperature": CREATIVITY_TEMPS.get(creativity, 0.6)}


# ---------------- main entry ----------------

def chat(messages: list[dict], tools: list[dict] | None = None, model: str | None = None,
         settings: dict | None = None, json_output: bool = False, temperature: float | None = None,
         think: str | None = None, creativity: str = "balanced") -> Reply:
    """think: reasoning effort for gpt-oss ("low", "medium", "high"); ignored by other models."""
    s = settings or db.get_settings()
    model = model or s["default_model"]
    tools = tools or []
    names = {t["name"] for t in tools}
    mode = s.get("tool_mode", "auto")
    use_json_mode = bool(tools) and (mode == "json" or (mode == "auto" and model in _json_mode_models))
    samp = sampling_for(model, creativity, s)
    if temperature is not None:
        samp["temperature"] = temperature
    if think not in ("low", "medium", "high") or not is_gpt_oss(model) or model in _no_think_models:
        think = None
    try:
        return _call_with_parse_retry(s, model, messages, tools, names, use_json_mode, json_output, samp, think)
    except LLMError as e:
        msg = str(e).lower()
        if think and "think" in msg:
            _no_think_models.add(model)
            return chat(messages, tools, model, settings, json_output, temperature, None, creativity)
        if (tools and not use_json_mode and mode == "auto" and not is_tool_parse_error(e)
                and ("support tools" in msg or "tool" in msg and "400" in msg)):
            _json_mode_models.add(model)
            return _call(s, model, messages, tools, names, True, json_output, samp, think)
        raise


PARSE_RETRIES = 2   # extra attempts when the model server can't parse the model's tool call


def is_tool_parse_error(err: Exception) -> bool:
    """Ollama answers HTTP 500 "error parsing tool call" when the model emits a malformed tool call
    (for example invalid JSON inside a long file). Sampling is random, so asking again usually works."""
    msg = str(err).lower()
    return "error parsing tool call" in msg or ("500" in msg and "parsing tool" in msg)


def _call_with_parse_retry(*args) -> Reply:
    for attempt in range(PARSE_RETRIES + 1):
        try:
            return _call(*args)
        except LLMError as e:
            if attempt >= PARSE_RETRIES or not is_tool_parse_error(e):
                raise


def _call(s: dict, model: str, messages: list[dict], tools: list[dict], names: set[str], json_mode: bool,
          json_output: bool, samp: dict, think: str | None) -> Reply:
    timeout = float(s.get("request_timeout", 600))
    base = s["llm_base_url"].rstrip("/")
    msgs = _to_json_mode(messages, tools) if json_mode else None
    thinking = ""

    if s["llm_backend"] == "ollama":
        body = {"model": model, "stream": False, "keep_alive": s.get("keep_alive", "30m"),
                "messages": msgs if json_mode else _to_ollama(messages, keep_thinking=is_gpt_oss(model)),
                "options": {"num_ctx": int(s.get("num_ctx", 32768)), **samp}}
        if think:
            body["think"] = think
        if tools and not json_mode:
            body["tools"] = [_tool_schema(t) for t in tools]
        if json_output:
            body["format"] = "json"
        data = _request(base + "/api/chat", body, _headers(s), timeout)
        msg = data.get("message") or {}
        content = msg.get("content") or ""
        thinking = msg.get("thinking") or ""
        raw_calls = [(tc.get("function") or {}) for tc in (msg.get("tool_calls") or [])]
        calls = [ToolCall(name=f.get("name", ""), arguments=_parse_args(f.get("arguments")), id=f"call_{i}")
                 for i, f in enumerate(raw_calls) if f.get("name")]
        tokens = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
    else:
        body = {"model": model, "messages": msgs if json_mode else _to_openai(messages),
                **{k: v for k, v in samp.items() if k in ("temperature", "top_p")}}
        if tools and not json_mode:
            body["tools"] = [_tool_schema(t) for t in tools]
            body["tool_choice"] = "auto"
        if json_output:
            body["response_format"] = {"type": "json_object"}
        data = _request(_openai_base(base) + "/chat/completions", body, _headers(s), timeout)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        thinking = msg.get("reasoning_content") or msg.get("reasoning") or ""
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            f = tc.get("function") or {}
            if f.get("name"):
                calls.append(ToolCall(name=f["name"], arguments=_parse_args(f.get("arguments")),
                                      id=tc.get("id") or f"call_{i}"))
        usage = data.get("usage") or {}
        tokens = int(usage.get("total_tokens") or 0)

    content, inline_thoughts = split_thinking(content)
    if inline_thoughts:
        thinking = (thinking + "\n\n" + inline_thoughts).strip() if thinking else inline_thoughts
    if tools and not calls:
        tc = _text_tool_call(content, names)
        if tc:
            calls = [tc]
            content = ""
    # Unknown tool names are kept so the agent gets a helpful error instead of silence.
    calls = calls if tools else []
    return Reply(content=content, tool_calls=calls, tokens=tokens, mode="json" if json_mode else "native",
                 thinking=thinking[:8000])


def complete_json(prompt: str, system: str = "", model: str | None = None, temperature: float = 0.7):
    """One-shot call that must return JSON (used for idea generation)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    reply = chat(messages, model=model, json_output=True, temperature=temperature)
    data = extract_json(reply.content)
    if data is None:
        raise LLMError("The model didn't return valid JSON. Try again or pick a stronger model in Settings.")
    return data
