#!/usr/bin/env python3
"""LectureLite 局域网服务（HTTPS + 账号体系）

默认只绑定 127.0.0.1；需要局域网访问时显式传 --lan（或 LL_LAN=1），此时绑定 0.0.0.0。

- 全站 HTTPS：首次启动用 cryptography 自动生成自签名证书（cert/server.crt|key）
- 账号体系：UID + 密码注册 / 登录，会话走 HttpOnly Cookie
- 个人数据：data/users.json（账号与收藏）、data/recordings.json（录制归属与分享）
- 录制文件仍存 shared/，但不再静态暴露，只能经 /api/recordings/<id>/file 做权限校验后下载

安全边界：
- 静态资源白名单：lecture-lite.html、tools/、docs/，拒绝一切点目录（.git 等）
- 上传大小限制（默认 100MB），multipart 流式落盘，不全量驻留内存
- JSON 请求体大小限制；TTS 句子数量/长度/倍速限制
- 重操作（上传 / LLM / TTS）并发上限
- 不做跨域（CORS 一律不发头，前端与服务同源）
"""

import sys, os, json, uuid, socket, webbrowser, urllib.parse, threading, asyncio, base64, tempfile, time, secrets, hashlib, ssl, shutil, subprocess
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

# ── LLM 文稿生成（按需导入，无 openai 包也能工作）──
try:
    from llm.script_generator import generate_script as _gen_script
    from llm.script_generator import generate_script_stream as _gen_script_stream
    from llm.script_generator import generate_script_timeline_stream as _gen_script_timeline_stream
except ImportError:
    _gen_script = None
    _gen_script_stream = None
    _gen_script_timeline_stream = None

# ── edge-tts 语音合成（按需导入，无 edge-tts 包也能工作）──
try:
    from llm.tts_generator import synth_stream as _tts_synth_stream
    from llm.tts_generator import list_zh_voices as _tts_list_voices
    from llm.tts_generator import DEFAULT_VOICE as _TTS_DEFAULT_VOICE
except ImportError:
    _tts_synth_stream = None
    _tts_list_voices = None
    _TTS_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

WEB_DIR = Path(__file__).parent.resolve()
SHARED_DIR = WEB_DIR / "shared"
DATA_DIR = WEB_DIR / "data"
DB_FILE = DATA_DIR / "store.json"
CERT_DIR = WEB_DIR / "cert"
SESSION_COOKIE = "ll_user"
PBKDF2_ITERS = 200_000
SHARE_TOKEN_BYTES = 24        # 分享令牌熵
AUTH_MAX_ATTEMPTS = 10        # 登录/注册限速：每 IP+UID 5 分钟内最多尝试次数
AUTH_WINDOW_SECONDS = 300
TRASH_KEEP_SECONDS = 30 * 86400   # 回收站保留 30 天，到期自动清空
DEMO_DIR = WEB_DIR / "demo"       # 示例课程包（不可删除，自动出现在收藏里）

# ── 限额与开关 ──
MAX_UPLOAD_BYTES = int(os.environ.get("LL_MAX_UPLOAD_MB", "100")) * 1024 * 1024
MAX_AUDIO_TRANSCODE_BYTES = 300 * 1024 * 1024
MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_TTS_SENTENCES = 600
MAX_TTS_SENTENCE_LEN = 1000
SHARE_KEEP_SECONDS = 7 * 24 * 3600          # 分享文件保留 7 天
HEAVY_CONCURRENCY = 3                        # 上传 / LLM / TTS 并发上限
MAX_MULTIPART_HEADER_BYTES = 64 * 1024

# 静态资源白名单：根目录文件 + 允许暴露的子目录（shared/ 单独处理）
STATIC_ROOT_FILES = {"lecture-lite.html", "home.html"}
STATIC_DIRS = {"tools", "docs"}

# 重操作并发闸门
_heavy_lock = threading.Semaphore(HEAVY_CONCURRENCY)

LAN_IP = "127.0.0.1"

# ── 数据存储（单一 store.json，按关系拆分）──
# users:            uid -> {salt, hash}                    账号凭证
# recordings:       rid -> {id, owner, title, stored, created, updated, deleted}   tombstone 软删除
# recording_shares: rid -> {uid: shared_at}                用户↔录制的直接分享关系
# user_favorites:   uid -> {rid: created_at}               用户↔录制的收藏关系（不在录制上放 is_favorite）
# user_progress:    uid -> {rid: {pos_ms, updated}}        用户↔录制的播放进度
# share_links:      sid -> {rec_id, owner, token_hash, created, expires_at, revoked}  可撤销的链接授权（只存令牌哈希）
_store_lock = threading.RLock()
_sessions = {}  # session token -> uid
_auth_attempts = {}  # "ip|uid" -> [timestamps]
_db = {
    "users": {}, "recordings": {}, "recording_shares": {},
    "user_favorites": {}, "user_progress": {}, "share_links": {},
}


def _load_store():
    DATA_DIR.mkdir(exist_ok=True)
    if not DB_FILE.is_file():
        _migrate_legacy()
        return
    try:
        data = json.loads(DB_FILE.read_text("utf-8"))
        for k in _db:
            _db[k].update(data.get(k) or {})
    except Exception as e:
        print(f"警告：读取 {DB_FILE.name} 失败: {e}")
    _migrate_legacy()


def _migrate_legacy():
    """旧格式（users.json 的 favorites / recordings.json 的 shared_with 列表）迁移到关系表。"""
    legacy_users = DATA_DIR / "users.json"
    legacy_recs = DATA_DIR / "recordings.json"
    changed = False
    if legacy_users.is_file():
        try:
            for uid, u in json.loads(legacy_users.read_text("utf-8")).items():
                if uid not in _db["users"]:
                    _db["users"][uid] = {"salt": u["salt"], "hash": u["hash"]}
                for rid in u.get("favorites", []):
                    _db["user_favorites"].setdefault(uid, {}).setdefault(rid, int(time.time()))
                changed = True
            legacy_users.unlink()
        except Exception as e:
            print(f"警告：迁移旧 users.json 失败: {e}")
    if legacy_recs.is_file():
        try:
            for rid, r in json.loads(legacy_recs.read_text("utf-8")).items():
                if rid in _db["recordings"]:
                    continue
                _db["recordings"][rid] = {
                    "id": rid, "owner": r.get("owner", ""), "title": r.get("title", ""),
                    "stored": r.get("stored", ""), "created": r.get("created", 0),
                    "updated": r.get("created", 0), "deleted": False,
                }
                for uid in r.get("shared_with", []):
                    _db["recording_shares"].setdefault(rid, {})[uid] = int(time.time())
                changed = True
            legacy_recs.unlink()
        except Exception as e:
            print(f"警告：迁移旧 recordings.json 失败: {e}")
    if changed:
        _save_store()


