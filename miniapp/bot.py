"""Бот: обязательные задания (подписка / заявка), админ-панель, статистика. Работает через webhook на том же сервере."""
import os, re, time, asyncio, hashlib
from html import escape as E
from urllib.parse import urlparse, unquote
import asyncpg, httpx
from fastapi import Request, HTTPException
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
"""

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
               allowed_updates=["message", "callback_query", "chat_join_request", "chat_member", "my_chat_member"])
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
        await send(cid, WELCOME, kb([{"text": "📱 Открыть результаты", "web_app": {"url": PUBLIC}}])); return
    rows = [[{"text": ("📢 Подписаться: " if t["kind"] == "link" else "✉️ Подать заявку: ") + t["title"][:40],
              "url": t["link"]}] for t in todo]
    rows.append([cb("✅ Проверить", "chk")])
    text = "Чтобы открыть результаты, выполни задания и нажми «Проверить»:"
    if cbid: await call("answerCallbackQuery", callback_query_id=cbid, text="Не все задания выполнены", show_alert=True)
    if mid: await call("editMessageText", chat_id=cid, message_id=mid, text=text, reply_markup=kb(*rows))
    else: await send(cid, text, kb(*rows))

PANEL = kb([cb("📊 Статистика", "st")], [cb("📢 Рассылка", "bc")], [cb("➕ Добавить задание", "add")], [cb("🔗 Статистика ссылок", "ls")])

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
    if text.startswith("/start"):
        await pool.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT (user_id) DO UPDATE SET blocked=FALSE", uid)  # считаем уникально
        await gate(uid, cid)
        if uid in ADMINS: await send(cid, "⚙️ Админ-панель", PANEL)
    elif uid in ADMINS and text == "/admin":
        STATE.pop(uid, None); await send(cid, "⚙️ Админ-панель", PANEL)
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
    if "message" in u: await on_message(u["message"])
    elif "callback_query" in u: await on_cb(u["callback_query"])
    elif "my_chat_member" in u: await on_block(u["my_chat_member"])
    elif "chat_member" in u: await on_member(u["chat_member"])
    elif "chat_join_request" in u: await on_join(u["chat_join_request"])

def register(app):
    app.on_event("startup")(startup)
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
