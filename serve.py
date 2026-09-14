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

import sys, os, json, uuid, socket, webbrowser, urllib.parse, urllib.request, threading, asyncio, base64, tempfile, time, secrets, hashlib, ssl, shutil, subprocess, zipfile, ipaddress, re, html
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from html.parser import HTMLParser

# ── LLM 文稿生成（按需导入，无 openai 包也能工作）──
try:
    from llm.script_generator import generate_script as _gen_script
    from llm.script_generator import generate_script_stream as _gen_script_stream
    from llm.script_generator import generate_script_timeline_stream as _gen_script_timeline_stream
except ImportError:
    _gen_script = None
    _gen_script_stream = None
    _gen_script_timeline_stream = None

try:
    from llm.learning_feedback import generate_interactions as _gen_interactions
    from llm.learning_feedback import grade_short_answer as _grade_short_answer
    from llm.learning_feedback import answer_question as _answer_course_question
except ImportError:
    _gen_interactions = _grade_short_answer = _answer_course_question = None

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
LEARNER_COOKIE = "ll_learner"
PBKDF2_ITERS = 200_000
SHARE_TOKEN_BYTES = 24        # 分享令牌熵
AUTH_MAX_ATTEMPTS = 10        # 登录/注册限速：每 IP+UID 5 分钟内最多尝试次数
AUTH_WINDOW_SECONDS = 300
TRASH_KEEP_SECONDS = 30 * 86400   # 回收站保留 30 天，到期自动清空
DRAFT_KEEP_SECONDS = 30 * 86400   # 草稿最后编辑 30 天后自动清理
DEMO_DIR = WEB_DIR / "demo"       # 示例课程包（不可删除，自动出现在收藏里）
WEB_IMPORT_DIR = DATA_DIR / "web-imports"

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
STATIC_ROOT_FILES = {"lecture-lite.html", "home.html", "admin.html"}
STATIC_DIRS = {"tools", "docs"}
ADMIN_UIDS = {x.strip() for x in os.environ.get("LL_ADMIN_UIDS", "").split(",") if x.strip()}

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
_learn_attempts = {}  # "ip|action" -> [timestamps]
_db = {
    "users": {}, "recordings": {}, "recording_shares": {},
    "user_favorites": {}, "user_progress": {}, "share_links": {},
    "learner_sessions": {}, "interaction_attempts": {}, "learner_questions": {},
    "author_replies": {}, "learning_events": {}, "admin_audit_logs": {}, "web_imports": {}, "lecture_drafts": {},
}


def _load_store():
    DATA_DIR.mkdir(exist_ok=True)
    if not DB_FILE.is_file():
        _migrate_legacy()
        return
    missing_collections = set()
    try:
        data = json.loads(DB_FILE.read_text("utf-8"))
        missing_collections = {k for k in _db if k not in data}
        for k in _db:
            _db[k].update(data.get(k) or {})
    except Exception as e:
        print(f"警告：读取 {DB_FILE.name} 失败: {e}")
    _migrate_legacy()
    changed = bool(missing_collections)
    with _store_lock:
        for uid, user in _db["users"].items():
            if "disabled" not in user: user["disabled"] = False; changed = True
            if "force_password_change" not in user: user["force_password_change"] = False; changed = True
            if "created_at" not in user:
                dates = [int(r.get("created") or 0) for r in _db["recordings"].values()
                         if r.get("owner") == uid and r.get("created")]
                user["created_at"] = min(dates) if dates else None; changed = True
            if "last_login_at" not in user: user["last_login_at"] = None; changed = True
        if changed: _save_store()


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


def _delete_learning_data_locked(rid):
    """Delete all anonymous learning data for a permanently removed recording.

    Caller must hold ``_store_lock``.
    """
    session_ids = {sid for sid, row in _db["learner_sessions"].items()
                   if row.get("recording_id") == rid}
    for sid in session_ids:
        _db["learner_sessions"].pop(sid, None)
        _db["interaction_attempts"].pop(sid, None)
    _db["learner_questions"].pop(rid, None)
    _db["author_replies"].pop(rid, None)
    _db["learning_events"].pop(rid, None)


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
            _delete_learning_data_locked(rid)
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


def _purge_expired_drafts():
    cutoff = time.time() - DRAFT_KEEP_SECONDS
    files, dirs = [], []
    with _store_lock:
        for did, row in list(_db["lecture_drafts"].items()):
            if (row.get("updated") or row.get("created") or 0) < cutoff:
                files.append(row.get("stored")); del _db["lecture_drafts"][did]
        for did, row in list(_db["web_imports"].items()):
            if (row.get("updated") or row.get("created") or 0) < cutoff:
                dirs.append(did); del _db["web_imports"][did]
        if files or dirs: _save_store()
    for stored in files:
        if stored:
            try: (SHARED_DIR / stored).unlink()
            except OSError: pass
    for did in dirs: shutil.rmtree(WEB_IMPORT_DIR / did, ignore_errors=True)


def _purge_loop():
    while True:
        time.sleep(3600)
        try:
            _purge_expired_trash()
            _purge_expired_drafts()
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
        now = int(time.time())
        _db["users"][uid] = {"salt": salt, "hash": _hash_password(password, salt),
                             "created_at": now, "last_login_at": now,
                             "disabled": False, "force_password_change": False}
        _save_store()
    return uid, None


def _verify_user(uid, password):
    with _store_lock:
        u = _db["users"].get(str(uid or "").strip())
        if not u or u.get("disabled"):
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


def _is_admin(uid):
    return bool(uid and uid in ADMIN_UIDS)


def _invalidate_user_sessions(uid, keep_token=None):
    for token, session_uid in list(_sessions.items()):
        if session_uid == uid and token != keep_token:
            _sessions.pop(token, None)


def _audit_admin(actor, action, target, success=True, detail=""):
    row_id = uuid.uuid4().hex[:16]
    _db["admin_audit_logs"][row_id] = {
        "id": row_id, "actor": actor, "action": action, "target": str(target or "")[:128],
        "success": bool(success), "detail": str(detail or "")[:300], "created": int(time.time())
    }
    if len(_db["admin_audit_logs"]) > 5000:
        oldest = sorted(_db["admin_audit_logs"].values(), key=lambda x: x.get("created", 0))[:500]
        for row in oldest:
            _db["admin_audit_logs"].pop(row["id"], None)


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


def _valid_share_access(rid, sid, token):
    """Validate unlisted-link access without leaking whether a recording exists."""
    link = _db["share_links"].get(str(sid or ""))
    if not link or link.get("rec_id") != rid or link.get("revoked"):
        return False
    if link.get("expires_at") and time.time() > link["expires_at"]:
        return False
    digest = hashlib.sha256(str(token or "").encode()).hexdigest()
    return bool(token) and secrets.compare_digest(digest, link.get("token_hash", ""))


def _course_json(rid):
    rec = _rec_visible(rid)
    if not rec:
        return {}
    path = SHARED_DIR / rec.get("stored", "")
    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("lecture.json")
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}


def _interaction_from_course(rid, interaction_id, revision):
    for item in _course_json(rid).get("interactions") or []:
        if item.get("id") == interaction_id and int(item.get("revision") or 1) == int(revision or 1):
            return item
    return None


