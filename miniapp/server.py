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

# уровень рейтинга -> % пользователей на этом уровне
def pct(level: int):
    if level < 0: return 10
    if level == 0: return 50
    if level == 1: return 40
    if level == 2: return 6
    for hi, v in ((5, 5), (10, 2), (20, 0.7), (30, 0.5), (40, 0.3), (50, 0.2), (60, 0.15), (70, 0.1), (80, 0.08), (90, 0.05)):
        if level <= hi: return v
    return 0.01

async def tg(client, method, **p):
    r = await client.post(f"{API}/{method}", json=p); j = r.json()
    if not j.get("ok"): print("TG ERROR", method, j)
    return j.get("result") if j.get("ok") else None

# Премиум-эмодзи: достаём файл у Telegram и отдаём приложению (TGS -> JSON для lottie)
EMOJI_IDS = {"5839049953497844833", "5469741319330996757", "5283228279988309088", "5280598054901145762", "5280651583078556009", "5280922999241859582", "5406812184359507637", "5289761157173775507", "5983201451227222757", "5443038326535759644",
    "5456299600702889290", "5454039464357682228", "5456504676801338603", "5456461744308249091", "5456147077824274112", "5456140674028019486", "5368469400695351161", "5373098778439982772", "5357107601584693888", "5445284980978621387", "5280769763398671636", "5372800046284674872", "5418028873705069979", "5397782960512444700", "5444965061749644170", "5231302159739395058", "5224648868850863664", "5453900977432188793", "5354820836442287981", "5438571934210082705", "5357395115285438516", "5339564150534200424"}
EMOJI_CACHE: dict = {}
RECOLOR = {"5453900977432188793"}   # у этих эмодзи звезду (сине-фиолетово-розовые цвета) перекрашиваем в жёлтый

def _yellow(rgb):
    import colorsys
    r, g, b = rgb; h, l, sat = colorsys.rgb_to_hls(r, g, b)
    if sat > 0.15 and 0.5 <= h <= 0.95:   # синий / фиолетовый / розовый -> жёлтый
        h = 0.125; sat = min(1.0, sat * 1.1 + 0.25); l = min(0.62, max(0.45, l))
        r, g, b = colorsys.hls_to_rgb(h, l, sat)
    return [r, g, b]

def _fix_list(arr, start):
    if isinstance(arr, list) and len(arr) >= start + 3 and all(isinstance(x, (int, float)) for x in arr[start:start + 3]):
        arr[start:start + 3] = _yellow(arr[start:start + 3])

def recolor(node):
    if isinstance(node, dict):
        ty = node.get("ty")
        if ty in ("fl", "st") and isinstance(node.get("c"), dict):
            k = node["c"].get("k")
            if node["c"].get("a") == 1 and isinstance(k, list):
                for kf in k:
                    if isinstance(kf, dict): _fix_list(kf.get("s"), 0); _fix_list(kf.get("e"), 0)
            else: _fix_list(k, 0)
        if ty in ("gf", "gs") and isinstance(node.get("g"), dict):
            p = node["g"].get("p", 0); gk = node["g"].get("k", {})
            if isinstance(gk, dict):
                if gk.get("a") == 1 and isinstance(gk.get("k"), list):
                    for kf in gk["k"]:
                        if isinstance(kf, dict):
                            for key in ("s", "e"):
                                for i in range(p): _fix_list(kf.get(key), i * 4 + 1)
                else:
                    for i in range(p): _fix_list(gk.get("k"), i * 4 + 1)
        for v in node.values(): recolor(v)
    elif isinstance(node, list):
        for v in node: recolor(v)
    return node

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
        if path.endswith(".tgs"):
            raw = gzip.decompress(body)
            if eid in RECOLOR:
                try: raw = json.dumps(recolor(json.loads(raw)), separators=(",", ":")).encode()
                except Exception as e: print("RECOLOR ERROR", eid, repr(e))
            EMOJI_CACHE[key] = (raw, "application/json")
        elif path.endswith(".webm"): EMOJI_CACHE[key] = (body, "video/webm")
        elif path.endswith((".jpg", ".jpeg")): EMOJI_CACHE[key] = (body, "image/jpeg")
        else: EMOJI_CACHE[key] = (body, "image/webp")
    return EMOJI_CACHE[key]

PNG_CACHE: dict = {}
ANIM_CACHE: dict = {}

