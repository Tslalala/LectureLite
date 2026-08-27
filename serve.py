#!/usr/bin/env python3
"""LectureLite 局域网服务 — 一键启动,自动打开浏览器

用法:
  python serve.py              # 默认 8000 端口
  python serve.py 9000         # 指定端口

功能:
  1. 启动 HTTP 服务并自动打开浏览器
  2. 前端点「分享」时,POST /share 上传 zip,返回局域网分享链接
  3. 同事打开链接自动播放,无需安装
"""

import sys, os, json, uuid, socket, webbrowser, urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 8000
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
        if self.path != "/share":
            self._send(404, b"Not Found", "text/plain")
            return

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send(400, b"Expected multipart/form-data", "text/plain")
            return

        # 解析 multipart
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

        # 保存到 shared/
        SHARED_DIR.mkdir(exist_ok=True)
        share_id = uuid.uuid4().hex[:8]
        safe_name = os.path.basename(filename)
        stored_name = f"{share_id}_{safe_name}"
        (SHARED_DIR / stored_name).write_bytes(filedata)

        # 生成分享 URL
        url = f"http://{LAN_IP}:{PORT}/lecture-lite.html?src=shared/{urllib.parse.quote(stored_name)}"

        body = json.dumps({ "url": url, "id": share_id }).encode()
        self._send(200, body, "application/json")

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
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)

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
