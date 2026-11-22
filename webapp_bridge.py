import hashlib
import hmac
import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")
PHOTO_DIR = os.path.join(os.path.dirname(__file__), "photo")
_LAST_OPT_UNDO = None


def verify_tg_init_data(init_data: str, bot_token: str) -> bool:
    if not init_data or not bot_token or bot_token == "ВСТАВ_СЮДИ_ТОКЕН":
        return True
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc_hash, received_hash)
    except Exception:
        return False


def start_miniapp_server(bot_module, port: int):
    class MiniAppHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, payload: dict):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _authorized(self) -> bool:
            init_data = self.headers.get("X-Telegram-Init-Data", "")
            return verify_tg_init_data(init_data, bot_module.TELEGRAM_BOT_TOKEN) if init_data else True

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                data = open(os.path.join(WEBAPP_DIR, "index.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/health":
                return self._send_json(200, {"status": "ok"})
            if path.startswith("/photo/"):
                fname = urllib.parse.unquote(path[len("/photo/"):])
                fpath = os.path.normpath(os.path.join(PHOTO_DIR, fname))
                if fpath.startswith(os.path.abspath(PHOTO_DIR)) and os.path.isfile(fpath):
                    data = open(fpath, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(fpath)[0] or "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404); self.end_headers(); return
            if path == "/api/tires":
                if not self._authorized():
                    return self._send_json(403, {"error": "invalid_init_data"})
                try:
                    df = bot_module.load_df(force=True)
                    items = [{
                        "id": idx, "row": int(r["row"]), "pallet": str(r["pallet"]).strip(),
                        "code": str(r["code"]).strip(), "size": str(r["size"]).strip(),
                        "name": str(r["name"]).strip(), "qty": int(r["q"]),
                    } for idx, (_, r) in enumerate(df.iterrows(), start=1) if str(r["code"]).strip() or str(r["name"]).strip()]
                    bot_module._hist_cache["ts"] = 0.0
                    raw_hist = bot_module.read_history() or []
                    hist = []
                    for h in raw_hist[-100:][::-1]:
                        ts_obj = h.get("ts")
                        t_str = ts_obj.strftime("%Y-%m-%d %H:%M") if hasattr(ts_obj, "strftime") else str(ts_obj or "")[:16]
                        was_i = int(h.get("old", 0) or 0)
                        bec_i = int(h.get("new", 0) or 0)
                        hist.append({
                            "time": t_str, "user": h.get("user", ""),
                            "code": str(h.get("code", "")), "action": h.get("action", ""),
                            "was": was_i, "became": bec_i, "diff": bec_i - was_i,
                            "loc": h.get("loc", ""), "name": h.get("name", "")
                        })
                    sk_raw = bot_module.load_sklad(force=True) or {}
                    sklad = [{"code": c, "name": v.get("name", ""), "qty": int(v.get("qty", 0))} for c, v in sk_raw.items()]
                    cat_raw = bot_module.build_catalog(df) or {}
                    return self._send_json(200, {"items": items, "history": hist, "sklad": sklad, "catalog": cat_raw})
                except Exception as e:
                    print(f"[MiniApp API Error] /api/tires: {e}", flush=True)
                    return self._send_json(500, {"error": str(e)})
            self.send_response(404); self.end_headers()

        def do_POST(self):
            global _LAST_OPT_UNDO
            if not self._authorized():
                return self._send_json(403, {"error": "invalid_init_data"})
            path = urllib.parse.urlparse(self.path).path
            try:
                body = self._read_json()

                class _WebUser:
                    id = 0
                    first_name = "MiniApp"
                    username = "miniapp"

                user = _WebUser()
                if path == "/api/qty":
                    row = int(body["row"])
                    new_qty = int(body["new_qty"])
                    bot_module.change_qty(user, row, "set", value=new_qty)
                    return self._send_json(200, {"ok": True})
                if path == "/api/add":
                    bot_module.add_tire(user, str(body["pallet"]), str(body["code"]), str(body.get("size", "")), str(body["name"]), int(body["qty"]), action="add")
                    return self._send_json(200, {"ok": True})
                if path == "/api/move":
                    mv = {
                        "src_row": int(body["from_row"]),
                        "src_pallet": str(body.get("from_pallet", "")),
                        "code": str(body["code"]),
                        "size": str(body.get("size", "")),
                        "name": str(body.get("name", "")),
                        "dest": str(body["to_pallet"]),
                        "qty": int(body["qty"]),
                    }
                    bot_module.do_move(user, mv)
                    return self._send_json(200, {"ok": True})
                if path == "/api/movepallet":
                    df = bot_module.load_df(force=True)
                    plan = bot_module.mp_plan(df, str(body["from_loc"]), str(body["to_loc"]))
                    bot_module.mp_apply(user, plan, df)
                    return self._send_json(200, {"ok": True})
                if path == "/api/optimize/apply":
                    df = bot_module.load_df(force=True)
                    plan = bot_module.optimize_plan(df, mode=body.get("mode", "min"))
                    _LAST_OPT_UNDO = bot_module.apply_plan(user, df, plan)
                    return self._send_json(200, {"ok": True})
                if path == "/api/optimize/undo":
                    if _LAST_OPT_UNDO:
                        bot_module.undo_plan(user, _LAST_OPT_UNDO)
                        _LAST_OPT_UNDO = None
                    return self._send_json(200, {"ok": True})
                if path == "/api/cleanup":
                    res = bot_module.cleanup_rows(user)
                    return self._send_json(200, {"ok": True, "result": res})
                return self._send_json(404, {"error": "not_found"})
            except Exception as e:
                print(f"[MiniApp API Error] {path}: {e}", flush=True)
                return self._send_json(500, {"error": str(e)})

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), MiniAppHandler).serve_forever()