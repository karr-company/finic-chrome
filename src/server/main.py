import asyncio
import copy
import logging
import os
from typing import Dict, Optional

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from browser_session import BrowserSession
from port_manager import PortManager

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change this to the list of allowed origins if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AUTH_TOKEN = os.getenv("AUTH_TOKEN")
bearer_scheme = HTTPBearer(auto_error=False)

if not AUTH_TOKEN:
    logging.warning("AUTH_TOKEN is not set; bearer authentication is disabled")


def require_bearer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not AUTH_TOKEN:
        return
    if credentials is None or credentials.credentials != AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    exc_str = f"{exc}".replace("\n", " ").replace("   ", " ")
    logging.error(f"{request}: {exc_str}")
    content = {"status_code": 10422, "message": exc_str, "data": None}
    return JSONResponse(
        content=content, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY
    )


port_manager = PortManager()
sessions: Dict[str, BrowserSession] = {}

# WebDriver commands (e.g. navigation) can legitimately take a while.
FORWARD_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

_HOP_BY_HOP_HEADERS = {"host", "content-length", "connection", "authorization"}


def _merge_capabilities(base: dict, override: Optional[dict]) -> dict:
    """Merge client-requested capabilities into the server-enforced set.

    The server always controls the browser binary and --user-data-dir; client
    args are appended after the hardened BASE_FLAGS.
    """
    merged = copy.deepcopy(base)
    if not override:
        return merged
    always = merged["capabilities"]["alwaysMatch"]
    client_always = (override.get("capabilities") or {}).get("alwaysMatch") or {}
    for key, value in client_always.items():
        if key == "goog:chromeOptions":
            opts = dict(value or {})
            client_args = opts.pop("args", None) or []
            opts.pop("binary", None)  # server-controlled
            client_args = [
                a for a in client_args if not a.startswith("--user-data-dir")
            ]
            chrome_opts = always["goog:chromeOptions"]
            chrome_opts.update(opts)
            chrome_opts["args"] = chrome_opts["args"] + client_args
        else:
            always[key] = value
    first_match = (override.get("capabilities") or {}).get("firstMatch")
    if first_match is not None:
        merged["capabilities"]["firstMatch"] = first_match
    return merged


def _driver_response(resp: httpx.Response) -> Response:
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@app.get("/status")
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": len(sessions),
        "maxSessions": port_manager.max_connections,
    }


@app.post("/session", dependencies=[Depends(require_bearer)])
async def create_session(request: Request):
    # Atomically reserve a port; combining find + mark under one lock prevents
    # two concurrent session creations from being handed the same port.
    port = port_manager.acquire_port()
    if port is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "value": {
                    "error": "session not created",
                    "message": "Maximum number of sessions reached",
                }
            },
        )
    session = BrowserSession(port=port)
    try:
        await asyncio.to_thread(session.start)
    except Exception as e:
        port_manager.release(port)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "value": {
                    "error": "session not created",
                    "message": f"Failed to start chromedriver: {e}",
                }
            },
        )

    try:
        body = await request.json()
    except Exception:
        body = None
    caps = _merge_capabilities(session.capabilities(), body)

    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"http://127.0.0.1:{port}/session", json=caps
            )
        except httpx.RequestError as e:
            await asyncio.to_thread(session.stop)
            port_manager.release(port)
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={
                    "value": {
                        "error": "session not created",
                        "message": f"chromedriver unreachable: {e}",
                    }
                },
            )

    session_id = None
    try:
        session_id = resp.json().get("value", {}).get("sessionId")
    except Exception:
        session_id = None

    if resp.status_code >= 400 or not session_id:
        await asyncio.to_thread(session.stop)
        port_manager.release(port)
        return _driver_response(resp)

    sessions[session_id] = session
    return _driver_response(resp)


@app.delete("/session/{session_id}", dependencies=[Depends(require_bearer)])
async def delete_session(session_id: str):
    session = sessions.get(session_id)
    if session is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "value": {
                    "error": "invalid session id",
                    "message": f"Unknown session: {session_id}",
                }
            },
        )
    async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
        try:
            resp = await client.delete(
                f"http://127.0.0.1:{session.port}/session/{session_id}"
            )
            response = _driver_response(resp)
        except httpx.RequestError:
            # Driver already gone; still clean up local state.
            response = JSONResponse(content={"value": None})
    await asyncio.to_thread(session.stop)
    port_manager.release(session.port)
    sessions.pop(session_id, None)
    return response


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    dependencies=[Depends(require_bearer)],
)
async def forward_to_driver(full_path: str, request: Request):
    parts = full_path.split("/")
    if len(parts) >= 2 and parts[0] == "session":
        session = sessions.get(parts[1])
        if session is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "value": {
                        "error": "invalid session id",
                        "message": f"Unknown session: {parts[1]}",
                    }
                },
            )
        body = await request.body()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        }
        target = f"http://127.0.0.1:{session.port}/{full_path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        async with httpx.AsyncClient(timeout=FORWARD_TIMEOUT) as client:
            try:
                resp = await client.request(
                    request.method,
                    target,
                    content=body or None,
                    headers=headers,
                )
            except httpx.RequestError as e:
                return JSONResponse(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    content={
                        "value": {
                            "error": "unknown error",
                            "message": f"chromedriver unreachable: {e}",
                        }
                    },
                )
            return _driver_response(resp)
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "value": {
                "error": "unknown command",
                "message": f"Unknown path: /{full_path}",
            }
        },
    )


def start():
    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
