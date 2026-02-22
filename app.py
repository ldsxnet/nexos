import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

BASE_URL = os.getenv("NEXOS_BASE_URL", "https://workspace.nexos.ai").rstrip("/")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "3000"))
ACCOUNTS_FILE = Path(os.getenv("NEXOS_ACCOUNTS_FILE", "./nexos_accounts.json"))
CURRENT_CHAT_FILE = Path(os.getenv("CURRENT_CHAT_FILE", "./current-chat.json"))
DISABLE_HISTORY_DEFAULT = os.getenv("DISABLE_HISTORY", "false").lower() == "true"
REQUEST_TIMEOUT = float(os.getenv("NEXOS_TIMEOUT", "120"))
PASSWORD = os.getenv("PASSWORD", "")

DEFAULT_CREATED_TS = 1677610602
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

app = FastAPI(title="Nexos OpenAI Proxy (FastAPI)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def read_json_file(path: Path, default_value: Any) -> Any:
    try:
        if not path.exists():
            return default_value
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_value


def load_accounts() -> List[Dict[str, Any]]:
    accounts = read_json_file(ACCOUNTS_FILE, [])
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(status_code=500, detail=f"Invalid or empty accounts file: {ACCOUNTS_FILE}")
    return accounts


def sanitize_cookie(cookie: str) -> str:
    return re.sub(r"[\r\n]+", "", cookie).strip()


def get_cookie_from_account(account: Dict[str, Any]) -> str:
    cookies = account.get("cookies")
    if isinstance(cookies, str) and cookies.strip():
        return sanitize_cookie(cookies)

    cookie_dict = account.get("cookie_dict")
    if isinstance(cookie_dict, dict) and cookie_dict:
        parts = [f"{k}={'' if v is None else v}" for k, v in cookie_dict.items()]
        return sanitize_cookie("; ".join(parts))

    raise HTTPException(status_code=500, detail="Account cookies not found")


def canonical_model_name(model_name: str) -> str:
    return re.sub(r"-+", "-", model_name.strip().lower().replace("_", "-").replace(".", "-"))


def model_aliases(model_name: str) -> List[str]:
    name = model_name.strip().lower()
    aliases = {
        name,
        name.replace(".", "-"),
        name.replace("-", "."),
        name.replace("_", "-"),
        name.replace("_", "."),
    }
    if name.endswith("-1"):
        aliases.add(name[:-2])
    if name.endswith(".1"):
        aliases.add(name[:-2])
    if name == "grok-code-fast":
        aliases.add("grok-code-fast-1")
    if name == "grok-code-fast-1":
        aliases.add("grok-code-fast")
    return [item for item in aliases if item]


def build_handler_lookup(model_mapping: Dict[str, Any]) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for key, value in model_mapping.items():
        if not isinstance(value, str):
            continue
        for alias in model_aliases(str(key)):
            lookup[alias] = value
    return lookup


def infer_model_owner(model_id: str) -> str:
    name = model_id.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini") or name.startswith("imagen"):
        return "google"
    if name.startswith("gpt"):
        return "openai"
    if name.startswith("grok"):
        return "xai"
    if name.startswith("mistral"):
        return "mistral"
    return "nexos"


def load_chat_state() -> Dict[str, Any]:
    data = read_json_file(CURRENT_CHAT_FILE, {})
    if isinstance(data, dict) and isinstance(data.get("by_account"), dict):
        return data

    # 兼容旧格式 {"chatId": "..."}
    if isinstance(data, dict) and isinstance(data.get("chatId"), str) and data["chatId"]:
        return {"by_account": {"default": data["chatId"]}}

    return {"by_account": {}}


def save_chat_state(state: Dict[str, Any]) -> None:
    CURRENT_CHAT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def account_key(account: Dict[str, Any], account_index: int) -> str:
    email = account.get("email")
    if isinstance(email, str) and email.strip():
        return f"email:{email.strip().lower()}"
    return f"index:{account_index}"


def get_current_chat_id_for_account(account: Dict[str, Any], account_index: int) -> Optional[str]:
    state = load_chat_state()
    by_account = state.get("by_account", {})
    key = account_key(account, account_index)

    value = by_account.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()

    default_value = by_account.get("default")
    if isinstance(default_value, str) and default_value.strip():
        return default_value.strip()

    account_chat_id = account.get("chat_id")
    if isinstance(account_chat_id, str) and account_chat_id.strip():
        return account_chat_id.strip()

    return None


def set_current_chat_id_for_account(account: Dict[str, Any], account_index: int, chat_id: str) -> None:
    state = load_chat_state()
    by_account = state.setdefault("by_account", {})
    by_account[account_key(account, account_index)] = chat_id
    by_account["default"] = chat_id
    save_chat_state(state)

ACCOUNTS_ROTAING = 0

def resolve_account(
    request: Request,
    payload: Dict[str, Any],
    accounts: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], int]:
    email = request.headers.get("x-nexos-account-email") or payload.get("account_email")
    account_index_raw: Optional[str] = request.headers.get("x-nexos-account-index")
    if account_index_raw is None and payload.get("account_index") is not None:
        account_index_raw = str(payload.get("account_index"))

    if isinstance(email, str) and email.strip():
        target = email.strip().lower()
        for i, account in enumerate(accounts):
            account_email = str(account.get("email", "")).strip().lower()
            if account_email == target:
                return account, i
        raise HTTPException(status_code=400, detail=f"Account email not found: {email}")

    if account_index_raw is not None:
        try:
            idx = int(account_index_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid account index") from exc
        if idx < 0 or idx >= len(accounts):
            raise HTTPException(status_code=400, detail="Account index out of range")
        return accounts[idx], idx

    global ACCOUNTS_ROTAING
    ACCOUNTS_ROTAING += 1
    return accounts[ACCOUNTS_ROTAING % len(accounts)], ACCOUNTS_ROTAING % len(accounts)


def make_common_headers(cookie: str, referer: str) -> Dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "referer": referer,
        "user-agent": DEFAULT_USER_AGENT,
        "cookie": cookie,
    }


