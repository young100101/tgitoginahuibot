# -*- coding: utf-8 -*-
"""Розыгрыши NFT-подарков. Встроено в bot.py: админ-экраны, билеты за друзей (задание = подписка на канал), выбор победителей.
Мини-апп получает данные через register(app, ...), которую вызывает server.py."""
import re, sys, json, time, asyncio, secrets, html
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import Header, HTTPException
from fastapi.responses import Response

MSK = timezone(timedelta(hours=3))
NFT_RE = re.compile(r"^(?:https?://)?t\.me/nft/([A-Za-z0-9_]+-\d+)/?$")
DT_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2})[:.](\d{2})\s*$")
IMG_CACHE: dict = {}
CFG: dict = {}
WIN_TEXT = "🎉 <b>Поздравляем! Ты выиграл в розыгрыше.</b>\nТвой приз: {prize}\n\nСкоро с тобой свяжутся для выдачи."

SCHEMA = """
CREATE TABLE IF NOT EXISTS gw(
  id SERIAL PRIMARY KEY, name TEXT NOT NULL, prizes JSONB NOT NULL,
  start_at TIMESTAMPTZ NOT NULL, end_type TEXT NOT NULL, end_at TIMESTAMPTZ, end_tickets INT,
  status TEXT NOT NULL DEFAULT 'active', created_by BIGINT, created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS gw_users(uid BIGINT PRIMARY KEY, name TEXT, username TEXT);
CREATE TABLE IF NOT EXISTS gw_tickets(
  id SERIAL PRIMARY KEY, gw_id INT NOT NULL, uid BIGINT NOT NULL, kind TEXT NOT NULL, friend_uid BIGINT, created_at TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS gw_t_join ON gw_tickets(gw_id, uid) WHERE kind='join';
CREATE UNIQUE INDEX IF NOT EXISTS gw_t_ref ON gw_tickets(gw_id, friend_uid) WHERE kind='ref';
CREATE TABLE IF NOT EXISTS gw_winners(gw_id INT NOT NULL, prize_idx INT NOT NULL, uid BIGINT NOT NULL, ticket_id INT, PRIMARY KEY(gw_id, prize_idx));
CREATE TABLE IF NOT EXISTS gw_refs(invitee BIGINT PRIMARY KEY, gw_id INT NOT NULL, inviter BIGINT NOT NULL, credited BOOLEAN DEFAULT FALSE);
"""

BG_RE = re.compile(r"back|bg|backdrop|background|pattern", re.I)

def _static(o):   # в слое нет анимированных свойств
    if isinstance(o, dict):
        if o.get("a") == 1 or "refId" in o: return False
        return all(_static(v) for v in o.values())
    if isinstance(o, list): return all(_static(v) for v in o)
    return True

def strip_backdrop(raw: bytes) -> bytes:
    """Убирает из анимации NFT-подарка фон и узор, оставляет только модель."""
    d = json.loads(raw); layers = d.get("layers") or []
    keep = [l for l in layers if not BG_RE.search(str(l.get("nm", "")))]
    if len(keep) == len(layers):      # у слоёв нет говорящих имён: фон — статичные слои в самом низу
        i = len(layers)
        while i > 1 and _static(layers[i - 1]): i -= 1
        keep = layers[:i]
    if keep and len(keep) < len(layers):
        d["layers"] = keep; print("GW: фон убран, слоёв было", len(layers), "стало", len(keep))
    return json.dumps(d).encode()

def B(): return sys.modules["bot"]
def P(): return B().pool

