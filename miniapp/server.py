# pip install fastapi uvicorn httpx   |  запуск: uvicorn server:app --host 0.0.0.0 --port $PORT
import gzip, os, hmac, hashlib, json, time, bisect
from urllib.parse import parse_qsl
import httpx
import asyncio
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

TOKEN = os.environ["BOT_TOKEN"]
STATIC = os.environ.get("CARDS_URL", os.environ.get("PUBLIC_URL", "https://example.com") + "/cards")  # картинки {slide}_{tier}.jpg
APP_URL = os.environ.get("APP_URL", "https://t.me/YOUR_BOT/app")
API = f"https://api.telegram.org/bot{TOKEN}"
app = FastAPI()
if os.environ.get("DATABASE_URL"):  # бот включается, только если подключена база
    import bot; bot.register(app)
else:
    print("DATABASE_URL не задан: бот выключен, работает только мини-апп")

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

# Премиум-эмодзи: достаём файл у Telegram и отдаём приложению (TGS -> JSON для lottie)
EMOJI_IDS = {"5839049953497844833", "5469741319330996757", "5283228279988309088", "5280598054901145762", "5280651583078556009", "5280922999241859582", "5406812184359507637",
    "5456299600702889290", "5454039464357682228", "5456504676801338603", "5456461744308249091", "5456147077824274112", "5456140674028019486", "5368469400695351161", "5373098778439982772", "5357107601584693888", "5445284980978621387", "5280769763398671636"}
EMOJI_CACHE: dict = {}

async def get_emoji(eid: str, thumb: bool = False):
    key = (eid, thumb)
    if key not in EMOJI_CACHE:
        async with httpx.AsyncClient(timeout=25) as c:
            st = await tg(c, "getCustomEmojiStickers", custom_emoji_ids=[eid])
            if not st: raise RuntimeError("Telegram не вернул эмодзи (getCustomEmojiStickers)")
            fid = st[0]["thumbnail"]["file_id"] if thumb and st[0].get("thumbnail") else st[0]["file_id"]
            f = await tg(c, "getFile", file_id=fid)
            if not f: raise RuntimeError("getFile не сработал")
            r = await c.get(f"https://api.telegram.org/file/bot{TOKEN}/{f['file_path']}")
        body, path = r.content, f["file_path"]
        if path.endswith(".tgs"): EMOJI_CACHE[key] = (gzip.decompress(body), "application/json")
        elif path.endswith(".webm"): EMOJI_CACHE[key] = (body, "video/webm")
        elif path.endswith((".jpg", ".jpeg")): EMOJI_CACHE[key] = (body, "image/jpeg")
        else: EMOJI_CACHE[key] = (body, "image/webp")
    return EMOJI_CACHE[key]

@app.get("/api/emoji/{eid}")
async def emoji(eid: str, thumb: int = 0):  # thumb=1 -> неподвижная картинка (для iPhone и падающих эмодзи)
    if eid not in EMOJI_IDS: raise HTTPException(404)
    try: body, mime = await get_emoji(eid, bool(thumb))
    except Exception as e:
        print("EMOJI ERROR", eid, repr(e)); raise HTTPException(502, str(e))
    return Response(content=body, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})

@app.get("/api/emoji-test")
async def emoji_test():  # диагностика: открой в браузере, чтобы увидеть, какие эмодзи грузятся
    out = {}
    for eid in sorted(EMOJI_IDS):
        try:
            b, m = await get_emoji(eid); t, tm = await get_emoji(eid, True)
            out[eid] = f"OK: {m}, {len(b)} байт; картинка: {tm}, {len(t)} байт"
        except Exception as e: out[eid] = "ОШИБКА: " + repr(e)
    return out

@app.get("/")
async def index(): return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"), headers={"Cache-Control": "no-store"})

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
        rt = chat.get("rating") or {}  # UserRating: level, rating (число на основе потраченных звёзд)
        lvl, stars = rt.get("level", 0), rt.get("rating", 0)
    days = days_in_tg(uid); points = days // 10 + gifts // 5 + uniq   # 1 балл за 10 дней, 5 обычных подарков, каждый NFT
    b = globals().get("bot")
    if b and b.pool:
        try: await b.save_profile(u, points)
        except Exception as e: print("PROFILE ERROR", repr(e))
    return {"id": uid, "days": days, "lvl": lvl, "stars": stars, "pct": pct(lvl),
            "prem": int(bool(u.get("is_premium"))), "gifts": gifts, "nft": uniq, "value": value, "points": points}

BOT_USERNAME = os.environ.get("BOT_USERNAME", "tgitoginahuibot").lstrip("@")
BOT_LINK = os.environ.get("BOT_LINK") or f"https://t.me/{BOT_USERNAME}?start=share"  # кнопка ведёт на /start бота

@app.post("/api/share")
async def share(req: Request, authorization: str = Header()):
    u = auth(authorization); b = await req.json()
    text = str(b.get("text") or b.get("title") or "Мои итоги в Telegram")[:4000]
    bt = globals().get("bot")
    label = (await bt.get_text("share_btn"))[0] if bt and bt.pool else "Проверь какая у тебя карточка!"   # текст кнопки редактируется в админке
    res = {"type": "article", "id": f"{b.get('slide', 's')}{b.get('tier', 0)}", "title": "Мои итоги в Telegram",
           "input_message_content": {"message_text": text, "link_preview_options": {"is_disabled": True}},
           "reply_markup": {"inline_keyboard": [[{"text": label, "url": f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}"}]]}}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await tg(c, "savePreparedInlineMessage", user_id=u["id"], result=res,
                     allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
    if not r: raise HTTPException(502, "Telegram не принял сообщение, смотри логи (TG ERROR savePreparedInlineMessage)")
    if bt and bt.pool:
        try: await bt.log_share(u["id"], "slide")
        except Exception as e: print("SHARE LOG ERROR", repr(e))
    return {"id": r["id"]}
