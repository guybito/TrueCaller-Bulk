import os, asyncio, re, contextlib
import time, hmac, hashlib, base64
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon import errors as tg_errors

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PHONE = os.getenv("PHONE", "")
TARGET_BOT = os.getenv("TARGET_BOT", "@TrueCaller1Bot")
SESSION = os.getenv("SESSION_NAME", "tc_user_session")
FRONTEND_API_BASE = os.getenv("FRONTEND_API_BASE", "").strip()
DEV_PASSWORD = os.getenv("DEV_PASSWORD", "")  # ← סיסמת מצב מפתח
SECRET_KEY = os.getenv("SECRET_KEY", "")
DEV_COOKIE_NAME = "dev_token"
DEV_TOKEN_TTL = 60 * 60 * 8  # 8 שעות



def _sign(data: bytes) -> str:
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY לא הוגדר")
    mac = hmac.new(SECRET_KEY.encode(), data, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def create_dev_token(user_agent: str = "") -> str:
    # אפשר לקשור ל-User-Agent כדי לצמצם גניבה ע"י צד שלישי
    ts = str(int(time.time()))
    payload = f"{ts}.{hashlib.sha256((user_agent or '').encode()).hexdigest()[:16]}"
    sig = _sign(payload.encode())
    return f"{payload}.{sig}"


def verify_dev_token(token: str, user_agent: str = "") -> bool:
    try:
        ts_str, ua_hash, sig = token.split(".", 2)
        expected_payload = f"{ts_str}.{hashlib.sha256((user_agent or '').encode()).hexdigest()[:16]}"
        good = hmac.compare_digest(sig, _sign(expected_payload.encode()))
        if not good:
            return False
        return (time.time() - int(ts_str)) <= DEV_TOKEN_TTL
    except Exception:
        return False



if not (API_ID and API_HASH and PHONE and TARGET_BOT):
    raise RuntimeError("חסרים API_ID / API_HASH / PHONE / TARGET_BOT בקובץ .env")

app = FastAPI(title="TrueCaller Relay API")
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "").rstrip("/")
ALLOWED_ORIGINS = [FRONTEND_ORIGIN] if FRONTEND_ORIGIN else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else ["http://localhost:8002","http://127.0.0.1:8002"],
    allow_credentials=True,
    allow_methods=["GET","POST"],
    allow_headers=["Content-Type","Authorization","X-CSRF-Token"],
)

# --------- Security headers middleware ---------
from starlette.middleware.base import BaseHTTPMiddleware

