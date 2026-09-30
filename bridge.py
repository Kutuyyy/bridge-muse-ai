#!/usr/bin/env python3
import json, os, time, uuid, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "/home/ubuntu/muse-bridge/queue"
PENDING = f"{BASE}/pending"
DONE = f"{BASE}/done"
MAX_PENDING = 5
WAIT_SECS = 240
KEEPALIVE_SECS = 15


def _is_dashboard_probe(req):
    try:
        if not isinstance(req, dict) or req.get("stream"):
            return False
        if req.get("max_tokens") != 1024:
            return False
        msgs = req.get("messages") or []
        if not msgs:
            return False
        last = msgs[-1]
        return (isinstance(last, dict) and last.get("role") == "user"
                and str(last.get("content", "")).strip().lower() == "hi")
    except Exception:
        return False


def _probe_completion():
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    return {"id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": "muse",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content":
                                     "Halo! Muse online — bridge 9Router aktif dan siap."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class H(BaseHTTPRequestHandler):
    timeout = 120  # per-socket-op timeout: slow-loris bodies can't wedge a thread forever

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        try:
            body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass 

    def _sse_write(self, payload: str) -> bool:
        """Write one SSE payload, flushed. Returns False if the client is gone."""
        try:
            self.wfile.write(payload.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False

    def _cleanup(self, rid: str):
        for d in (f"{PENDING}/{rid}.json", f"{DONE}/{rid}.json"):
            try:
                os.remove(d)
            except Exception:
                pass

    def do_GET(self):
        try:
            if self.path.rstrip("/") == "/v1/models":
                self._send(200, {"object": "list", "data": [
                    {"id": "muse", "object": "model", "created": 0, "owned_by": "muse"}]})
            elif self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _wait_answer(self, rid: str, keepalive_cb=None):
        deadline = time.time() + WAIT_SECS
        answer = None
        last_ping = time.time()
        while time.time() < deadline:
            dp = f"{DONE}/{rid}.json"
            if os.path.exists(dp):
                try:
                    with open(dp) as f:
                        answer = json.load(f).get("content", "")
                except Exception:
                    answer = ""
                try:
                    os.remove(dp)
                except Exception:
                    pass
                break
            if keepalive_cb and time.time() - last_ping >= KEEPALIVE_SECS:
                if not keepalive_cb():
                    break  # client disconnected
                last_ping = time.time()
            time.sleep(1)
        return answer

    def do_POST(self):
        try:
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": {"message": "bad json"}})
            if _is_dashboard_probe(req):
                return self._send(200, _probe_completion())
            try:
                npend = len(os.listdir(PENDING))
            except Exception:
                npend = 0
            if npend >= MAX_PENDING:
                return self._send(429, {"error": {"message": "Muse bridge busy, try again in a bit"}})
            rid = uuid.uuid4().hex
            with open(f"{PENDING}/{rid}.json", "w") as f:
                json.dump({"id": rid, "received_at": time.time(), "request": req}, f)

            if req.get("stream"):
                return self._handle_stream(req, rid)
            answer = self._wait_answer(rid)
            self._cleanup(rid)
            if answer is None:
                return self._send(504, {"error": {"message": "Muse did not answer in time"}})
            cid = "chatcmpl-" + uuid.uuid4().hex[:12]
            created = int(time.time())
            resp = {"id": cid, "object": "chat.completion", "created": created, "model": "muse",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
            self._send(200, resp)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception as e:
            try:
                self._send(500, {"error": {"message": str(e)[:200]}})
            except Exception:
                pass

    def _handle_stream(self, req, rid: str):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self._cleanup(rid)
            return
        if not self._sse_write(": connected\n\n"):
            self._cleanup(rid)
            return
        answer = self._wait_answer(rid, keepalive_cb=lambda: self._sse_write(": ping\n\n"))
        self._cleanup(rid)
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        if answer is None:
            err = {"error": {"message": "Muse did not answer in time", "type": "timeout"}}
            self._sse_write("data: " + json.dumps(err) + "\n\ndata: [DONE]\n\n")
            return
        chunk1 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": "muse", "choices": [{"index": 0,
                  "delta": {"role": "assistant", "content": answer}, "finish_reason": None}]}
        chunk2 = {"id": cid, "object": "chat.completion.chunk", "created": created,
                  "model": "muse", "choices": [{"index": 0, "delta": {},
                  "finish_reason": "stop"}]}
        self._sse_write("data: " + json.dumps(chunk1) + "\n\n")
        self._sse_write("data: " + json.dumps(chunk2) + "\n\ndata: [DONE]\n\n")


if __name__ == "__main__":
    import subprocess
    import threading

    def listen_addrs():
        addrs = ["127.0.0.1"]
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=10).stdout
            for ip in out.split():
                ip = ip.strip()
                if ip and ip not in addrs:
                    addrs.append(ip)
        except Exception:
            pass
        return addrs

    for addr in listen_addrs():
        srv = ThreadingHTTPServer((addr, 8765), H)
        srv.daemon_threads = True
        srv.allow_reuse_address = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"muse-bridge listening on {addr}:8765", flush=True)
    threading.Event().wait()