def _learning_context(rid, client_context=None):
    course = _course_json(rid)
    position = max(0, int((client_context or {}).get("position_ms") or 0))
    script = course.get("script") or []
    nearby = [row for row in script if abs(int(row.get("t") or 0) - position) <= 45000]
    return {
        "position_ms": position,
        "time": f"{position//60000}:{(position//1000)%60:02d}",
        "file_name": str((client_context or {}).get("file_name") or "")[:300],
        "slide": (client_context or {}).get("slide"),
        "subtitle": str((client_context or {}).get("subtitle") or "")[:1000],
        "nearby_script": "\n".join(str(x.get("text") or "") for x in nearby)[:8000],
        "client_excerpt": str((client_context or {}).get("nearby_script") or "")[:4000],
    }


def _learn_rate_ok(ip, action, limit=40, window=3600):
    now = time.time(); key = f"{ip}|{action}"
    with _store_lock:
        stamps = [x for x in _learn_attempts.get(key, []) if now - x < window]
        if len(stamps) >= limit:
            _learn_attempts[key] = stamps
            return False
        stamps.append(now); _learn_attempts[key] = stamps
    return True


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


class _WebPageMarkdown(HTMLParser):
    """Small, dependency-free HTML → readable Markdown converter."""
    _skip = {"script", "style", "noscript", "svg", "canvas", "iframe", "nav", "footer", "aside", "form"}
    _blocks = {"p", "div", "section", "article", "main", "header", "figure", "figcaption", "blockquote"}

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url, self.parts, self.images, self.media, self.title = base_url, [], [], [], ""
        self._skip_depth = self._heading = 0; self._in_title = self._in_pre = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self._skip: self._skip_depth += 1; return
        if self._skip_depth: return
        if tag == "title": self._in_title = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}: self._heading = int(tag[1]); self.parts.append("\n\n" + "#" * self._heading + " ")
        elif tag in self._blocks: self.parts.append("\n\n")
        elif tag == "br": self.parts.append("\n")
        elif tag in {"ul", "ol"}: self.parts.append("\n")
        elif tag == "li": self.parts.append("\n- ")
        elif tag == "pre": self._in_pre = True; self.parts.append("\n\n```text\n")
        elif tag == "code" and not self._in_pre: self.parts.append("`")
        elif tag == "blockquote": self.parts.append("\n> ")
        elif tag == "a":
            href = attrs.get("href")
            if href and not href.lower().startswith(("javascript:", "mailto:")):
                self.parts.append("["); self._link = urllib.parse.urljoin(self.base_url, href)
        elif tag == "img":
            src = attrs.get("src") or attrs.get("data-src") or attrs.get("data-original")
            if src:
                src = urllib.parse.urljoin(self.base_url, src)
                if src.startswith(("http://", "https://")): self.images.append((src, (attrs.get("alt") or "图片").strip()[:120]))
        elif tag in {"video", "audio", "source"}:
            src = attrs.get("src")
            if src:
                src = urllib.parse.urljoin(self.base_url, src)
                if src.startswith(("http://", "https://")): self.media.append((tag, src))

    def handle_endtag(self, tag):
        if tag in self._skip: self._skip_depth = max(0, self._skip_depth - 1); return
        if self._skip_depth: return
        if tag == "title": self._in_title = False
        elif tag == "pre": self._in_pre = False; self.parts.append("\n```\n")
        elif tag == "code" and not self._in_pre: self.parts.append("`")
        elif tag == "a" and hasattr(self, "_link"): self.parts.append("](" + self._link + ")"); del self._link

    def handle_data(self, data):
        if self._skip_depth: return
        clean = re.sub(r"\s+", " ", data).strip() if not self._in_pre else data
        if not clean: return
        if self._in_title: self.title += clean + " "
        else: self.parts.append(clean + ("\n" if self._in_pre else " "))

    def markdown(self):
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", "".join(self.parts))).strip()