def render_anim(tgs_json: bytes, size: int):  # вся анимация -> ОДНА неподвижная картинка-лист кадров (спрайт); приложение просто листает кадры, без нагрузки
    import io
    from PIL import Image
    from rlottie_python import LottieAnimation
    a = LottieAnimation.from_data(tgs_json.decode("utf-8"))
    n = max(1, a.lottie_animation_get_totalframe()); fr = a.lottie_animation_get_framerate() or 30
    dur = n / fr
    if size > 256:  # режим высокой плавности: родная частота кадров (до 60/с), лимит кадров по памяти телефона
        F = max(1, min(int(dur * min(60, fr)), max(1, 10_000_000 // (size * size))))
        cols = min(F, max(1, 4096 // size))
    else:
        F = max(1, min(60, int(dur * 24))); cols = 8  # ~24 кадра/с, максимум 60
    rows = -(-F // cols)
    sheet = Image.new("RGBA", (cols * size, rows * size), (0, 0, 0, 0))
    for i in range(F):
        sheet.paste(a.render_pillow_frame(frame_num=min(n - 1, int(i * n / F)), width=size, height=size), ((i % cols) * size, (i // cols) * size))
    buf = io.BytesIO(); sheet.save(buf, "WEBP", quality=84, method=4)
    return buf.getvalue(), f"{cols},{rows},{F},{int(dur * 1000)}"


def render_png(tgs_json: bytes, size: int, at: float = -1) -> bytes:  # один кадр векторной анимации -> PNG высокого качества
    import io
    from rlottie_python import LottieAnimation
    a = LottieAnimation.from_data(tgs_json.decode("utf-8"))
    n = max(1, a.lottie_animation_get_totalframe())
    if at >= 0:  # кадр из заданного места анимации (0..1)
        img = a.render_pillow_frame(frame_num=min(n - 1, int(n * at)), width=size, height=size)
        buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()
    best = None
    for f in (0, n // 10, n // 4, n // 2):  # первый кадр; если пустой — ближайший непустой
        img = a.render_pillow_frame(frame_num=min(f, n - 1), width=size, height=size)
        best = best or img
        if img.getchannel("A").getextrema()[1] > 0: best = img; break
    buf = io.BytesIO(); best.save(buf, "PNG"); return buf.getvalue()

WARM = [("5354820836442287981", 288)]   # эмодзи, у которых покадровый лист готовим заранее
async def _warm():
    for eid, size in WARM:
        try:
            body, mime = await get_emoji(eid)
            if mime == "application/json" and (eid, size) not in ANIM_CACHE:
                ANIM_CACHE[(eid, size)] = await asyncio.to_thread(render_anim, body, size)
        except Exception as e: print("WARM ERROR", eid, repr(e))

@app.on_event("startup")
async def _startup(): asyncio.create_task(_warm())

@app.get("/api/emoji/{eid}")
async def emoji(eid: str, thumb: int = 0, png: int = 0, anim: int = 0, at: float = -1):  # thumb=1 -> неподвижная картинка; png=512 -> кадр в высоком качестве
    if eid not in EMOJI_IDS: raise HTTPException(404)
    if anim:
        size = min(max(anim, 64), 384); key = (eid, size)
        try:
            if key not in ANIM_CACHE:
                body, mime = await get_emoji(eid)
                if mime != "application/json": raise RuntimeError("не векторная анимация: " + mime)
                ANIM_CACHE[key] = await asyncio.to_thread(render_anim, body, size)
            img, meta = ANIM_CACHE[key]
            return Response(content=img, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400", "X-Sheet": meta, "Access-Control-Expose-Headers": "X-Sheet"})
        except Exception as e:
            print("EMOJI ANIM ERROR", eid, repr(e)); raise HTTPException(502, str(e))
    if png:
        size = min(max(png, 64), 1024); key = (eid, size, at)
        try:
            if key not in PNG_CACHE:
                body, mime = await get_emoji(eid)
                if mime != "application/json": raise RuntimeError("не векторная анимация: " + mime)
                PNG_CACHE[key] = await asyncio.to_thread(render_png, body, size, at)
            return Response(content=PNG_CACHE[key], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
        except Exception as e:
            print("EMOJI PNG ERROR", eid, repr(e)); raise HTTPException(502, str(e))
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
                else:  # обычный подарок: считаем только БЕЗ лимита (у лимитированных есть total_count / remaining_count)
                    gf = g.get("gift") or {}
                    if gf.get("total_count") is not None or gf.get("remaining_count") is not None: continue
                    gifts += 1; value += gf.get("star_count", 0)  # цена подарка в звёздах
            off = r.get("next_offset")
            if not off: break
        chat = await tg(c, "getChat", chat_id=uid) or {}
        rt = chat.get("rating") or {}  # UserRating: level, rating (число на основе потраченных звёзд)
        lvl, stars = rt.get("level", 0), rt.get("rating", 0)
        stars = int(round((stars or 0) * 1.15))   # к потраченным звёздам прибавляем 15% (в приложении об этом нигде не пишем)
    days = days_in_tg(uid); points = days // 20 + gifts // 3 + uniq * 2 + int(stars or 0) // 2000   # 1 балл за 20 дней, за 3 обычных подарка, 3 за NFT, 1 за каждые 1000 звёзд
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
    return {"id": r["id"]}