async def create_chat_id(cookie: str, client: httpx.AsyncClient) -> str:
    url = f"{BASE_URL}/chat.data"
    resp = await client.get(
        url,
        headers=make_common_headers(cookie, f"{BASE_URL}/"),
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
    )

    combined = f"{resp.headers.get('location', '')} {resp.text}"
    match = re.search(r"/chat/([a-f0-9-]{36})", combined, flags=re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=502, detail="Failed to create chat id from nexos response")
    return match.group(1)


async def fetch_last_message_id(chat_id: str, cookie: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        url = f"{BASE_URL}/api/chat/{chat_id}/history?offset=0"
        resp = await client.get(
            url,
            headers=make_common_headers(cookie, f"{BASE_URL}/chat/{chat_id}"),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                message_id = first.get("id")
                if isinstance(message_id, str) and message_id:
                    return message_id
    except Exception:
        return None
    return None


def extract_last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be an array")

    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue

        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content

        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            joined = "\n".join(parts).strip()
            if joined:
                return joined

    raise HTTPException(status_code=400, detail="No user message found")


def replace_direct_file_links(text: str, server_host: str) -> str:
    if not text:
        return text
    pattern = re.compile(rf"{re.escape(BASE_URL)}/api/chat/([^/]+)/files/([^/]+)/download")
    return pattern.sub(lambda m: f"http://{server_host}/v1/files/{m.group(1)}/{m.group(2)}/download", text)


def replace_sandbox_links(text: str, file_mapping: Dict[str, str], chat_id: str, server_host: str) -> str:
    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        file_name = match.group(2)
        file_uuid = file_mapping.get(file_name)
        if not file_uuid:
            return match.group(0)
        return f"![{alt_text}](http://{server_host}/v1/files/{chat_id}/{file_uuid}/download)"

    return re.sub(r"!\[([^\]]*)\]\(sandbox:/mnt/output-data/([^)]+)\)", _replace, text)


def parse_nexos_sse_payload(raw_text: str, chat_id: str, server_host: str) -> str:
    lines = raw_text.splitlines()
    file_mapping: Dict[str, str] = {}
    text_chunks: List[str] = []

    for line in lines:
        if not line.startswith("data: "):
            continue
        if "[DONE]" in line:
            continue

        payload = line[6:].strip()
        if not payload:
            continue

        try:
            data = json.loads(payload)
        except Exception:
            continue

        tool_result = data.get("tool_result") if isinstance(data, dict) else None
        if isinstance(tool_result, dict):
            result_obj = tool_result.get("result")
            if isinstance(result_obj, dict):
                results = result_obj.get("results")
                if isinstance(results, list):
                    for item in results:
                        if not isinstance(item, dict):
                            continue
                        files_obj = item.get("files")
                        if not isinstance(files_obj, dict):
                            continue
                        files = files_obj.get("files")
                        if not isinstance(files, list):
                            continue
                        for file_item in files:
                            if not isinstance(file_item, dict):
                                continue
                            name = file_item.get("name")
                            file_uuid = file_item.get("file_uuid")
                            if isinstance(name, str) and isinstance(file_uuid, str):
                                file_mapping[name] = file_uuid

        content_type = data.get("content_type") if isinstance(data, dict) else None
        content_obj = data.get("content") if isinstance(data, dict) else None
        if content_type == "text" and isinstance(content_obj, dict):
            text = content_obj.get("text")
            if isinstance(text, str) and text:
                text_chunks.append(text)

    full_text = "".join(text_chunks)
    full_text = replace_sandbox_links(full_text, file_mapping, chat_id, server_host)
    full_text = replace_direct_file_links(full_text, server_host)
    return full_text


def build_models(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    model_names = set()
    for account in accounts:
        mapping = account.get("model_mapping")
        if not isinstance(mapping, dict):
            continue
        for model_name in mapping.keys():
            canonical = canonical_model_name(str(model_name))
            if canonical:
                model_names.add(canonical)

    if model_names:
        model_names.add("nexos-chat")

    models = []
    for model_id in sorted(model_names):
        models.append(
            {
                "id": model_id,
                "object": "model",
                "created": DEFAULT_CREATED_TS,
                "owned_by": infer_model_owner(model_id),
            }
        )
    return models


def choose_handler_id(account: Dict[str, Any], requested_model: str) -> str:
    model_mapping = account.get("model_mapping")
    if not isinstance(model_mapping, dict) or not model_mapping:
        raise HTTPException(status_code=500, detail="Account model_mapping is missing")

    lookup = build_handler_lookup(model_mapping)
    for alias in model_aliases(requested_model):
        if alias in lookup:
            return lookup[alias]

    # 兼容默认模型
    for alias in model_aliases("claude-opus-4-6"):
        if alias in lookup:
            return lookup[alias]

    first = next(iter(model_mapping.values()), None)
    if not isinstance(first, str) or not first:
        raise HTTPException(status_code=500, detail="Cannot resolve handler id")
    return first


def should_disable_history(payload: Dict[str, Any]) -> bool:
    body_value = payload.get("disable_history")
    if isinstance(body_value, bool):
        return body_value
    return DISABLE_HISTORY_DEFAULT


def get_server_host(request: Request) -> str:
    host = request.headers.get("host")
    if host:
        return host
    return f"{HOST}:{PORT}"


@app.get("/v1/models")
async def list_models(request: Request) -> Dict[str, Any]:
    if PASSWORD:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token != PASSWORD:
            raise HTTPException(status_code=401, detail="Unauthorized")

    accounts = load_accounts()
    return {"object": "list", "data": build_models(accounts)}


@app.get("/v1/files/{chat_id}/{file_id}/download")
async def download_file(chat_id: str, file_id: str, request: Request) -> Response:
    accounts = load_accounts()
    account, _ = resolve_account(request, {}, accounts)
    cookie = get_cookie_from_account(account)

    file_url = f"{BASE_URL}/api/chat/{chat_id}/files/{file_id}/download"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            file_url,
            headers=make_common_headers(cookie, f"{BASE_URL}/chat/{chat_id}"),
            timeout=REQUEST_TIMEOUT,
        )

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    headers: Dict[str, str] = {}
    for key in ["content-type", "content-length", "content-disposition"]:
        value = resp.headers.get(key)
        if value:
            headers[key] = value

    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


@app.post("/v1/chat/create")
async def create_chat(request: Request, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    if PASSWORD:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token != PASSWORD:
            raise HTTPException(status_code=401, detail="Unauthorized")

    payload = payload or {}
    accounts = load_accounts()
    account, account_index = resolve_account(request, payload, accounts)
    cookie = get_cookie_from_account(account)

    async with httpx.AsyncClient() as client:
        chat_id = await create_chat_id(cookie, client)

    auto_switch = payload.get("auto_switch", True) is not False
    if auto_switch:
        set_current_chat_id_for_account(account, account_index, chat_id)

    return {
        "success": True,
        "chatId": chat_id,
        "url": f"{BASE_URL}/chat/{chat_id}",
        "currentChat": auto_switch,
        "message": (
            f"New chat created and set as current: {chat_id}"
            if auto_switch
            else f"New chat created: {chat_id}"
        ),
    }


@app.post("/v1/chat/switch")
async def switch_chat(request: Request, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    if PASSWORD:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token != PASSWORD:
            raise HTTPException(status_code=401, detail="Unauthorized")

    payload = payload or {}
    chat_id = payload.get("chatId")
    if not isinstance(chat_id, str) or not chat_id.strip():
        raise HTTPException(status_code=400, detail="chatId is required")

    accounts = load_accounts()
    account, account_index = resolve_account(request, payload, accounts)
    set_current_chat_id_for_account(account, account_index, chat_id.strip())

    return {
        "success": True,
        "chatId": chat_id.strip(),
        "message": f"Switched to chat: {chat_id.strip()}",
    }


@app.get("/v1/chat/current")
async def current_chat(request: Request) -> Dict[str, Any]:
    if PASSWORD:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token != PASSWORD:
            raise HTTPException(status_code=401, detail="Unauthorized")

    accounts = load_accounts()
    account, account_index = resolve_account(request, {}, accounts)
    chat_id = get_current_chat_id_for_account(account, account_index)
    return {
        "chatId": chat_id,
        "account": account.get("email") or account_index,
        "source": "current-chat.json" if load_chat_state().get("by_account") else "account",
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Response:
    if PASSWORD:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not token or token != PASSWORD:
            raise HTTPException(status_code=401, detail="Unauthorized")

    payload = payload or {}

    accounts = load_accounts()
    account, account_index = resolve_account(request, payload, accounts)
    cookie = get_cookie_from_account(account)

    messages = payload.get("messages")
    user_message = extract_last_user_text(messages)

    model_name = str(payload.get("model") or "nexos-chat")
    handler_id = choose_handler_id(account, model_name)

    chat_id = request.headers.get("x-nexos-chat-id") or payload.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id.strip():
        # 需求：请求未传 chat id 时自动创建
        async with httpx.AsyncClient() as client:
            chat_id = await create_chat_id(cookie, client)
        set_current_chat_id_for_account(account, account_index, chat_id)
    else:
        chat_id = chat_id.strip()

    disable_history = should_disable_history(payload)
    max_tokens = payload.get("max_tokens")
    temperature = payload.get("temperature", 1)
    stream = bool(payload.get("stream", False))

    adjusted_max_tokens = max_tokens
    if isinstance(adjusted_max_tokens, int) and "gemini" in canonical_model_name(model_name) and adjusted_max_tokens > 65536:
        adjusted_max_tokens = 65536

    async with httpx.AsyncClient() as client:
        last_message_id = None if disable_history else await fetch_last_message_id(chat_id, cookie, client)

    nexos_data: Dict[str, Any] = {
        "handler": {"id": handler_id, "type": "model", "fallbacks": True},
        "user_message": {
            "text": user_message,
            "client_metadata": {},
            "files": [],
        },
        "advanced_parameters": {},
        "functionalityHeader": "chat",
        "tools": {
            "web_search": {"enabled": False},
            "deep_research": {"enabled": False},
            "code_interpreter": {"enabled": True},
        },
        "enabled_integrations": [],
        "chat": {},
    }

    if isinstance(adjusted_max_tokens, int):
        nexos_data["advanced_parameters"]["max_completion_tokens"] = adjusted_max_tokens

    if isinstance(temperature, (int, float)) and temperature != 1:
        nexos_data["advanced_parameters"]["temperature"] = temperature

    if isinstance(last_message_id, str) and last_message_id:
        nexos_data["chat"]["last_message_id"] = last_message_id

    nexos_files = {
        "action": (None, json.dumps("chat_completion", ensure_ascii=False)),
        "chatId": (None, json.dumps(chat_id, ensure_ascii=False)),
        "data": (None, json.dumps(nexos_data, ensure_ascii=False)),
    }
    nexos_headers = {
        **make_common_headers(cookie, f"{BASE_URL}/chat/{chat_id}"),
        "origin": BASE_URL,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }

    if stream:
        async def event_gen() -> Any:
            stream_id = f"chatcmpl-{uuid.uuid4()}"
            created_ts = int(time.time())
            host_for_stream = get_server_host(request)
            file_mapping: Dict[str, str] = {}

            async with httpx.AsyncClient() as stream_client:
                async with stream_client.stream(
                    "POST",
                    f"{BASE_URL}/api/chat/{chat_id}",
                    files=nexos_files,
                    headers=nexos_headers,
                    timeout=REQUEST_TIMEOUT,
                ) as upstream_resp:
                    if upstream_resp.status_code != 200:
                        error_body = await upstream_resp.aread()
                        error_chunk = {
                            "error": {
                                "message": f"Nexos API returned {upstream_resp.status_code}",
                                "body": error_body.decode("utf-8", errors="ignore"),
                            }
                        }
                        yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    async for line in upstream_resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        if "[DONE]" in line:
                            break

                        raw_payload = line[6:].strip()
                        if not raw_payload:
                            continue

                        try:
                            data = json.loads(raw_payload)
                        except Exception:
                            continue

                        tool_result = data.get("tool_result") if isinstance(data, dict) else None
                        if isinstance(tool_result, dict):
                            result_obj = tool_result.get("result")
                            if isinstance(result_obj, dict):
                                results = result_obj.get("results")
                                if isinstance(results, list):
                                    for item in results:
                                        if not isinstance(item, dict):
                                            continue
                                        files_obj = item.get("files")
                                        if not isinstance(files_obj, dict):
                                            continue
                                        files = files_obj.get("files")
                                        if not isinstance(files, list):
                                            continue
                                        for file_item in files:
                                            if not isinstance(file_item, dict):
                                                continue
                                            name = file_item.get("name")
                                            file_uuid = file_item.get("file_uuid")
                                            if isinstance(name, str) and isinstance(file_uuid, str):
                                                file_mapping[name] = file_uuid

                        content_type = data.get("content_type") if isinstance(data, dict) else None
                        content_obj = data.get("content") if isinstance(data, dict) else None
                        if content_type == "text" and isinstance(content_obj, dict):
                            text_piece = content_obj.get("text")
                            if isinstance(text_piece, str) and text_piece:
                                text_piece = replace_sandbox_links(text_piece, file_mapping, chat_id, host_for_stream)
                                text_piece = replace_direct_file_links(text_piece, host_for_stream)
                                chunk = {
                                    "id": stream_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_ts,
                                    "model": canonical_model_name(model_name),
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": text_piece},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            end_chunk = {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": canonical_model_name(model_name),
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(end_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "x-accel-buffering": "no",
            },
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{BASE_URL}/api/chat/{chat_id}",
            files=nexos_files,
            headers=nexos_headers,
            timeout=REQUEST_TIMEOUT,
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail={
                "message": f"Nexos API returned {resp.status_code}",
                "body": resp.text,
            },
        )

    host = get_server_host(request)
    content_text = parse_nexos_sse_payload(resp.text, chat_id, host)

    response_payload = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": canonical_model_name(model_name),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_text or "No response",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "chat_id": chat_id,
        "account": account.get("email") or account_index,
    }
    return JSONResponse(content=response_payload)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