def _safe_web_url(value):
    """Accept public HTTP(S) only; block localhost/private-address SSRF targets."""
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请输入完整的 http:// 或 https:// 公开网页地址")
    host = parsed.hostname.rstrip(".")
    if host.lower() == "localhost": raise ValueError("不能抓取本机或内网地址")
    try: addresses = {x[4][0] for x in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError: raise ValueError("无法解析该网址的域名")
    if any(not ipaddress.ip_address(raw).is_global for raw in addresses): raise ValueError("不能抓取本机、内网或保留地址")
    return parsed.geturl()


def _fetch_public(url, max_bytes, accept="text/html,application/xhtml+xml"):
    """Fetch a bounded redirect chain and revalidate every redirect target."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener, current = urllib.request.build_opener(_NoRedirect), _safe_web_url(url)
    for _ in range(5):
        request = urllib.request.Request(current, headers={"User-Agent": "LectureLite/1.0 (personal study importer)", "Accept": accept})
        try: response = opener.open(request, timeout=12)
        except urllib.error.HTTPError as exc:
            if exc.code in (301,302,303,307,308) and exc.headers.get("Location"):
                current = _safe_web_url(urllib.parse.urljoin(current, exc.headers["Location"])); continue
            raise ValueError(f"网页抓取失败（HTTP {exc.code}）")
        except (urllib.error.URLError, TimeoutError, OSError): raise ValueError("无法连接该网页或请求超时")
        with response:
            final, content_type, data = _safe_web_url(response.geturl()), response.headers.get_content_type(), response.read(max_bytes + 1)
            if len(data) > max_bytes: raise ValueError("网页内容过大，暂不支持导入")
            return final, content_type, data
    raise ValueError("网页重定向次数过多")


def _article_fragment(page):
    """Prefer a known article body before the generic converter sees site chrome."""
    needles = ("content_views", "article_content", "article-content", "articlecontent",
               "post-content", "post_content", "blog-content", "article-body")
    for needle in needles:
        start = re.search(r"<(article|main|div)\\b[^>]*(?:id|class)=[\"'][^\"']*" + re.escape(needle) + r"[^\"']*[\"'][^>]*>", page, re.I)
        if not start:
            continue
        tag = start.group(1).lower(); depth = 0
        for match in re.finditer(r"</?(article|main|div)\\b[^>]*>", page[start.start():], re.I):
            raw, name = match.group(0), match.group(1).lower()
            if name != tag:
                continue
            if raw.startswith("</"):
                depth -= 1
                if depth == 0:
                    return page[start.start():start.start() + match.end()]
            elif not raw.rstrip().endswith("/>"):
                depth += 1
    return page


def _clean_import_markdown(text):
    """Remove common web chrome that may still be nested inside an article node."""
    noise = re.compile(r"^(?:点赞|收藏|关注|分享|评论|扫一扫|登录后.*|广告|推荐阅读|相关文章|版权声明|发布于.*|阅读量.*|原创.*(?:发布于|阅读).*)$", re.I)
    kept, previous = [], ""
    for line in text.splitlines():
        trimmed = line.strip()
        if trimmed and noise.match(trimmed):
            continue
        if trimmed and trimmed == previous:
            continue
        kept.append(line); previous = trimmed or previous
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _decode_web_html(raw):
    """Honor page-declared encodings; GB18030 is a useful fallback for legacy CN sites."""
    head = raw[:8192].decode("latin1", "ignore")
    declared = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_.-]+)", head, re.I)
    encodings = [declared.group(1)] if declared else []
    encodings += ["utf-8", "gb18030", "gbk"]
    tried = set()
    for encoding in encodings:
        encoding = encoding.lower()
        if encoding in tried: continue
        tried.add(encoding)
        try: return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError): continue
    return raw.decode("utf-8", "replace")


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
        if token == "":
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
        elif token is not None:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict",
            )
        if token is not None:
            self._cookie_to_set = None
        learner = getattr(self, "_learner_cookie_to_set", None)
        if learner:
            self.send_header(
                "Set-Cookie",
                f"{LEARNER_COOKIE}={learner}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000",
            )
            self._learner_cookie_to_set = None

    def _learner_session(self, rid=None):
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookies.get(LEARNER_COOKIE)
            if not morsel or "." not in morsel.value:
                return None, None
            sid, token = morsel.value.split(".", 1)
            row = _db["learner_sessions"].get(sid)
            if not row or (rid and row.get("recording_id") != rid):
                return None, None
            if not secrets.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row.get("token_hash", "")):
                return None, None
            return sid, row
        except (TypeError, ValueError, KeyError):
            return None, None

    def _learning_access(self, rid, data=None):
        data = data or {}
        uid = self._session_uid()
        if _can_view(uid, rid):
            return True
        if _valid_share_access(rid, data.get("share_id"), data.get("share_token")):
            return True
        sid, row = self._learner_session(rid)
        if not sid or not row:
            return False
        share_id = row.get("access_share_id")
        if not share_id:
            return False
        link = _db["share_links"].get(share_id)
        return bool(link and link.get("rec_id") == rid and not link.get("revoked")
                    and (not link.get("expires_at") or time.time() <= link["expires_at"])
                    and _rec_visible(rid))

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
        requested_uid = str(data.get("uid") or "").strip()
        with _store_lock:
            requested_user = _db["users"].get(requested_uid) or {}
            if requested_user.get("disabled"):
                return self._send_json(403, {"error": "账号已被停用，请联系管理员"})
        uid = _verify_user(data.get("uid"), data.get("password"))
        if not uid:
            self._send_json(401, {"error": "UID 或密码错误"})
            return
        token = secrets.token_urlsafe(32)
        _sessions[token] = uid
        with _store_lock:
            _db["users"][uid]["last_login_at"] = int(time.time())
            _save_store()
        self._set_session_cookie(token)
        self._send_json(200, {"success": True, "uid": uid,
                             "password_change_required": bool(_db["users"][uid].get("force_password_change"))})

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
        user = _db["users"].get(uid) or {}
        self._send_json(200, {"uid": uid, "avatar": user.get("avatar", ""),
                             "is_admin": _is_admin(uid),
                             "password_change_required": bool(user.get("force_password_change"))})

    def _handle_profile(self):
        uid = self._session_uid()
        if not uid:
            self.close_connection = True
            return self._send_json(401, {"error": "未登录"})
        data, err = self._read_json_body(512 * 1024)
        if err:
            return
        new_uid = str(data.get("uid") or uid).strip()
        new_password = data.get("new_password") or ""
        current_password = data.get("current_password") or ""
        avatar = data.get("avatar") if "avatar" in data else None
        if not new_uid or len(new_uid) > 64:
            return self._send_json(400, {"error": "UID 不能为空（最长 64 字符）"})
        if new_password and len(new_password) < 4:
            return self._send_json(400, {"error": "新密码至少 4 位"})
        if _is_admin(uid) and new_uid != uid and not _is_admin(new_uid):
            return self._send_json(400, {"error": "管理员 UID 由服务器配置管理，修改前请先更新管理员名单"})
        if (new_uid != uid or new_password) and not _verify_user(uid, current_password):
            return self._send_json(403, {"error": "当前密码不正确"})
        if avatar is not None:
            avatar = str(avatar or "")
            if avatar and (not avatar.startswith("data:image/") or len(avatar) > 400000):
                return self._send_json(400, {"error": "头像格式无效或文件过大"})
        with _store_lock:
            if new_uid != uid and new_uid in _db["users"]:
                return self._send_json(409, {"error": "该 UID 已存在"})
            user = dict(_db["users"].get(uid) or {})
            if avatar is not None:
                user["avatar"] = avatar
            if new_password:
                salt = secrets.token_hex(16)
                user.update({"salt": salt, "hash": _hash_password(new_password, salt)})
                user["force_password_change"] = False
            if new_uid != uid:
                _db["users"].pop(uid, None)
                _db["users"][new_uid] = user
                for rec in _db["recordings"].values():
                    if rec.get("owner") == uid: rec["owner"] = new_uid
                for shares in _db["recording_shares"].values():
                    if uid in shares: shares[new_uid] = shares.pop(uid)
                for table in ("user_favorites", "user_progress"):
                    if uid in _db[table]: _db[table][new_uid] = _db[table].pop(uid)
                for link in _db["share_links"].values():
                    if link.get("owner") == uid: link["owner"] = new_uid
                for token, session_uid in list(_sessions.items()):
                    if session_uid == uid: _sessions[token] = new_uid
            else:
                _db["users"][uid] = user
            _save_store()
        self._send_json(200, {"success": True, "uid": new_uid, "avatar": user.get("avatar", ""),
                             "is_admin": _is_admin(new_uid),
                             "password_change_required": bool(user.get("force_password_change"))})

    # ── 管理员后台 ──
    def _admin_uid(self):
        uid = self._session_uid()
        if not uid:
            self._send_json(401, {"error": "未登录"})
            return None
        if not _is_admin(uid):
            self._send_json(403, {"error": "需要管理员权限"})
            return None
        return uid

    def _admin_write_ok(self, actor, action, target):
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin:
            parsed = urllib.parse.urlparse(origin)
            if parsed.netloc != host or parsed.scheme not in ("http", "https"):
                with _store_lock:
                    _audit_admin(actor, action, target, False, "origin rejected"); _save_store()
                self._send_json(403, {"error": "请求来源校验失败"})
                return False
        return True

    @staticmethod
    def _page_params(query):
        def number(name, default, lo, hi):
            try: return max(lo, min(hi, int((query.get(name) or [default])[0])))
            except (TypeError, ValueError): return default
        return number("page", 1, 1, 1000000), number("page_size", 30, 1, 100)

    @staticmethod
    def _paged(rows, page, page_size):
        total = len(rows); start = (page - 1) * page_size
        return {"items": rows[start:start + page_size], "page": page, "page_size": page_size,
                "total": total, "pages": max(1, (total + page_size - 1) // page_size)}

    @staticmethod
    def _recording_size(rec):
        try:
            path = (SHARED_DIR / str(rec.get("stored") or "")).resolve()
            path.relative_to(SHARED_DIR.resolve())
            return path.stat().st_size if path.is_file() else 0
        except (OSError, ValueError):
            return 0

    def _admin_user_row(self, uid, user):
        owned = [r for r in _db["recordings"].values() if r.get("owner") == uid and not r.get("demo")]
        progress = _db["user_progress"].get(uid, {})
        activity = [int(user.get("last_login_at") or 0)]
        activity += [int(r.get("updated") or r.get("created") or 0) for r in owned]
        activity += [int(x.get("updated") or 0) for x in progress.values()]
        return {"uid": uid, "avatar": user.get("avatar", ""), "created_at": user.get("created_at"),
                "last_login_at": user.get("last_login_at"), "last_active_at": max(activity or [0]) or None,
                "disabled": bool(user.get("disabled")),
                "password_change_required": bool(user.get("force_password_change")),
                "is_admin": _is_admin(uid), "courses": len(owned),
                "active_courses": sum(not r.get("deleted") for r in owned),
                "favorites": len(_db["user_favorites"].get(uid, {})),
                "progress_items": len(progress), "storage_bytes": sum(self._recording_size(r) for r in owned)}

    def _handle_admin_overview(self):
        if not self._admin_uid(): return
        now = int(time.time()); day = 86400
        with _store_lock:
            users = [self._admin_user_row(uid, u) for uid, u in _db["users"].items()]
            recs = [r for r in _db["recordings"].values() if not r.get("demo")]
            links = list(_db["share_links"].values()); sessions = list(_db["learner_sessions"].items())
            attempts = [a for rows in _db["interaction_attempts"].values() for a in rows]
            questions = [q for rows in _db["learner_questions"].values() for q in rows.values()]
            complete_sids = {e.get("session") for rows in _db["learning_events"].values() for e in rows if e.get("type") == "complete"}
            direct_shares = sum(len(x) for x in _db["recording_shares"].values())
            correct = sum(a.get("result") == "correct" for a in attempts)
            metrics = {"users": len(users), "active_7d": sum((u.get("last_active_at") or 0) >= now-7*day for u in users),
                       "active_30d": sum((u.get("last_active_at") or 0) >= now-30*day for u in users),
                       "new_30d": sum((u.get("created_at") or 0) >= now-30*day for u in users),
                       "courses": sum(not r.get("deleted") for r in recs), "trash": sum(bool(r.get("deleted")) for r in recs),
                       "storage_bytes": sum(self._recording_size(r) for r in recs), "direct_shares": direct_shares,
                       "active_links": sum(not l.get("revoked") and (not l.get("expires_at") or l["expires_at"] >= now) for l in links),
                       "inactive_links": sum(bool(l.get("revoked")) or bool(l.get("expires_at") and l["expires_at"] < now) for l in links),
                       "learners": len(sessions), "completion_rate": len(complete_sids)/len(sessions) if sessions else 0,
                       "attempts": len(attempts), "correct_rate": correct/len(attempts) if attempts else 0,
                       "questions": len(questions), "pending_questions": sum(q.get("status") != "author_replied" for q in questions)}
            trends = {}
            for days in (7, 30, 90):
                rows=[]
                for offset in range(days-1, -1, -1):
                    start=(now//day-offset)*day; end=start+day
                    rows.append({"date":time.strftime("%Y-%m-%d",time.localtime(start)),
                      "users":sum(start <= (u.get("created_at") or 0) < end for u in users),
                      "courses":sum(start <= int(r.get("created") or 0) < end for r in recs),
                      "learning":sum(start <= int(row.get("created") or 0) < end for _,row in sessions),
                      "attempts":sum(start*1000 <= int(a.get("created") or 0) < end*1000 for a in attempts),
                      "questions":sum(start*1000 <= int(q.get("created") or 0) < end*1000 for q in questions)})
                trends[str(days)] = rows
        self._send_json(200, {"metrics": metrics, "trends": trends})

    def _handle_admin_users(self, query):
        if not self._admin_uid(): return
        page,size=self._page_params(query); q=str((query.get("q") or [""])[0]).casefold()
        sort=str((query.get("sort") or ["last_active_at"])[0]); reverse=str((query.get("order") or ["desc"])[0]).lower()!="asc"
        allowed={"uid","created_at","last_login_at","last_active_at","courses","storage_bytes"}; sort=sort if sort in allowed else "last_active_at"
        with _store_lock: rows=[self._admin_user_row(uid,u) for uid,u in _db["users"].items() if not q or q in uid.casefold()]
        rows.sort(key=lambda x:(x.get(sort) is not None,x.get(sort) or ""),reverse=reverse)
        self._send_json(200,self._paged(rows,page,size))

    def _handle_admin_user(self, target_uid):
        if not self._admin_uid(): return
        with _store_lock:
            user=_db["users"].get(target_uid)
            if not user:return self._send_json(404,{"error":"用户不存在"})
            result=self._admin_user_row(target_uid,user)
            owned=[]
            for rid,r in _db["recordings"].items():
                if r.get("owner")==target_uid and not r.get("demo"):
                    owned.append({"id":rid,"title":r.get("title",""),"created":r.get("created"),"updated":r.get("updated"),"deleted":bool(r.get("deleted")),"size":self._recording_size(r)})
            result.update({"recordings":owned,"favorite_ids":list(_db["user_favorites"].get(target_uid,{})),
                           "progress":_db["user_progress"].get(target_uid,{})})
            learner_sids = [sid for sid, row in _db["learner_sessions"].items()
                            if row.get("registered_uid") == target_uid]
            learner_attempts = [attempt for sid in learner_sids
                                for attempt in _db["interaction_attempts"].get(sid, [])]
            learner_questions = [question for questions in _db["learner_questions"].values()
                                 for question in questions.values()
                                 if question.get("session") in learner_sids]
            result["learning"] = {
                "sessions": len(learner_sids),
                "attempts": len(learner_attempts),
                "correct_rate": (sum(a.get("result") == "correct" for a in learner_attempts)
                                 / len(learner_attempts) if learner_attempts else 0),
                "questions": len(learner_questions),
                "pending_questions": sum(q.get("status") != "author_replied"
                                         for q in learner_questions),
            }
        self._send_json(200,result)

    def _handle_admin_courses(self, query):
        if not self._admin_uid(): return
        page,size=self._page_params(query); q=str((query.get("q") or [""])[0]).casefold(); sort=str((query.get("sort") or ["updated"])[0]); reverse=str((query.get("order") or ["desc"])[0]).lower()!="asc"
        allowed={"title","owner","created","updated","size","learners"}; sort=sort if sort in allowed else "updated"
        with _store_lock:
            rows=[]
            for rid,r in _db["recordings"].items():
                if r.get("demo") or (q and q not in (str(r.get("title",""))+" "+str(r.get("owner",""))).casefold()):continue
                learner_count=sum(x.get("recording_id")==rid for x in _db["learner_sessions"].values())
                rows.append({"id":rid,"title":r.get("title",""),"owner":r.get("owner",""),"created":r.get("created"),"updated":r.get("updated"),"deleted":bool(r.get("deleted")),"size":self._recording_size(r),"direct_shares":len(_db["recording_shares"].get(rid,{})),"active_links":sum(l.get("rec_id")==rid and not l.get("revoked") and (not l.get("expires_at") or l["expires_at"]>=time.time()) for l in _db["share_links"].values()),"learners":learner_count,"attempts":sum(len(_db["interaction_attempts"].get(sid,[])) for sid,x in _db["learner_sessions"].items() if x.get("recording_id")==rid),"questions":len(_db["learner_questions"].get(rid,{}))})
        rows.sort(key=lambda x:(x.get(sort) is not None,x.get(sort) or ""),reverse=reverse);self._send_json(200,self._paged(rows,page,size))

    def _handle_admin_audit(self, query):
        if not self._admin_uid(): return
        page,size=self._page_params(query)
        with _store_lock:rows=sorted(_db["admin_audit_logs"].values(),key=lambda x:x.get("created",0),reverse=True)
        self._send_json(200,self._paged(rows,page,size))

    def _handle_admin_status(self, target_uid):
        actor=self._admin_uid()
        if not actor:return
        if not self._admin_write_ok(actor,"user_status",target_uid):return
        data,err=self._read_json_body(4096)
        if err:return
        disabled=bool(data.get("disabled"))
        with _store_lock:
            user=_db["users"].get(target_uid)
            if not user:_audit_admin(actor,"user_status",target_uid,False,"not found");_save_store();return self._send_json(404,{"error":"用户不存在"})
            if target_uid==actor and disabled:_audit_admin(actor,"user_status",target_uid,False,"self disable rejected");_save_store();return self._send_json(400,{"error":"不能停用当前管理员账号"})
            user["disabled"]=disabled
            if disabled:_invalidate_user_sessions(target_uid)
            _audit_admin(actor,"user_status",target_uid,True,"disabled" if disabled else "enabled");_save_store()
        self._send_json(200,{"success":True,"disabled":disabled})

    def _handle_admin_reset_password(self, target_uid):
        actor=self._admin_uid()
        if not actor:return
        if not self._admin_write_ok(actor,"reset_password",target_uid):return
        with _store_lock:
            user=_db["users"].get(target_uid)
            if not user:_audit_admin(actor,"reset_password",target_uid,False,"not found");_save_store();return self._send_json(404,{"error":"用户不存在"})
            temporary=secrets.token_urlsafe(12);salt=secrets.token_hex(16)
            user.update({"salt":salt,"hash":_hash_password(temporary,salt),"force_password_change":True})
            _invalidate_user_sessions(target_uid);_audit_admin(actor,"reset_password",target_uid,True,"temporary password issued");_save_store()
        self._send_json(200,{"success":True,"temporary_password":temporary,"shown_once":True})

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

    def _handle_recording_captions(self, rec_id):
        """Replace only lecture.json.script in an owned course package."""
        uid = self._session_uid()
        if not uid:
            self.close_connection = True
            return self._send_json(401, {"error": "未登录"})
        data, err = self._read_json_body(2 * 1024 * 1024)
        if err:
            return
        raw = data.get("script")
        if not isinstance(raw, list) or len(raw) > 5000:
            return self._send_json(400, {"error": "字幕格式无效或条目过多"})
        script = []
        for row in raw:
            if not isinstance(row, dict):
                return self._send_json(400, {"error": "字幕条目格式无效"})
            text = str(row.get("text") or "").strip()
            try:
                at_ms = max(0, int(float(row.get("t", 0))))
                duration_ms = max(0, min(60000, int(float(row.get("d", 0)))))
            except (TypeError, ValueError, OverflowError):
                return self._send_json(400, {"error": "字幕时间无效"})
            if not text or len(text) > 2000:
                return self._send_json(400, {"error": "字幕内容为空或过长"})
            script.append({"text": text, "t": at_ms, "d": duration_ms, "source": "edited"})
        script.sort(key=lambda item: item["t"])
        with _store_lock:
            rec = _rec_for_owner(rec_id, uid)
            if not rec or rec.get("deleted"):
                return self._send_json(403, {"error": "只能编辑自己的有效课程"})
            package_path = (SHARED_DIR / rec.get("stored", "")).resolve()
            if SHARED_DIR.resolve() not in package_path.parents or not package_path.is_file():
                return self._send_json(404, {"error": "课程包不存在"})
        fd, tmp_name = tempfile.mkstemp(prefix="captions-", suffix=".zip", dir=str(SHARED_DIR))
        os.close(fd)
        try:
            with zipfile.ZipFile(package_path, "r") as source_zip:
                infos = source_zip.infolist()
                if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UPLOAD_BYTES * 4:
                    raise ValueError("课程包内容异常")
                try:
                    lecture = json.loads(source_zip.read("lecture.json").decode("utf-8"))
                except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("课程包缺少有效的 lecture.json") from exc
                lecture["script"] = script
                with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as target_zip:
                    for info in infos:
                        if info.filename != "lecture.json":
                            target_zip.writestr(info, source_zip.read(info))
                    target_zip.writestr("lecture.json", json.dumps(lecture, ensure_ascii=False, separators=(",", ":")))
            os.replace(tmp_name, package_path)
            with _store_lock:
                current = _db["recordings"].get(rec_id)
                if current:
                    current["updated"] = int(time.time())
                    _save_store()
            return self._send_json(200, {"success": True, "count": len(script)})
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return self._send_json(400, {"error": str(exc)})

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
            _delete_learning_data_locked(rec_id)
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

    # ── 互动学习与匿名学习档案 ──
    def _handle_learn_session(self):
        data, err = self._read_json_body(16384)
        if err: return
        rid = str(data.get("recording_id") or "")
        if not self._learning_access(rid, data):
            return self._send_json(404, {"error": "课程不可用或分享链接已失效"})
        sid, row = self._learner_session(rid)
        now = int(time.time())
        if not sid:
            sid, token = uuid.uuid4().hex[:16], secrets.token_urlsafe(24)
            share_id = str(data.get("share_id") or "") if _valid_share_access(rid, data.get("share_id"), data.get("share_token")) else None
            row = {"recording_id": rid, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                   "access_share_id": share_id, "registered_uid": self._session_uid(),
                   "created": now, "last_seen": now}
            with _store_lock:
                _db["learner_sessions"][sid] = row; _save_store()
            self._learner_cookie_to_set = f"{sid}.{token}"
        else:
            with _store_lock:
                row["last_seen"] = now; _save_store()
        attempts = list(_db["interaction_attempts"].get(sid, []))
        questions = [{k:v for k,v in q.items() if k != "learner_sid"}
                     for q in _db["learner_questions"].get(rid, {}).values() if q.get("learner_sid") == sid]
        self._send_json(200, {"success": True, "attempts": attempts, "questions": questions})

    def _handle_learn_state(self, rid):
        sid, _ = self._learner_session(rid)
        if not sid or not self._learning_access(rid):
            return self._send_json(403, {"error": "学习会话无效或分享已撤销"})
        attempts = list(_db["interaction_attempts"].get(sid, []))
        questions = [{k:v for k,v in q.items() if k != "learner_sid"}
                     for q in _db["learner_questions"].get(rid, {}).values() if q.get("learner_sid") == sid]
        self._send_json(200, {"attempts": attempts, "questions": questions})

    def _handle_learn_attempt(self, rid):
        data, err = self._read_json_body(65536)
        if err: return
        sid, _ = self._learner_session(rid)
        if not sid or not self._learning_access(rid, data): return self._send_json(403, {"error":"学习会话无效"})
        interaction_id = str(data.get("interaction_id") or "")[:128]
        revision = max(1, int(data.get("revision") or 1))
        item = _interaction_from_course(rid, interaction_id, revision)
        if not item: return self._send_json(409, {"error":"题目已更新，请重新打开课程"})
        answer = str(data.get("answer") or "")[:2000].strip()
        if not answer: return self._send_json(400, {"error":"答案不能为空"})
        context = _learning_context(rid, data.get("context"))
        result, feedback, evidence, review_ms = "等待反馈", "答案已保存，稍后可回来查看反馈。", "", item.get("atMs",0)
        if item.get("type") == "single_choice":
            ok = answer in (item.get("correctOptionIds") or [])
            result = "correct" if ok else "incorrect"
            feedback = ("回答正确。" if ok else "还需要再想一想。") + (str(item.get("explanation") or ""))
            evidence = (item.get("anchor") or {}).get("quote") or context["time"]
        elif _grade_short_answer and _learn_rate_ok(self.client_address[0], "grade", 30):
            try:
                graded = _grade_short_answer({"prompt":item.get("prompt"),"rubric":item.get("rubric"),
                                              "explanation":item.get("explanation")}, answer, context)
                result = graded.get("result", result); feedback = str(graded.get("feedback") or feedback)[:3000]
                evidence = str(graded.get("evidence") or "")[:1000]; review_ms = max(0,int(graded.get("review_ms") or review_ms))
            except Exception:
                pass
        row = {"interaction_id":interaction_id,"revision":revision,"type":item.get("type"),"answer":answer,
               "result":result,"feedback":feedback,"evidence":evidence,"review_ms":review_ms,
               "at_ms":int(item.get("atMs") or 0),"created":int(time.time()*1000)}
        with _store_lock:
            rows = _db["interaction_attempts"].setdefault(sid, [])
            rows[:] = [x for x in rows if not (x.get("interaction_id")==interaction_id and int(x.get("revision") or 1)==revision)]
            rows.append(row); _save_store()
        self._send_json(200, row)

    def _handle_learn_question(self, rid):
        data, err = self._read_json_body(65536)
        if err: return
        sid, _ = self._learner_session(rid)
        if not sid or not self._learning_access(rid, data): return self._send_json(403,{"error":"学习会话无效"})
        question = str(data.get("question") or "").strip()[:1000]
        if not question: return self._send_json(400,{"error":"问题不能为空"})
        if not _learn_rate_ok(self.client_address[0], "question", 40): return self._send_json(429,{"error":"提问过于频繁，请稍后再试"})
        context = _learning_context(rid, data.get("context")); answer="问题已保存，等待作者反馈。"; evidence=""; confidence="low"
        if _answer_course_question:
            try:
                reply = _answer_course_question(question, context)
                answer = str(reply.get("answer") or answer)[:5000]; evidence = str(reply.get("evidence") or "")[:1000]
                confidence = reply.get("confidence") if reply.get("confidence") in ("high","low") else "low"
            except Exception:
                pass
        qid = "lq_"+uuid.uuid4().hex[:12]; now=int(time.time()*1000)
        row={"id":qid,"learner_sid":sid,"question":question,"at_ms":context["position_ms"],
             "context":{"time":context["time"],"file_name":context["file_name"],"slide":context["slide"],"subtitle":context["subtitle"]},
             "ai_answer":answer,"evidence":evidence,"confidence":confidence,
             "status":"ai_answered" if confidence=="high" else "pending_author","created":now}
        with _store_lock:
            _db["learner_questions"].setdefault(rid,{})[qid]=row; _save_store()
        self._send_json(200,{k:v for k,v in row.items() if k!="learner_sid"})

    def _handle_learn_event(self, rid):
        data, err = self._read_json_body(16384)
        if err: return
        sid, _ = self._learner_session(rid)
        if not sid or not self._learning_access(rid, data):
            return self._send_json(403,{"error":"学习会话无效或分享已撤销"})
        kind = str(data.get("type") or "")
        if kind not in ("play","pause","skip","checkpoint","replay","complete"):
            return self._send_json(400,{"error":"未知事件"})
        row={"session":sid,"type":kind,"position_ms":max(0,int(data.get("position_ms") or 0)),
             "interaction_id":str(data.get("interaction_id") or "")[:128] or None,"created":int(time.time()*1000)}
        with _store_lock:
            rows=_db["learning_events"].setdefault(rid,[]);rows.append(row)
            if len(rows)>10000: del rows[:-10000]
            _save_store()
        self._send_json(200,{"success":True})

    def _handle_generate_interactions(self):
        if not self._session_uid(): return self._send_json(401,{"error":"未登录"})
        if not _gen_interactions: return self._send_json(503,{"error":"互动题生成服务不可用"})
        if not _heavy_lock.acquire(blocking=False): return self._send_json(429,{"error":"服务器繁忙，请稍后再试"})
        try:
            data,err=self._read_json_body(2*1024*1024)
            if err:return
            content=str(data.get("content") or "")[:20000];script=data.get("script") or []
            if not content and not script:return self._send_json(400,{"error":"请先生成讲稿或加载课程内容"})
            items=_gen_interactions(content,script,str(data.get("topic") or ""))
            now=int(time.time()*1000)
            clean=[]
            for i,q in enumerate(items[:8]):
                if not isinstance(q,dict):continue
                q["id"]=str(q.get("id") or f"q_{now}_{i}")[:128];q["revision"]=1;q["approved"]=False
                q["previewMs"]=30000;q["atMs"]=max(0,int(q.get("atMs") or 0));clean.append(q)
            self._send_json(200,{"interactions":clean})
        except Exception as e:self._send_json(500,{"error":"互动题生成失败: "+str(e)[:500]})
        finally:_heavy_lock.release()

    def _handle_insights(self, rid):
        uid=self._session_uid()
        if not _rec_for_owner(rid,uid):return self._send_json(403,{"error":"只能查看自己课程的反馈"})
        session_ids=[sid for sid,row in _db["learner_sessions"].items() if row.get("recording_id")==rid]
        attempts=[a for sid in session_ids for a in _db["interaction_attempts"].get(sid,[])]
        events=_db["learning_events"].get(rid,[]); questions=list(_db["learner_questions"].get(rid,{}).values())
        byq={}
        for a in attempts:
            key=f"{a.get('interaction_id')}@{a.get('revision',1)}";s=byq.setdefault(key,{"attempts":0,"correct":0,"results":{},"answers":[]})
            s["attempts"]+=1;s["correct"]+=a.get("result") in ("correct","理解正确")
            s["results"][a.get("result")]=s["results"].get(a.get("result"),0)+1
            s["answers"].append(str(a.get("answer") or "")[:500])
        for s in byq.values():s["correct_rate"]=round(s["correct"]/max(1,s["attempts"]),3)
        hotspots={}
        for e in events:
            if e.get("type") in ("pause","replay","checkpoint"):
                bucket=int(e.get("position_ms") or 0)//10000*10000;hotspots[bucket]=hotspots.get(bucket,0)+1
        self._send_json(200,{"learners":len(session_ids),"attempts":len(attempts),"questions_count":len(questions),
                            "by_question":byq,"hotspots":sorted(({"position_ms":k,"count":v} for k,v in hotspots.items()),key=lambda x:-x["count"])[:10],
                            "questions":[{k:v for k,v in q.items() if k!="learner_sid"} for q in questions]})

    def _handle_author_questions(self, rid):
        uid=self._session_uid()
        if not _rec_for_owner(rid,uid):return self._send_json(403,{"error":"只能查看自己课程的问题"})
        rows=[{k:v for k,v in q.items() if k!="learner_sid"} for q in _db["learner_questions"].get(rid,{}).values()]
        rows.sort(key=lambda x:-int(x.get("created") or 0));self._send_json(200,{"questions":rows})

    def _handle_author_reply(self, rid, qid):
        uid=self._session_uid()
        if not _rec_for_owner(rid,uid):return self._send_json(403,{"error":"只能回复自己课程的问题"})
        data,err=self._read_json_body(16384)
        if err:return
        text=str(data.get("reply") or "").strip()[:3000]
        if not text:return self._send_json(400,{"error":"回复不能为空"})
        with _store_lock:
            q=_db["learner_questions"].get(rid,{}).get(qid)
            if not q:return self._send_json(404,{"error":"问题不存在"})
            q["author_reply"]=text;q["status"]="author_replied";q["replied_at"]=int(time.time()*1000)
            _db["author_replies"][qid]={"recording_id":rid,"reply":text,"created":q["replied_at"]};_save_store()
        self._send_json(200,{"success":True,"question":{k:v for k,v in q.items() if k!="learner_sid"}})

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
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        current_uid = self._session_uid()
        if path.startswith("/api/") and path != "/api/me" and current_uid and (_db["users"].get(current_uid) or {}).get("force_password_change"):
            return self._send_json(403, {"error": "请先修改临时密码", "code": "password_change_required"})

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
        if path == "/api/admin/overview":
            return self._handle_admin_overview()
        if path == "/api/admin/users":
            return self._handle_admin_users(query)
        if path.startswith("/api/admin/users/"):
            return self._handle_admin_user(path[len("/api/admin/users/"):])
        if path == "/api/admin/courses":
            return self._handle_admin_courses(query)
        if path == "/api/admin/audit":
            return self._handle_admin_audit(query)
        if path == "/api/recordings":
            return self._handle_recordings_list()
        if path == "/api/web-imports":
            return self._handle_web_imports()
        if path.startswith("/api/web-imports/"):
            return self._handle_web_imports(path[len("/api/web-imports/"):])
        if path == "/api/drafts":
            return self._handle_drafts()
        if path.startswith("/api/drafts/") and path.endswith("/file"):
            return self._handle_drafts(path[len("/api/drafts/"):-len("/file")])
        if path.startswith("/api/learn/") and path.endswith("/state"):
            return self._handle_learn_state(path[len("/api/learn/"):-len("/state")])
        if path.startswith("/api/recordings/") and path.endswith("/insights"):
            return self._handle_insights(path[len("/api/recordings/"):-len("/insights")])
        if path.startswith("/api/recordings/") and path.endswith("/questions"):
            return self._handle_author_questions(path[len("/api/recordings/"):-len("/questions")])
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
            with _store_lock:
                rid = (_db["share_links"].get(sid) or {}).get("rec_id")
                if not rid or not _valid_share_access(rid, sid, tk):
                    return self._send(404, b"Not Found", "text/plain")
            file_url = "/api/link/" + sid + "/file" + (f"?tk={urllib.parse.quote(tk)}" if tk else "")
            self.send_response(302)
            self.send_header("Location", "/lecture-lite.html?src=" + urllib.parse.quote(file_url, safe="") + "&rid=" + urllib.parse.quote(rid))
            self.end_headers()
            return
        if path.startswith("/api/link/") and path.endswith("/file"):
            return self._handle_link_file(path[len("/api/link/"):-len("/file")])

        # 静态文件（白名单；登录态由页面内 /api/me 检查并弹出登录层）
        if path == "/":
            path = "/lecture-lite.html"
        target = resolve_static(path)
        if target:
            if path == "/admin.html" and not _is_admin(self._session_uid()):
                return self._send_json(403 if self._session_uid() else 401,
                                       {"error": "需要管理员权限" if self._session_uid() else "未登录"})
            ctype = self._guess_type(target.name)
            body = target.read_bytes()
            self._send(200, body, ctype)
            return

        self._send(404, b"Not Found", "text/plain")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        current_uid = self._session_uid()
        if path not in ("/api/login", "/api/register", "/api/logout", "/api/profile") and current_uid and (_db["users"].get(current_uid) or {}).get("force_password_change"):
            return self._send_json(403, {"error": "请先修改临时密码", "code": "password_change_required"})
        known = ("/api/register", "/api/login", "/api/logout", "/api/profile", "/api/convert-ppt", "/api/compress-audio",
                 "/api/upload", "/share", "/api/progress",
                 "/api/web-import", "/api/drafts",
                 "/generate-script", "/generate-script-stream",
                 "/generate-script-timeline", "/generate-speech-stream", "/generate-interactions",
                 "/api/learn/session")
        if path not in known and not path.startswith("/api/admin/") and not path.startswith("/api/web-imports/") and not path.startswith("/api/drafts/") and not (path.startswith("/api/recordings/")
                                      and len(path.strip("/").split("/")) in (4, 5, 6)) \
                and not path.startswith("/api/learn/"):
            return self._send(404, b"Not Found", "text/plain")
        if path == "/api/register":
            return self._handle_register()
        if path == "/api/login":
            return self._handle_login()
        if path == "/api/logout":
            return self._handle_logout()
        if path == "/api/profile":
            return self._handle_profile()
        if path.startswith("/api/admin/users/"):
            parts=path[len("/api/admin/users/"):].split("/")
            if len(parts)==2 and parts[1]=="status":return self._handle_admin_status(parts[0])
            if len(parts)==2 and parts[1]=="reset-password":return self._handle_admin_reset_password(parts[0])
            return self._send_json(404,{"error":"未知管理员接口"})
        if path == "/api/convert-ppt":
            return self._handle_ppt_convert()
        if path == "/api/compress-audio":
            return self._handle_audio_compress()
        if path == "/api/upload" or path == "/share":
            return self._handle_upload()
        if path == "/api/web-import":
            return self._handle_web_import()
        if path.startswith("/api/web-imports/"):
            return self._handle_web_import_update(path[len("/api/web-imports/"):])
        if path == "/api/drafts":
            return self._handle_draft_upload()
        if path.startswith("/api/drafts/") and path.endswith("/delete"):
            return self._handle_drafts(path[len("/api/drafts/"):-len("/delete")],"delete")
        if path == "/api/progress":
            return self._handle_progress()
        if path == "/api/learn/session":
            return self._handle_learn_session()
        if path.startswith("/api/learn/"):
            parts=path.strip("/").split("/")
            if len(parts)==4:
                rid,action=parts[2],parts[3]
                if action=="attempts":return self._handle_learn_attempt(rid)
                if action=="questions":return self._handle_learn_question(rid)
                if action=="events":return self._handle_learn_event(rid)
            return self._send_json(404,{"error":"未知学习接口"})
        if path.startswith("/api/recordings/"):
            parts = path.strip("/").split("/")
            if len(parts)==6 and parts[3]=="questions" and parts[5]=="reply":
                return self._handle_author_reply(parts[2],parts[4])
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
                if action == "captions":
                    return self._handle_recording_captions(rid)
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
        elif path == "/generate-interactions":
            self._handle_generate_interactions()

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

    # ── 网页导入：仅抓公开 HTML，图片落到受登录保护的草稿资产目录 ──
    def _handle_web_import(self):
        uid = self._session_uid()
        if not uid: return self._send_json(401, {"error": "请先登录后再导入网页"})
        if not _learn_rate_ok(self.client_address[0], "web_import", limit=12, window=3600):
            return self._send_json(429, {"error": "网页导入过于频繁，请稍后再试"})
        data, err = self._read_json_body(8192)
        if err: return
        if not _heavy_lock.acquire(blocking=False): return self._send_json(429, {"error": "服务器正在处理其他导入，请稍后再试"})
        try:
            original = str(data.get("url") or "").strip()
            final_url, content_type, raw = _fetch_public(original, 3 * 1024 * 1024)
            if content_type not in ("text/html", "application/xhtml+xml"):
                return self._send_json(400, {"error": "该地址不是可导入的网页 HTML"})
            page = _decode_web_html(raw)
            parser = _WebPageMarkdown(final_url); parser.feed(_article_fragment(page))
            body = _clean_import_markdown(parser.markdown())
            if len(body) < 20: return self._send_json(422, {"error": "没有提取到足够的正文；该网页可能需要登录或由脚本动态生成"})
            draft_id = "web_" + uuid.uuid4().hex[:16]
            asset_dir = WEB_IMPORT_DIR / draft_id / "images"; asset_dir.mkdir(parents=True, exist_ok=True)
            seen, assets, total = set(), [], 0
            for img_url, alt in parser.images:
                if img_url in seen or len(assets) >= 16 or total >= 12 * 1024 * 1024: continue
                seen.add(img_url)
                try:
                    _, image_type, image_raw = _fetch_public(img_url, min(2 * 1024 * 1024, 12 * 1024 * 1024 - total), "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
                    if not image_type.startswith("image/") or image_type in ("image/svg+xml",): continue
                    ext = {"image/jpeg":"jpg", "image/png":"png", "image/webp":"webp", "image/gif":"gif", "image/avif":"avif"}.get(image_type, "img")
                    name = f"image-{len(assets)+1}.{ext}"; (asset_dir / name).write_bytes(image_raw)
                    assets.append({"name":name, "alt":alt, "size":len(image_raw), "content_type":image_type}); total += len(image_raw)
                except (ValueError, OSError):
                    continue
            title = re.sub(r"\s+", " ", parser.title).strip() or urllib.parse.urlsplit(final_url).hostname or "网页导入"
            title = title[:120]
            markdown = f"# {title}\n\n> 来源：[{final_url}]({final_url})\n> 导入时间：{time.strftime('%Y-%m-%d %H:%M')}\n\n{body}"
            if assets:
                markdown += "\n\n## 页面图片\n" + "\n".join(f"\n![{asset['alt'] or '图片'}](images/{asset['name']})" for asset in assets)
            media, seen_media = [], set()
            for kind, media_url in parser.media:
                if media_url not in seen_media:
                    seen_media.add(media_url); media.append((kind, media_url))
            if media:
                markdown += "\n\n## 页面媒体（原链接占位）\n" + "\n".join(
                    f"\n- [{('视频' if kind == 'video' else '音频' if kind == 'audio' else '媒体')}：{media_url}]({media_url})"
                    for kind, media_url in media[:20])
            with _store_lock:
                _db["web_imports"][draft_id] = {"id":draft_id, "owner":uid, "title":title, "url":final_url,
                    "markdown":markdown, "assets":assets, "created":int(time.time()), "updated":int(time.time())}
                _save_store()
            self._send_json(200, {"success":True, "id":draft_id, "title":title, "image_count":len(assets), "url":final_url})
        except ValueError as exc:
            self._send_json(400, {"error":str(exc)})
        except Exception as exc:
            print("网页导入失败:", repr(exc)); self._send_json(500, {"error":"网页导入失败，请稍后重试"})
        finally:
            _heavy_lock.release()

    def _handle_web_imports(self, draft_id=None):
        uid = self._session_uid()
        if not uid: return self._send_json(401, {"error":"未登录"})
        with _store_lock:
            if draft_id:
                draft = _db["web_imports"].get(draft_id)
                if not draft or draft.get("owner") != uid: return self._send_json(404, {"error":"网页草稿不存在"})
                result = {key:draft.get(key) for key in ("id","title","url","markdown","created","updated")}
                encoded=[]
                for asset in draft.get("assets", []):
                    path = WEB_IMPORT_DIR / draft_id / "images" / asset.get("name", "")
                    if path.is_file() and path.resolve().is_relative_to(WEB_IMPORT_DIR.resolve()):
                        encoded.append({**asset, "data":base64.b64encode(path.read_bytes()).decode("ascii")})
                result["assets"] = encoded
                return self._send_json(200, result)
            rows=[{key:d.get(key) for key in ("id","title","url","created","updated")} | {"image_count":len(d.get("assets",[]))}
                  for d in _db["web_imports"].values() if d.get("owner")==uid]
        rows.sort(key=lambda row: -(row.get("updated") or row.get("created") or 0))
        self._send_json(200, {"items":rows})

    def _handle_web_import_update(self, draft_id):
        uid = self._session_uid()
        if not uid: return self._send_json(401, {"error":"未登录"})
        data, err = self._read_json_body(2 * 1024 * 1024)
        if err: return
        markdown = str(data.get("markdown") or "").strip()
        if not markdown: return self._send_json(400, {"error":"Markdown 不能为空"})
        with _store_lock:
            draft = _db["web_imports"].get(draft_id)
            if not draft or draft.get("owner") != uid: return self._send_json(404, {"error":"网页草稿不存在"})
            draft["markdown"] = markdown
            heading = re.search(r"^#\s+(.+)$", markdown, re.M)
            if heading: draft["title"] = re.sub(r"\s+", " ", heading.group(1)).strip()[:120]
            draft["updated"] = int(time.time()); _save_store()
        self._send_json(200, {"success":True, "title":draft.get("title", "")})

    def _handle_drafts(self, draft_id=None, action=None):
        uid = self._session_uid()
        if not uid: return self._send_json(401, {"error":"未登录"})
        if not draft_id:
            with _store_lock:
                lectures=[{key:d.get(key) for key in ("id","title","created","updated")} | {"kind":"lecture"}
                          for d in _db["lecture_drafts"].values() if d.get("owner")==uid]
                webs=[{key:d.get(key) for key in ("id","title","url","created","updated")} | {"kind":"web","image_count":len(d.get("assets",[]))}
                      for d in _db["web_imports"].values() if d.get("owner")==uid]
            items=lectures+webs;items.sort(key=lambda d:-(d.get("updated") or d.get("created") or 0))
            return self._send_json(200,{"items":items,"expires_after_days":30})
        with _store_lock:
            table = _db["web_imports"] if draft_id.startswith("web_") else _db["lecture_drafts"]
            row = table.get(draft_id)
            if not row or row.get("owner") != uid: return self._send_json(404,{"error":"草稿不存在"})
            if action == "delete":
                table.pop(draft_id,None);_save_store();stored=row.get("stored")
            else: stored=row.get("stored")
        if action == "delete":
            if draft_id.startswith("web_"): shutil.rmtree(WEB_IMPORT_DIR/draft_id,ignore_errors=True)
            elif stored:
                try:(SHARED_DIR/stored).unlink()
                except OSError:pass
            return self._send_json(200,{"success":True})
        if not stored: return self._send_json(400,{"error":"网页草稿请通过编辑入口打开"})
        path=SHARED_DIR/stored
        if not path.is_file(): return self._send_json(404,{"error":"草稿文件不存在"})
        body=path.read_bytes();self._send(200,body,"application/zip")

    def _handle_draft_upload(self):
        uid=self._session_uid()
        if not uid:return self._send_json(401,{"error":"未登录"})
        ctype=self.headers.get("Content-Type","");boundary=self._get_boundary(ctype)
        if "multipart/form-data" not in ctype or not boundary:return self._send_json(400,{"error":"需要草稿文件"})
        try:length=int(self.headers.get("Content-Length") or 0)
        except ValueError:length=0
        if length<=0 or length>MAX_UPLOAD_BYTES:return self._send_json(413 if length>MAX_UPLOAD_BYTES else 400,{"error":"草稿为空或过大"})
        SHARED_DIR.mkdir(exist_ok=True)
        filename,tmp_path,size,remaining,complete=self._stream_multipart_file(length,boundary)
        if not filename or not tmp_path or not complete or remaining>0:
            if tmp_path:
                try:os.unlink(tmp_path)
                except OSError:pass
            return self._send_json(400,{"error":"草稿上传不完整"})
        requested=str(self.headers.get("X-Draft-Id") or "").strip()
        with _store_lock:
            old=_db["lecture_drafts"].get(requested) if requested else None
            if old and old.get("owner")!=uid:old=None
            did=old.get("id") if old else "draft_"+uuid.uuid4().hex[:16]
            stored=did+".lecture.zip";os.replace(tmp_path,SHARED_DIR/stored)
            title=urllib.parse.unquote(self.headers.get("X-Draft-Title") or "").strip()[:120] or os.path.basename(filename)
            created=old.get("created") if old else int(time.time())
            _db["lecture_drafts"][did]={"id":did,"owner":uid,"title":title,"stored":stored,"created":created,"updated":int(time.time()),"size":size}
            _save_store()
        self._send_json(200,{"success":True,"id":did,"title":title,"updated":int(time.time())})

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
    _purge_expired_drafts()
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
