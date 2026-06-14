#!/usr/bin/env python3
  # Forge Dual-GPU Mesh router - sovereign least-connections proxy across two local
  # Ollama workers (NVIDIA dGPU + Intel iGPU). Python stdlib only, zero dependencies.
  import http.client, json, threading
  from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

  GPU = "100.125.77.31"
  DGPU = (GPU, 11434)
  IGPU = (GPU, 11435)
  LISTEN = ("0.0.0.0", 11500)
  EMBED_PATHS = ("/api/embeddings", "/api/embed", "/v1/embeddings")
  HOP = {"connection","keep-alive","proxy-authenticate","proxy-authorization","te","trailers","transfer-encoding","upgrade","content-length"}

  lock = threading.Lock()
  inflight = {DGPU: 0, IGPU: 0}

  def pick_backend(path):
      p = path.split("?")[0]
      if any(p == e or p.endswith(e) for e in EMBED_PATHS):
          return IGPU, DGPU
      with lock:
          return (DGPU, IGPU) if inflight[DGPU] <= inflight[IGPU] else (IGPU, DGPU)

  def probe(b):
      try:
          c = http.client.HTTPConnection(b[0], b[1], timeout=4)
          c.request("GET", "/api/version"); r = c.getresponse(); r.read(); c.close()
          return r.status == 200
      except Exception:
          return False

  class H(BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"
      def log_message(self, *a): pass
      def _send_json(self, code, obj):
          body = json.dumps(obj).encode()
          self.send_response(code); self.send_header("Content-Type","application/json")
          self.send_header("Content-Length", str(len(body))); self.send_header("Connection","close")
          self.end_headers(); self.wfile.write(body); self.close_connection = True
      def _status(self):
          self._send_json(200, {
              "mesh": "forge-dual-gpu",
              "backends": {
                  "dgpu": {"addr": f"{DGPU[0]}:{DGPU[1]}", "inflight": inflight[DGPU], "up": probe(DGPU)},
                  "igpu": {"addr": f"{IGPU[0]}:{IGPU[1]}", "inflight": inflight[IGPU], "up": probe(IGPU)},
              },
              "routing": "embeddings->igpu ; chat/generate->least-conn(prefer dgpu) ; auto-failover",
          })
      def _proxy(self, method):
          path = self.path
          if path == "/mesh/status":
              return self._status()
          length = int(self.headers.get("Content-Length") or 0)
          body = self.rfile.read(length) if length else None
          primary, fallback = pick_backend(path)
          headers_sent = False
          for attempt, b in enumerate((primary, fallback)):
              with lock: inflight[b] += 1
              try:
                  hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
                  hdrs["Host"] = f"{b[0]}:{b[1]}"
                  conn = http.client.HTTPConnection(b[0], b[1], timeout=600)
                  conn.request(method, path, body=body, headers=hdrs)
                  resp = conn.getresponse()
                  self.send_response(resp.status)
                  for k, v in resp.getheaders():
                      if k.lower() in HOP: continue
                      self.send_header(k, v)
                  self.send_header("X-Forge-Backend", f"{b[0]}:{b[1]}")
                  self.send_header("Connection", "close")
                  self.end_headers(); headers_sent = True
                  while True:
                      chunk = resp.read(8192)
                      if not chunk: break
                      self.wfile.write(chunk); self.wfile.flush()
                  conn.close(); self.close_connection = True
                  with lock: inflight[b] -= 1
                  return
              except Exception as e:
                  with lock: inflight[b] -= 1
                  if not headers_sent and attempt == 0:
                      continue
                  if not headers_sent:
                      self._send_json(502, {"error": "mesh: both backends failed", "detail": str(e)})
                  self.close_connection = True
                  return
      def do_GET(self): self._proxy("GET")
      def do_POST(self): self._proxy("POST")
      def do_DELETE(self): self._proxy("DELETE")
      def do_PUT(self): self._proxy("PUT")
      def do_HEAD(self): self._proxy("HEAD")

  if __name__ == "__main__":
      srv = ThreadingHTTPServer(LISTEN, H)
      srv.daemon_threads = True
      print(f"forge-mesh-router {LISTEN} -> dgpu {DGPU} / igpu {IGPU}", flush=True)
      srv.serve_forever()
  