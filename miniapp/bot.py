"""Бот: обязательные задания (подписка / заявка), админ-панель, статистика. Работает через webhook на том же сервере."""
import os, re, json, html, time, asyncio, hashlib
from datetime import datetime, timedelta, timezone
from html import escape as E
from urllib.parse import urlparse, unquote
import asyncpg, httpx
from fastapi import Request, HTTPException, Header
from fastapi.responses import RedirectResponse

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
PUBLIC = os.environ.get("PUBLIC_URL", "").rstrip("/")
ADMINS = {8008322348, 1237551150}
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
pool = http = None
BOT_ID = 0
BC = {"run": False, "stop": False}
BG: set = set()
STATE: dict = {}   # админ -> шаг добавления задания (хранится в памяти)
ERR = [""]
WELCOME = "👋 <b>Привет!</b>\n\nЗдесь ты узнаешь <b>всё о своём Telegram-аккаунте</b> ✨\n\nЖми кнопку ниже 👇"
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id BIGINT PRIMARY KEY, first_seen TIMESTAMPTZ DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS tasks(id SERIAL PRIMARY KEY, chat_id BIGINT, title TEXT, kind TEXT, link TEXT,
  active BOOLEAN DEFAULT TRUE, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS events(task_id INT, user_id BIGINT, kind TEXT, ts TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY(task_id, user_id, kind));
CREATE TABLE IF NOT EXISTS profiles(user_id BIGINT PRIMARY KEY, name TEXT, username TEXT, photo TEXT,
  points INT DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS referrals(invitee BIGINT PRIMARY KEY, inviter BIGINT, qualified BOOLEAN DEFAULT FALSE, created TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS purchases(charge_id TEXT PRIMARY KEY, user_id BIGINT, stars INT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS shares(id SERIAL PRIMARY KEY, user_id BIGINT, kind TEXT, ts TIMESTAMPTZ DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS passed_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS limit_n INT DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sort INT DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS links(id SERIAL PRIMARY KEY, name TEXT NOT NULL, created TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS links_name_ci ON links(lower(name));
CREATE TABLE IF NOT EXISTS link_starts(link_id INT, user_id BIGINT, ts TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(link_id, user_id));
ALTER TABLE link_starts ADD COLUMN IF NOT EXISTS is_new BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS link_clicks(link_id INT, user_id BIGINT, ts TIMESTAMPTZ DEFAULT now());
"""

BOT_USERNAME = os.environ.get("BOT_USERNAME", "tgitoginahuibot").lstrip("@")
def ref_link(uid): return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
def src_link(name): return f"https://t.me/{BOT_USERNAME}?start=src_{name}"   # уникальная ссылка для статистики источников

def auth_user(h: str) -> dict:  # проверка подписи Telegram (initData)
    import hmac, json
    from urllib.parse import parse_qsl
    data = dict(parse_qsl(h.removeprefix("tma "), keep_blank_values=True)); got = data.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), got): raise HTTPException(401)
    return json.loads(data["user"])

TZ = timezone(timedelta(hours=int(os.environ.get("REPORT_TZ_OFFSET", "3"))))   # часовой пояс статистики и отчётов (по умолчанию UTC+3)

async def get_setting(key, default=None):
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if v is None else v

async def set_setting(key, value):
    await pool.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, str(value))

# редактируемые тексты: ключ -> (название, значение по умолчанию, поддерживает форматирование и премиум-эмодзи)
TEXTS = {
    "welcome": ("Приветствие после проверки", "👋 <b>Привет!</b>\n\nЗдесь ты узнаешь <b>всё о своём Telegram-аккаунте</b> ✨\n\n<blockquote>📅 Твой стаж и «возраст» аккаунта\n🎁 Обычные и NFT-подарки\n⭐ Рейтинг и Telegram Premium\n🏆 Баллы и место среди топ-100</blockquote>\n\nЖми кнопку ниже и держи палец на экране 👇", True),
    "gate": ("Текст над заданиями", "👋 <b>Привет! Добро пожаловать</b>\n\nЧтобы открыть свои результаты, сделай пару простых шагов:\n\n<blockquote>1️⃣ Выполни задания ниже\n2️⃣ Нажми «✅ Проверить»</blockquote>", True),
    "invite_text": ("Текст приглашения друзьям", "👋 Смотри результаты своего Telegram-аккаунта!\nУзнай свой стаж, подарки и баллы и сравни себя с топ-100 👇", True),
    "welcome_btn": ("Кнопка под приветствием", "📱 Открыть результаты", False),
    "invite_btn": ("Кнопка в приглашении друзьям", "Узнать мои результаты", False),
    "share_btn": ("Кнопка под пересланным слайдом", "Проверь какая у тебя карточка!", False)}
RICH_OK = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "text_link", "custom_emoji", "blockquote"}

async def get_text(key):  # -> (текст, entities или None)
    raw = await get_setting("txt_" + key)
    if raw:
        d = json.loads(raw); t, e = d["t"], d.get("e") or None
        if e is None and TEXTS[key][2]: t = E(t)   # текст без форматирования уходит в HTML-режиме — экранируем < > &
        return t, e
    return TEXTS[key][1], None

async def log_share(uid, kind):  # нажатие «Поделиться» / «Переслать приглашение»
    await pool.execute("INSERT INTO shares(user_id,kind) VALUES($1,$2)", uid, kind)

async def get_need() -> int:
    v = await pool.fetchval("SELECT value FROM settings WHERE key='ref_need'")
    return int(v) if v is not None else 2   # по умолчанию нужно пригласить 2 человек

async def get_price() -> int:  # цена доп. информации в звёздах (0 = платный вариант выключен)
    v = await pool.fetchval("SELECT value FROM settings WHERE key='ref_price'")
    return int(v) if v is not None else 0

async def save_profile(u, points):  # профиль для таблицы лидеров (вызывает мини-апп)
    name = ((u.get("first_name") or "") + " " + (u.get("last_name") or "")).strip() or u.get("username") or "Без имени"
    await pool.execute("""INSERT INTO profiles(user_id,name,username,photo,points) VALUES($1,$2,$3,$4,$5)
        ON CONFLICT (user_id) DO UPDATE SET name=$2, username=$3, photo=$4, points=$5""",
        u["id"], name, u.get("username") or "", u.get("photo_url") or "", int(points))

async def qualify(uid):  # приглашённый выполнил условия -> засчитываем реферала
    inv = await pool.fetchval("UPDATE referrals SET qualified=TRUE WHERE invitee=$1 AND NOT qualified RETURNING inviter", uid)
    if inv:
        n = await pool.fetchval("SELECT count(*) FROM referrals WHERE inviter=$1 AND qualified", inv); need = await get_need()
        await send(inv, f"🎉 Друг присоединился по твоей ссылке! Приглашено: <b>{n}</b> из <b>{need}</b>" + ("\n✅ Дополнительная информация открыта!" if n >= need else ""))

async def call(method, **p):
    r = await http.post(f"{API}/{method}", json=p); j = r.json()
    if not j.get("ok"):
        ERR[0] = j.get("description", ""); print("TG ERROR", method, ERR[0]); return None
    return j["result"]

def kb(*rows): return {"inline_keyboard": [list(r) for r in rows]}
def cb(text, data): return {"text": text, "callback_data": data}
def no_ce(ents): return [e for e in ents if e.get("type") != "custom_emoji"]

async def send(cid, text, markup=None, entities=None):
    p = {"chat_id": cid, "text": text, "disable_web_page_preview": True}
    if entities: p["entities"] = entities
    else: p["parse_mode"] = "HTML"
    if markup: p["reply_markup"] = markup
    r = await call("sendMessage", **p)
    if r is None and entities and len(no_ce(entities)) != len(entities):  # премиум-эмодзи не приняты -> отправляем без них
        p["entities"] = no_ce(entities); r = await call("sendMessage", **p)
    return r

async def edit(cid, mid, text, markup=None, entities=None):
    p = {"chat_id": cid, "message_id": mid, "text": text, "disable_web_page_preview": True}
    if entities: p["entities"] = entities
    else: p["parse_mode"] = "HTML"
    if markup: p["reply_markup"] = markup
    r = await call("editMessageText", **p)
    if r is None and entities and len(no_ce(entities)) != len(entities):
        p["entities"] = no_ce(entities); r = await call("editMessageText", **p)
    return r

async def startup():
    try: await _startup()
    except Exception as e:  # ошибка бота не должна ронять мини-апп
        print("BOT STARTUP FAILED:", repr(e))

async def _startup():
    global pool, http, BOT_ID
    http = httpx.AsyncClient(timeout=25)
    u = urlparse(os.environ["DATABASE_URL"])
    pool = await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=unquote(u.username or ""),
        password=unquote(u.password or ""), database=u.path.lstrip("/"),
        ssl=None if u.hostname in ("localhost", "127.0.0.1") else "require")
    await pool.execute(SCHEMA)
    await pool.execute("UPDATE tasks SET sort=id WHERE sort=0")
    me = await call("getMe")
    if not me: raise RuntimeError("getMe не сработал: проверь BOT_TOKEN")
    BOT_ID = me["id"]
    await call("setWebhook", url=PUBLIC + "/webhook", secret_token=SECRET,
               allowed_updates=["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member", "pre_checkout_query"])
    t = asyncio.create_task(report_loop()); BG.add(t); t.add_done_callback(BG.discard)   # ежедневный отчёт
    print("BOT READY", BOT_ID)

async def is_done(t, uid):
    m = await call("getChatMember", chat_id=t["chat_id"], user_id=uid)
    if m and (m["status"] in ("member", "administrator", "creator") or (m["status"] == "restricted" and m.get("is_member"))):
        return True
    if t["kind"] == "request":  # заявка ещё не принята -> статус left, смотрим нашу запись о заявке
        return bool(await pool.fetchval("SELECT 1 FROM events WHERE task_id=$1 AND user_id=$2 AND kind='request'", t["id"], uid))
    return False

async def active_tasks():  # включённые задания по порядку; задания с достигнутым лимитом больше не требуются
    return await pool.fetch("""SELECT t.* FROM tasks t WHERE t.active AND NOT t.deleted AND
        (t.limit_n = 0 OR (SELECT count(*) FROM events e WHERE e.task_id=t.id AND e.kind='done') < t.limit_n) ORDER BY t.sort, t.id""")

async def gate(uid, cid, mid=None, cbid=None):
    todo = []
    for t in await active_tasks():
        if await is_done(t, uid):
            new = await pool.fetchval("INSERT INTO events(task_id,user_id,kind) VALUES($1,$2,'done') ON CONFLICT DO NOTHING RETURNING 1", t["id"], uid)
            if new and t["limit_n"]:
                n = await pool.fetchval("SELECT count(*) FROM events WHERE task_id=$1 AND kind='done'", t["id"])
                if n >= t["limit_n"] and await pool.fetchval("INSERT INTO settings(key,value) VALUES($1,'1') ON CONFLICT DO NOTHING RETURNING 1", f"limit_{t['id']}"):
                    for a in ADMINS: await send(a, f"🎯 Задание #{t['id']} «{E(t['title'])}» выполнили {n} человек — лимит достигнут, оно больше не требуется.")
        else: todo.append(t)
    if not todo:
        if cbid: await call("answerCallbackQuery", callback_query_id=cbid)
        await pool.execute("UPDATE users SET passed_at=now() WHERE user_id=$1 AND passed_at IS NULL", uid)   # для воронки
        await qualify(uid)
        wt, we = await get_text("welcome"); btn, _ = await get_text("welcome_btn")
        await send(cid, wt, kb([{"text": btn, "web_app": {"url": PUBLIC}}]), we); return
    rows = [[{"text": ("📢 Подписаться: " if t["kind"] == "link" else "✉️ Подать заявку: ") + t["title"][:40],
              "url": t["link"]}] for t in todo]
    rows.append([cb("✅ Проверить", "chk")])
    text, ents = await get_text("gate")
    if cbid: await call("answerCallbackQuery", callback_query_id=cbid, text="Не все задания выполнены", show_alert=True)
    if mid: await edit(cid, mid, text, kb(*rows), ents)
    else: await send(cid, text, kb(*rows), ents)

def pb(text, data, eid=None):  # кнопка с премиум-эмодзи слева (icon_custom_emoji_id)
    b = {"text": text, "callback_data": data}
    if eid: b["icon_custom_emoji_id"] = eid
    return b
PANEL = kb([pb("Рассылка", "bc", "5260268501515377807"), pb("Задания", "tk", "5257965174979042426")],
           [pb("Добавить задание", "add", "5274008024585871702"), pb("Стат. ссылок", "ls", "5260730055880876557")],
           [pb("Доступ к доп. инфо", "refs", "5258362837411045098"), pb("Отчёт", "rp", "5258362837411045098")],
           [pb("Статистика", "st", "5258391025281408576")],
           [pb("Уникальные ссылки", "ul", "5260730055880876557")])
PANEL_PLAIN = kb([cb("📢 Рассылка", "bc"), cb("📋 Задания", "tk")], [cb("➕ Добавить задание", "add"), cb("🔗 Стат. ссылок", "ls")],
                 [cb("👥 Доступ к доп. инфо", "refs"), cb("📬 Отчёт", "rp")], [cb("📊 Статистика", "st")],
                 [cb("🔗 Уникальные ссылки", "ul")])   # запасной вариант, если премиум-эмодзи не приняты
ADMIN_HELLO = "<b>Привет! Это админ панель, здесь ты можешь управлять своим ботом!</b> "

async def resolve_chat(msg):
    t = (msg.get("text") or "").strip()
    fo = msg.get("forward_origin") or {}
    if fo.get("chat"): return fo["chat"]
    m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/\d+)?/?", t) or re.fullmatch(r"@([A-Za-z0-9_]{4,})", t)
    if m: return await call("getChat", chat_id="@" + m.group(1))
    if re.fullmatch(r"-?\d{6,}", t): return await call("getChat", chat_id=int(t))
    return None

# ================= Админка: каждый экран — в одном сообщении =================
def u16(s): return len(s.encode("utf-16-le")) // 2   # Telegram считает смещения форматирования в UTF-16

def tgt(q):  # какое сообщение сейчас на экране у админа
    m = q["message"]; return {"cid": m["chat"]["id"], "mid": m.get("message_id"), "photo": bool(m.get("photo"))}

async def delete(cid, mid):
    if mid: await call("deleteMessage", chat_id=cid, message_id=mid)

async def scr(T, text, markup=None, entities=None):
    """Показать экран: правим текущее сообщение; если нельзя (например, это баннер-фото) — сразу шлём новое и удаляем старое."""
    cid, mid = T["cid"], T.get("mid")
    if mid and not T.get("photo"):
        r = await edit(cid, mid, text, markup, entities)
        if r is not None or "not modified" in ERR[0]: return mid
    r = await send(cid, text, markup, entities)
    await delete(cid, mid)
    T["mid"] = r["message_id"] if r else None; T["photo"] = False
    return T["mid"]

async def send_banner(cid, caption):  # главное меню с картинкой «АДМИН ПАНЕЛЬ»
    fid = await get_setting("admin_banner_id")
    srcs = ([fid] if fid else []) + [PUBLIC + "/cards/admin_panel.jpg"]
    # сначала с премиум-эмодзи, потом без них
    for cap, mk in ((caption + '<tg-emoji emoji-id="5258073068852485953">👋</tg-emoji>', PANEL), (caption + "👋", PANEL_PLAIN)):
        for src in srcs:
            r = await call("sendPhoto", chat_id=cid, photo=src, caption=cap, parse_mode="HTML", reply_markup=mk)
            if r:
                if src != fid and r.get("photo"): await set_setting("admin_banner_id", r["photo"][-1]["file_id"])   # дальше шлём по file_id, без загрузки
                return r
    return await send(cid, caption + "👋", PANEL_PLAIN)   # картинка не загрузилась — обычное меню

async def panel(T, note=""):
    r = await send_banner(T["cid"], (note + "\n\n" if note else "") + ADMIN_HELLO)
    await delete(T["cid"], T.get("mid"))
    T["mid"] = r["message_id"] if r else None; T["photo"] = bool(r and r.get("photo"))
    return r

def compose(header, body, ents, footer=""):  # текст с форматированием/премиум-эмодзи внутри экрана: сдвигаем смещения
    if not ents: return E(header) + E(body) + E(footer), None
    off = u16(header)
    return header + body + footer, [{**x, "offset": x["offset"] + off} for x in ents]

CANCEL = kb([cb("❌ Отмена", "cancel")])
def T_for(uid, cid): return {"cid": cid, "mid": STATE.get(uid, {}).get("pm"), "photo": False}

async def ask(T, uid, step, text, markup=CANCEL, **extra):  # экран-вопрос; ответ админа придёт сообщением
    await scr(T, text, markup)
    STATE[uid] = {"step": step, "pm": T["mid"], "prompt": text, "pmk": markup, **extra}

async def bad(uid, cid, note):  # ошибка ввода — показываем в том же сообщении
    st = STATE[uid]; T = T_for(uid, cid)
    await scr(T, "⚠️ " + note + "\n\n" + st.get("prompt", ""), st.get("pmk", CANCEL)); st["pm"] = T["mid"]

async def bc_ask(T, st, step, text, markup=CANCEL):  # то же, но внутри рассылки (состояние не сбрасываем)
    await scr(T, text, markup); st.update(step=step, pm=T["mid"], prompt=text, pmk=markup)

def bc_markup(btns):
    rows = []
    for b in btns:
        x = {"text": b["text"], "url": b["url"]}
        if b["style"]: x["style"] = b["style"]   # danger = красная, success = зелёная
        if rows and not b.get("new_row", True) and len(rows[-1]) < 8: rows[-1].append(x)   # рядом с предыдущей
        else: rows.append([x])
    return {"inline_keyboard": rows} if rows else None

async def bc_show(T, uid):  # меню рассылки; кнопки у предпросмотра обновляем на месте
    st = STATE[uid]; mk = bc_markup(st["btns"])
    if st.get("pv"):
        p = {"chat_id": T["cid"], "message_id": st["pv"]}
        if mk: p["reply_markup"] = mk
        await call("editMessageReplyMarkup", **p)
    await scr(T, f"Так увидят сообщение (выше). Кнопок: {len(st['btns'])}\nДобавить ещё кнопку или запустить рассылку?",
              kb([cb("➕ Добавить кнопку", "bcadd")], [cb("🚀 Запустить рассылку", "bcgo")], [cb("❌ Отмена", "cancel")]))
    st.update(step="bc_menu", pm=T["mid"])

async def bc_cleanup(cid, st):  # убрать предпросмотр и исходное сообщение рассылки
    await delete(cid, st.get("pv")); await delete(cid, st.get("mid"))

async def copy_to(uid, src, mid, mk):
    for _ in range(3):
        p = {"chat_id": uid, "from_chat_id": src, "message_id": mid}
        if mk: p["reply_markup"] = mk
        j = (await http.post(f"{API}/copyMessage", json=p)).json()
        if j.get("ok"): return "ok"
        d, code = j.get("description", ""), j.get("error_code")
        if code == 429: await asyncio.sleep(j.get("parameters", {}).get("retry_after", 1) + 1); continue
        if code == 403 or "deactivated" in d or "chat not found" in d: return "blocked"
        return "fail"
    return "fail"

async def bc_text(sent, total, fail, done):
    blocked = await pool.fetchval("SELECT count(*) FROM users WHERE blocked")
    return (("✅ Рассылка завершена" if done else "📤 Рассылка идёт…") + f"\n\nОтправлено: <b>{sent}</b> из <b>{total}</b>"
            f"\n🚫 Заблокировали бота: <b>{blocked}</b>\n⚠️ Ошибок: <b>{fail}</b>")

async def run_bc(cid, src, mk, smid, pv=None):
    BC.update(run=True, stop=False)
    try:
        total = await pool.fetchval("SELECT count(*) FROM users")
        users = [r["user_id"] for r in await pool.fetch("SELECT user_id FROM users WHERE NOT blocked")]
        sent = fail = 0; last = time.time()
        stop_kb = kb([cb("⏹ Остановить", "bcstop")])
        for u in users:
            if BC["stop"]: break
            r = await copy_to(u, cid, src, mk)
            if r == "ok": sent += 1
            elif r == "blocked": await pool.execute("UPDATE users SET blocked=TRUE WHERE user_id=$1", u)
            else: fail += 1
            if time.time() - last > 3:
                last = time.time()
                await call("editMessageText", chat_id=cid, message_id=smid, parse_mode="HTML", text=await bc_text(sent, total, fail, False), reply_markup=stop_kb)
            await asyncio.sleep(0.05)   # ~20 сообщений в секунду, в рамках лимитов Telegram
        txt = await bc_text(sent, total, fail, True) + ("\n⏹ Остановлена вручную" if BC["stop"] else "")
        await call("editMessageText", chat_id=cid, message_id=smid, parse_mode="HTML", text=txt, reply_markup=kb([cb("⬅️ Меню", "adm")]))
    except Exception as e: print("broadcast error:", repr(e))
    finally:
        BC["run"] = False
        await delete(cid, pv); await delete(cid, src)   # убираем предпросмотр и исходное сообщение


# ================= Статистика и ежедневный отчёт =================
async def period_counts(table, col, since, extra=""):
    return await pool.fetchval(f"SELECT count(*) FROM {table} WHERE {col} >= $1 {extra}", since)

async def stats_text():
    now = datetime.now(TZ); t0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    n = await pool.fetchval("SELECT count(*) FROM users"); b = await pool.fetchval("SELECT count(*) FROM users WHERE blocked")
    nt, nw, nm = [await period_counts("users", "first_seen", x) for x in (t0, now - timedelta(days=7), now - timedelta(days=30))]
    passed = await pool.fetchval("SELECT count(*) FROM users WHERE passed_at IS NOT NULL")
    app = await pool.fetchval("SELECT count(*) FROM profiles p JOIN users u ON u.user_id=p.user_id")
    need = await get_need()
    buyers = await pool.fetchval("SELECT count(DISTINCT user_id) FROM purchases")
    inviters = await pool.fetchval("SELECT count(*) FROM (SELECT inviter FROM referrals WHERE qualified GROUP BY inviter HAVING count(*) >= $1) t", need)
    opened = await pool.fetchval("""SELECT count(*) FROM (SELECT user_id FROM purchases UNION
        SELECT inviter FROM referrals WHERE qualified GROUP BY inviter HAVING count(*) >= $1) t""", need)
    pc, ps = await pool.fetchrow("SELECT count(*), coalesce(sum(stars),0) FROM purchases")
    st = [await pool.fetchval("SELECT coalesce(sum(stars),0) FROM purchases WHERE ts >= $1", x) for x in (t0, now - timedelta(days=7), now - timedelta(days=30))]
    sh = {r["kind"]: r["c"] for r in await pool.fetch("SELECT kind, count(*) c FROM shares WHERE ts >= $1 GROUP BY kind", now - timedelta(days=7))}
    pct = lambda x: f"{x * 100 // n}%" if n else "—"
    return (f"📊 <b>Статистика</b>\n\n👥 Уникальных стартов: <b>{n}</b>\n🚫 Заблокировали бота: <b>{b}</b> · ✅ Активных: <b>{n - b}</b>\n\n"
            f"🆕 <b>Новые</b>: сегодня <b>{nt}</b> · 7 дней <b>{nw}</b> · 30 дней <b>{nm}</b>\n\n"
            f"🔻 <b>Воронка</b>\n1️⃣ Нажали «Начать»: <b>{n}</b> (100%)\n2️⃣ Выполнили задания: <b>{passed}</b> ({pct(passed)})\n"
            f"3️⃣ Открыли приложение: <b>{app}</b> ({pct(app)})\n4️⃣ Открыли доп. информацию: <b>{opened}</b> ({pct(opened)})\n"
            f"      └ купили: {buyers} · пригласили: {inviters}\n\n"
            f"⭐ <b>Звёзды</b>\n💰 Заработано всего: <b>{ps}</b> ⭐\n🛒 Потрачено на доп. информацию: <b>{ps}</b> ⭐ ({pc} покупок)\nсегодня {st[0]} · 7 дней {st[1]} · 30 дней {st[2]}\n\n"
            f"📤 <b>Пересылки</b> за 7 дней: слайды <b>{sh.get('slide', 0)}</b> · приглашения <b>{sh.get('invite', 0)}</b>\n\n"
            f"<i>Этап «выполнили задания» считается с момента этого обновления.</i>")

async def last_purchases_text():
    rows = await pool.fetch("""SELECT p.user_id, p.stars, p.ts, pr.name, pr.username FROM purchases p
        LEFT JOIN profiles pr ON pr.user_id=p.user_id ORDER BY p.ts DESC LIMIT 10""")
    if not rows: return "🧾 Покупок пока нет."
    return "🧾 <b>Последние покупки</b>\n\n" + "\n".join(
        f"⭐ <b>{r['stars']}</b> — <a href=\"tg://user?id={r['user_id']}\">{E(r['name'] or 'Без имени')}</a>"
        f"{' (@' + E(r['username']) + ')' if r['username'] else ''} · {r['ts'].astimezone(TZ):%d.%m %H:%M}" for r in rows)

async def report_text():
    now = datetime.now(TZ); since = now - timedelta(days=1)
    new = await period_counts("users", "first_seen", since); refs = await period_counts("referrals", "created", since)
    pc, ps = await pool.fetchrow("SELECT count(*), coalesce(sum(stars),0) FROM purchases WHERE ts >= $1", since)
    sh = {r["kind"]: r["c"] for r in await pool.fetch("SELECT kind, count(*) c FROM shares WHERE ts >= $1 GROUP BY kind", since)}
    a, b2 = sh.get("slide", 0), sh.get("invite", 0)
    total = await pool.fetchval("SELECT count(*) FROM users"); blocked = await pool.fetchval("SELECT count(*) FROM users WHERE blocked")
    return (f"📬 <b>Отчёт за сутки</b> · {now:%d.%m.%Y %H:%M}\n\n🆕 Новых пользователей: <b>{new}</b>\n🛒 Покупок: <b>{pc}</b> · ⭐ Звёзд: <b>{ps}</b>\n"
            f"📤 Пересылок: <b>{a + b2}</b> (слайды: {a}, приглашения: {b2})\n🤝 Пришло по реферальным ссылкам: <b>{refs}</b>\n\n"
            f"👥 Всего пользователей: {total} · 🚫 заблокировали: {blocked}")

async def send_report(targets=None):
    txt = await report_text()
    for a in (targets or ADMINS): await send(a, txt)

async def report_loop():  # раз в минуту проверяет, не пора ли отправить отчёт (если сервер спал — отправит, когда проснётся)
    while True:
        try:
            if await get_setting("rep_on", "1") == "1":
                now = datetime.now(TZ); today = now.date().isoformat(); last = await get_setting("rep_last", "")
                if not last: await set_setting("rep_last", today)   # первый запуск: отчёт придёт завтра
                elif now.hour >= int(await get_setting("rep_hour", "9")) and last != today:
                    await set_setting("rep_last", today); await send_report()
        except Exception as e: print("report error:", repr(e))
        await asyncio.sleep(60)

# ================= Задания =================
async def task_rows():
    return await pool.fetch("""SELECT t.*, (SELECT count(*) FROM events e WHERE e.task_id=t.id AND e.kind='done') done
        FROM tasks t WHERE NOT t.deleted ORDER BY t.sort, t.id""")

async def tk_list(T):
    rows = await task_rows()
    if not rows: await scr(T, "Заданий пока нет.", kb([cb("➕ Добавить задание", "add")], [cb("⬅️ Меню", "adm")])); return
    lines = [f"{i}. {'✅' if r['active'] else '⏸'} <b>{E(r['title'])}</b> — {'заявки' if r['kind'] == 'request' else 'ссылка'}, выполнили {r['done']}" +
             (f" из {r['limit_n']}" if r['limit_n'] else "") for i, r in enumerate(rows, 1)]
    await scr(T, "📋 <b>Задания</b> (в таком порядке их видят пользователи)\n\n" + "\n".join(lines) + "\n\nВыбери задание, чтобы изменить:",
              kb(*[[cb(f"{i}. {r['title'][:32]} {'✅' if r['active'] else '⏸'}", f"t:{r['id']}")] for i, r in enumerate(rows, 1)], [cb("⬅️ Меню", "adm")]))

async def tk_card(T, tid):
    r = next((x for x in await task_rows() if x["id"] == tid), None)
    if not r: await scr(T, "Задание не найдено.", kb([cb("📋 К списку", "tk")])); return
    await scr(T, f"📌 <b>#{r['id']} {E(r['title'])}</b>\nТип: {'заявки' if r['kind'] == 'request' else 'обычная ссылка'} · {'включено ✅' if r['active'] else 'выключено ⏸'}\n"
                 f"Выполнили: <b>{r['done']}</b>" + (f" из <b>{r['limit_n']}</b>" if r['limit_n'] else " (лимита нет)") + f"\nСсылка: {E(r['link'])}",
              kb([cb("⏸ Выключить" if r["active"] else "▶️ Включить", f"tg:{tid}")], [cb("⬆️ Выше", f"tu:{tid}"), cb("⬇️ Ниже", f"td:{tid}")],
                 [cb("✏️ Название", f"te:{tid}"), cb("🎯 Лимит", f"tl:{tid}")], [cb("🗑 Удалить", f"tx:{tid}")], [cb("📋 К списку", "tk")]))

async def move_task(tid, d):
    ids = [r["id"] for r in await pool.fetch("SELECT id FROM tasks WHERE NOT deleted ORDER BY sort, id")]
    if tid not in ids: return
    i = ids.index(tid); j = i + d
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for k, x in enumerate(ids): await pool.execute("UPDATE tasks SET sort=$2 WHERE id=$1", x, k + 1)

def note_(n): return (n + "\n\n") if n else ""

async def refs_view(T, note=""):
    ok = await pool.fetchval("SELECT count(*) FROM referrals WHERE qualified"); pc, ps = await pool.fetchrow("SELECT count(*), coalesce(sum(stars),0) FROM purchases")
    price = await get_price()
    await scr(T, note_(note) + f"👥 <b>Доступ к доп. информации</b>\n\n🆓 Бесплатно: пригласить <b>{await get_need()}</b> (засчитано приглашений: {ok})\n⭐ Платно: <b>{str(price) + ' ⭐' if price else 'выключено'}</b> (покупок: {pc}, на {ps} ⭐)",
              kb([cb("🆓 Бесплатно — число рефералов", "refs_free")], [cb("⭐ Платная — сумма в звёздах", "refs_paid")], [cb("⬅️ Меню", "adm")]))

async def rp_view(T, note=""):
    on = await get_setting("rep_on", "1") == "1"; h = int(await get_setting("rep_hour", "9"))
    await scr(T, note_(note) + f"📬 <b>Ежедневный отчёт</b>\n\nСтатус: {'включён ✅' if on else 'выключен ⏸'}\nВремя: <b>{h:02d}:00</b> (UTC{TZ.utcoffset(None).total_seconds() / 3600:+.0f})\n"
                 "В отчёте: новые пользователи, покупки, звёзды, пересылки.\n\n<i>На бесплатном Render сервер спит без запросов, тогда отчёт придёт, когда он проснётся.</i>",
              kb([cb("🔕 Выключить" if on else "🔔 Включить", "rpt"), cb("⏰ Время", "rph")], [cb("📨 Отправить сейчас", "rps")], [cb("⬅️ Меню", "adm")]))

# ================= Уникальные ссылки =================
LINK_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")

async def ul_menu(T, note=""):
    await scr(T, note_(note) + "🔗 <b>Уникальные ссылки</b>\n\nЗдесь можно создать уникальную ссылку на бота и смотреть по ней статистику: "
                 "сколько людей нажали «Начать», выполнили задания и зашли в мини-апп.",
              kb([cb("📋 Текущие ссылки", "ull")], [cb("➕ Создать ссылку", "ulc")], [cb("⬅️ Меню", "adm")]))

async def ul_list(T):
    rows = await pool.fetch("SELECT id, name FROM links ORDER BY id DESC LIMIT 90")
    if not rows:
        await scr(T, "Ссылок пока нет. Создай первую!", kb([cb("➕ Создать ссылку", "ulc")], [cb("⬅️ Назад", "ul")])); return
    await scr(T, "📋 <b>Вот текущие ссылки</b>\n\nНажми на ссылку, чтобы посмотреть статистику:",
              kb(*[[cb(r["name"], f"lk:{r['id']}")] for r in rows], [cb("➕ Создать ссылку", "ulc")], [cb("⬅️ Назад", "ul")]))

async def ul_card(T, lid):
    r = await pool.fetchrow("SELECT id, name, created FROM links WHERE id=$1", lid)
    if not r: await scr(T, "Ссылка не найдена.", kb([cb("📋 Текущие ссылки", "ull")])); return
    clicks = await pool.fetchval("SELECT count(*) FROM link_clicks WHERE link_id=$1", lid)
    q = lambda extra: pool.fetchval(f"SELECT count(*) FROM link_starts l {extra}", lid)
    uniq = await q("WHERE l.link_id=$1"); new_n = await q("WHERE l.link_id=$1 AND l.is_new")
    done = await q("JOIN users u ON u.user_id=l.user_id WHERE l.link_id=$1 AND u.passed_at IS NOT NULL")
    done_n = await q("JOIN users u ON u.user_id=l.user_id WHERE l.link_id=$1 AND l.is_new AND u.passed_at IS NOT NULL")
    app = await q("JOIN profiles p ON p.user_id=l.user_id WHERE l.link_id=$1")
    app_n = await q("JOIN profiles p ON p.user_id=l.user_id WHERE l.link_id=$1 AND l.is_new")
    await scr(T, f"🔗 <b>{E(r['name'])}</b>\n<code>{E(src_link(r['name']))}</code>\nСоздана: {r['created'].astimezone(TZ):%d.%m.%Y %H:%M}\n\n"
                 f"👥 Нажали старт: <b>{uniq}</b> (новых: <b>{new_n}</b>)\n"
                 f"✅ Выполнили задания (подписка / заявки): <b>{done}</b> (новых: <b>{done_n}</b>)\n"
                 f"📱 Зашли в мини-апп: <b>{app}</b> (новых: <b>{app_n}</b>)\n\n"
                 f"<i>Каждый человек считается один раз, сколько бы раз он ни нажимал старт. «Новых» — те, кого эта ссылка привела в бота впервые (до этого они не заходили). Всего переходов по ссылке, включая повторные: {clicks}.</i>",
              kb([cb("🔄 Обновить", f"lk:{lid}")], [cb("📋 Текущие ссылки", "ull")], [cb("⬅️ Меню", "adm")]))

async def tt_menu(T, note=""):
    await scr(T, note_(note) + "✏️ <b>Тексты</b>\n\nВыбери, что изменить. В текстах сообщений работает форматирование и премиум-эмодзи.",
              kb(*[[cb(v[0], "tt:" + k)] for k, v in TEXTS.items()], [cb("⬅️ Меню", "adm")]))

async def on_message(msg):
    if msg["chat"]["type"] != "private": return
    uid, cid, text = msg["from"]["id"], msg["chat"]["id"], msg.get("text") or ""
    sp = msg.get("successful_payment")
    if sp:  # оплата звёздами прошла
        new = await pool.fetchval("INSERT INTO purchases(charge_id,user_id,stars) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING 1", sp["telegram_payment_charge_id"], uid, sp["total_amount"])
        if new:
            f = msg["from"]; name = ((f.get("first_name") or "") + " " + (f.get("last_name") or "")).strip() or "Без имени"
            note = (f"🛒 <b>Новая покупка доп. информации</b>\n\nСумма: <b>{sp['total_amount']} ⭐</b>\n"
                    f"Ник: <a href=\"tg://user?id={uid}\">{E(name)}</a>\nUsername: {'@' + E(f['username']) if f.get('username') else '—'}\nID: <code>{uid}</code>")
            for a in ADMINS: await send(a, note)
            await send(cid, "✅ Спасибо за оплату! Дополнительная информация открыта — вернись в приложение.")
        return
    if text.startswith("/start"):
        new = await pool.fetchval("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO UPDATE SET blocked=FALSE RETURNING (xmax = 0)", uid)  # считаем уникально
        payload = text.split(maxsplit=1)[1] if " " in text else ""
        if new and payload.startswith("ref_") and payload[4:].isdigit() and int(payload[4:]) != uid:  # реферал только за нового пользователя
            await pool.execute("INSERT INTO referrals(invitee,inviter) VALUES($1,$2) ON CONFLICT DO NOTHING", uid, int(payload[4:]))
        if payload.startswith("src_"):  # переход по уникальной ссылке -> считаем уникального человека
            lid = await pool.fetchval("SELECT id FROM links WHERE lower(name)=lower($1)", payload[4:])
            if lid:
                await pool.execute("INSERT INTO link_clicks(link_id,user_id) VALUES($1,$2)", lid, uid)   # все нажатия «Начать»
                await pool.execute("INSERT INTO link_starts(link_id,user_id,is_new) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", lid, uid, bool(new))   # уникальные люди; is_new — раньше в бота не заходил
        await gate(uid, cid)
        if uid in ADMINS: await panel({"cid": cid, "mid": None})
    elif uid in ADMINS and text == "/texts":
        await delete(cid, msg["message_id"]); await tt_menu({"cid": cid, "mid": None, "photo": False})
    elif uid in ADMINS and text == "/admin":
        STATE.pop(uid, None); await delete(cid, msg["message_id"]); await panel({"cid": cid, "mid": None})
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "lnkname":
        await delete(cid, msg["message_id"]); name = text.strip()
        if not LINK_RE.fullmatch(name): await bad(uid, cid, "Название — только английские буквы, цифры, _ или - (до 40 символов), без пробелов."); return
        if await pool.fetchval("SELECT 1 FROM links WHERE lower(name)=lower($1)", name): await bad(uid, cid, "Ссылка с таким названием уже есть. Придумай другое."); return
        lid = await pool.fetchval("INSERT INTO links(name) VALUES($1) RETURNING id", name)
        T = T_for(uid, cid); STATE.pop(uid)
        await scr(T, f"✅ Ссылка создана: <b>{E(name)}</b>\n\n<code>{E(src_link(name))}</code>\n\nОна появилась в «Текущих ссылках», там смотри статистику.",
                  kb([cb("📊 Статистика ссылки", f"lk:{lid}")], [cb("📋 Текущие ссылки", "ull")], [cb("⬅️ Меню", "adm")]))
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "tedit":
        await delete(cid, msg["message_id"])
        if not text.strip() or len(text) > 60: await bad(uid, cid, "Название — до 60 символов."); return
        tid = STATE[uid]["tid"]; T = T_for(uid, cid); STATE.pop(uid)
        await pool.execute("UPDATE tasks SET title=$2 WHERE id=$1", tid, text.strip()); await tk_card(T, tid)
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "tlim":
        await delete(cid, msg["message_id"])
        if not text.strip().isdigit() or int(text) > 1000000: await bad(uid, cid, "Нужно число (0 — без лимита)."); return
        tid = STATE[uid]["tid"]; T = T_for(uid, cid); STATE.pop(uid)
        await pool.execute("UPDATE tasks SET limit_n=$2 WHERE id=$1", tid, int(text))
        await pool.execute("DELETE FROM settings WHERE key=$1", f"limit_{tid}"); await tk_card(T, tid)
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "rphour":
        await delete(cid, msg["message_id"])
        if not (text.strip().isdigit() and 0 <= int(text) <= 23): await bad(uid, cid, "Нужен час от 0 до 23."); return
        T = T_for(uid, cid); STATE.pop(uid); await set_setting("rep_hour", int(text)); await rp_view(T, f"✅ Отчёт будет приходить в {int(text):02d}:00")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "txt":
        key = STATE[uid]["key"]; rich = TEXTS[key][2]; await delete(cid, msg["message_id"])
        if not text.strip(): await bad(uid, cid, "Пришли текст сообщением (не фото и не файл)."); return
        if rich:
            ents = [e for e in (msg.get("entities") or []) if e.get("type") in RICH_OK]
            await set_setting("txt_" + key, json.dumps({"t": text, "e": ents}, ensure_ascii=False))
            ce = any(e["type"] == "custom_emoji" for e in ents)
        else:
            if len(text) > 64: await bad(uid, cid, "Текст кнопки — до 64 символов."); return
            await set_setting("txt_" + key, json.dumps({"t": text.strip()}, ensure_ascii=False)); ents = None; ce = False
        T = T_for(uid, cid); STATE.pop(uid); kbd = kb([cb("✏️ Тексты", "tt")], [cb("⬅️ Меню", "adm")])
        if rich:
            full, e2 = compose("✅ Сохранено. Вот как это выглядит:\n\n", text, ents,
                               "\n\nℹ️ Премиум-эмодзи доходят до людей, только если у владельца бота есть Telegram Premium (или у бота есть username с Fragment). Если не доходят — они показываются обычными." if ce else "")
            await scr(T, full, kbd, e2)
        else: await scr(T, "✅ Сохранено. Кнопка: <b>" + E(text.strip()) + "</b>", kbd)
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "refprice":
        await delete(cid, msg["message_id"])
        if not (text.strip().isdigit() and int(text) <= 10000): await bad(uid, cid, "Нужно число от 0 до 10000."); return
        T = T_for(uid, cid); STATE.pop(uid); n = int(text); await set_setting("ref_price", n)
        await refs_view(T, f"✅ Цена доп. информации: {n} ⭐" if n else "✅ Платный вариант выключен")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "refneed":
        await delete(cid, msg["message_id"])
        if not (text.strip().isdigit() and int(text) <= 1000): await bad(uid, cid, "Нужно число от 0 до 1000."); return
        T = T_for(uid, cid); STATE.pop(uid); n = int(text); await set_setting("ref_need", n)
        await refs_view(T, f"✅ Теперь нужно пригласить: {n}" if n else "✅ Доп. информация теперь доступна без приглашений")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_msg" and not text.startswith("/"):
        if msg.get("media_group_id"):
            await delete(cid, msg["message_id"])
            if not STATE[uid].get("warned"): STATE[uid]["warned"] = 1; await bad(uid, cid, "Альбомы не поддерживаются. Пришли одно сообщение (фото, GIF, видео или текст).")
            return
        pv = await call("copyMessage", chat_id=cid, from_chat_id=cid, message_id=msg["message_id"])   # предпросмотр
        if not pv: await delete(cid, msg["message_id"]); await bad(uid, cid, "Не получилось скопировать это сообщение. Пришли другое."); return
        await delete(cid, STATE[uid].get("pm"))
        STATE[uid] = {"step": "bc_menu", "mid": msg["message_id"], "pv": pv["message_id"], "btns": []}
        await bc_show({"cid": cid, "mid": None, "photo": False}, uid)
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_url":
        await delete(cid, msg["message_id"]); st = STATE[uid]; url = text.strip()
        if url.startswith(("t.me/", "telegram.me/")): url = "https://" + url
        if not re.fullmatch(r"(https?|tg)://\S+", url): await bad(uid, cid, "Это не ссылка. Пришли ссылку вида https://… или t.me/…"); return
        st["url"] = url; await bc_ask(T_for(uid, cid), st, "bc_name", "Теперь пришли название кнопки (до 60 символов).")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_name":
        await delete(cid, msg["message_id"]); st = STATE[uid]; st["name"] = text.strip()[:60]
        await bc_ask(T_for(uid, cid), st, "bc_color", "Выбери цвет кнопки:", kb([cb("Обычный", "col:"), cb("🔴 Красный", "col:danger"), cb("🟢 Зелёный", "col:success")], [cb("❌ Отмена", "cancel")]))
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "link":
        await delete(cid, msg["message_id"])
        if "t.me/+" in text or "joinchat" in text:
            await bad(uid, cid, "По приватной ссылке канал не определить. Перешли мне любое сообщение из него или пришли ID (вида -100123…)."); return
        ch = await resolve_chat(msg)
        if not ch or ch.get("type") not in ("channel", "supergroup", "group"):
            await bad(uid, cid, "Не нашёл такой канал или чат. Пришли ссылку https://t.me/название, @название или перешли сообщение оттуда."); return
        me = await call("getChatMember", chat_id=ch["id"], user_id=BOT_ID)
        if not me or me["status"] not in ("administrator", "creator"):
            await bad(uid, cid, f"Сначала сделай меня администратором в «{ch.get('title','')}» (с правом приглашать пользователей) и пришли ссылку ещё раз."); return
        T = T_for(uid, cid)
        await scr(T, f"Нашёл: <b>{E(ch.get('title',''))}</b>\nКакой тип задания?",
                  kb([cb("🔗 Обычная ссылка", "kind:link")], [cb("✉️ Заявки", "kind:req")], [cb("❌ Отмена", "cancel")]))
        STATE[uid] = {"step": "kind", "pm": T["mid"], "chat": {"id": ch["id"], "title": ch.get("title", ""), "username": ch.get("username")}}

async def on_cb(q):
    uid, data, cid, mid = q["from"]["id"], q["data"], q["message"]["chat"]["id"], q["message"]["message_id"]
    if data == "chk":
        await gate(uid, cid, mid, q["id"]); return
    await call("answerCallbackQuery", callback_query_id=q["id"])
    if uid not in ADMINS: return
    st = STATE.get(uid, {}); T = tgt(q)
    if data == "cancel":
        STATE.pop(uid, None); await bc_cleanup(cid, st); await panel(T, "Отменено.")
    elif data == "adm": STATE.pop(uid, None); await panel(T)
    elif data == "bc": await ask(T, uid, "bc_msg", "📢 Пришли сообщение для рассылки: текст, фото, GIF или видео, можно с форматированием (жирный, курсив, ссылки и т.д.). Одним сообщением.")
    elif data == "bcadd" and st.get("step") == "bc_menu": await bc_ask(T, st, "bc_url", "Пришли ссылку, куда будет вести кнопка.")
    elif data.startswith("col:") and st.get("step") == "bc_color":
        b = {"text": st["name"], "url": st["url"], "style": data[4:], "new_row": True}
        if st["btns"]:  # вторая и следующие кнопки: спрашиваем, куда поставить
            st["pending"] = b
            await bc_ask(T, st, "bc_place", "Куда поставить эту кнопку?", kb([cb("⬇️ Новым рядом", "pl:row")], [cb("➡️ Рядом с предыдущей", "pl:side")], [cb("❌ Отмена", "cancel")]))
        else: st["btns"].append(b); await bc_show(T, uid)
    elif data in ("pl:row", "pl:side") and st.get("step") == "bc_place":
        b = st.pop("pending"); b["new_row"] = data == "pl:row"; st["btns"].append(b); await bc_show(T, uid)
    elif data == "bcstop": BC["stop"] = True
    elif data == "bcgo" and st.get("step") == "bc_menu":
        if BC["run"]: return
        mk = bc_markup(st["btns"]); src, pv = st["mid"], st.get("pv"); STATE.pop(uid, None)
        await scr(T, "📤 Запускаю рассылку…", kb([cb("⏹ Остановить", "bcstop")]))
        t = asyncio.create_task(run_bc(cid, src, mk, T["mid"], pv)); BG.add(t); t.add_done_callback(BG.discard)
    elif data == "refs": await refs_view(T)
    elif data == "refs_free": await ask(T, uid, "refneed", f"Сейчас нужно пригласить: <b>{await get_need()}</b>\nПришли новое число (0 — доступ без приглашений).")
    elif data == "refs_paid": await ask(T, uid, "refprice", f"Сейчас цена: <b>{await get_price() or 'выключено'}</b>\nПришли сумму в звёздах (от 1 до 10000) или 0, чтобы выключить платный вариант.")
    elif data == "st": await scr(T, await stats_text(), kb([cb("🧾 Последние покупки", "sl")], [cb("⬅️ Меню", "adm")]))
    elif data == "sl": await scr(T, await last_purchases_text(), kb([cb("📊 Статистика", "st")], [cb("⬅️ Меню", "adm")]))
    elif data == "ul": STATE.pop(uid, None); await ul_menu(T)
    elif data == "ull": STATE.pop(uid, None); await ul_list(T)
    elif data == "ulc": await ask(T, uid, "lnkname", "➕ Пришли название для новой ссылки на английском (буквы, цифры, _ или -, без пробелов), например: <b>instagram_1</b>")
    elif re.fullmatch(r"lk:\d+", data): await ul_card(T, int(data[3:]))
    elif data == "tk": await tk_list(T)
    elif re.fullmatch(r"t:\d+", data): await tk_card(T, int(data[2:]))
    elif re.fullmatch(r"tg:\d+", data):
        await pool.execute("UPDATE tasks SET active = NOT active WHERE id=$1", int(data[3:])); await tk_card(T, int(data[3:]))
    elif re.fullmatch(r"t[ud]:\d+", data):
        await move_task(int(data[3:]), -1 if data[1] == "u" else 1); await tk_list(T)
    elif re.fullmatch(r"te:\d+", data): await ask(T, uid, "tedit", "Пришли новое название задания (его видят пользователи на кнопке).", tid=int(data[3:]))
    elif re.fullmatch(r"tl:\d+", data):
        await ask(T, uid, "tlim", "🎯 Пришли лимит выполнений, например <b>500</b>: когда столько человек выполнят задание, оно перестанет требоваться. 0 — без лимита.", tid=int(data[3:]))
    elif re.fullmatch(r"tx:\d+", data):
        await scr(T, "Точно удалить задание? Статистика по нему тоже скроется.", kb([cb("Да, удалить", "ty:" + data[3:])], [cb("Отмена", "t:" + data[3:])]))
    elif re.fullmatch(r"ty:\d+", data):
        await pool.execute("UPDATE tasks SET deleted=TRUE, active=FALSE WHERE id=$1", int(data[3:])); await tk_list(T)
    elif data == "tt": await tt_menu(T)
    elif data.startswith("tt:") and data[3:] in TEXTS:
        key = data[3:]; t, e = await get_text(key); rich = TEXTS[key][2]
        mk = kb([cb("↩️ Сбросить по умолчанию", "ttr:" + key)], [cb("❌ Отмена", "cancel")])
        foot = "Пришли новый " + ("текст одним сообщением — можно с жирным шрифтом, ссылками и премиум-эмодзи." if rich else "текст кнопки (до 64 символов, без форматирования).")
        if rich:
            plain_t = html.unescape(t) if not e else t   # без форматирования текст хранится экранированным
            full, e2 = compose(f"✏️ {TEXTS[key][0]}\n\nСейчас:\n", plain_t, e, "\n\n— — —\n" + foot); await scr(T, full, mk, e2)
        else: await scr(T, f"✏️ <b>{TEXTS[key][0]}</b>\nСейчас: <b>{E(t)}</b>\n\n{foot}", mk)
        STATE[uid] = {"step": "txt", "key": key, "pm": T["mid"], "prompt": foot, "pmk": mk}
    elif data.startswith("ttr:") and data[4:] in TEXTS:
        await pool.execute("DELETE FROM settings WHERE key=$1", "txt_" + data[4:]); STATE.pop(uid, None); await tt_menu(T, "↩️ Возвращён текст по умолчанию.")
    elif data == "rp": await rp_view(T)
    elif data == "rpt": await set_setting("rep_on", "0" if await get_setting("rep_on", "1") == "1" else "1"); await rp_view(T)
    elif data == "rph": await ask(T, uid, "rphour", "Пришли час отправки отчёта (0–23).")
    elif data == "rps": await send_report([cid])
    elif data == "add": await ask(T, uid, "link", "Пришли ссылку на канал или чат (https://t.me/название или @название). Для приватного — перешли любое сообщение из него. Я должен быть там администратором.")
    elif data.startswith("kind:") and st.get("step") == "kind":
        c, req = st["chat"], data == "kind:req"
        inv = None if (not req and c["username"]) else await call("createChatInviteLink", chat_id=c["id"], name="mini-app", creates_join_request=req)
        link = ("https://t.me/" + c["username"]) if (not req and c["username"]) else (inv["invite_link"] if inv else None)
        if not link:
            await scr(T, f"⚠️ Не получилось создать ссылку: {E(ERR[0])}\nПроверь, что у меня есть право приглашать пользователей.", kb([cb("⬅️ Меню", "adm")])); STATE.pop(uid, None); return
        tid = await pool.fetchval("INSERT INTO tasks(chat_id,title,kind,link,sort) VALUES($1,$2,$3,$4,(SELECT coalesce(max(sort),0)+1 FROM tasks)) RETURNING id", c["id"], c["title"], "request" if req else "link", link)
        STATE.pop(uid, None)
        await scr(T, f"✅ Задание #{tid} добавлено: <b>{E(c['title'])}</b> ({'заявки' if req else 'обычная ссылка'})", kb([cb("📋 Задания", "tk")], [cb("⬅️ Меню", "adm")]))
    elif data == "ls":
        rows = await pool.fetch("""SELECT t.id,t.title,t.kind,
          count(*) FILTER (WHERE e.kind='join') joins, count(*) FILTER (WHERE e.kind='request') reqs,
          count(*) FILTER (WHERE e.kind='done') done
          FROM tasks t LEFT JOIN events e ON e.task_id=t.id WHERE NOT t.deleted GROUP BY t.id ORDER BY t.id""")
        if not rows: await scr(T, "Заданий пока нет.", kb([cb("⬅️ Меню", "adm")])); return
        txt = "\n\n".join(f"<b>#{r['id']} {E(r['title'])}</b> ({'заявки' if r['kind']=='request' else 'ссылка'})\n" +
            (f"✉️ Новых заявок: {r['reqs']}" if r['kind'] == 'request' else f"👥 Уникальных заходов: {r['joins']}\n✅ Выполнили: {r['done']}") for r in rows)
        await scr(T, "🔗 Статистика ссылок\n\n" + txt, kb([cb("📋 Задания", "tk")], [cb("⬅️ Меню", "adm")]))

async def on_join(r):  # человек подал заявку в канал/чат, где у нас есть задание типа «заявки»
    await pool.execute("""INSERT INTO events(task_id,user_id,kind) SELECT id,$2,'request' FROM tasks
        WHERE chat_id=$1 AND kind='request' AND NOT deleted ON CONFLICT DO NOTHING""", r["chat"]["id"], r["from"]["id"])

async def on_member(m):  # кто-то вступил в канал/чат, где есть задание
    new, old = m["new_chat_member"], m["old_chat_member"]
    ok = lambda x: x["status"] in ("member", "administrator", "creator") or (x["status"] == "restricted" and x.get("is_member"))
    if ok(new) and not ok(old):
        await pool.execute("""INSERT INTO events(task_id,user_id,kind) SELECT id,$2,'join' FROM tasks
            WHERE chat_id=$1 AND NOT deleted ON CONFLICT DO NOTHING""", m["chat"]["id"], new["user"]["id"])

async def on_block(m):  # человек заблокировал или разблокировал бота
    if m["chat"]["type"] == "private":
        await pool.execute("UPDATE users SET blocked=$2 WHERE user_id=$1", m["chat"]["id"], m["new_chat_member"]["status"] == "kicked")

async def handle(u):
    if "pre_checkout_query" in u: await call("answerPreCheckoutQuery", pre_checkout_query_id=u["pre_checkout_query"]["id"], ok=True); return
    if "message" in u: await on_message(u["message"])
    elif "callback_query" in u: await on_cb(u["callback_query"])
    elif "my_chat_member" in u: await on_block(u["my_chat_member"])
    elif "chat_member" in u: await on_member(u["chat_member"])
    elif "chat_join_request" in u: await on_join(u["chat_join_request"])

async def more_data(uid):
    need = await get_need(); price = await get_price()
    cnt = await pool.fetchval("SELECT count(*) FROM referrals WHERE inviter=$1 AND qualified", uid)
    paid = bool(await pool.fetchval("SELECT 1 FROM purchases WHERE user_id=$1 LIMIT 1", uid))
    me = await pool.fetchrow("""SELECT p.points, (SELECT rn FROM (SELECT p2.user_id, row_number() OVER (ORDER BY p2.points DESC, p2.created, p2.user_id) rn
        FROM profiles p2 JOIN users u2 ON u2.user_id=p2.user_id) t WHERE t.user_id=$1) rank FROM profiles p WHERE p.user_id=$1""", uid)
    return {"need": need, "count": cnt, "price": price, "paid": paid, "unlocked": paid or cnt >= need, "link": ref_link(uid),
            "rank": me["rank"] if me else None, "points": me["points"] if me else 0}

def register(app):
    app.on_event("startup")(startup)
    @app.get("/api/more")
    async def more(authorization: str = Header()):
        return await more_data(auth_user(authorization)["id"])
    @app.get("/api/top")
    async def top(authorization: str = Header()):
        uid = auth_user(authorization)["id"]; m = await more_data(uid)
        if not m["unlocked"]: raise HTTPException(403, "нужно пригласить друзей или оплатить")
        rows = await pool.fetch("""SELECT p.name, p.photo, p.points, row_number() OVER (ORDER BY p.points DESC, p.created, p.user_id) rank
            FROM profiles p JOIN users u ON u.user_id=p.user_id ORDER BY p.points DESC, p.created, p.user_id LIMIT 100""")  # только те, кто запускал бота
        return {"top": [dict(r) for r in rows], "me": {"rank": m["rank"], "points": m["points"]}}
    @app.post("/api/pay")
    async def pay(authorization: str = Header()):
        uid = auth_user(authorization)["id"]; price = await get_price()
        if price <= 0: raise HTTPException(400, "платный вариант выключен")
        link = await call("createInvoiceLink", title="Дополнительная информация", description="Таблица лидеров и другие данные", payload=f"more:{uid}",
                          provider_token="", currency="XTR", prices=[{"label": "Доп. информация", "amount": price}])
        if not link: raise HTTPException(502, "Telegram не создал счёт")
        return {"url": link}
    @app.get("/health")
    async def health(): return {"ok": True}   # сюда можно слать пинг (UptimeRobot), чтобы бесплатный сервер не засыпал
    @app.post("/api/invite")
    async def invite(authorization: str = Header()):
        uid = auth_user(authorization)["id"]
        t, e = await get_text("invite_text"); lbl, _ = await get_text("invite_btn")
        def make(ents):
            m = {"message_text": t, "link_preview_options": {"is_disabled": True}}
            if ents: m["entities"] = ents
            return {"type": "article", "id": f"inv{uid}", "title": "Приглашение", "input_message_content": m,
                    "reply_markup": {"inline_keyboard": [[{"text": lbl, "url": ref_link(uid)}]]}}
        r = await call("savePreparedInlineMessage", user_id=uid, result=make(e), allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
        if not r and e and len(no_ce(e)) != len(e):  # премиум-эмодзи в приглашении не приняты -> без них
            r = await call("savePreparedInlineMessage", user_id=uid, result=make(no_ce(e)), allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
        if not r: raise HTTPException(502, "Telegram не принял сообщение")
        await log_share(uid, "invite")
        return {"id": r["id"]}
    @app.post("/webhook")
    async def webhook(req: Request):
        if req.headers.get("x-telegram-bot-api-secret-token") != SECRET: raise HTTPException(403)
        try: await handle(await req.json())
        except Exception as e: print("handler error:", repr(e))
        return {"ok": True}
    @app.get("/go/{tid}")
    async def go(tid: int, u: int = 0):  # считаем переход и отправляем на ссылку
        row = await pool.fetchrow("SELECT link FROM tasks WHERE id=$1", tid)
        if not row: raise HTTPException(404)
        if u: await pool.execute("INSERT INTO events(task_id,user_id,kind) VALUES($1,$2,'click') ON CONFLICT DO NOTHING", tid, u)
        return RedirectResponse(row["link"])
