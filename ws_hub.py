"""WebSocket hub (FastAPI + uvicorn) — alag module.

app.py mein FastAPI/uvicorn ka koi code nahi rakha, kyunki Streamlit (1.57+)
script mein `x = FastAPI(...)` jaisa assignment dekh ke use ASGI app maan leta
hai aur `uvicorn app:x` chalane ki koshish karta hai (\"Attribute ... not found
in module app\" error). Ye file Streamlit ki script nahi hai, isliye safe hai.
"""
import json
import time
import asyncio as _ws_asyncio


def run(hub_state: dict, port: int, origin_ok, slog, slog_exception) -> None:
    """Blocking — WSHub thread ke andar chalta hai. app.py se args milte hain."""
    _WS_HUB = hub_state
    _WS_PORT = port
    _ws_origin_ok = origin_ok
    _slog = slog
    _slog_exception = slog_exception
    try:
        import uvicorn
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    except Exception as e:
        _WS_HUB["stats"]["last_error"] = f"import: {type(e).__name__}: {e}"
        _slog(f"WSHub: fastapi/uvicorn import fail — requirements.txt me "
              f"'fastapi' aur 'uvicorn[standard]' chahiye ({e})", level="err")
        return
    hub  = _WS_HUB
    loop = _ws_asyncio.new_event_loop()
    _ws_asyncio.set_event_loop(loop)
    ws_api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @ws_api.get("/ws/health")
    async def _ws_health():
        return {"ok": True, "ts": time.time(), "clients": len(hub["clients"]),
                "actions": sorted(hub["actions"].keys()), "stats": hub["stats"]}

    async def _send_snapshots(ws, topics):
        """Topics ka last payload turant bhejo (naya client / reconnect ke baad)."""
        for t in topics:
            snap = hub["last"].get(t)
            if snap is not None:
                await ws.send_text(json.dumps(
                    {"type": "push", "topic": t, "data": snap, "snapshot": True},
                    default=str))

    async def _run_action(ws, rid, action, data):
        fn = hub["actions"].get(action)
        if fn is None:
            out = {"id": rid, "ok": False, "error": f"unknown action: {action}"}
        else:
            try:
                res = await loop.run_in_executor(None, fn, data)
                out = {"id": rid, "ok": True, "data": res}
            except Exception as e:
                _slog_exception(f"WS action '{action}'", e)
                out = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            text = json.dumps(out, default=str)
        except Exception as e:
            text = json.dumps({"id": rid, "ok": False, "error": f"serialize: {e}"})
        try:
            await ws.send_text(text)
            hub["stats"]["out"] += 1
        except Exception:
            pass

    @ws_api.websocket("/ws")
    async def _ws_endpoint(ws: WebSocket):
        if not _ws_origin_ok(ws):
            hub["stats"]["last_error"] = f"origin reject: {ws.headers.get('origin')}"
            await ws.close(code=1008)
            return
        await ws.accept()
        # Browser ko subscribe karne ki zaroorat nahi: auto_topics par har client
        # connect hote hi subscribed hota hai, aur last snapshot turant mil jaata hai.
        auto = sorted(t for t in (hub.get("auto_topics") or ()) if isinstance(t, str))
        hub["clients"][ws] = set(auto)
        hub["stats"]["connects"] += 1
        try:
            await ws.send_text(json.dumps({"type": "hello", "ts": time.time(), "auto_topics": auto}))
            await _send_snapshots(ws, auto)
            while True:
                raw = await ws.receive_text()
                hub["stats"]["in"] += 1
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise ValueError("JSON object chahiye")
                except Exception as e:
                    await ws.send_text(json.dumps({"ok": False, "error": f"bad json: {e}"}))
                    continue
                rid    = msg.get("id")
                action = msg.get("action")
                data   = msg.get("data")
                dd     = data if isinstance(data, dict) else {}
                if action == "subscribe":
                    topics = [t for t in (dd.get("topics") or []) if isinstance(t, str)]
                    hub["clients"][ws].update(topics)
                    await ws.send_text(json.dumps({"id": rid, "ok": True,
                                       "data": {"topics": sorted(hub["clients"][ws])}}))
                    await _send_snapshots(ws, topics)   # turant last snapshot
                    continue
                if action == "unsubscribe":
                    for t in (dd.get("topics") or []):
                        hub["clients"][ws].discard(t)
                    await ws.send_text(json.dumps({"id": rid, "ok": True,
                                       "data": {"topics": sorted(hub["clients"][ws])}}))
                    continue
                # baaki actions alag task me — lamba action ping/tick ko block na kare
                task = _ws_asyncio.create_task(_run_action(ws, rid, action, data))
                hub["tasks"].add(task)
                task.add_done_callback(hub["tasks"].discard)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            hub["stats"]["last_error"] = f"conn: {type(e).__name__}: {e}"
        finally:
            hub["clients"].pop(ws, None)

    cfg = uvicorn.Config(ws_api, host="127.0.0.1", port=_WS_PORT, log_level="warning",
                         ws_ping_interval=20, ws_ping_timeout=20, ws_max_size=40 * 1024 * 1024,
                         lifespan="off")
    server = uvicorn.Server(cfg)
    hub["loop"] = loop
    hub["stats"]["started_ts"] = time.time()
    _slog(f"WSHub: FastAPI/uvicorn 127.0.0.1:{_WS_PORT} (/ws) start ho raha hai", level="ok")
    try:
        loop.run_until_complete(server.serve())
    except (Exception, SystemExit) as e:      # port busy => uvicorn SystemExit deta hai
        hub["stats"]["last_error"] = f"server: {type(e).__name__}: {e}"
        _slog(f"WSHub server band: {type(e).__name__}: {e}", level="err")
    finally:
        hub["loop"] = None
