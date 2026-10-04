# pip install fastapi uvicorn httpx   |  запуск: uvicorn server:app --host 0.0.0.0 --port $PORT
import os, hmac, hashlib, json, time, bisect
from urllib.parse import parse_qsl
import httpx
import asyncio
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

TOKEN = os.environ["BOT_TOKEN"]
STATIC = os.environ.get("CARDS_URL", os.environ.get("PUBLIC_URL", "https://example.com") + "/cards")  # картинки {slide}_{tier}.jpg
APP_URL = os.environ.get("APP_URL", "https://t.me/YOUR_BOT/app")
API = f"https://api.telegram.org/bot{TOKEN}"
app = FastAPI()

FLOORS: dict = {}  # маркет не используется
app.mount("/cards", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "cards")), name="cards")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"], allow_methods=["*"])

def auth(h: str) -> dict:
    data = dict(parse_qsl(h.removeprefix("tma "), keep_blank_values=True))
    got = data.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), got):
        raise HTTPException(401)
    return json.loads(data["user"])

# Приблизительная дата регистрации по user_id (Telegram её не отдаёт). Подправь точки при желании.
PTS = [(2768409,"2013-11-01"),(46145305,"2015-04-01"),(101260938,"2016-08-01"),(500000000,"2018-12-01"),
       (1000000000,"2020-06-01"),(2000000000,"2021-04-01"),(5000000000,"2022-06-01"),
       (6500000000,"2023-10-01"),(7500000000,"2024-10-01"),(8500000000,"2025-10-01")]
def days_in_tg(uid: int) -> int:
    ids = [p[0] for p in PTS]; ts = [time.mktime(time.strptime(p[1], "%Y-%m-%d")) for p in PTS]
    j = min(max(bisect.bisect(ids, uid), 1), len(ids) - 1)
    t = ts[j-1] + (uid - ids[j-1]) / (ids[j] - ids[j-1]) * (ts[j] - ts[j-1])
    return max(1, int((time.time() - t) / 86400))

# уровень рейтинга -> примерный % пользователей (подставь реальные цифры)
def pct(level: int) -> int:
    return 60 if level < 1 else 35 if level < 5 else 15 if level < 10 else 5 if level < 20 else 1

async def tg(client, method, **p):
    r = await client.post(f"{API}/{method}", json=p); j = r.json()
    if not j.get("ok"): print("TG ERROR", method, j)
    return j.get("result") if j.get("ok") else None

@app.get("/")
async def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

@app.get("/api/me")
async def me(authorization: str = Header()):
    u = auth(authorization); uid = u["id"]
    async with httpx.AsyncClient(timeout=20) as c:
        gifts, uniq, value, off = 0, 0, 0, ""
        while True:  # все подарки профиля
            r = await tg(c, "getUserGifts", user_id=uid, limit=100, offset=off)
            if not r: break
            for g in r["gifts"]:
                if g["type"] == "unique":
                    uniq += 1; value += FLOORS.get(g["gift"].get("base_name", ""), 0)  # флор-цена с маркета
                else: gifts += 1; value += g.get("gift", {}).get("star_count", 0)
            off = r.get("next_offset")
            if not off: break
        chat = await tg(c, "getChat", chat_id=uid) or {}
        lvl = (chat.get("rating") or {}).get("level", 0)  # проверь имя поля в актуальной доке Bot API
    return {"id": uid, "days": days_in_tg(uid), "lvl": lvl, "pct": pct(lvl),
            "prem": int(bool(u.get("is_premium"))), "gifts": gifts, "nft": uniq, "value": value}

@app.post("/api/share")
async def share(req: Request, authorization: str = Header()):
    u = auth(authorization); b = await req.json()
    res = {"type": "photo", "id": f"{b['slide']}{b['tier']}",
           "photo_url": f"{STATIC}/{b['slide']}_{b['tier']}.jpg", "thumbnail_url": f"{STATIC}/{b['slide']}_{b['tier']}.jpg",
           "caption": b["title"],
           "reply_markup": {"inline_keyboard": [[{"text": "Проверь какая у тебя карточка!", "url": APP_URL}]]}}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await tg(c, "savePreparedInlineMessage", user_id=u["id"], result=res,
                     allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
    if not r: raise HTTPException(502)
    return {"id": r["id"]}