def _save_store():
    """调用方须已持有 _store_lock。临时文件写完原子重命名，崩溃不留半个文件。"""
    DATA_DIR.mkdir(exist_ok=True)
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_db, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, DB_FILE)


def _seed_demo_courses():
    """Register the single built-in guide; retire only the previous built-in demos."""
    package = DEMO_DIR / "教你使用lecturelite.lecture.zip"
    if not package.is_file():
        return
    rid = "dmguide"
    with _store_lock:
        for old_id, rec in _db["recordings"].items():
            if rec.get("demo") and old_id != rid:
                rec["deleted"] = True
                rec["retired_demo"] = True  # Hidden, but never garbage-collected.
        SHARED_DIR.mkdir(exist_ok=True)
        stored = rid + "_" + package.name
        dest = SHARED_DIR / stored
        if not dest.exists() or package.stat().st_mtime > dest.stat().st_mtime or package.stat().st_size != dest.stat().st_size:
            shutil.copyfile(package, dest)
        _db["recordings"][rid] = {
            "id": rid, "owner": "__demo__", "demo": True,
            "title": "教你使用 LectureLite", "stored": stored,
            "created": int(package.stat().st_mtime), "updated": int(package.stat().st_mtime),
            "deleted": False,
        }
        _save_store()


def _purge_expired_trash():
    """清空回收站中超过 30 天的项目（连文件一起删，授权一并收回）。"""
    cutoff = time.time() - TRASH_KEEP_SECONDS
    with _store_lock:
        victims = [rid for rid, r in _db["recordings"].items()
                   if r.get("deleted") and not r.get("retired_demo") and (r.get("deleted_at") or 0) < cutoff]
        removed = []
        for rid in victims:
            r = _db["recordings"].pop(rid)
            removed.append(r.get("stored"))
            _db["recording_shares"].pop(rid, None)
            for link in _db["share_links"].values():
                if link.get("rec_id") == rid:
                    link["revoked"] = True
            for favs in _db["user_favorites"].values():
                favs.pop(rid, None)
            for prog in _db["user_progress"].values():
                prog.pop(rid, None)
        if removed:
            _save_store()
    for stored in removed:
        if stored:
            try:
                (SHARED_DIR / stored).unlink()
            except OSError:
                pass
    if removed:
        print(f"回收站已自动清空 {len(removed)} 个过期录制")


def _purge_loop():
    while True:
        time.sleep(3600)
        try:
            _purge_expired_trash()
        except Exception as e:
            print(f"回收站清理失败: {e}")


def _hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERS
    ).hex()


def _register_user(uid, password):
    uid = str(uid or "").strip()
    if not uid or len(uid) > 64:
        return None, "UID 不能为空（最长 64 字符）"
    if not isinstance(password, str) or len(password) < 4:
        return None, "密码至少 4 位"
    with _store_lock:
        if uid in _db["users"]:
            return None, "该 UID 已注册"
        salt = secrets.token_hex(16)
        _db["users"][uid] = {"salt": salt, "hash": _hash_password(password, salt)}
        _save_store()
    return uid, None


def _verify_user(uid, password):
    with _store_lock:
        u = _db["users"].get(str(uid or "").strip())
        if not u:
            return None
        if secrets.compare_digest(u["hash"], _hash_password(password or "", u["salt"])):
            return str(uid).strip()
    return None


def _check_auth_rate(client_ip, uid):
    """登录/注册限速；返回 True 表示允许。"""
    key = f"{client_ip}|{uid}"
    now = time.time()
    with _store_lock:
        stamps = [t for t in _auth_attempts.get(key, []) if now - t < AUTH_WINDOW_SECONDS]
        if len(stamps) >= AUTH_MAX_ATTEMPTS:
            _auth_attempts[key] = stamps
            return False
        stamps.append(now)
        _auth_attempts[key] = stamps
    return True


def _add_recording(owner, title, stored_name):
    rec_id = uuid.uuid4().hex[:8]
    now = int(time.time())
    with _store_lock:
        _db["recordings"][rec_id] = {
            "id": rec_id, "owner": owner, "title": title, "stored": stored_name,
            "created": now, "updated": now, "deleted": False,
        }
        _save_store()
    return rec_id


# ── 统一的所有权 / 可见性判断（所有接口都走这几个入口，不在接口里各自实现）──
def _rec_visible(rid):
    """返回未删除的录制；已删除（tombstone）对任何调用者都不存在。"""
    rec = _db["recordings"].get(rid)
    if not rec or rec.get("deleted"):
        return None
    return rec


def _rec_for_owner(rid, uid):
    """所有权隔离的统一入口：只有所有者能拿到（且未删除）。"""
    if not uid:
        return None
    rec = _rec_visible(rid)
    if rec and rec.get("owner") == uid:
        return rec
    return None


def _can_view(uid, rid):
    """登录用户对录制的查看权：所有者 / 被直接分享 / 已收藏 / 示例课程。链接访问走 share_links 另行校验。"""
    if not uid or not _rec_visible(rid):
        return False
    rec = _db["recordings"][rid]
    if rec.get("demo"):
        return True
    if rec.get("owner") == uid:
        return True
    with _store_lock:
        if uid in _db["recording_shares"].get(rid, {}):
            return True
        return rid in _db["user_favorites"].get(uid, {})


