#!/usr/bin/env python3

import sys, os, json, uuid, socket, webbrowser, urllib.parse, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

# ── LLM 文稿生成（按需导入，无 openai 包也能工作）──
try:
    from llm.script_generator import generate_script as _gen_script
    from llm.script_generator import generate_script_stream as _gen_script_stream
except ImportError:
    _gen_script = None
    _gen_script_stream = None

PORT = 8663
WEB_DIR = Path(__file__).parent.resolve()
SHARED_DIR = WEB_DIR / "shared"


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LAN_IP = "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    # 使用 HTTP/1.1 以支持流式响应 (SSE)
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?")[0])

        # /share/<id> → 重定向到播放页带参数
        if path.startswith("/share/"):
            share_id = path[len("/share/"):]
            params = urllib.parse.parse_qs(self.path.split("?", 1)[-1])
            src = params.get("src", [share_id])[0] if "?" in self.path else share_id
            redirect_url = f"/lecture-lite.html?src=shared/{urllib.parse.quote(src)}"
            self.send_response(302)
            self.send_header("Location", redirect_url)
            self.end_headers()
            return

        # 读取文件
        if path == "/":
            path = "/lecture-lite.html"

        # 安全检查: 只允许 web_dir 和 shared_dir 下
        clean_path = path.lstrip("/")
        for base in [WEB_DIR, SHARED_DIR]:
            # 对于 SHARED_DIR, 路径可能带 "shared/" 前缀, 去掉
            if base == SHARED_DIR and clean_path.startswith("shared/"):
                clean = clean_path[len("shared/"):]
            else:
                clean = clean_path
            parts = (base / clean).resolve()
            try:
                parts.relative_to(base)
                if parts.is_file():
                    ctype = self._guess_type(parts.name)
                    body = parts.read_bytes()
                    self._send(200, body, ctype)
                    return
            except ValueError:
                continue

        self._send(404, b"Not Found", "text/plain")

    def do_POST(self):
        if self.path == "/share":
            self._handle_share()
        elif self.path == "/generate-script":
            self._handle_generate_script()
        elif self.path == "/generate-script-stream":
            self._handle_generate_script_stream()
        else:
            self._send(404, b"Not Found", "text/plain")
            return

    # ── /share ──
    def _handle_share(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send(400, b"Expected multipart/form-data", "text/plain")
            return

        boundary = self._get_boundary(ctype)
        if not boundary:
            self._send(400, b"No boundary", "text/plain")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        filename, filedata = self._parse_multipart(raw, boundary)
        if not filename:
            self._send(400, b"No file in upload", "text/plain")
            return

        SHARED_DIR.mkdir(exist_ok=True)
        share_id = uuid.uuid4().hex[:8]
        safe_name = os.path.basename(filename)
        stored_name = f"{share_id}_{safe_name}"
        (SHARED_DIR / stored_name).write_bytes(filedata)

        url = f"http://{LAN_IP}:{PORT}/lecture-lite.html?src=shared/{urllib.parse.quote(stored_name)}"

        body = json.dumps({ "url": url, "id": share_id }).encode()
        self._send(200, body, "application/json")

    # ── /generate-script ──
    def _handle_generate_script(self):
        if not _gen_script:
            self._send_json(503, {"error": "llm.script_generator 未找到"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))

            content = data.get("content", "")
            topic = data.get("topic", "")
            length = data.get("length", "medium")

            if not content:
                self._send_json(400, {"error": "缺少文档内容"})
                return

            script = _gen_script(content, topic, length)

            self._send_json(200, {
                "success": True,
                "script": script,
            })
        except Exception as e:
            self._send_json(500, {"error": f"生成失败: {str(e)}"})

    # ── /generate-script-stream（流式输出）──
    def _handle_generate_script_stream(self):
        if not _gen_script_stream:
            self._send_json(503, {"error": "llm.script_generator 未找到"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))

            content = data.get("content", "")
            topic = data.get("topic", "")
            length = data.get("length", "medium")

            if not content:
                self._send_json(400, {"error": "缺少文档内容"})
                return

            # SSE 流式响应（不设 Content-Length，发完关闭连接通知客户端）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self._cors()
            self.end_headers()

            full_text = ""
            for piece in _gen_script_stream(content, topic, length):
                full_text += piece
                # SSE 格式：data: <json>\n\n
                sse_data = json.dumps({"delta": piece}, ensure_ascii=False)
                chunk = f"data: {sse_data}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()

            # 发送结束标记
            end_data = json.dumps({"done": True, "full": full_text}, ensure_ascii=False)
            end_chunk = f"data: {end_data}\n\n".encode("utf-8")
            self.wfile.write(end_chunk)
            self.wfile.flush()

            # 通知客户端完成并关闭连接
            done_marker = b"data: [DONE]\n\n"
            self.wfile.write(done_marker)
            self.wfile.flush()
            # 关闭当前请求的连接
            self.close_connection = True

        except Exception as e:
            error_data = json.dumps({"error": str(e)}, ensure_ascii=False)
            error_chunk = f"data: {error_data}\n\n".encode("utf-8")
            try:
                self.wfile.write(error_chunk)
                self.wfile.flush()
            except Exception:
                pass

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ── multipart helpers ──

    @staticmethod
    def _get_boundary(ctype):
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                return part[len("boundary="):].strip('"')
        return None

    @staticmethod
    def _parse_multipart(raw, boundary):
        delim = b"--" + boundary.encode()
        parts = raw.split(delim)
        for part in parts:
            part = part.strip()
            if not part or part == b"--":
                continue
            # 去掉结尾 \r\n
            if part.endswith(b"\r\n"):
                part = part[:-2]
            # 分离 header 和 body
            try:
                header_block, filedata = part.split(b"\r\n\r\n", 1)
            except ValueError:
                continue
            # 提取 filename
            filename = None
            for line in header_block.split(b"\r\n"):
                line = line.decode("utf-8", "replace")
                if "filename=" in line.lower():
                    for seg in line.split(";"):
                        seg = seg.strip()
                        if seg.lower().startswith("filename="):
                            filename = seg[len("filename="):].strip('"')
                            break
            if filename:
                # 去掉结尾的 \r\n
                if filedata.endswith(b"\r\n"):
                    filedata = filedata[:-2]
                return filename, filedata
        return None, None

    @staticmethod
    def _guess_type(name):
        n = name.lower()
        for ext, ct in [(".html", "text/html; charset=utf-8"), (".zip", "application/zip"),
                        (".js", "application/javascript"), (".css", "text/css"),
                        (".svg", "image/svg+xml"), (".png", "image/png"),
                        (".jpg", "image/jpeg"), (".webm", "audio/webm"), (".m4a", "audio/mp4"),
                        (".json", "application/json"), (".md", "text/markdown"),
                        (".pdf", "application/pdf"), (".gif", "image/gif")]:
            if n.endswith(ext):
                return ct
        return "application/octet-stream"

    def log_message(self, *args):
        pass


def main():
    global PORT, LAN_IP
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    if not (WEB_DIR / "lecture-lite.html").exists():
        print(f"找不到 lecture-lite.html — serve.py 必须和它放在同一目录")
        sys.exit(1)

    LAN_IP = get_lan_ip()

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        """多线程 HTTP 服务器，支持流式响应。"""
        pass

    httpd = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)

    url = f"http://127.0.0.1:{PORT}/lecture-lite.html"
    print("=" * 60)
    print("  LectureLite 服务已启动")
    print(f"  局域网 IP: {LAN_IP}")
    print(f"  服务端口: {PORT}")
    print(f"  本机访问: {url}")
    print(f"  局域网访问: http://{LAN_IP}:{PORT}/lecture-lite.html")

    # 自动打开浏览器
    webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        httpd.server_close()


if __name__ == "__main__":
    main()
