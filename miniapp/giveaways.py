# -*- coding: utf-8 -*-
"""Розыгрыши NFT-подарков: таблицы в базе, API для мини-аппа, диалог создания в админке бота, выбор победителей.

Подключение: в server.py уже добавлен вызов giveaways.register(app, ...).
В боте нужны две вещи (см. конец файла): кнопка PANEL_BUTTON в админ-панели и вызов handle_update(update).
"""
import os, re, sys, json, time, asyncio, secrets, html
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import Header, HTTPException, Request
from fastapi.responses import Response

MSK = timezone(timedelta(hours=3))
NFT_RE = re.compile(r"^(?:https?://)?t\.me/nft/([A-Za-z0-9_]+-\d+)/?$")
DT_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2})[:.](\d{2})\s*$")
CFG: dict = {}
STATE: dict = {}          # состояние диалога создания розыгрыша: id админа -> {"step", "d"}
IMG_CACHE: dict = {}
PANEL_BUTTON = {"text": "🎁 Розыгрыши", "callback_data": "gw:menu"}   # кнопка в самый низ админ-панели
WIN_TEXT = "🎉 Поздравляем! Вы выиграли в розыгрыше.\nВаш приз: {prize}\nСкоро с вами свяжутся для выдачи."

SCHEMA = """
CREATE TABLE IF NOT EXISTS gw(
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, prizes JSONB NOT NULL,
  start_at TIMESTAMPTZ NOT NULL, end_type TEXT NOT NULL, end_at TIMESTAMPTZ, end_tickets INT,
  status TEXT NOT NULL DEFAULT 'active', created_by BIGINT, created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS gw_users(
  uid BIGINT PRIMARY KEY, name TEXT, username TEXT, first_seen TIMESTAMPTZ DEFAULT now(), ref_gw INT, ref_uid BIGINT);
CREATE TABLE IF NOT EXISTS gw_tickets(
  id SERIAL PRIMARY KEY, gw_id INT NOT NULL, uid BIGINT NOT NULL, kind TEXT NOT NULL, friend_uid BIGINT, created_at TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS gw_t_join ON gw_tickets(gw_id, uid) WHERE kind='join';
CREATE UNIQUE INDEX IF NOT EXISTS gw_t_ref ON gw_tickets(gw_id, friend_uid) WHERE kind='ref';
CREATE TABLE IF NOT EXISTS gw_winners(
  gw_id INT NOT NULL, prize_idx INT NOT NULL, uid BIGINT NOT NULL, ticket_id INT, PRIMARY KEY(gw_id, prize_idx));
"""

# ───────────────────────── общее ─────────────────────────
def pool():
    b = sys.modules.get("bot")
    return getattr(b, "pool", None)

def is_admin(uid: int) -> bool:
    env = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
    if uid in env: return True
    b = sys.modules.get("bot")
    for n in ("ADMIN_IDS", "ADMINS"):
        v = getattr(b, n, None)
        if v:
            try: return uid in {int(x) for x in v}
            except Exception: pass
    return False

async def tg(method, **p):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"https://api.telegram.org/bot{CFG['token']}/{method}", json=p)
        j = r.json()
    if not j.get("ok"): print("GW TG ERROR", method, j); return None
    return j["result"]