# ───────────────────────── общее ─────────────────────────
def prize_from_url(url):
    m = NFT_RE.match(url.strip())
    if not m: return None
    slug = m.group(1); base, num = slug.rsplit("-", 1)
    return {"url": f"https://t.me/nft/{slug}", "slug": slug, "name": re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", base).strip() + f" #{num}"}

def load_prizes(v): return json.loads(v) if isinstance(v, str) else v
def fmt(dt): return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
def ulink(uid, name): return f'<a href="tg://user?id={uid}">{html.escape(name or "Без имени")}</a>'

def parse_dt(s):
    m = DT_RE.match(s or "")
    if not m: return None
    d, mo, y, h, mi = map(int, m.groups())
    try: return datetime(y, mo, d, h, mi, tzinfo=MSK)
    except ValueError: return None

async def counts(c, gid):
    r = await c.fetchrow("SELECT count(*) FILTER (WHERE kind='join') AS parts, count(*) AS tickets FROM gw_tickets WHERE gw_id=$1", gid)
    return int(r["parts"]), int(r["tickets"])

async def init():
    """Вызывается из bot._startup после создания пула."""
    await P().execute(SCHEMA)
    t = asyncio.create_task(scheduler()); B().BG.add(t); t.add_done_callback(B().BG.discard)

# ───────────────────────── друзья и билеты ─────────────────────────
async def on_start(uid, payload, new):
    """/start gw_<id розыгрыша>_<кто пригласил>: считаем только тех, кто раньше в бота не заходил."""
    m = re.fullmatch(r"gw_(\d+)_(\d+)", payload or "")
    if new and m and int(m.group(2)) != uid:
        await P().execute("INSERT INTO gw_refs(invitee,gw_id,inviter) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", uid, int(m.group(1)), int(m.group(2)))

async def on_qualified(uid):
    """Друг выполнил задание (подписался на канал(ы)) -> пригласившему +1 билет."""
    try:
        r = await P().fetchrow("UPDATE gw_refs SET credited=TRUE WHERE invitee=$1 AND NOT credited RETURNING gw_id, inviter", uid)
        if not r: return
        gid, inv = r["gw_id"], r["inviter"]
        async with P().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1 AND status='active' AND start_at<=now()", gid)
            if not g or (g["end_type"] == "time" and g["end_at"] <= datetime.now(timezone.utc)): return
            ins = await c.fetchval("""INSERT INTO gw_tickets(gw_id,uid,kind,friend_uid)
                SELECT $1,$2,'ref',$3 WHERE EXISTS(SELECT 1 FROM gw_tickets WHERE gw_id=$1 AND uid=$2 AND kind='join')
                ON CONFLICT DO NOTHING RETURNING id""", gid, inv, uid)
            _, tickets = await counts(c, gid)
        if ins: await B().send(inv, "🎟 <b>Друг присоединился по твоей ссылке!</b>\nТебе начислен +1 билет в розыгрыше.")
        if g["end_type"] == "tickets" and tickets >= g["end_tickets"]: await finish(gid)
    except Exception as e: print("GW QUALIFY ERROR", repr(e))

# ───────────────────────── завершение и выбор победителей ─────────────────────────
async def finish(gid):
    async with P().acquire() as c:
        async with c.transaction():
            g = await c.fetchrow("UPDATE gw SET status='finished', finished_at=now() WHERE id=$1 AND status='active' RETURNING *", gid)
            if not g: return
            prizes = load_prizes(g["prizes"])
            tickets = list(await c.fetch("SELECT id, uid FROM gw_tickets WHERE gw_id=$1", gid))
            secrets.SystemRandom().shuffle(tickets)      # выигрывают случайные билеты
            used, win = set(), []
            for t in tickets:
                if len(win) >= len(prizes): break
                if t["uid"] in used: continue            # один человек — один приз, пока есть другие участники
                used.add(t["uid"]); win.append(t)
            for i, t in enumerate(win):
                await c.execute("INSERT INTO gw_winners(gw_id,prize_idx,uid,ticket_id) VALUES($1,$2,$3,$4)", gid, i, t["uid"], t["id"])
    async with P().acquire() as c:
        g = await c.fetchrow("SELECT * FROM gw WHERE id=$1", gid); ws = await winners_of(c, gid); parts, tickets = await counts(c, gid)
    prizes = load_prizes(g["prizes"])
    for w in ws:
        pr = prizes[w["prize_idx"]]
        await B().send(w["uid"], WIN_TEXT.format(prize=f'<a href="{pr["url"]}">{html.escape(pr["name"])}</a>'))
    await B().send(g["created_by"], "🏁 <b>Розыгрыш завершён</b>\n\n" + stats_text(g, parts, tickets, ws))

async def winners_of(c, gid):
    return await c.fetch("""SELECT w.prize_idx, w.uid, w.ticket_id, u.name, u.username FROM gw_winners w
                            LEFT JOIN gw_users u ON u.uid=w.uid WHERE w.gw_id=$1 ORDER BY w.prize_idx""", gid)

async def scheduler():
    while True:
        try:
            for r in await P().fetch("""SELECT id FROM gw WHERE status='active' AND (
                (end_type='time' AND end_at<=now()) OR
                (end_type='tickets' AND end_tickets <= (SELECT count(*) FROM gw_tickets t WHERE t.gw_id=gw.id)))"""):
                await finish(r["id"])
        except Exception as e: print("GW SCHEDULER ERROR", repr(e))
        await asyncio.sleep(15)

def stats_text(g, parts, tickets, ws):
    prizes = load_prizes(g["prizes"])
    st = "✅ завершён" if g["status"] == "finished" else ("⏳ ждёт старта" if g["start_at"] > datetime.now(timezone.utc) else "🟢 идёт")
    t = [f"🎁 <b>{html.escape(g['name'])}</b>", f"Статус: {st}", f"Старт: {fmt(g['start_at'])} МСК",
         f"Конец: {fmt(g['end_at'])} МСК" if g["end_type"] == "time" else f"Конец: по билетам ({g['end_tickets']})",
         f"Призов: {len(prizes)} · Участников: <b>{parts}</b> · Билетов: {tickets}"]
    if g["status"] == "finished":
        t.append("\n<b>Победители:</b>")
        if not ws: t.append("— никто не участвовал")
        for w in ws:
            pr = prizes[w["prize_idx"]]
            t.append(f"{w['prize_idx'] + 1}. {ulink(w['uid'], w['name'])}" + (f" (@{html.escape(w['username'])})" if w["username"] else "")
                     + f" — <a href=\"{pr['url']}\">{html.escape(pr['name'])}</a> · билет №{w['ticket_id']}")
        if len(ws) < len(prizes): t.append(f"\nПризов без победителя: {len(prizes) - len(ws)} (участников меньше, чем призов)")
    return "\n".join(t)

# ───────────────────────── админка (экраны бота) ─────────────────────────
async def admin_cb(T, uid, data, st):
    b = B(); kb, cb = b.kb, b.cb; a = data.split(":"); ask = b.ask
    if data == "gw":
        b.STATE.pop(uid, None)
        await b.scr(T, "🎁 <b>Розыгрыши</b>", kb([cb("➕ Создать розыгрыш", "gw:new")], [cb("📜 Прошлые розыгрыши", "gw:h:0")], [cb("⬅️ Меню", "adm")]))
    elif data == "gw:new":
        await ask(T, uid, "gw_name", "Придумай название для розыгрыша <i>(видишь только ты)</i>", d={"prizes": []})
    elif data == "gw:more" and st.get("step") == "gw_choose":
        await ask(T, uid, "gw_prize", "Пришли ссылку на следующий подарок", d=st["d"])
    elif data == "gw:done" and st.get("step") == "gw_choose":
        await ask(T, uid, "gw_start", "Во сколько <b>по московскому времени</b> розыгрыш появится в боте?\nНапример: <code>11.12.2026 15:00</code>", d=st["d"])
    elif a[1:2] == ["t"] and st.get("step") == "gw_endtype":
        st["d"]["end_type"] = a[2]
        if a[2] == "time": await ask(T, uid, "gw_endtime", "Когда завершить розыгрыш (МСК)?\nНапример: <code>15.12.2026 18:00</code>", d=st["d"])
        else: await ask(T, uid, "gw_endtickets", "Сколько всего билетов нужно набрать, чтобы розыгрыш завершился? Пришли число.", d=st["d"])
    elif data == "gw:ok" and st.get("step") == "gw_confirm":
        d = st["d"]; b.STATE.pop(uid, None)
        gid = await P().fetchval("""INSERT INTO gw(name,prizes,start_at,end_type,end_at,end_tickets,created_by) VALUES($1,$2::jsonb,$3,$4,$5,$6,$7) RETURNING id""",
                                 d["name"], json.dumps(d["prizes"]), d["start_at"], d["end_type"], d.get("end_at"), d.get("end_tickets"), uid)
        await b.scr(T, f"✅ Розыгрыш №{gid} создан. В боте он появится <b>{fmt(d['start_at'])} МСК</b>.", kb([cb("🎁 Розыгрыши", "gw")], [cb("⬅️ Меню", "adm")]))
    elif a[1:2] == ["h"]:
        page = int(a[2]); rows = await P().fetch("SELECT id,name,status FROM gw ORDER BY id DESC LIMIT 11 OFFSET $1", page * 10)
        if not rows and page == 0: await b.scr(T, "Розыгрышей пока нет.", kb([cb("⬅️ Назад", "gw")])); return
        btn = [[cb(("✅ " if r["status"] == "finished" else "⏳ ") + r["name"][:50], f"gw:v:{r['id']}")] for r in rows[:10]]
        nav = ([cb("◀️", f"gw:h:{page - 1}")] if page else []) + ([cb("▶️", f"gw:h:{page + 1}")] if len(rows) > 10 else [])
        if nav: btn.append(nav)
        btn.append([cb("⬅️ Назад", "gw")]); await b.scr(T, "📜 <b>Прошлые розыгрыши</b>", kb(*btn))
    elif a[1:2] == ["v"]:
        gid = int(a[2])
        async with P().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1", gid)
            if not g: await b.scr(T, "Розыгрыш не найден.", kb([cb("⬅️ Назад", "gw:h:0")])); return
            parts, tickets = await counts(c, gid); ws = await winners_of(c, gid)
        await b.scr(T, stats_text(g, parts, tickets, ws), kb([cb("⬅️ К списку", "gw:h:0")]))

async def admin_msg(msg, uid, cid, text):
    b = B(); kb, cb = b.kb, b.cb; ask = b.ask; await b.delete(cid, msg["message_id"])
    st = b.STATE[uid]; step, d = st["step"], st["d"]; T = b.T_for(uid, cid); text = text.strip(); cancel = cb("❌ Отмена", "cancel")
    if step == "gw_name":
        d["name"] = text[:100]
        await ask(T, uid, "gw_prize", "Пришли ссылку на подарок\nНапример: <code>https://t.me/nft/PlushPepe-123</code>", d=d)
    elif step == "gw_prize":
        pr = prize_from_url(text)
        if not pr: await b.bad(uid, cid, "Нужна ссылка на NFT-подарок вида https://t.me/nft/PlushPepe-123"); return
        if any(x["slug"].lower() == pr["slug"].lower() for x in d["prizes"]): await b.bad(uid, cid, "Этот подарок уже добавлен."); return
        d["prizes"].append(pr)
        await ask(T, uid, "gw_choose", f"Добавлено: <b>{html.escape(pr['name'])}</b>\nПодарков в розыгрыше: {len(d['prizes'])}\n\nДостаточно подарков?",
                  markup=kb([cb("✅ Да, идём дальше", "gw:done")], [cb("➕ Добавить ещё", "gw:more")], [cancel]), d=d)
    elif step == "gw_start":
        dt = parse_dt(text)
        if not dt: await b.bad(uid, cid, "Не понял дату. Формат: 11.12.2026 15:00"); return
        if dt < datetime.now(MSK) - timedelta(minutes=1): await b.bad(uid, cid, "Это время уже прошло, укажи будущее."); return
        d["start_at"] = dt
        await ask(T, uid, "gw_endtype", "Как завершится розыгрыш?", markup=kb([cb("⏰ По времени", "gw:t:time")], [cb("🎟 По количеству билетов", "gw:t:tickets")], [cancel]), d=d)
    elif step in ("gw_endtime", "gw_endtickets"):
        if step == "gw_endtime":
            dt = parse_dt(text)
            if not dt: await b.bad(uid, cid, "Не понял дату. Формат: 15.12.2026 18:00"); return
            if dt <= d["start_at"]: await b.bad(uid, cid, "Конец должен быть позже старта."); return
            d["end_at"] = dt
        else:
            if not text.isdigit() or int(text) < 1: await b.bad(uid, cid, "Пришли число больше нуля."); return
            d["end_tickets"] = int(text)
        end = f"по времени — {fmt(d['end_at'])} МСК" if d["end_type"] == "time" else f"по билетам — {d['end_tickets']}"
        pr = "\n".join(f"{i + 1}. {html.escape(x['name'])}" for i, x in enumerate(d["prizes"]))
        await ask(T, uid, "gw_confirm", f"Проверь розыгрыш:\n\n🎁 <b>{html.escape(d['name'])}</b>\nПоявится в боте: {fmt(d['start_at'])} МСК\nИтоги: {end}\n\nПризы ({len(d['prizes'])}):\n{pr}",
                  markup=kb([cb("✅ Создать", "gw:ok")], [cancel]), d=d)
    else: await b.bad(uid, cid, "Выбери вариант кнопкой.")

# ───────────────────────── API мини-аппа ─────────────────────────
def short(g, parts, tickets):
    pr = load_prizes(g["prizes"])
    return {"id": g["id"], "status": g["status"], "prizes": pr[:3], "total_prizes": len(pr), "participants": parts, "end_type": g["end_type"],
            "end_ts": int(g["end_at"].timestamp()) if g["end_at"] else None,
            "tickets_left": max(0, (g["end_tickets"] or 0) - tickets) if g["end_type"] == "tickets" else None}

def register(app, auth, token, render_png, app_url):
    CFG.update(render_png=render_png)

    @app.get("/api/gw/list")
    async def gw_list(authorization: str = Header()):
        auth(authorization)
        async with P().acquire() as c:
            rows = await c.fetch("""(SELECT * FROM gw WHERE status='active' AND start_at<=now() ORDER BY COALESCE(end_at, start_at))
                                    UNION ALL (SELECT * FROM gw WHERE status='finished' AND start_at<=now() ORDER BY finished_at DESC LIMIT 5)""")
            items = [short(g, *await counts(c, g["id"])) for g in rows]
        return {"now": int(time.time()), "items": items}

    @app.get("/api/gw/img/{slug}.png")
    async def gw_img(slug: str):
        if not re.match(r"^[A-Za-z0-9_]+-\d+$", slug): raise HTTPException(404)
        k = slug.lower()
        if k not in IMG_CACHE:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.get(f"https://nft.fragment.com/gift/{k.replace('_', '')}.lottie.json")   # только модель, без фона
                r.raise_for_status()
                IMG_CACHE[k] = await asyncio.to_thread(lambda: CFG["render_png"](strip_backdrop(r.content), 256))
            except Exception as e: print("GW IMG ERROR", slug, repr(e)); raise HTTPException(404)
        return Response(IMG_CACHE[k], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/gw/dbg/{slug}")
    async def gw_dbg(slug: str):   # список слоёв анимации подарка — на случай, если фон не убрался
        if not re.match(r"^[A-Za-z0-9_]+-\d+$", slug): raise HTTPException(404)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://nft.fragment.com/gift/{slug.lower().replace('_', '')}.lottie.json")
        d = r.json()
        return {"w": d.get("w"), "h": d.get("h"), "layers": [{"i": i, "nm": l.get("nm"), "ty": l.get("ty"), "ref": l.get("refId"), "static": _static(l)} for i, l in enumerate(d.get("layers", []))]}

    @app.post("/api/gw/{gid}/invite")
    async def gw_invite(gid: int, authorization: str = Header()):
        """Готовое сообщение для пересылки — с кнопкой «Участвовать» внутри."""
        uid = auth(authorization)["id"]
        if not await P().fetchval("SELECT 1 FROM gw WHERE id=$1 AND start_at<=now()", gid): raise HTTPException(404)
        link = f"https://t.me/{B().BOT_USERNAME}?start=gw_{gid}_{uid}"
        res = {"type": "article", "id": f"gw{gid}u{uid}", "title": "Розыгрыш",
               "input_message_content": {"message_text": "🎁 <b>Розыгрыш NFT-подарков!</b>\n\nУчаствуй и выигрывай подарки абсолютно бесплатно 👇", "parse_mode": "HTML",
                                         "link_preview_options": {"is_disabled": True}},
               "reply_markup": {"inline_keyboard": [[{"text": "🎁 Участвовать", "url": link}]]}}
        r = await B().call("savePreparedInlineMessage", user_id=uid, result=res, allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
        if not r: raise HTTPException(502, "Telegram не принял сообщение")
        return {"id": r["id"]}

    async def detail(gid, uid):
        async with P().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1 AND start_at<=now()", gid)
            if not g: raise HTTPException(404)
            parts, tickets = await counts(c, gid)
            mine = await c.fetchval("SELECT count(*) FROM gw_tickets WHERE gw_id=$1 AND uid=$2", gid, uid)
            joined = bool(await c.fetchval("SELECT 1 FROM gw_tickets WHERE gw_id=$1 AND uid=$2 AND kind='join'", gid, uid))
            ws = await winners_of(c, gid) if g["status"] == "finished" else []
        out = short(g, parts, tickets); pr = load_prizes(g["prizes"]); out["prizes"] = pr
        out.update(now=int(time.time()), tickets=tickets, mine=int(mine), joined=joined, ref=f"https://t.me/{B().BOT_USERNAME}?start=gw_{gid}_{uid}",
                   winners=[{"name": w["name"] or "Без имени", "username": w["username"], "prize": pr[w["prize_idx"]]["name"], "slug": pr[w["prize_idx"]]["slug"]} for w in ws])
        return out

    @app.get("/api/gw/{gid}")
    async def gw_get(gid: int, authorization: str = Header()):
        return await detail(gid, auth(authorization)["id"])

    @app.post("/api/gw/{gid}/join")
    async def gw_join(gid: int, authorization: str = Header()):
        u = auth(authorization); uid = u["id"]
        name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or None
        async with P().acquire() as c:
            g = await c.fetchrow("SELECT * FROM gw WHERE id=$1 AND status='active' AND start_at<=now()", gid)
            if not g or (g["end_type"] == "time" and g["end_at"] <= datetime.now(timezone.utc)): raise HTTPException(409, "розыгрыш не идёт")
            await c.execute("INSERT INTO gw_users(uid,name,username) VALUES($1,$2,$3) ON CONFLICT (uid) DO UPDATE SET name=$2, username=$3", uid, name, u.get("username"))
            await c.execute("INSERT INTO gw_tickets(gw_id,uid,kind) VALUES($1,$2,'join') ON CONFLICT DO NOTHING", gid, uid)
            _, tickets = await counts(c, gid)
        if g["end_type"] == "tickets" and tickets >= g["end_tickets"]: asyncio.create_task(finish(gid))
        return await detail(gid, uid)