@app.middleware("http")
async def security_headers_mw(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';")
    return resp

client = TelegramClient(SESSION, API_ID, API_HASH)
# --------- Security helpers ---------
SAFE_ORIGINS = set([o for o in (os.getenv('FRONTEND_ORIGIN','').rstrip('/'),) if o])
_FAILED_BUCKETS: Dict[str, Dict[str,int]] = {}
API_KEY = os.getenv('API_KEY', '')

def _is_safe_origin(request: Request) -> bool:
    origin = (request.headers.get('origin') or '').rstrip('/')
    referer = (request.headers.get('referer') or '').rstrip('/')
    if SAFE_ORIGINS:
        for src in filter(None, [origin, referer]):
            if any(src.startswith(a) for a in SAFE_ORIGINS):
                return True
        return False
    return True

def _rate_limit(request: Request, max_per_min: int = 60):
    ip = request.client.host if request.client else '?'
    now_min = int(time.time() // 60)
    bucket = _FAILED_BUCKETS.get(ip, {'min': now_min, 'count': 0})
    if bucket['min'] != now_min:
        bucket = {'min': now_min, 'count': 0}
    bucket['count'] += 1
    _FAILED_BUCKETS[ip] = bucket
    if bucket['count'] > max_per_min:
        raise HTTPException(429, 'Too Many Requests')

def _require_api_key(request: Request):
    if API_KEY:
        auth = request.headers.get('authorization', '')
        if not auth.lower().startswith('bearer '):
            raise HTTPException(401, 'Missing API key')
        provided = auth.split(' ',1)[1].strip()
        if not hmac.compare_digest(provided, API_KEY):
            raise HTTPException(403, 'Bad API key')

# --------- Models ---------
class AskBody(BaseModel):
    text: str = Field(..., min_length=1)
    window_sec: float = Field(default=1.0, ge=0, le=15)  # ברירת מחדל 1

class BatchBody(BaseModel):
    messages: List[str] = Field(..., min_items=1)
    delay_ms: int = Field(default=500, ge=0, le=10000)
    window_sec: float = Field(default=1.0, ge=0, le=15)  # ברירת מחדל 1

class DevAuthBody(BaseModel):
    password: str

# --------- Helpers ---------
def normalize_msisdn(num: str) -> str:
    """
    מנקה תווים נפוצים ומחזיר מספר בינלאומי כשאפשר.
    """
    s = (num or "").strip()
    s = re.sub(r"[ \-\.\(\)/]", "", s)

    if s.startswith("+972"): return s
    if s.startswith("972"):  return "+" + s
    if s.startswith("+"):    return s

    if re.fullmatch(r"05\d{8}", s):                 # מובייל IL
        return "+972" + s[1:]
    if re.fullmatch(r"0\d{8,9}", s):                # קווי IL
        return "+972" + s[1:]
    if re.fullmatch(r"\d{9}", s) and s.startswith("5"):
        return "+972" + s

    return s

def looks_like_phone(num: str) -> bool:
    s = normalize_msisdn(num)
    return bool(
        re.fullmatch(r"\+\d{9,15}", s) or
        re.fullmatch(r"972\d{8,9}", s)
    )

# --------- App meta ---------
@app.get("/config")
async def config(request: Request):
    """
    מחזיר ל-Frontend את ה-API Base שנקבע ב-ENV, ואם לא נקבע – את origin של הבקשה.
    """
    origin = str(request.headers.get("origin") or "").rstrip("/")
    api_base = FRONTEND_API_BASE or origin or ""
    return {"ok": True, "api_base": api_base}

@app.post("/dev-auth")
async def dev_auth(body: DevAuthBody, request: Request):
    """
    אימות מצב מפתח מול סיסמה שנקבעת בקובץ .env (DEV_PASSWORD).
    """
    if not DEV_PASSWORD:
        return {"ok": False, "error": "DEV_PASSWORD לא הוגדר בשרת"}
    _rate_limit(request, max_per_min=30)
    if not _is_safe_origin(request):
        raise HTTPException(403, "Bad origin")
    return {"ok": hmac.compare_digest(body.password, DEV_PASSWORD)}


@app.post("/dev-auth/login")
async def dev_login(body: DevAuthBody, request: Request, response: Response):
    if not DEV_PASSWORD:
        return {"ok": False, "error": "DEV_PASSWORD לא הוגדר בשרת"}
    if body.password != DEV_PASSWORD:
        # אפשר להוסיף rate-limit בסיסי כאן אם תרצה
        return {"ok": False}

    token = create_dev_token(request.headers.get("user-agent", ""))

    secure_flag = os.getenv("COOKIE_SECURE", "1") != "0"
    response.set_cookie(
        DEV_COOKIE_NAME, token,
        max_age=DEV_TOKEN_TTL,
        httponly=True,           # חשוב! מגן מגישה ע"י JS
        samesite="strict",       # מונע שליחה מצד-שלישי (ב-localhost זה עדיין same-site)
        secure=secure_flag,      # ב-HTTPS חובה True
        path="/"
    )
    return {"ok": True}

@app.get("/dev-auth/status")
async def dev_status(request: Request):
    token = request.cookies.get(DEV_COOKIE_NAME)
    ok = bool(token and verify_dev_token(token, request.headers.get("user-agent","")))
    return {"ok": ok}

@app.post("/dev-auth/logout")
async def dev_logout(response: Response):
    response.delete_cookie(DEV_COOKIE_NAME, path="/")
    return {"ok": True}

@app.on_event("startup")
async def startup():
    await client.connect()
    if not await client.is_user_authorized():
        # חד-פעמי: צור session ע"י client.start(PHONE)
        raise RuntimeError(
            "Session לא מאומת. בצע פעם אחת התחברות כדי ליצור קובץ .session: "
            "with TelegramClient(SESSION, API_ID, API_HASH) as c: c.start(PHONE)"
        )

@app.on_event("shutdown")
async def shutdown():
    await client.disconnect()

# --------- Core logic ---------
async def _refresh_first_and_collect(entity, first_msg_id: int, window_sec: float) -> List[str]:
    # המתנה לחלון – בוטים עשויים לשלוח כמה הודעות בהדרגה
    await asyncio.sleep(max(0.1, window_sec))

    # נסה לרענן את ההודעה הראשונה (ייתכן ונערכה/השתנתה)
    refreshed_first = None
    with contextlib.suppress(Exception):
        refreshed_first = await client.get_messages(entity, ids=first_msg_id)
        if isinstance(refreshed_first, (list, tuple)):
            refreshed_first = refreshed_first[0] if refreshed_first else None

    replies: List[str] = []
    if refreshed_first and (refreshed_first.text or "").strip():
        replies.append(refreshed_first.text.strip())

    # הבא כל מה שנכנס אחרי ההודעה הראשונה (לא OUT)
    more: List[str] = []
    async for msg in client.iter_messages(entity, min_id=first_msg_id):
        if not msg.out:
            t = (msg.text or "").strip()
            if t:
                more.append(t)

    # סדר כרונולוגי: first ואז השאר
    replies.extend(reversed(more))

    # ניקוי כפילויות צמודות וריקות
    cleaned: List[str] = []
    for r in replies:
        if r and (not cleaned or cleaned[-1] != r):
            cleaned.append(r)
    return cleaned

async def ask_truecaller_once(text: str, window_sec: float) -> List[str]:
    entity = await client.get_entity(TARGET_BOT)

    # נסה בתוך conversation, ואם ניפול על timeout/ratelimit – fallback
    try:
        async with client.conversation(entity, timeout=max(30, int(window_sec) + 5)) as conv:
            await conv.send_message(text)
            first = await conv.get_response()
    except tg_errors.FloodWaitError as fw:
        await asyncio.sleep(fw.seconds + 1)
        async with client.conversation(entity, timeout=max(30, int(window_sec) + 5)) as conv:
            await conv.send_message(text)
            first = await conv.get_response()
    except asyncio.TimeoutError:
        await client.send_message(entity, text)
        msgs = await client.get_messages(entity, limit=1)
        first = msgs[0] if msgs else None
        if not first:
            raise HTTPException(status_code=504, detail="Timeout בקבלת תגובה מהבוט")

    replies = await _refresh_first_and_collect(entity, first.id, window_sec)
    if not replies:
        replies = [((first.text or "").strip()) if first else ""]
    return replies

# --------- Endpoints ---------
@app.post("/ask")
async def ask(body: AskBody, request: Request):
    try:
        text = normalize_msisdn(body.text)
        if not looks_like_phone(text):
            return {"ok": False, "query": body.text, "status": "invalid", "error": "מספר לא תקין"}
        _require_api_key(request)
        replies = await ask_truecaller_once(text, body.window_sec)
        return {"ok": True, "query": text, "replies": replies, "status": "ok"}
    except Exception:
        return {"ok": False, "query": body.text, "error": "שגיאה פנימית", "status": "error"}

@app.post("/ask-batch")
async def ask_batch(body: BatchBody, request: Request):
    results: List[Dict[str, Any]] = []
    for raw in body.messages:
        q = normalize_msisdn(raw.strip())
        if not q:
            results.append({"query": raw, "status": "invalid", "error": "ריק"})
            continue
        if not looks_like_phone(q):
            results.append({"query": raw, "status": "invalid", "error": "לא נראה כמספר טלפון"})
            continue
        try:
            replies = await ask_truecaller_once(q, body.window_sec)
            results.append({"query": q, "replies": replies, "status": "ok"})
        except Exception as e:
            results.append({"query": q, "status": "error", "error": "שגיאה פנימית"})
        if body.delay_ms:
            await asyncio.sleep(body.delay_ms / 1000.0)
    return {"ok": True, "count": len(results), "results": results}

@app.get("/health")
async def health(request: Request):
    token = request.cookies.get(DEV_COOKIE_NAME)
    show = bool(token and verify_dev_token(token, request.headers.get("user-agent","")))
    if show:
        me = await client.get_me()
        return {"ok": True, "me": getattr(me, "username", None)}
    return {"ok": True}