def _create_share_link(owner, rid):
    """为本人录制创建 unlisted 链接；令牌只返回一次，库里只存哈希。"""
    token = secrets.token_urlsafe(SHARE_TOKEN_BYTES)
    sid = uuid.uuid4().hex[:8]
    with _store_lock:
        _db["share_links"][sid] = {
            "rec_id": rid, "owner": owner,
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created": int(time.time()), "expires_at": None, "revoked": False,
        }
        _save_store()
    return sid, token


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _cleanup_expired_shares():
    """启动时清掉过期分享文件。"""
    if not SHARED_DIR.is_dir():
        return
    now = time.time()
    for p in SHARED_DIR.iterdir():
        try:
            if p.is_file() and now - p.stat().st_mtime > SHARE_KEEP_SECONDS:
                p.unlink()
        except OSError:
            pass


def resolve_static(urlpath):
    """把 URL 路径解析到允许暴露的文件；不允许时返回 None。
    shared/ 里的录制文件不在此列，只能经 /api/recordings/<id>/file 鉴权后下载。"""
    clean = urlpath.lstrip("/")
    if clean in ("", "index.html"):
        clean = "lecture-lite.html"
    if clean in STATIC_ROOT_FILES:
        target = (WEB_DIR / clean).resolve()
        return target if target.is_file() else None
    top = clean.split("/", 1)[0]
    if top in STATIC_DIRS:
        base = WEB_DIR
        rel = clean
    else:
        return None
    if not rel:
        return None
    for seg in rel.split("/"):
        if not seg or seg == ".." or seg.startswith("."):
            return None
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


