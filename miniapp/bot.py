"""Бот: обязательные задания (подписка / заявка), админ-панель, статистика. Работает через webhook на том же сервере."""
import os, re, time, asyncio, hashlib
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
WELCOME = "Привет! 👋 Смотри результаты своего Telegram-аккаунта"
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
"""

BOT_USERNAME = os.environ.get("BOT_USERNAME", "tgitoginahuibot").lstrip("@")
def ref_link(uid): return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

def auth_user(h: str) -> dict:  # проверка подписи Telegram (initData)
    import hmac, json
    from urllib.parse import parse_qsl
    data = dict(parse_qsl(h.removeprefix("tma "), keep_blank_values=True)); got = data.pop("hash", "")
    chk = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), got): raise HTTPException(401)
    return json.loads(data["user"])

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
async def send(cid, text, markup=None):
    return await call("sendMessage", chat_id=cid, text=text, parse_mode="HTML",
                      disable_web_page_preview=True, **({"reply_markup": markup} if markup else {}))

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
    me = await call("getMe")
    if not me: raise RuntimeError("getMe не сработал: проверь BOT_TOKEN")
    BOT_ID = me["id"]
    await call("setWebhook", url=PUBLIC + "/webhook", secret_token=SECRET,
               allowed_updates=["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member", "pre_checkout_query"])
    print("BOT READY", BOT_ID)

async def is_done(t, uid):
    m = await call("getChatMember", chat_id=t["chat_id"], user_id=uid)
    if m and (m["status"] in ("member", "administrator", "creator") or (m["status"] == "restricted" and m.get("is_member"))):
        return True
    if t["kind"] == "request":  # заявка ещё не принята -> статус left, смотрим нашу запись о заявке
        return bool(await pool.fetchval("SELECT 1 FROM events WHERE task_id=$1 AND user_id=$2 AND kind='request'", t["id"], uid))
    return False

async def gate(uid, cid, mid=None, cbid=None):
    todo = []
    for t in await pool.fetch("SELECT * FROM tasks WHERE active ORDER BY id"):
        if await is_done(t, uid):
            await pool.execute("INSERT INTO events(task_id,user_id,kind) VALUES($1,$2,'done') ON CONFLICT DO NOTHING", t["id"], uid)
        else: todo.append(t)
    if not todo:
        if cbid: await call("answerCallbackQuery", callback_query_id=cbid)
        await qualify(uid)
        await send(cid, WELCOME, kb([{"text": "📱 Открыть результаты", "web_app": {"url": PUBLIC}}])); return
    rows = [[{"text": ("📢 Подписаться: " if t["kind"] == "link" else "✉️ Подать заявку: ") + t["title"][:40],
              "url": t["link"]}] for t in todo]
    rows.append([cb("✅ Проверить", "chk")])
    text = "Чтобы открыть результаты, выполни задания и нажми «Проверить»:"
    if cbid: await call("answerCallbackQuery", callback_query_id=cbid, text="Не все задания выполнены", show_alert=True)
    if mid: await call("editMessageText", chat_id=cid, message_id=mid, text=text, reply_markup=kb(*rows))
    else: await send(cid, text, kb(*rows))

PANEL = kb([cb("📊 Статистика", "st")], [cb("📢 Рассылка", "bc")], [cb("👥 Рефералы за доп. инфо", "refs")], [cb("➕ Добавить задание", "add")], [cb("🔗 Статистика ссылок", "ls")])

async def resolve_chat(msg):
    t = (msg.get("text") or "").strip()
    fo = msg.get("forward_origin") or {}
    if fo.get("chat"): return fo["chat"]
    m = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/\d+)?/?", t) or re.fullmatch(r"@([A-Za-z0-9_]{4,})", t)
    if m: return await call("getChat", chat_id="@" + m.group(1))
    if re.fullmatch(r"-?\d{6,}", t): return await call("getChat", chat_id=int(t))
    return None

def bc_markup(btns):
    rows = []
    for b in btns:
        x = {"text": b["text"], "url": b["url"]}
        if b["style"]: x["style"] = b["style"]   # danger = красная, success = зелёная
        rows.append([x])
    return {"inline_keyboard": rows} if rows else None

async def bc_menu(cid, st):
    mk = bc_markup(st["btns"])
    await call("copyMessage", chat_id=cid, from_chat_id=cid, message_id=st["mid"], **({"reply_markup": mk} if mk else {}))  # предпросмотр
    await send(cid, f"Так увидят сообщение. Кнопок: {len(st['btns'])}\nДобавить ещё кнопку или запустить рассылку?",
               kb([cb("➕ Добавить кнопку", "bcadd")], [cb("🚀 Запустить рассылку", "bcgo")], [cb("❌ Отмена", "cancel")]))

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

async def run_bc(cid, mid, mk, smid):
    BC.update(run=True, stop=False)
    try:
        total = await pool.fetchval("SELECT count(*) FROM users")
        users = [r["user_id"] for r in await pool.fetch("SELECT user_id FROM users WHERE NOT blocked")]
        sent = fail = 0; last = time.time()
        stop_kb = kb([cb("⏹ Остановить", "bcstop")])
        for u in users:
            if BC["stop"]: break
            r = await copy_to(u, cid, mid, mk)
            if r == "ok": sent += 1
            elif r == "blocked": await pool.execute("UPDATE users SET blocked=TRUE WHERE user_id=$1", u)
            else: fail += 1
            if time.time() - last > 3:
                last = time.time()
                await call("editMessageText", chat_id=cid, message_id=smid, parse_mode="HTML", text=await bc_text(sent, total, fail, False), reply_markup=stop_kb)
            await asyncio.sleep(0.05)   # ~20 сообщений в секунду, в рамках лимитов Telegram
        txt = await bc_text(sent, total, fail, True) + ("\n⏹ Остановлена вручную" if BC["stop"] else "")
        await call("editMessageText", chat_id=cid, message_id=smid, parse_mode="HTML", text=txt)
        await send(cid, txt, kb([cb("⬅️ Меню", "adm")]))
    except Exception as e: print("broadcast error:", repr(e))
    finally: BC["run"] = False

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
        await gate(uid, cid)
        if uid in ADMINS: await send(cid, "⚙️ Админ-панель", PANEL)
    elif uid in ADMINS and text == "/admin":
        STATE.pop(uid, None); await send(cid, "⚙️ Админ-панель", PANEL)
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "refprice":
        if text.strip().isdigit() and int(text) <= 10000:
            await pool.execute("INSERT INTO settings(key,value) VALUES('ref_price',$1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", text.strip())
            STATE.pop(uid, None); n = int(text)
            await send(cid, f"✅ Цена доп. информации: <b>{n} ⭐</b>" if n else "✅ Платный вариант выключен", PANEL)
        else: await send(cid, "Пришли число от 0 до 10000.")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "refneed":
        if text.strip().isdigit() and int(text) <= 1000:
            await pool.execute("INSERT INTO settings(key,value) VALUES('ref_need',$1) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", text.strip())
            STATE.pop(uid, None); n = int(text)
            await send(cid, f"✅ Теперь для доп. информации нужно пригласить: <b>{n}</b>" if n else "✅ Доп. информация теперь доступна без приглашений", PANEL)
        else: await send(cid, "Пришли число от 0 до 1000.")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_msg" and not text.startswith("/"):
        if msg.get("media_group_id"):
            if not STATE[uid].get("warned"): STATE[uid]["warned"] = 1; await send(cid, "Альбомы не поддерживаются. Пришли одно сообщение (фото, GIF, видео или текст).")
            return
        STATE[uid] = {"step": "bc_menu", "mid": msg["message_id"], "btns": []}
        await bc_menu(cid, STATE[uid])
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_url":
        url = text.strip()
        if url.startswith(("t.me/", "telegram.me/")): url = "https://" + url
        if not re.fullmatch(r"(https?|tg)://\S+", url): await send(cid, "Это не ссылка. Пришли ссылку вида https://… или t.me/…"); return
        STATE[uid].update(step="bc_name", url=url)
        await send(cid, "Теперь пришли название кнопки (до 60 символов).")
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "bc_name":
        STATE[uid].update(step="bc_color", name=text.strip()[:60])
        await send(cid, "Выбери цвет кнопки:", kb([cb("Обычный", "col:"), cb("🔴 Красный", "col:danger"), cb("🟢 Зелёный", "col:success")]))
    elif uid in ADMINS and STATE.get(uid, {}).get("step") == "link":
        if "t.me/+" in text or "joinchat" in text:
            await send(cid, "По приватной ссылке канал не определить. Перешли мне любое сообщение из него или пришли ID (вида -100123…)."); return
        ch = await resolve_chat(msg)
        if not ch or ch.get("type") not in ("channel", "supergroup", "group"):
            await send(cid, "Не нашёл такой канал или чат. Пришли ссылку https://t.me/название, @название или перешли сообщение оттуда."); return
        me = await call("getChatMember", chat_id=ch["id"], user_id=BOT_ID)
        if not me or me["status"] not in ("administrator", "creator"):
            await send(cid, f"Сначала сделай меня администратором в «{E(ch.get('title',''))}» (с правом приглашать пользователей) и пришли ссылку ещё раз."); return
        STATE[uid] = {"step": "kind", "chat": {"id": ch["id"], "title": ch.get("title", ""), "username": ch.get("username")}}
        await send(cid, f"Нашёл: <b>{E(ch.get('title',''))}</b>\nКакой тип задания?",
                   kb([cb("🔗 Обычная ссылка", "kind:link")], [cb("✉️ Заявки", "kind:req")]))

async def on_cb(q):
    uid, data, cid, mid = q["from"]["id"], q["data"], q["message"]["chat"]["id"], q["message"]["message_id"]
    if data == "chk":
        await gate(uid, cid, mid, q["id"]); return
    await call("answerCallbackQuery", callback_query_id=q["id"])
    if uid not in ADMINS: return
    st = STATE.get(uid, {})
    if data == "cancel": STATE.pop(uid, None); await send(cid, "Отменено.", PANEL)
    elif data == "bc":
        STATE[uid] = {"step": "bc_msg"}
        await send(cid, "📢 Пришли сообщение для рассылки: текст, фото, GIF или видео, можно с форматированием (жирный, курсив, ссылки и т.д.). Одним сообщением.", kb([cb("❌ Отмена", "cancel")]))
    elif data == "bcadd" and st.get("step") == "bc_menu":
        st["step"] = "bc_url"; await send(cid, "Пришли ссылку, куда будет вести кнопка.")
    elif data.startswith("col:") and st.get("step") == "bc_color":
        st["btns"].append({"text": st["name"], "url": st["url"], "style": data[4:]})
        st["step"] = "bc_menu"; await bc_menu(cid, st)
    elif data == "bcstop": BC["stop"] = True
    elif data == "bcgo" and st.get("step") == "bc_menu":
        if BC["run"]: await send(cid, "Рассылка уже идёт."); return
        m = await send(cid, "📤 Запускаю рассылку…", kb([cb("⏹ Остановить", "bcstop")]))
        STATE.pop(uid, None)
        t = asyncio.create_task(run_bc(cid, st["mid"], bc_markup(st["btns"]), m["message_id"])); BG.add(t); t.add_done_callback(BG.discard)
    elif data == "refs":
        ok = await pool.fetchval("SELECT count(*) FROM referrals WHERE qualified"); pc, ps = await pool.fetchrow("SELECT count(*), coalesce(sum(stars),0) FROM purchases")
        price = await get_price()
        await send(cid, f"👥 Доступ к доп. информации\n\n🆓 Бесплатно: пригласить <b>{await get_need()}</b> (засчитано приглашений: {ok})\n⭐ Платно: <b>{str(price) + ' ⭐' if price else 'выключено'}</b> (покупок: {pc}, на {ps} ⭐)",
                   kb([cb("🆓 Бесплатно — число рефералов", "refs_free")], [cb("⭐ Платная — сумма в звёздах", "refs_paid")], [cb("⬅️ Меню", "adm")]))
    elif data == "refs_free":
        STATE[uid] = {"step": "refneed"}
        await send(cid, f"Сейчас нужно пригласить: <b>{await get_need()}</b>\nПришли новое число (0 — доступ без приглашений).", kb([cb("❌ Отмена", "cancel")]))
    elif data == "refs_paid":
        STATE[uid] = {"step": "refprice"}
        await send(cid, f"Сейчас цена: <b>{await get_price() or 'выключено'}</b>\nПришли сумму в звёздах (от 1 до 10000) или 0, чтобы выключить платный вариант.", kb([cb("❌ Отмена", "cancel")]))
    elif data == "adm": await send(cid, "⚙️ Админ-панель", PANEL)
    elif data == "st":
        n = await pool.fetchval("SELECT count(*) FROM users"); b = await pool.fetchval("SELECT count(*) FROM users WHERE blocked")
        await send(cid, f"📊 Статистика\n\n👥 Уникальных стартов: <b>{n}</b>\n🚫 Заблокировали бота: <b>{b}</b>\n✅ Активных: <b>{n-b}</b>", kb([cb("⬅️ Меню", "adm")]))
    elif data == "add":
        STATE[uid] = {"step": "link"}
        await send(cid, "Пришли ссылку на канал или чат (https://t.me/название или @название). Для приватного — перешли любое сообщение из него. Я должен быть там администратором.")
    elif data.startswith("kind:") and STATE.get(uid, {}).get("step") == "kind":
        c, req = STATE[uid]["chat"], data == "kind:req"
        inv = None if (not req and c["username"]) else await call("createChatInviteLink", chat_id=c["id"], name="mini-app", creates_join_request=req)
        link = ("https://t.me/" + c["username"]) if (not req and c["username"]) else (inv["invite_link"] if inv else None)
        if not link:
            await send(cid, f"Не получилось создать ссылку: {E(ERR[0])}\nПроверь, что у меня есть право приглашать пользователей."); return
        tid = await pool.fetchval("INSERT INTO tasks(chat_id,title,kind,link) VALUES($1,$2,$3,$4) RETURNING id", c["id"], c["title"], "request" if req else "link", link)
        STATE.pop(uid, None)
        await send(cid, f"✅ Задание #{tid} добавлено: <b>{E(c['title'])}</b> ({'заявки' if req else 'обычная ссылка'})", kb([cb("⬅️ Меню", "adm")]))
    elif data == "ls":
        rows = await pool.fetch("""SELECT t.id,t.title,t.kind,
          count(*) FILTER (WHERE e.kind='join') joins, count(*) FILTER (WHERE e.kind='request') reqs,
          count(*) FILTER (WHERE e.kind='done') done
          FROM tasks t LEFT JOIN events e ON e.task_id=t.id WHERE t.active GROUP BY t.id ORDER BY t.id""")
        if not rows: await send(cid, "Заданий пока нет.", kb([cb("⬅️ Меню", "adm")])); return
        txt = "\n\n".join(f"<b>#{r['id']} {E(r['title'])}</b> ({'заявки' if r['kind']=='request' else 'ссылка'})\n"
            f"👥 Вступили: {r['joins']}\n✉️ Заявок: {r['reqs']}\n✅ Выполнили: {r['done']}" for r in rows)
        await send(cid, "🔗 Статистика ссылок\n\n" + txt, kb(*[[cb(f"🗑 Удалить #{r['id']}", f"del:{r['id']}")] for r in rows], [cb("⬅️ Меню", "adm")]))
    elif data.startswith("del:"):
        await pool.execute("UPDATE tasks SET active=FALSE WHERE id=$1", int(data[4:]))
        await send(cid, "🗑 Задание удалено.", kb([cb("⬅️ Меню", "adm")]))

async def on_join(r):  # человек подал заявку в канал/чат, где у нас есть задание типа «заявки»
    await pool.execute("""INSERT INTO events(task_id,user_id,kind) SELECT id,$2,'request' FROM tasks
        WHERE chat_id=$1 AND kind='request' AND active ON CONFLICT DO NOTHING""", r["chat"]["id"], r["from"]["id"])

async def on_member(m):  # кто-то вступил в канал/чат, где есть задание
    new, old = m["new_chat_member"], m["old_chat_member"]
    ok = lambda x: x["status"] in ("member", "administrator", "creator") or (x["status"] == "restricted" and x.get("is_member"))
    if ok(new) and not ok(old):
        await pool.execute("""INSERT INTO events(task_id,user_id,kind) SELECT id,$2,'join' FROM tasks
            WHERE chat_id=$1 AND active ON CONFLICT DO NOTHING""", m["chat"]["id"], new["user"]["id"])

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
    @app.post("/api/invite")
    async def invite(authorization: str = Header()):
        uid = auth_user(authorization)["id"]
        res = {"type": "article", "id": f"inv{uid}", "title": "Приглашение",
               "input_message_content": {"message_text": "👋 Смотри результаты своего Telegram-аккаунта!\nУзнай свой стаж, подарки и баллы и сравни себя с топ-100 👇", "link_preview_options": {"is_disabled": True}},
               "reply_markup": {"inline_keyboard": [[{"text": "Узнать мои результаты", "url": ref_link(uid)}]]}}
        r = await call("savePreparedInlineMessage", user_id=uid, result=res, allow_user_chats=True, allow_group_chats=True, allow_channel_chats=True)
        if not r: raise HTTPException(502, "Telegram не принял сообщение")
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