def prize_from_url(url: str):
    m = NFT_RE.match(url.strip())
    if not m: return None
    slug = m.group(1); base, num = slug.rsplit("-", 1)
    return {"url": f"https://t.me/nft/{slug}", "slug": slug, "name": re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", base).strip() + f" #{num}"}

def load_prizes(v): return json.loads(v) if isinstance(v, str) else v
def fmt(dt): return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")

def parse_dt(s: str):
    m = DT_RE.match(s or "")
    if not m: return None
    d, mo, y, h, mi = map(int, m.groups())
    try: return datetime(y, mo, d, h, mi, tzinfo=MSK)
    except ValueError: return None

async def counts(c, gid):
    r = await c.fetchrow("SELECT count(*) FILTER (WHERE kind='join') AS parts, count(*) AS tickets FROM gw_tickets WHERE gw_id=$1", gid)
    return int(r["parts"]), int(r["tickets"])

def uname(u): return (u["name"] or "Без имени") if u else "Без имени"
def ulink(uid, name): return f'<a href="tg://user?id={uid}">{html.escape(name or "Без имени")}</a>'

# ───────────────────────── завершение и выбор победителей ─────────────────────────
async def finish(gid: int):
    p = pool()
    async with p.acquire() as c:
        async with c.transaction():
            g = await c.fetchrow("UPDATE gw SET status='finished', finished_at=now() WHERE id=$1 AND status='active' RETURNING *", gid)
            if not g: return
            prizes = load_prizes(g["prizes"])
            tickets = await c.fetch("SELECT id, uid FROM gw_tickets WHERE gw_id=$1", gid)
            rnd = secrets.SystemRandom(); tickets = list(tickets); rnd.shuffle(tickets)   # случайный билет = случайный порядок
            used, win = set(), []
            for t in tickets:
                if len(win) >= len(prizes): break
                if t["uid"] in used: continue      # один человек — один приз, пока есть другие участники
                used.add(t["uid"]); win.append(t)
            for i, t in enumerate(win):
                await c.execute("INSERT INTO gw_winners(gw_id, prize_idx, uid, ticket_id) VALUES($1,$2,$3,$4)", gid, i, t["uid"], t["id"])
    await notify_finish(gid)

async def winners_of(c, gid):
    return await c.fetch("""SELECT w.prize_idx, w.uid, w.ticket_id, u.name, u.username FROM gw_winners w
                            LEFT JOIN gw_users u ON u.uid=w.uid WHERE w.gw_id=$1 ORDER BY w.prize_idx""", gid)

async def notify_finish(gid: int):
    p = pool()
    async with p.acquire() as c:
        g = await c.fetchrow("SELECT * FROM gw WHERE id=$1", gid)
        ws = await winners_of(c, gid)
        parts, tickets = await counts(c, gid)
    prizes = load_prizes(g["prizes"])
    for w in ws:
        pr = prizes[w["prize_idx"]]
        await tg("sendMessage", chat_id=w["uid"], parse_mode="HTML", text=WIN_TEXT.format(prize=f'<a href="{pr["url"]}">{html.escape(pr["name"])}</a>'))
    if g["created_by"]:
        await tg("sendMessage", chat_id=g["created_by"], parse_mode="HTML", disable_web_page_preview=True,
                 text="🏁 Розыгрыш завершён\n\n" + stats_text(g, parts, tickets, ws))

async def scheduler():
    while True:
        try:
            rows = await pool().fetch("""SELECT id FROM gw WHERE status='active' AND (
                (end_type='time' AND end_at<=now()) OR
                (end_type='tickets' AND end_tickets <= (SELECT count(*) FROM gw_tickets t WHERE t.gw_id=gw.id)))""")
            for r in rows: await finish(r["id"])
        except Exception as e: print("GW SCHEDULER ERROR", repr(e))
        await asyncio.sleep(15)

# ───────────────────────── статистика для админа ─────────────────────────
def stats_text(g, parts, tickets, ws) -> str:
    prizes = load_prizes(g["prizes"])
    st = "✅ завершён" if g["status"] == "finished" else ("⏳ ждёт старта" if g["start_at"] > datetime.now(timezone.utc) else "🟢 идёт")
    t = [f"🎁 <b>{html.escape(g['name'])}</b>", f"Статус: {st}", f"Старт: {fmt(g['start_at'])} МСК"]
    t.append(f"Конец: {fmt(g['end_at'])} МСК" if g["end_type"] == "time" else f"Конец: по билетам ({g['end_tickets']})")
    t.append(f"Призов: {len(prizes)} · Участников: {parts} · Билетов: {tickets}")
    if g["status"] == "finished":
        t.append("\n<b>Победители:</b>")
        if not ws: t.append("— никто не участвовал")
        for w in ws:
            pr = prizes[w["prize_idx"]]
            t.append(f"{w['prize_idx'] + 1}. {ulink(w['uid'], w['name'])}" + (f" (@{html.escape(w['username'])})" if w["username"] else "")
                     + f" — <a href=\"{pr['url']}\">{html.escape(pr['name'])}</a> · билет №{w['ticket_id']}")
        if len(ws) < len(prizes): t.append(f"\nПризов без победителя: {len(prizes) - len(ws)} (участников меньше, чем призов)")
    return "\n".join(t)

# ───────────────────────── диалог в админке ─────────────────────────
def kb(rows): return {"inline_keyboard": rows}
def B(text, data): return {"text": text, "callback_data": data}

async def send(chat, text, rows=None):
    p = dict(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
    if rows: p["reply_markup"] = kb(rows)
    return await tg("sendMessage", **p)

async def menu(chat):
    rows = [[B("➕ Создать розыгрыш", "gw:new")], [B("📜 Прошлые розыгрыши", "gw:hist:0")]]
    if os.environ.get("GW_BACK_CB"): rows.append([B("⬅️ Назад", os.environ["GW_BACK_CB"])])
    await send(chat, "🎁 <b>Розыгрыши</b>", rows)

async def on_cb(uid, chat, data):
    st = STATE.get(uid); a = data.split(":")
    if a[1] == "menu": STATE.pop(uid, None); await menu(chat)
    elif a[1] == "new":
        STATE[uid] = {"step": "name", "d": {"prizes": []}}
        await send(chat, "Придумайте название для розыгрыша\n<i>(видите только вы)</i>\n\nОтмена: /cancel")
    elif a[1] == "more" and st:
        st["step"] = "prize"; await send(chat, "Пришлите ссылку на следующий подарок")
    elif a[1] == "done" and st and st["d"]["prizes"]:
        st["step"] = "start"
        await send(chat, "Во сколько <b>по московскому времени</b> розыгрыш появится в боте?\nНапример: <code>11.12.2026 15:00</code>")
    elif a[1] == "t" and st and st["step"] == "endtype":
        st["d"]["end_type"] = a[2]
        if a[2] == "time":
            st["step"] = "end_time"; await send(chat, "Когда завершить розыгрыш (МСК)?\nНапример: <code>15.12.2026 18:00</code>")
        else:
            st["step"] = "end_tickets"; await send(chat, "Сколько всего билетов нужно набрать, чтобы розыгрыш завершился? Пришлите число")
    elif a[1] == "ok" and st and st["step"] == "confirm": await create(uid, chat)
    elif a[1] == "cancel": STATE.pop(uid, None); await send(chat, "Создание отменено"); await menu(chat)
    elif a[1] == "hist":
        page = int(a[2]); rows = await pool().fetch("SELECT id, name, status FROM gw ORDER BY id DESC LIMIT 11 OFFSET $1", page * 10)
        if not rows and page == 0: await send(chat, "Розыгрышей пока нет", [[B("⬅️ Назад", "gw:menu")]]); return
        btn = [[B(("✅ " if r["status"] == "finished" else "⏳ ") + r["name"][:50], f"gw:v:{r['id']}")] for r in rows[:10]]
        nav = ([B("◀️", f"gw:hist:{page - 1}")] if page else []) + ([B("▶️", f"gw:hist:{page + 1}")] if len(rows) > 10 else [])
        if nav: btn.append(nav)
        btn.append([B("⬅️ Назад", "gw:menu")]); await send(chat, "📜 <b>Прошлые розыгрыши</b>", btn)
    elif a[1] == "v":
        gid = int(a[2])
        async with pool().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1", gid)
            if not g: await send(chat, "Розыгрыш не найден"); return
            parts, tickets = await counts(c, gid); ws = await winners_of(c, gid)
        await send(chat, stats_text(g, parts, tickets, ws), [[B("⬅️ К списку", "gw:hist:0")]])

async def on_text(uid, chat, text):
    st = STATE[uid]; step = st["step"]; d = st["d"]; text = text.strip()
    if step == "name":
        d["name"] = text[:100]; st["step"] = "prize"
        await send(chat, "Пришлите ссылку на подарок\nНапример: <code>https://t.me/nft/PlushPepe-123</code>")
    elif step == "prize":
        pr = prize_from_url(text)
        if not pr: await send(chat, "Это не похоже на ссылку на NFT-подарок. Нужна вида <code>https://t.me/nft/PlushPepe-123</code>"); return
        if any(x["slug"].lower() == pr["slug"].lower() for x in d["prizes"]): await send(chat, "Этот подарок уже добавлен"); return
        d["prizes"].append(pr); st["step"] = "choose"
        await send(chat, f"Добавлено: <b>{html.escape(pr['name'])}</b>\nПодарков в розыгрыше: {len(d['prizes'])}\n\nДостаточно подарков?",
                   [[B("✅ Да, идём дальше", "gw:done")], [B("➕ Добавить ещё", "gw:more")]])
    elif step == "start":
        dt = parse_dt(text)
        if not dt: await send(chat, "Не понял дату. Формат: <code>11.12.2026 15:00</code>"); return
        if dt < datetime.now(MSK) - timedelta(minutes=1): await send(chat, "Это время уже прошло, укажите будущее"); return
        d["start_at"] = dt; st["step"] = "endtype"
        await send(chat, "Как завершится розыгрыш?", [[B("⏰ По времени", "gw:t:time")], [B("🎟 По количеству билетов", "gw:t:tickets")]])
    elif step == "end_time":
        dt = parse_dt(text)
        if not dt: await send(chat, "Не понял дату. Формат: <code>15.12.2026 18:00</code>"); return
        if dt <= d["start_at"]: await send(chat, "Конец должен быть позже старта"); return
        d["end_at"] = dt; await confirm(chat, st)
    elif step == "end_tickets":
        if not text.isdigit() or int(text) < 1: await send(chat, "Пришлите число больше нуля"); return
        d["end_tickets"] = int(text); await confirm(chat, st)
    else: await send(chat, "Выберите вариант кнопкой выше или /cancel")

async def confirm(chat, st):
    d = st["d"]; st["step"] = "confirm"
    end = f"по времени — {fmt(d['end_at'])} МСК" if d["end_type"] == "time" else f"по билетам — {d['end_tickets']}"
    pr = "\n".join(f"{i + 1}. {html.escape(x['name'])}" for i, x in enumerate(d["prizes"]))
    await send(chat, f"Проверьте розыгрыш:\n\n🎁 <b>{html.escape(d['name'])}</b>\nПоявится в боте: {fmt(d['start_at'])} МСК\nИтоги: {end}\n\nПризы ({len(d['prizes'])}):\n{pr}",
               [[B("✅ Создать", "gw:ok")], [B("✖️ Отмена", "gw:cancel")]])

async def create(uid, chat):
    d = STATE.pop(uid)["d"]
    gid = await pool().fetchval("""INSERT INTO gw(name, prizes, start_at, end_type, end_at, end_tickets, created_by)
        VALUES($1,$2::jsonb,$3,$4,$5,$6,$7) RETURNING id""", d["name"], json.dumps(d["prizes"]), d["start_at"], d["end_type"],
        d.get("end_at"), d.get("end_tickets"), uid)
    await send(chat, f"✅ Розыгрыш создан (№{gid}). В боте он появится {fmt(d['start_at'])} МСК")
    await menu(chat)

async def handle_update(upd: dict) -> bool:
    """Вызови из обработчика обновлений бота. True — обновление обработано здесь, дальше его обрабатывать не надо."""
    cb, m = upd.get("callback_query"), upd.get("message")
    if cb and str(cb.get("data", "")).startswith("gw:"):
        await tg("answerCallbackQuery", callback_query_id=cb["id"])
        if is_admin(cb["from"]["id"]):
            try: await on_cb(cb["from"]["id"], cb["message"]["chat"]["id"], cb["data"])
            except Exception as e: print("GW ADMIN ERROR", repr(e)); await send(cb["message"]["chat"]["id"], "Ошибка, смотри логи")
        return True
    if m and m.get("text") and m["chat"]["type"] == "private" and m["from"]["id"] in STATE:
        uid, text = m["from"]["id"], m["text"]
        if text.startswith("/cancel"): STATE.pop(uid, None); await send(m["chat"]["id"], "Создание отменено"); return True
        if text.startswith("/"): STATE.pop(uid, None); return False      # любая другая команда сбрасывает диалог
        try: await on_text(uid, m["chat"]["id"], text)
        except Exception as e: print("GW ADMIN ERROR", repr(e)); await send(m["chat"]["id"], "Ошибка, смотри логи")
        return True
    return False

# ───────────────────────── API мини-аппа ─────────────────────────
def short(g, parts, tickets):
    pr = load_prizes(g["prizes"])
    return {"id": g["id"], "status": g["status"], "prizes": pr[:3], "total_prizes": len(pr), "participants": parts,
            "end_type": g["end_type"], "end_ts": int(g["end_at"].timestamp()) if g["end_at"] else None,
            "tickets_left": max(0, (g["end_tickets"] or 0) - tickets) if g["end_type"] == "tickets" else None}

def register(app, auth, token, render_png, app_url):
    CFG.update(token=token, render_png=render_png, app_url=app_url)

    async def _boot():
        for _ in range(120):
            if pool(): break
            await asyncio.sleep(0.5)
        else: print("GW: пул базы не появился, розыгрыши выключены"); return
        await pool().execute(SCHEMA)
        seed = os.environ.get("GW_SEED_SQL")     # запрос, возвращающий id уже существующих пользователей бота (первая колонка)
        if seed:
            try:
                rows = await pool().fetch(seed)
                await pool().executemany("INSERT INTO gw_users(uid) VALUES($1) ON CONFLICT DO NOTHING", [(int(r[0]),) for r in rows])
                print("GW: загружено существующих пользователей:", len(rows))
            except Exception as e: print("GW SEED ERROR", repr(e))
        asyncio.create_task(scheduler())
    app.add_event_handler("startup", lambda: asyncio.create_task(_boot()))

    def need_db():
        if not pool(): raise HTTPException(503, "база не готова")

    @app.post("/api/gw/open")
    async def gw_open(req: Request, authorization: str = Header()):
        """Вызывается при каждом открытии мини-аппа. Первый вход человека с ?startapp=gw<id>_<кто пригласил> запоминает пригласившего."""
        u = auth(authorization); need_db(); uid = u["id"]
        param = str((await req.json()).get("param") or "")
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or None
        ref_gw = ref_uid = None
        m = re.match(r"^gw(\d+)_(\d+)$", param)
        if m and int(m.group(2)) != uid: ref_gw, ref_uid = int(m.group(1)), int(m.group(2))
        async with pool().acquire() as c:
            known = await c.fetchval("SELECT 1 FROM gw_users WHERE uid=$1", uid)
            if known: await c.execute("UPDATE gw_users SET name=$2, username=$3 WHERE uid=$1", uid, name, u.get("username"))
            else: await c.execute("INSERT INTO gw_users(uid,name,username,ref_gw,ref_uid) VALUES($1,$2,$3,$4,$5)", uid, name, u.get("username"), ref_gw, ref_uid)
        return {"ok": True}

    @app.get("/api/gw/list")
    async def gw_list(authorization: str = Header()):
        auth(authorization); need_db()
        async with pool().acquire() as c:
            rows = await c.fetch("""(SELECT * FROM gw WHERE status='active' AND start_at<=now() ORDER BY COALESCE(end_at, start_at))
                                    UNION ALL (SELECT * FROM gw WHERE status='finished' AND start_at<=now() ORDER BY finished_at DESC LIMIT 5)""")
            items = []
            for g in rows:
                parts, tickets = await counts(c, g["id"]); items.append(short(g, parts, tickets))
        return {"now": int(time.time()), "items": items}

    @app.get("/api/gw/img/{slug}.png")
    async def gw_img(slug: str):
        if not re.match(r"^[A-Za-z0-9_]+-\d+$", slug): raise HTTPException(404)
        if slug.lower() not in IMG_CACHE:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.get(f"https://nft.fragment.com/gift/{slug.lower().replace('_', '')}.lottie.json")   # только модель, без фона
                r.raise_for_status()
                IMG_CACHE[slug.lower()] = await asyncio.to_thread(CFG["render_png"], r.content, 256)
            except Exception as e: print("GW IMG ERROR", slug, repr(e)); raise HTTPException(404)
        return Response(IMG_CACHE[slug.lower()], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    async def detail(gid, uid):
        async with pool().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1 AND start_at<=now()", gid)
            if not g: raise HTTPException(404)
            parts, tickets = await counts(c, gid)
            mine = await c.fetchval("SELECT count(*) FROM gw_tickets WHERE gw_id=$1 AND uid=$2", gid, uid)
            joined = bool(await c.fetchval("SELECT 1 FROM gw_tickets WHERE gw_id=$1 AND uid=$2 AND kind='join'", gid, uid))
            ws = await winners_of(c, gid) if g["status"] == "finished" else []
        out = short(g, parts, tickets); pr = load_prizes(g["prizes"]); out["prizes"] = pr
        out.update(now=int(time.time()), tickets=tickets, mine=int(mine), joined=joined,
                   ref=f"{app_url}{'&' if '?' in app_url else '?'}startapp=gw{gid}_{uid}",
                   winners=[{"name": w["name"] or "Без имени", "username": w["username"], "prize": pr[w["prize_idx"]]["name"]} for w in ws])
        return out

    @app.get("/api/gw/{gid}")
    async def gw_get(gid: int, authorization: str = Header()):
        u = auth(authorization); need_db(); return await detail(gid, u["id"])

    @app.post("/api/gw/{gid}/join")
    async def gw_join(gid: int, authorization: str = Header()):
        u = auth(authorization); need_db(); uid = u["id"]
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or None
        async with pool().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1 AND status='active' AND start_at<=now()", gid)
            if not g or (g["end_type"] == "time" and g["end_at"] <= datetime.now(timezone.utc)): raise HTTPException(409, "розыгрыш не идёт")
            await c.execute("""INSERT INTO gw_users(uid,name,username) VALUES($1,$2,$3)
                               ON CONFLICT (uid) DO UPDATE SET name=$2, username=$3""", uid, name, u.get("username"))
            ins = await c.fetchval("INSERT INTO gw_tickets(gw_id,uid,kind) VALUES($1,$2,'join') ON CONFLICT DO NOTHING RETURNING id", gid, uid)
            if ins:     # друг, впервые пришедший по ссылке и вступивший в розыгрыш, приносит пригласившему +1 билет
                ru = await c.fetchrow("SELECT ref_gw, ref_uid FROM gw_users WHERE uid=$1", uid)
                if ru and ru["ref_gw"] == gid and ru["ref_uid"]:
                    await c.execute("""INSERT INTO gw_tickets(gw_id,uid,kind,friend_uid)
                        SELECT $1,$2,'ref',$3 WHERE EXISTS(SELECT 1 FROM gw_tickets WHERE gw_id=$1 AND uid=$2 AND kind='join')
                        ON CONFLICT DO NOTHING""", gid, ru["ref_uid"], uid)
            _, tickets = await counts(c, gid)
        if g["end_type"] == "tickets" and tickets >= g["end_tickets"]: asyncio.create_task(finish(gid))
        return await detail(gid, uid)


# ───────────────────────── что добавить в бота ─────────────────────────
# 1) В админ-панели в самом низу добавь кнопку:   giveaways.PANEL_BUTTON      ({"text": "🎁 Розыгрыши", "callback_data": "gw:menu"})
# 2) В самом начале обработчика обновлений:        if await giveaways.handle_update(update): return
# Админы берутся из ADMIN_IDS (переменная окружения, id через запятую) или из bot.ADMIN_IDS / bot.ADMINS.