class Handler(BaseHTTPRequestHandler):
    # 使用 HTTP/1.1 以支持流式响应 (SSE)
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        # 避免慢速客户端长期占住上传 / 生成的工作线程。
        self.connection.settimeout(30)

    # ── 通用响应（不发 CORS 头：前端与服务同源）──
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._send_session_cookie()
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_session_cookie()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _session_uid(self):
        """从 Cookie 取当前登录用户；未登录返回 None。"""
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies.get(SESSION_COOKIE)
            if not token:
                return None
            return _sessions.get(token.value)
        except (TypeError, ValueError, KeyError):
            return None

    def _set_session_cookie(self, token):
        """token 为 None 时清除 Cookie。"""
        self._cookie_to_set = token

    def _send_session_cookie(self):
        token = getattr(self, "_cookie_to_set", None)
        if token is None:
            return
        if token == "":
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
        else:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict",
            )
        self._cookie_to_set = None

    def do_OPTIONS(self):
        self._send(204)

    # ── auth APIs ──
    def _handle_register(self):
        data, err = self._read_json_body(4096)
        if err:
            return
        uid = str(data.get("uid") or "").strip()
        if not _check_auth_rate(self.client_address[0], uid):
            self._send_json(429, {"error": "尝试过于频繁，请 5 分钟后再试"})
            return
        uid, error = _register_user(data.get("uid"), data.get("password"))
        if error:
            self._send_json(400, {"error": error})
            return
        token = secrets.token_urlsafe(32)
        _sessions[token] = uid
        self._set_session_cookie(token)
        self._send_json(200, {"success": True, "uid": uid})

    def _handle_login(self):
        data, err = self._read_json_body(4096)
        if err:
            return
        uid = str(data.get("uid") or "").strip()
        if not _check_auth_rate(self.client_address[0], uid):
            self._send_json(429, {"error": "尝试过于频繁，请 5 分钟后再试"})
            return
        uid = _verify_user(data.get("uid"), data.get("password"))
        if not uid:
            self._send_json(401, {"error": "UID 或密码错误"})
            return
        token = secrets.token_urlsafe(32)
        _sessions[token] = uid
        self._set_session_cookie(token)
        self._send_json(200, {"success": True, "uid": uid})

    def _handle_logout(self):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookies.get(SESSION_COOKIE)
            if token:
                _sessions.pop(token.value, None)
        except (TypeError, ValueError, KeyError):
            pass
        self._set_session_cookie("")
        self._send_json(200, {"success": True})

    def _handle_me(self):
        uid = self._session_uid()
        if not uid:
            self._send_json(401, {"error": "未登录"})
            return
        self._send_json(200, {"uid": uid})

    @staticmethod
    def _rec_item(uid, rid):
        """列表视图的统一投影：不暴露内部字段（stored、令牌哈希等）。"""
        rec = _db["recordings"][rid]
        return {
            "id": rid, "title": rec.get("title", ""), "owner": rec.get("owner", ""),
            "created": rec.get("created", 0), "updated": rec.get("updated", 0),
            "favorite": rid in _db["user_favorites"].get(uid, {}),
            "progress_ms": (_db["user_progress"].get(uid, {}).get(rid, {}) or {}).get("pos_ms", 0),
        }

    def _handle_recordings_list(self):
        uid = self._session_uid()
        if not uid:
            self._send_json(401, {"error": "未登录"})
            return
        with _store_lock:
            mine, shared, favorites, trash = [], [], [], []
            for rid, rec in _db["recordings"].items():
                if rec.get("demo"):
                    if not rec.get("deleted"):
                        favorites.append({**self._rec_item(uid, rid), "favorite": True, "demo": True})
                    continue
                if rec.get("deleted"):
                    if rec.get("owner") == uid:
                        trash.append({**self._rec_item(uid, rid), "deleted_at": rec.get("deleted_at")})
                    continue
                if rec.get("owner") == uid:
                    mine.append(self._rec_item(uid, rid))
                    if rid in _db["user_favorites"].get(uid, {}):
                        favorites.append(self._rec_item(uid, rid))
                    continue
                if uid in _db["recording_shares"].get(rid, {}):
                    shared.append(self._rec_item(uid, rid))
                if rid in _db["user_favorites"].get(uid, {}):
                    favorites.append(self._rec_item(uid, rid))
        mine.sort(key=lambda x: -x["created"])
        shared.sort(key=lambda x: -x["created"])
        favorites.sort(key=lambda x: -x["created"])
        trash.sort(key=lambda x: -(x.get("deleted_at") or 0))
        self._send_json(200, {"mine": mine, "shared": shared, "favorites": favorites, "trash": trash})

    def _handle_recording_file(self, rec_id):
        uid = self._session_uid()
        with _store_lock:
            if not _can_view(uid, rec_id):
                self._send_json(403 if uid else 401, {"error": "无权访问该录制" if uid else "未登录"})
                return
            rec = _db["recordings"][rec_id]
            path = SHARED_DIR / rec["stored"]
        if not path.is_file():
            self._send_json(404, {"error": "录制文件已过期或不存在"})
            return
        size = path.stat().st_size
        self.connection.settimeout(300)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        title = urllib.parse.quote(rec.get("title") or "recording.lecture.zip")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{title}")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as src:
                shutil.copyfileobj(src, self.wfile, length=64 * 1024)

    def _handle_favorite(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            if not _can_view(uid, rec_id):
                self._send_json(403, {"error": "无权访问该录制"})
                return
            favs = _db["user_favorites"].setdefault(uid, {})
            on = rec_id not in favs
            if on:
                favs[rec_id] = int(time.time())
            else:
                del favs[rec_id]
            _save_store()
        self._send_json(200, {"success": True, "favorite": on})

    def _handle_share_to(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        data, err = self._read_json_body(4096)
        if err:
            return
        target = str(data.get("uid") or "").strip()
        with _store_lock:
            if not _rec_for_owner(rec_id, uid):
                self._send_json(403, {"error": "只能分享自己的录制"})
                return
            if not target or target not in _db["users"]:
                self._send_json(404, {"error": f"用户 {target or '(空)'} 不存在"})
                return
            shares = _db["recording_shares"].setdefault(rec_id, {})
            if target != uid and target not in shares:
                shares[target] = int(time.time())
                _save_store()
        self._send_json(200, {"success": True, "shared_with": sorted(shares)})

    def _handle_recording_delete(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            rec = _rec_for_owner(rec_id, uid)
            if not rec:
                self._send_json(403, {"error": "只能删除自己的录制"})
                return
            # tombstone 软删除：进回收站，30 天后自动清空；期间可恢复
            rec["deleted"] = True
            rec["deleted_at"] = int(time.time())
            rec["updated"] = int(time.time())
            # 收回所有授权：直接分享清空、链接全部吊销、所有用户的收藏与进度清除
            _db["recording_shares"].pop(rec_id, None)
            for link in _db["share_links"].values():
                if link.get("rec_id") == rec_id:
                    link["revoked"] = True
            for favs in _db["user_favorites"].values():
                favs.pop(rec_id, None)
            for prog in _db["user_progress"].values():
                prog.pop(rec_id, None)
            _save_store()
            stored = rec["stored"]
        try:
            (SHARED_DIR / stored).unlink()
        except OSError:
            pass
        self._send_json(200, {"success": True})

    def _handle_recording_restore(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            rec = _db["recordings"].get(rec_id)
            if not rec or rec.get("owner") != uid or not rec.get("deleted"):
                self._send_json(404, {"error": "回收站里没有这个录制"})
                return
            rec["deleted"] = False
            rec["deleted_at"] = None
            _save_store()
        self._send_json(200, {"success": True})

    def _handle_recording_purge(self, rec_id):
        """从回收站立即彻底删除（等同到期自动清空）。"""
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            rec = _db["recordings"].get(rec_id)
            if not rec or rec.get("owner") != uid or not rec.get("deleted"):
                self._send_json(404, {"error": "回收站里没有这个录制"})
                return
            del _db["recordings"][rec_id]
            _db["recording_shares"].pop(rec_id, None)
            for link in _db["share_links"].values():
                if link.get("rec_id") == rec_id:
                    link["revoked"] = True
            for favs in _db["user_favorites"].values():
                favs.pop(rec_id, None)
            for prog in _db["user_progress"].values():
                prog.pop(rec_id, None)
            _save_store()
            stored = rec.get("stored")
        if stored:
            try:
                (SHARED_DIR / stored).unlink()
            except OSError:
                pass
        self._send_json(200, {"success": True})

    # ── 分享链接（unlisted 授权：可重新生成 / 吊销 / 有效期）──
    def _handle_link_create(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        data, err = self._read_json_body(4096)
        if err:
            return
        try:
            days = float(data.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        with _store_lock:
            if not _rec_for_owner(rec_id, uid):
                self._send_json(403, {"error": "只能分享自己的录制"})
                return
            sid, token = _create_share_link(uid, rec_id)
            if days > 0:
                _db["share_links"][sid]["expires_at"] = int(time.time() + days * 86400)
                _save_store()
        url = f"{PUBLIC_BASE_URL}/s/{sid}?tk={token}"
        self._send_json(200, {"success": True, "sid": sid, "url": url})

    def _handle_link_revoke(self, sid):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            link = _db["share_links"].get(sid)
            if not link or not _rec_for_owner(link.get("rec_id"), uid):
                self._send_json(403, {"error": "无权操作该链接"})
                return
            link["revoked"] = True
            _save_store()
        self._send_json(200, {"success": True})

    def _handle_links_list(self, rec_id):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        with _store_lock:
            if not _rec_for_owner(rec_id, uid):
                self._send_json(403, {"error": "只能查看自己的录制"})
                return
            now = int(time.time())
            links = [
                {"sid": sid, "created": l["created"], "expires_at": l.get("expires_at"),
                 "revoked": l.get("revoked") or (l.get("expires_at") and now > l["expires_at"])}
                for sid, l in _db["share_links"].items() if l.get("rec_id") == rec_id
            ]
        links.sort(key=lambda x: -x["created"])
        self._send_json(200, {"links": links})

    def _handle_link_file(self, sid):
        """unlisted 链接访问：无需登录，凭 URL 中的令牌；已吊销/过期/已删除一律 404。"""
        query = urllib.parse.parse_qs(self.path.split("?", 1)[-1]) if "?" in self.path else {}
        token = (query.get("tk") or [None])[0]
        with _store_lock:
            link = _db["share_links"].get(sid)
            if not link or link.get("revoked"):
                return self._send(404, b"Not Found", "text/plain")
            if link.get("expires_at") and time.time() > link["expires_at"]:
                return self._send(404, b"Not Found", "text/plain")
            if not token or not secrets.compare_digest(
                    hashlib.sha256(token.encode()).hexdigest(), link.get("token_hash", "")):
                return self._send(404, b"Not Found", "text/plain")
            rec = _rec_visible(link.get("rec_id"))
            if not rec:
                return self._send(404, b"Not Found", "text/plain")
            path = SHARED_DIR / rec["stored"]
        if not path.is_file():
            return self._send(404, b"Not Found", "text/plain")
        size = path.stat().st_size
        self.connection.settimeout(300)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        title = urllib.parse.quote(rec.get("title") or "recording.lecture.zip")
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{title}")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as src:
                shutil.copyfileobj(src, self.wfile, length=64 * 1024)

    # ── 播放进度（用户级，不写入录制包）──
    def _handle_progress(self):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录"})
        data, err = self._read_json_body(65536)
        if err:
            return
        rid = data.get("recording_id")
        try:
            pos = max(0, int(data.get("position_ms") or 0))
        except (TypeError, ValueError):
            pos = 0
        with _store_lock:
            if not _can_view(uid, rid):
                return self._send_json(403, {"error": "无权访问该录制"})
            _db["user_progress"].setdefault(uid, {})[rid] = {"pos_ms": pos, "updated": int(time.time())}
            _save_store()
        self._send_json(200, {"success": True})

    def _handle_ppt_convert(self):
        """Convert an uploaded PowerPoint file to PDF for the existing PDF renderer."""
        if not self._session_uid():
            return self._send_json(401, {"error": "未登录"})
        soffice = shutil.which("libreoffice") or shutil.which("soffice")
        if not soffice:
            return self._send_json(503, {"error": "服务器尚未安装 LibreOffice"})
        if not _heavy_lock.acquire(blocking=False):
            return self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
        tmp_path = None
        work_dir = None
        try:
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._send_json(400, {"error": "需要 multipart/form-data"})
            boundary = self._get_boundary(ctype)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not boundary or length <= 0:
                return self._send_json(400, {"error": "上传内容为空"})
            if length > MAX_UPLOAD_BYTES:
                return self._send_json(413, {"error": f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024*1024)}MB）"})
            self.connection.settimeout(120)
            SHARED_DIR.mkdir(exist_ok=True)
            filename, tmp_path, _, remaining, complete = self._stream_multipart_file(length, boundary)
            if not filename or not tmp_path or remaining > 0 or not complete:
                return self._send_json(400, {"error": "PPT 上传不完整"})
            suffix = Path(filename).suffix.lower()
            if suffix not in (".ppt", ".pptx"):
                return self._send_json(415, {"error": "仅支持 .ppt 和 .pptx"})
            work_dir = Path(tempfile.mkdtemp(prefix="lecturelite-ppt-"))
            src = work_dir / ("source" + suffix)
            os.replace(tmp_path, src)
            tmp_path = None
            profile = (work_dir / "profile").resolve().as_uri()
            result = subprocess.run(
                [soffice, f"-env:UserInstallation={profile}", "--headless",
                 "--convert-to", "pdf:impress_pdf_Export", "--outdir", str(work_dir), str(src)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False
            )
            pdf_path = work_dir / "source.pdf"
            if result.returncode != 0 or not pdf_path.is_file():
                detail = result.stdout.decode("utf-8", "replace").strip()[-500:]
                return self._send_json(422, {"error": "PPT 转换失败" + (("：" + detail) if detail else "")})
            self.connection.settimeout(300)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(pdf_path.stat().st_size))
            self.send_header("Content-Disposition", "inline; filename=converted.pdf")
            self.end_headers()
            if self.command != "HEAD":
                with pdf_path.open("rb") as src_file:
                    shutil.copyfileobj(src_file, self.wfile, length=64 * 1024)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "PPT 转换超时"})
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            _heavy_lock.release()

    def _handle_audio_compress(self):
        """Transcode generated PCM WAV audio to a compact, speech-focused MP3."""
        if not self._session_uid():
            return self._send_json(401, {"error": "未登录"})
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return self._send_json(503, {"error": "服务器尚未安装 FFmpeg"})
        if not _heavy_lock.acquire(blocking=False):
            return self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
        tmp_path = None
        work_dir = None
        try:
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                return self._send_json(400, {"error": "需要 multipart/form-data"})
            boundary = self._get_boundary(ctype)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not boundary or length <= 0:
                return self._send_json(400, {"error": "音频上传内容为空"})
            if length > MAX_AUDIO_TRANSCODE_BYTES:
                return self._send_json(413, {"error": "待压缩音频超过 300MB"})
            self.connection.settimeout(300)
            SHARED_DIR.mkdir(exist_ok=True)
            filename, tmp_path, _, remaining, complete = self._stream_multipart_file(length, boundary)
            if not filename or not tmp_path or remaining > 0 or not complete:
                return self._send_json(400, {"error": "音频上传不完整"})
            work_dir = Path(tempfile.mkdtemp(prefix="lecturelite-audio-"))
            src = work_dir / "source.wav"
            out = work_dir / "speech.mp3"
            os.replace(tmp_path, src)
            tmp_path = None
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                 "-c:a", "libmp3lame", "-b:a", "24k", "-map_metadata", "-1", str(out)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180, check=False
            )
            if result.returncode != 0 or not out.is_file():
                detail = result.stdout.decode("utf-8", "replace").strip()[-500:]
                return self._send_json(422, {"error": "音频压缩失败" + (("：" + detail) if detail else "")})
            self.connection.settimeout(300)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(out.stat().st_size))
            self.send_header("Content-Disposition", "inline; filename=speech.mp3")
            self.end_headers()
            with out.open("rb") as audio_file:
                shutil.copyfileobj(audio_file, self.wfile, length=64 * 1024)
        except subprocess.TimeoutExpired:
            self._send_json(504, {"error": "音频压缩超时"})
        finally:
            if tmp_path:
                try:
                    Path(tmp_path).unlink()
                except OSError:
                    pass
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            _heavy_lock.release()

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?")[0])

        # /tts-voices → edge-tts 中文语音列表（需登录）
        if path == "/tts-voices":
            if not self._session_uid():
                return self._send_json(401, {"error": "未登录"})
            if _tts_list_voices is None:
                self._send_json(503, {"error": "llm.tts_generator 未找到（需 pip install edge-tts）"})
                return
            try:
                self._send_json(200, {"success": True, "voices": _tts_list_voices()})
            except Exception as e:
                self._send_json(500, {"error": f"获取语音列表失败: {e}"})
            return

        # ── 账号 API ──
        if path == "/api/me":
            return self._handle_me()
        if path == "/api/recordings":
            return self._handle_recordings_list()
        if path == "/api/progress":
            return self._send_json(405, {"error": "用 POST 提交进度"})
        m = None
        if path.startswith("/api/recordings/") and path.endswith("/file"):
            m = path[len("/api/recordings/"):-len("/file")]
        if m:
            return self._handle_recording_file(m)
        if path.startswith("/api/recordings/") and path.endswith("/links"):
            return self._handle_links_list(path[len("/api/recordings/"):-len("/links")])
        # /s/<sid> → 分享链接落地页（带令牌跳进播放器）
        if path.startswith("/s/"):
            sid = path[len("/s/"):]
            params = urllib.parse.parse_qs(self.path.split("?", 1)[-1]) if "?" in self.path else {}
            tk = (params.get("tk") or [""])[0]
            file_url = "/api/link/" + sid + "/file" + (f"?tk={urllib.parse.quote(tk)}" if tk else "")
            self.send_response(302)
            self.send_header("Location", "/lecture-lite.html?src=" + urllib.parse.quote(file_url, safe=""))
            self.end_headers()
            return
        if path.startswith("/api/link/") and path.endswith("/file"):
            return self._handle_link_file(path[len("/api/link/"):-len("/file")])

        # 静态文件（白名单；登录态由页面内 /api/me 检查并弹出登录层）
        if path == "/":
            path = "/lecture-lite.html"
        target = resolve_static(path)
        if target:
            ctype = self._guess_type(target.name)
            body = target.read_bytes()
            self._send(200, body, ctype)
            return

        self._send(404, b"Not Found", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        known = ("/api/register", "/api/login", "/api/logout", "/api/convert-ppt", "/api/compress-audio",
                 "/api/upload", "/share", "/api/progress",
                 "/generate-script", "/generate-script-stream",
                 "/generate-script-timeline", "/generate-speech-stream")
        if path not in known and not (path.startswith("/api/recordings/")
                                      and len(path.strip("/").split("/")) in (4, 5)):
            return self._send(404, b"Not Found", "text/plain")
        if path == "/api/register":
            return self._handle_register()
        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()
        if path == "/api/convert-ppt":
            return self._handle_ppt_convert()
        if path == "/api/compress-audio":
            return self._handle_audio_compress()
        if path == "/api/upload" or path == "/share":
            return self._handle_upload()
        if path == "/api/progress":
            return self._handle_progress()
        if path.startswith("/api/recordings/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4:                  # api/recordings/<id>/<action>
                rid, action = parts[2], parts[3]
                if action == "favorite":
                    return self._handle_favorite(rid)
                if action == "share":
                    return self._handle_share_to(rid)
                if action == "delete":
                    return self._handle_recording_delete(rid)
                if action == "restore":
                    return self._handle_recording_restore(rid)
                if action == "purge":
                    return self._handle_recording_purge(rid)
                if action == "link":
                    return self._handle_link_create(rid)
            elif len(parts) == 5 and parts[2] == "link" and parts[4] == "revoke":
                # api/recordings/link/<sid>/revoke
                return self._handle_link_revoke(parts[3])
            return self._send(404, b"Not Found", "text/plain")
        # 生成类端点仍要求登录
        if not self._session_uid():
            return self._send_json(401, {"error": "未登录"})
        if path == "/generate-script":
            self._handle_generate_script()
        elif path == "/generate-script-stream":
            self._handle_generate_script_stream()
        elif path == "/generate-script-timeline":
            self._handle_generate_script_timeline()
        elif path == "/generate-speech-stream":
            self._handle_generate_speech_stream()

    # ── 请求体读取（先于任何响应头，失败可安全返回普通错误码）──
    def _read_json_body(self, max_bytes):
        """读取并解析 JSON 请求体。返回 (data, error_response)。
        error_response 非 None 时已发送响应，调用方直接 return。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "空请求体"})
            return None, True
        if length > max_bytes:
            self._send_json(413, {"error": f"请求体过大（上限 {max_bytes // (1024*1024)}MB）"})
            return None, True
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError):
            self._send_json(408, {"error": "读取请求体超时"})
            return None, True
        try:
            return json.loads(raw.decode("utf-8")), None
        except Exception:
            self._send_json(400, {"error": "请求体不是合法 JSON"})
            return None, True

    # ── /api/upload（原 /share；登录后归属当前用户）──
    def _handle_upload(self):
        uid = self._session_uid()
        if not uid:
            return self._send_json(401, {"error": "未登录，请先注册 / 登录后再上传"})
        if not _heavy_lock.acquire(blocking=False):
            self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
            return
        try:
            self._handle_upload_inner(uid)
        finally:
            _heavy_lock.release()

    def _handle_upload_inner(self, uid):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send(400, b"Expected multipart/form-data", "text/plain")
            return

        boundary = self._get_boundary(ctype)
        if not boundary:
            self._send(400, b"No boundary", "text/plain")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send(400, b"Empty upload", "text/plain")
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json(413, {"error": f"文件过大（上限 {MAX_UPLOAD_BYTES // (1024*1024)}MB）"})
            return

        SHARED_DIR.mkdir(exist_ok=True)
        filename, tmp_path, size, remaining, complete = self._stream_multipart_file(length, boundary)
        # 读不完（客户端断流）：丢弃临时文件
        if tmp_path and (remaining > 0 or not filename or not complete):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if not filename or not tmp_path or not complete:
            self._send(400, b"Incomplete or malformed multipart upload", "text/plain")
            return

        share_id = uuid.uuid4().hex[:8]
        safe_name = os.path.basename(filename)
        stored_name = f"{share_id}_{safe_name}"
        os.replace(tmp_path, SHARED_DIR / stored_name)

        rec_id = _add_recording(uid, safe_name, stored_name)
        url = f"{PUBLIC_BASE_URL}/api/recordings/{rec_id}/file"

        self._send_json(200, {"url": url, "id": rec_id, "rec_id": rec_id, "size": size})
        # 结束边界后若还有字节，不复用连接，避免残留字节被解释成下一次 HTTP 请求。
        self.close_connection = True

    # ── /generate-script ──
    def _handle_generate_script(self):
        if not _gen_script:
            self._send_json(503, {"error": "llm.script_generator 未找到（需 pip install openai）"})
            return
        if not _heavy_lock.acquire(blocking=False):
            self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
            return
        try:
            data, err = self._read_json_body(MAX_JSON_BYTES)
            if err:
                return
            content = data.get("content", "")
            if not content:
                self._send_json(400, {"error": "缺少文档内容"})
                return
            try:
                script = _gen_script(content, data.get("topic", ""), data.get("length", "medium"))
                self._send_json(200, {"success": True, "script": script})
            except Exception as e:
                self._send_json(500, {"error": f"生成失败: {str(e)}"})
        finally:
            _heavy_lock.release()

    # ── /generate-script-stream（流式输出）──
    def _handle_generate_script_stream(self):
        if not _gen_script_stream:
            self._send_json(503, {"error": "llm.script_generator 未找到（需 pip install openai）"})
            return
        if not _heavy_lock.acquire(blocking=False):
            self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
            return
        try:
            data, err = self._read_json_body(MAX_JSON_BYTES)
            if err:
                return
            content = data.get("content", "")
            if not content:
                self._send_json(400, {"error": "缺少文档内容"})
                return
            self._sse_headers()
            full_text = ""
            try:
                for piece in _gen_script_stream(content, data.get("topic", ""), data.get("length", "medium")):
                    full_text += piece
                    self._sse_write({"delta": piece})
                self._sse_write({"done": True, "full": full_text})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception as e:
                # 响应头已发出，只能以 SSE 错误事件通知
                self._sse_write({"error": str(e)})
            finally:
                self.close_connection = True
        finally:
            _heavy_lock.release()

    # ── /generate-script-timeline（流式 NDJSON 透传）──
    def _handle_generate_script_timeline(self):
        if not _gen_script_timeline_stream:
            self._send_json(503, {"error": "llm.script_generator 未找到（需 pip install openai）"})
            return
        if not _heavy_lock.acquire(blocking=False):
            self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
            return
        try:
            data, err = self._read_json_body(MAX_JSON_BYTES)
            if err:
                return
            content = data.get("content", "")
            if not content:
                self._send_json(400, {"error": "缺少文档内容"})
                return
            self._sse_headers()
            try:
                for piece in _gen_script_timeline_stream(content, data.get("topic", ""), data.get("length", "medium")):
                    self._sse_write({"delta": piece})
                self.wfile.write(b"data: {\"done\":true}\n\n")
                self.wfile.flush()
            except Exception as e:
                self._sse_write({"error": str(e)})
            finally:
                self.close_connection = True
        finally:
            _heavy_lock.release()

    def _sse_headers(self):
        # 必须在任何请求校验都通过、且只调用一次后才进入流式
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._send_session_cookie()
        self.end_headers()

    def _sse_write(self, obj):
        chunk = f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    # ── /generate-speech-stream（SSE 逐句返回合成音频，base64 mp3）──
    def _handle_generate_speech_stream(self):
        if _tts_synth_stream is None:
            self._send_json(503, {"error": "llm.tts_generator 未找到（需 pip install edge-tts）"})
            return
        if not _heavy_lock.acquire(blocking=False):
            self._send_json(429, {"error": "服务器繁忙，请稍后再试"})
            return
        try:
            data, err = self._read_json_body(MAX_JSON_BYTES)
            if err:
                return
            sentences = data.get("sentences", [])
            voice = data.get("voice") or _TTS_DEFAULT_VOICE
            try:
                rate = float(data.get("rate", 1.0))
            except (TypeError, ValueError):
                rate = 1.0
            rate = max(0.5, min(2.0, rate))
            if not isinstance(sentences, list) or not sentences:
                self._send_json(400, {"error": "缺少句子列表"})
                return
            if len(sentences) > MAX_TTS_SENTENCES:
                self._send_json(400, {"error": f"句子过多（上限 {MAX_TTS_SENTENCES} 句）"})
                return
            if not all(isinstance(s, str) and 0 < len(s) <= MAX_TTS_SENTENCE_LEN for s in sentences):
                self._send_json(400, {"error": f"句子必须是 1~{MAX_TTS_SENTENCE_LEN} 字的字符串"})
                return
            self._sse_headers()
            try:
                async def _drive():
                    async for idx, audio in _tts_synth_stream(sentences, voice, rate):
                        if audio is None:
                            self._sse_write({"i": idx, "empty": True})
                        else:
                            self._sse_write({"i": idx, "b64": base64.b64encode(audio).decode("ascii")})
                    self._sse_write({"done": True})
                asyncio.run(_drive())
            except Exception as e:
                try:
                    self._sse_write({"error": f"语音合成失败: {e}"})
                except Exception:
                    pass
            finally:
                self.close_connection = True
        finally:
            _heavy_lock.release()

    # ── multipart helpers ──

    @staticmethod
    def _get_boundary(ctype):
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                return part[len("boundary="):].strip('"')
        return None

    @staticmethod
    def _extract_filename(header_block):
        for line in header_block.split(b"\r\n"):
            line = line.decode("utf-8", "replace")
            if "filename=" in line.lower():
                for seg in line.split(";"):
                    seg = seg.strip()
                    if seg.lower().startswith("filename="):
                        return seg[len("filename="):].strip('"') or None
        return None

    @staticmethod
    def _append_file(path, data):
        with open(path, "ab") as f:
            f.write(data)

    def _stream_multipart_file(self, length, boundary):
        """流式读取 multipart 上传，只把文件部分写入临时文件。
        返回 (filename, tmp_path, size, remaining, complete)。"""
        delim = b"--" + boundary.encode()
        keep = len(delim) + 2
        buf = b""
        remaining = length
        filename = None
        tmp_path = None
        size = 0
        done = False
        while remaining > 0 and not done:
            try:
                chunk = self.rfile.read(min(1 << 20, remaining))
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            remaining -= len(chunk)
            buf += chunk
            while True:
                if tmp_path is None:
                    idx = buf.find(delim)
                    if idx < 0:
                        buf = buf[-keep:] if len(buf) > keep else buf
                        break
                    buf = buf[idx + len(delim):]
                    if buf.startswith(b"--"):
                        done = True
                        break
                    if buf.startswith(b"\r\n"):
                        buf = buf[2:]
                    hidx = buf.find(b"\r\n\r\n")
                    if hidx < 0:
                        if len(buf) > MAX_MULTIPART_HEADER_BYTES:
                            return None, tmp_path, size, remaining, False
                        break
                    header_block, rest = buf[:hidx], buf[hidx + 4:]
                    buf = rest
                    filename = self._extract_filename(header_block)
                    if not filename:
                        # 非文件 part：丢弃到下一个分隔符
                        idx2 = buf.find(delim)
                        if idx2 >= 0:
                            buf = buf[idx2 + len(delim):]
                            continue
                        buf = buf[-keep:] if len(buf) > keep else b""
                        break
                    fd, tmp_path = tempfile.mkstemp(suffix=".part", dir=str(SHARED_DIR))
                    os.close(fd)
                    continue
                idx = buf.find(delim)
                if idx < 0:
                    if len(buf) > keep:
                        data, buf = buf[:-keep], buf[-keep:]
                        self._append_file(tmp_path, data)
                        size += len(data)
                    break
                data = buf[:idx]
                buf = buf[idx + len(delim):]
                if data.endswith(b"\r\n"):
                    data = data[:-2]
                self._append_file(tmp_path, data)
                size += len(data)
                # 分享端点只接收一个文件 part；文件后的分隔符必须是
                # multipart 的最终边界（--boundary--），而非下一个 part。
                # 边界的两个 '-' 可能恰好跨越 socket read，因此补读至多两个字节。
                while len(buf) < 2 and remaining > 0:
                    try:
                        tail = self.rfile.read(min(2 - len(buf), remaining))
                    except (TimeoutError, OSError):
                        return filename, tmp_path, size, remaining, False
                    if not tail:
                        return filename, tmp_path, size, remaining, False
                    remaining -= len(tail)
                    buf += tail
                if not buf.startswith(b"--"):
                    return filename, tmp_path, size, remaining, False
                tail = buf[2:]
                # 最终边界之后只允许可选 CRLF；其他 part 或尾随内容一律拒绝。
                if len(tail) + remaining > 2:
                    return filename, tmp_path, size, remaining, False
                if remaining:
                    try:
                        tail += self.rfile.read(remaining)
                    except (TimeoutError, OSError):
                        return filename, tmp_path, size, remaining, False
                    if len(tail) > 2:
                        return filename, tmp_path, size, remaining, False
                    remaining = 0
                if tail not in (b"", b"\r\n"):
                    return filename, tmp_path, size, remaining, False
                done = True
                break
        return filename, tmp_path, size, remaining, done

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

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


URL_SCHEME = "https"
PUBLIC_BASE_URL = "https://47.109.104.139:8663"


def ensure_self_signed_cert():
    """首次启动生成自签名证书（cryptography），返回 (cert, key) 路径。"""
    CERT_DIR.mkdir(exist_ok=True)
    cert_p, key_p = CERT_DIR / "server.crt", CERT_DIR / "server.key"
    if cert_p.is_file() and key_p.is_file():
        return cert_p, key_p
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime, ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LectureLite")])
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(get_lan_ip())))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


def main():
    global LAN_IP, URL_SCHEME
    args = [a for a in sys.argv[1:]]
    lan = ("--lan" in args) or os.environ.get("LL_LAN") == "1"
    use_https = "--http" not in args
    pos = [a for a in args if not a.startswith("--") and a.lstrip("-").isdigit()]
    port = int(pos[0]) if pos else 8000

    if not (WEB_DIR / "lecture-lite.html").exists():
        print(f"找不到 lecture-lite.html — serve.py 必须和它放在同一目录")
        sys.exit(1)

    _cleanup_expired_shares()
    _load_store()
    _seed_demo_courses()
    _purge_expired_trash()
    threading.Thread(target=_purge_loop, daemon=True).start()

    bind = "0.0.0.0" if lan else "127.0.0.1"
    if lan:
        LAN_IP = get_lan_ip()
    else:
        LAN_IP = "127.0.0.1"

    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        """多线程 HTTP 服务器，支持流式响应。"""
        daemon_threads = True

    httpd = ThreadedHTTPServer((bind, port), Handler)

    # 全站 HTTPS（--http 可关闭）
    if use_https:
        try:
            cert_p, key_p = ensure_self_signed_cert()
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(cert_p), str(key_p))
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        except ImportError:
            use_https = False
            print("未安装 cryptography（pip install cryptography），无法启用 HTTPS，回退 HTTP")
    if not use_https:
        URL_SCHEME = "http"
        print("已按 --http 参数禁用 HTTPS，使用明文 HTTP（仅建议本机调试）")

    url = f"{URL_SCHEME}://127.0.0.1:{port}/home.html"
    print("LectureLite 服务已启动（HTTPS）")
    if lan:
        print(f"局域网模式：绑定 0.0.0.0")
        print(f"本机访问: {url}")
        print(f"局域网访问: {URL_SCHEME}://{LAN_IP}:{port}/lecture-lite.html")
    else:
        print("本机模式：仅绑定 127.0.0.1（局域网访问请加 --lan）")
        print(f"本机访问: {url}")

    webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        httpd.server_close()


if __name__ == "__main__":
    main()
