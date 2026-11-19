"""Buildless local WebUI. Python standard library only."""

import json
import hashlib
import base64
import mimetypes
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from .feedback import ValidationError
from .storage import ConflictError, Store
from .demo import catalog
from .generation import GenerationService
from .genetics import available_models

WEB = Path(__file__).with_name("web")
MAX_UPLOAD = 128 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024


def application_shell():
    """One response for the critical UI so tunnel request failures cannot split startup."""
    nonce = secrets.token_urlsafe(18)
    html = (WEB / "index.html").read_text()
    html = html.replace('<link rel="stylesheet" href="/styles.css">',
                        f'<style nonce="{nonce}">{(WEB / "styles.css").read_text()}</style>')
    html = html.replace('<script src="/app.js" defer></script>', '')
    html = html.replace('</body>', f'<script nonce="{nonce}">{(WEB / "app.js").read_text()}</script></body>')
    csp = (f"default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
           f"style-src 'self' 'nonce-{nonce}'; script-src 'self' 'nonce-{nonce}'; frame-ancestors 'none'")
    return html, csp


def example_svg(index):
    palettes = [("#e9c858", "#a44232", "#314e50"), ("#d1c5af", "#bb563d", "#343f33"),
                ("#d7b75e", "#804132", "#3f5545"), ("#cad2ba", "#9a4f3f", "#384d43"),
                ("#b5c2bb", "#af4935", "#293e44"), ("#e6c986", "#a55643", "#454b3b"),
                ("#e2bea7", "#935f51", "#334b4d"), ("#c9c7a9", "#ae5e3d", "#3c4840"),
                ("#d9bd67", "#9b4332", "#2d4542")]
    bg, red, dark = palettes[index]
    x = 175 + (index % 3 - 1) * 30
    circle = 58 + (index % 4) * 8
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 280">
      <defs><filter id="paper"><feTurbulence baseFrequency=".65" numOctaves="3" stitchTiles="stitch"/>
      <feColorMatrix type="saturate" values="0"/><feComponentTransfer><feFuncA type="linear" slope=".12"/></feComponentTransfer>
      <feBlend in="SourceGraphic" mode="multiply"/></filter></defs>
      <g filter="url(#paper)"><path fill="{bg}" d="M0 0h400v280H0z"/>
      <path fill="#f2e5c9" opacity=".5" d="M{35 + index * 5} 0h90v280h-90z"/>
      <circle cx="{310 - index * 9}" cy="65" r="{circle}" fill="#f0dfb0" opacity=".65"/>
      <path fill="{red}" d="M{x-65} 135q0-25 25-25h90q25 0 25 25v110H{x-65}z"/>
      <path fill="{dark}" d="M{x-38} 180q0-58 39-58t45 58l15 100H{x-55}z"/>
      <ellipse cx="{x+5}" cy="92" rx="31" ry="38" fill="{dark}"/>
      <path fill="none" stroke="#e1cba3" stroke-width="8" stroke-linecap="round" d="M{x-22} 171q38 36 62-4"/>
      <path fill="none" stroke="{dark}" opacity=".35" stroke-width="2" d="M30 250h340M{x-62} 245v35m130-35v35"/>
      </g><text x="22" y="260" fill="{dark}" font-family="monospace" font-size="10" letter-spacing="2">STUDY {index+1:02} / INTERFACE SAMPLE</text></svg>'''


def make_handler(store, generator):
    def with_previews(generation):
        """Embed small grid previews in snapshots sent over the live stream."""
        for candidate in generation.get("candidates", []):
            media = candidate.get("media")
            if not media or media.get("preview_data"):
                continue
            preview_url = media.get("preview_url")
            if not preview_url:
                continue
            preview = store.assets / preview_url.removeprefix("/media/")
            if preview.is_file():
                media["preview_data"] = "data:image/jpeg;base64," + base64.b64encode(preview.read_bytes()).decode()
        return generation

    class Handler(BaseHTTPRequestHandler):
        server_version = "Mutations/1"
        protocol_version = "HTTP/1.1"

        def valid_host(self):
            host = self.headers.get("Host", "").split(":", 1)[0].lower().rstrip(".")
            return host in ("127.0.0.1", "localhost") or host.endswith(".devtunnels.ms")

        def log_message(self, fmt, *args):
            pass

        def access_log(self, status, size, started):
            elapsed = (time.monotonic() - started) * 1000
            print(f"HTTP {self.request_id} <- {status} {size} bytes {elapsed:.1f} ms", flush=True)

        def begin_request(self):
            self.request_started = time.monotonic()
            self.request_id = secrets.token_hex(4)
            print(f"HTTP {self.request_id} -> {self.command} {self.path}", flush=True)

        def send(self, status, data, mime="application/json; charset=utf-8", headers=None):
            started = getattr(self, "request_started", time.monotonic())
            if isinstance(data, (dict, list)):
                data = json.dumps(data).encode()
            elif isinstance(data, str):
                data = data.encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            if not headers or "Cache-Control" not in headers:
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if not headers or "Content-Security-Policy" not in headers:
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.access_log(status, len(data), started)

        def do_GET(self):
            self.begin_request()
            if not self.valid_host():
                return self.send(403, {"error": "Use the local server address."})
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/api/health":
                return self.send(200, {"app": "mutations-selection-room", "version": 4, "workspace": hashlib.sha256(str(store.root).encode()).hexdigest()})
            if path == "/api/catalog":
                return self.send(200, catalog())
            if path == "/api/models":
                return self.send(200, {"models": available_models()})
            if path == "/api/bootstrap":
                summaries = store.generation_summaries()
                active = next((g for g in summaries if g.get("demo", {}).get("mode") == "local-diffusion"
                               and g.get("generation", {}).get("status") in ("queued", "preparing", "generating")), None)
                selected = active or next((g for g in summaries if g.get("demo", {}).get("mode") == "local-diffusion"), None)
                generation = with_previews(store.get_generation(selected["id"])) if selected else None
                return self.send(200, {"generations": summaries, "generation": generation,
                                      "catalog": catalog(), "models": available_models()})
            if path == "/api/generations":
                return self.send(200, {"generations": store.generations()})
            match = re.fullmatch(r"/api/generations/([a-f0-9]{32})/events", path)
            if match:
                return self.events(match[1])
            match = re.fullmatch(r"/api/generations/([a-f0-9]{32})", path)
            if match:
                try:
                    return self.send(200, with_previews(store.get_generation(match[1])))
                except KeyError:
                    return self.send(404, {"error": "Generation not found."})
            if path == "/api/export":
                return self.send(200, store.export(parse_qs(parsed.query).get("include_demo") == ["1"]),
                                 headers={"Content-Disposition": 'attachment; filename="mutations-feedback.json"'})
            match = re.fullmatch(r"/examples/([0-8])\.svg", path)
            if match:
                return self.send(200, example_svg(int(match[1])), "image/svg+xml")
            if path.startswith("/media/"):
                name = path.removeprefix("/media/")
                if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpe?g|webp|gif|avif|mp4|webm|mov)", name):
                    return self.send(404, {"error": "Media not found."})
                file = store.assets / name
            else:
                if path in ("/", "/index.html"):
                    shell, csp = application_shell()
                    return self.send(200, shell, "text/html; charset=utf-8",
                                     headers={"Content-Security-Policy": csp})
                name = "index.html" if path == "/" else path.removeprefix("/")
                if name not in ("index.html", "app.js", "styles.css", "favicon.svg"):
                    return self.send(404, {"error": "Not found."})
                file = WEB / name
            if not file.is_file():
                return self.send(404, {"error": "File not found."})
            mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if path.startswith("/media/") and mime.startswith("video/"):
                return self.video(file, mime)
            self.send(200, file.read_bytes(), mime,
                      headers={"Cache-Control": "public, max-age=31536000, immutable"}
                      if path.startswith("/media/") else None)

        def events(self, gid):
            started = getattr(self, "request_started", time.monotonic())
            try:
                generation = store.get_generation(gid)
            except KeyError:
                return self.send(404, {"error": "Generation not found."})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last_snapshot = None
            last_progress = None
            sequence = 0
            last_heartbeat = 0
            try:
                while True:
                    generation = store.get_generation(gid)
                    job = generation.get("generation", {})
                    snapshot_signature = (job.get("status"), job.get("completed"), job.get("message"), job.get("error"))
                    progress_signature = (job.get("current_slot"), job.get("step"), job.get("steps"))
                    now = time.monotonic()
                    if snapshot_signature != last_snapshot:
                        sequence += 1
                        payload = json.dumps(with_previews(generation), separators=(",", ":"))
                        self.wfile.write(f"id: {sequence}\nevent: snapshot\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                        last_snapshot = snapshot_signature
                        last_progress = progress_signature
                        last_heartbeat = now
                        if job.get("status") in ("ready", "error", "paused"):
                            self.close_connection = True
                            return self.access_log(200, 0, started)
                    elif progress_signature != last_progress:
                        sequence += 1
                        payload = json.dumps(job, separators=(",", ":"))
                        self.wfile.write(f"id: {sequence}\nevent: progress\ndata: {payload}\n\n".encode())
                        self.wfile.flush()
                        last_progress = progress_signature
                        last_heartbeat = now
                    elif now - last_heartbeat >= 10:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = now
                    time.sleep(.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.access_log(200, 0, started)

        def video(self, file, mime):
            size = file.stat().st_size
            header = self.headers.get("Range")
            start, end, status = 0, size - 1, 200
            if header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
                if not match or not any(match.groups()):
                    return self.send(416, b"", headers={"Content-Range": f"bytes */{size}"})
                first, last = match.groups()
                if first:
                    start, end = int(first), min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    return self.send(416, b"", headers={"Content-Range": f"bytes */{size}"})
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with file.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                try:
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_POST(self):
            self.begin_request()
            if not self.valid_host():
                return self.send(403, {"error": "Use the local server address."})
            # Same-origin service. CLI clients without Origin are supported.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin not in (f"http://{host}", f"https://{host}"):
                return self.send(403, {"error": "Cross-origin requests are not accepted."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                limit = MAX_UPLOAD if self.path == "/api/assets" else MAX_JSON
                if not 0 < length <= limit:
                    return self.send(413, {"error": f"Request must be between 1 and {limit // 1024 // 1024} MB."})
                raw = self.rfile.read(length)
                if self.path == "/api/assets":
                    return self.send(201, store.add_asset(unquote(self.headers.get("X-Filename", "")), raw))
                if self.headers.get_content_type() != "application/json":
                    return self.send(415, {"error": "Send JSON with Content-Type: application/json."})
                value = json.loads(raw)
                if self.path == "/api/generations":
                    return self.send(201, store.create_generation(value))
                if self.path == "/api/demo":
                    if not isinstance(value, dict):
                        raise ValidationError("A session request must be an object.")
                    return self.send(201, generator.start_session(value.get("preset", "overview"), value.get("model_ids"), value.get("request_id")))
                match = re.fullmatch(r"/api/generations/([a-f0-9]{32})/next", self.path)
                if match:
                    if not isinstance(value, dict):
                        raise ValidationError("A continuation request must be an object.")
                    return self.send(200, generator.next_generation(match[1], value.get("expected_revision")))
                match = re.fullmatch(r"/api/generations/([a-f0-9]{32})/(retry|pause)", self.path)
                if match:
                    return self.send(200, generator.retry(match[1]) if match[2] == "retry" else generator.pause(match[1]))
                match = re.fullmatch(r"/api/generations/([a-f0-9]{32})/reviews", self.path)
                if match:
                    return self.send(201, store.save_review(match[1], value))
                self.send(404, {"error": "Endpoint not found."})
            except ConflictError as exc:
                self.send(409, {"error": str(exc)})
            except (ValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.send(400, {"error": str(exc)})
            except KeyError as exc:
                self.send(404, {"error": str(exc)})
            except ValueError:
                self.send(400, {"error": "Invalid request."})

    return Handler


def make_server(store, port=8765, auto_generate=True):
    generator = GenerationService(store, auto_start=auto_generate)
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 128
        def server_close(self):
            generator.close()
            super().server_close()
    server = Server(("127.0.0.1", port), make_handler(store, generator))
    server.generator = generator
    generator.bootstrap()
    return server

