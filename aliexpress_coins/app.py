"""AliExpress coins check-in add-on.

One Chromium profile in /data/profile, always headed inside Xvfb. Login runs it
as a plain browser (streamed over noVNC through ingress); the daily check-in
drives the same binary and profile through Playwright.

  GET  /              status page (ingress panel)
  POST /login/start   open the coins page and expose the screen over noVNC
  POST /login/stop    close the login browser
  GET  /websockify    noVNC websocket <-> x11vnc bridge
  POST /api/collect   run the check-in (HA shell_command / rest_command)
  POST /api/tasks     run only the extra coin tasks; force=<taskId> also tries a
                      skipped game/order/app-only task (never an ad task)
  stdin "collect"     same as /api/collect, via the hassio.addon_stdin action
  stdin "tasks"       same as /api/tasks without force
"""
import asyncio
import collections
import datetime
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import string
import subprocess
import tempfile
import time
from contextlib import closing

from aiohttp import ClientSession, WSMsgType, web
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

DATA = "/data"
PROFILE = f"{DATA}/profile"
COOKIES = f"{PROFILE}/Default/Cookies"
STATUS_FILE = f"{DATA}/status.json"
RUN_FILE = f"{DATA}/last_run.json"
SHOT_FILE = f"{DATA}/last.png"
SEEN_FILE = f"{DATA}/seen_tasks.json"
PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
COIN_URL = "https://m.aliexpress.com/p/coin-index/index.html"
DISPLAY = ":99"
W, H = 412, 915  # phone-shaped screen so the noVNC view fits a phone
CHROME = sorted(glob.glob("/ms-playwright/chromium-*/chrome-linux/chrome"))[-1]
LOGIN_TIMEOUT = 15 * 60
SENSOR = "sensor.aliexpress_coins"
# Login ticket cookies: set at login with a ~1 year expiry, never refreshed by visits.
LOGIN_COOKIES = ("xman_t", "xman_us_t")


def load_options():
    try:
        with open(f"{DATA}/options.json") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


OPTIONS = load_options()
LOCALE = OPTIONS.get("locale") or "en-US"
EXTRA_TASKS = bool(OPTIONS.get("extra_tasks"))
TIMEZONE = os.environ.get("TZ") or None  # Supervisor passes Home Assistant's time zone
CHROME_ARGS = ["--kiosk", "--no-first-run", f"--lang={LOCALE}", f"--window-size={W},{H}", "--window-position=0,0"]

# Whole-label matches only: the page is full of banners like "Collect, shop and save".
BUTTON_RE = re.compile(r"^\s*((fazer\s+)?check.?in|coletar|resgatar|collect|claim)\s*!?\s*$", re.I)
# Logged-in mobile page after today's check-in: "Today ✓ 10", "Get 15 check-in coins tomorrow!"
DONE_RE = re.compile(r"today\s*✓|hoje\s*✓|check-in coins tomorrow|moedas de check-in amanh", re.I)

# Coin tasks ("Earn more coins") come from task.delivery.query. Type 0 opens a page
# (sometimes to browse it for N seconds) and type 15 is the bonus for the check-in;
# games, orders and app-only tasks use other types.
SAFE_TASK_TYPES = {0, 15}
# Never view or tap ads to earn coins: that is fake ad traffic billed to advertisers.
AD_RE = re.compile(r"sponsor|patrocin|adclick|adpost|recommends\.html", re.I)
TASKS_BUDGET = 25 * 60  # seconds for the whole task loop; each task takes 1-2 min on a Pi
TASK_TRIES = 2

lock = asyncio.Lock()
start_lock = asyncio.Lock()  # serialises login starts (double taps, retries)
login = {}  # chrome, vnc, timer while a login session is open
tasks = set()

# Panel strings; the language follows the browser's Accept-Language.
TEXT = {
    "en": {
        "never_run": "Not run yet", "running": "Checking in…", "collected": "Coins collected",
        "session_expired": "Login needed", "button_not_found": "Button not found", "error": "Failed",
        "last_success": "Last check-in", "checked_at": "Last run", "never": "never",
        "login_expires": "Login valid until", "unknown": "unknown",
        "extra": "Extra tasks today", "coins": "coins", "tasks_running": "running…", "tasks_error": "failed",
        "tasks": "Coin tasks", "t_done": "Done", "t_failed": "Not done (will retry next run)",
        "t_skipped": "Skipped", "t_new": "New since the last run",
        "s_done": "Done", "s_failed": "Not done", "s_skipped": "Skipped",
        "r_ads": "ad task: never automated", "r_other": "not automated by default",
        "retry": "Retry extra tasks", "force": "Try anyway", "by_hand": "Do tasks myself in the browser",
        "ads_note": "Ad tasks are never done by the add-on (that would be fake ad traffic). "
                    "Open the browser to do them yourself.",
        "no_tasks": "No task list yet. Retry to load it.",
        "collect": "Check in now", "login": "Open browser to log in",
        "back": "Back to browser", "close": "Close browser",
        "login_open": "The login browser is open. It closes by itself after 15 minutes.",
        "screenshot": "Last screenshot",
        "help": "Log in once with the browser button. Home Assistant then triggers the daily "
                "check-in; see the add-on's Documentation tab.",
    },
    "pt": {
        "never_run": "Ainda não rodou", "running": "Fazendo check-in…", "collected": "Moedas coletadas",
        "session_expired": "Precisa fazer login", "button_not_found": "Botão não encontrado",
        "error": "Falhou",
        "last_success": "Último check-in", "checked_at": "Última execução", "never": "nunca",
        "login_expires": "Login válido até", "unknown": "desconhecido",
        "extra": "Tarefas extras hoje", "coins": "moedas", "tasks_running": "em andamento…",
        "tasks_error": "falhou",
        "tasks": "Tarefas de moedas", "t_done": "Feitas", "t_failed": "Não feitas (tenta de novo na próxima)",
        "t_skipped": "Ignoradas", "t_new": "Novas desde a última execução",
        "s_done": "Feita", "s_failed": "Não feita", "s_skipped": "Ignorada",
        "r_ads": "anúncio: nunca automatizada", "r_other": "não automatizada por padrão",
        "retry": "Tentar tarefas extras de novo", "force": "Tentar mesmo assim",
        "by_hand": "Fazer as tarefas eu mesmo no navegador",
        "ads_note": "Tarefas com anúncio nunca são feitas pelo add-on (seria tráfego falso de anúncio). "
                    "Abra o navegador para fazê-las você mesmo.",
        "no_tasks": "Ainda não há lista de tarefas. Tente de novo para carregá-la.",
        "collect": "Fazer check-in agora", "login": "Abrir navegador para login",
        "back": "Voltar ao navegador", "close": "Fechar navegador",
        "login_open": "O navegador de login está aberto. Ele fecha sozinho após 15 minutos.",
        "screenshot": "Última captura de tela",
        "help": "Faça login uma vez pelo botão do navegador. O Home Assistant dispara o check-in "
                "diário; veja a aba Documentação do add-on.",
    },
}
TONE = {"collected": "ok", "running": "busy", "session_expired": "warn", "button_not_found": "warn",
        "error": "bad", "never_run": "idle"}


def now():
    return datetime.datetime.now().astimezone()


def load_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {"status": "never_run", "detail": "", "checked_at": None, "last_success": None}


def login_expires():
    """When the login cookies expire (ISO 8601), or None if there are none.

    AliExpress can still end the session earlier on its side; that shows up
    as session_expired on the next check-in.
    """
    try:
        with tempfile.NamedTemporaryFile() as tmp:
            shutil.copyfile(COOKIES, tmp.name)  # Chromium may hold the DB open
            with closing(sqlite3.connect(tmp.name)) as db:
                (us,) = db.execute(
                    "SELECT min(expires_utc) FROM cookies WHERE host_key = '.aliexpress.com'"
                    " AND is_persistent AND name IN (?, ?)", LOGIN_COOKIES).fetchone()
    except (OSError, sqlite3.Error):
        return None
    if not us:
        return None
    epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)  # Chromium's cookie epoch
    return (epoch + datetime.timedelta(microseconds=us)).astimezone().isoformat(timespec="seconds")


async def push_state(st):
    attrs = {
        "friendly_name": "AliExpress coins",
        "icon": "mdi:hand-coin",
        "last_success": st.get("last_success"),
        "detail": st.get("detail"),
        "checked_at": st.get("checked_at"),
        "login_expires": login_expires(),
    }
    if EXTRA_TASKS:
        tk = st.get("tasks") or {}
        attrs.update(
            tasks_status=tk.get("status"),
            extra_coins=tk.get("coins", 0) if tk.get("date") == now().date().isoformat() else 0,
            tasks_done=tk.get("done", []),
            tasks_skipped=tk.get("skipped", {}),
            new_tasks=tk.get("new", []),
            tasks_checked_at=tk.get("checked_at"),
        )
    try:
        async with ClientSession() as s:
            await s.post(
                f"http://supervisor/core/api/states/{SENSOR}",
                headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"},
                json={"state": st["status"], "attributes": attrs},
            )
    except Exception as e:  # HA may be restarting; the keepalive loop retries
        print("push_state failed:", e)


async def save_status(status, detail):
    st = load_status()
    st.update(status=status, detail=detail, checked_at=now().isoformat(timespec="seconds"))
    if status == "collected":
        st["last_success"] = now().date().isoformat()
    with open(STATUS_FILE, "w") as f:
        json.dump(st, f)
    await push_state(st)
    return st


async def save_tasks(**fields):
    st = load_status()
    st["tasks"] = {**(st.get("tasks") or {}), **fields}
    with open(STATUS_FILE, "w") as f:
        json.dump(st, f)
    await push_state(st)


def clear_profile_locks():
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = f"{PROFILE}/{name}"
        if os.path.lexists(path):
            os.remove(path)


async def launch(pw):
    clear_profile_locks()
    # Phone emulation: desktop Chromium is redirected to coin-pc-index, which
    # has no check-in ("earn coins on app" QR only).
    dev = dict(pw.devices["Pixel 7"])
    dev.pop("default_browser_type", None)
    return await pw.chromium.launch_persistent_context(
        PROFILE,
        executable_path=CHROME,
        headless=False,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        args=CHROME_ARGS,
        env={**os.environ, "DISPLAY": DISPLAY},
        **dev,
    )


async def stop_login():
    if not login:
        return
    if (t := login.get("timer")) and t is not asyncio.current_task():
        t.cancel()
    try:
        login["chrome"].terminate()
        await asyncio.to_thread(login["chrome"].wait, 15)
    finally:
        login["vnc"].terminate()
        login.clear()
        lock.release()


async def login_timeout():
    await asyncio.sleep(LOGIN_TIMEOUT)
    await stop_login()


def is_login_wall(page, mtop):
    # The coins API answers FAIL_SYS_SESSION_EXPIRED when logged out.
    # Desktop Chromium is redirected to www.aliexpress.com/p/ug-login-page/ instead.
    return "login.aliexpress" in page.url or "login-page" in page.url or any(
        "coin" in c["api"] and "SESSION_EXPIRED" in (c["body"] or "") for c in mtop)


def task_title(m):
    return " ".join(m.get("mainTitle", "").split())


def task_done(m):
    return m.get("instanceStatus") == 2 or m.get("timesJoined", 0) >= m.get("timesLimit", 1)


def skip_reason(m):
    """Why a coin task is left alone, or None if the add-on may do it."""
    text = " ".join(str(m.get(k) or "") for k in ("mainTitle", "secondTitle", "materialUrl", "behaviorConfig"))
    if AD_RE.search(text):
        return "ads"
    if m.get("type") not in SAFE_TASK_TYPES:
        return f"type {m.get('type')}: game, order or app-only"
    if m.get("type") == 0 and not m.get("materialUrl"):
        return "no page to open"
    return None


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


async def load_task_list(page, run):
    """Reload the coins page and return its coin tasks (None if the list never arrives)."""
    for _ in range(2):  # the Pi sometimes needs more than 90 s for this page
        run["tasks"] = None
        try:
            await page.goto(COIN_URL, wait_until="domcontentloaded", timeout=90000)
        except PlaywrightError:
            continue
        for _ in range(60):
            if run["tasks"] is not None:
                return [m for g in run["tasks"] for m in g.get("materials", [])]
            await page.wait_for_timeout(1000)
    return None


async def do_task(ctx, page, m):
    """Tap a task in the list and stay on the page it opens for as long as it asks."""
    loc = page.get_by_text(task_title(m))  # substring: exact matching misses some titles
    try:
        await loc.first.wait_for(timeout=45000)
    except PlaywrightError:
        # Only a few unfinished tasks are listed; the rest are in the "Earn more coins" drawer.
        await page.get_by_text("Earn more coins").first.click(force=True, timeout=10000)
        await loc.first.wait_for(timeout=20000)
    await loc.first.scroll_into_view_if_needed(timeout=20000)
    await loc.first.click(force=True, timeout=15000)
    try:
        secs = int(json.loads(m.get("behaviorConfig") or "{}").get("time") or 15)
    except (ValueError, TypeError, AttributeError):
        secs = 15
    await page.wait_for_timeout(4000)
    target = ctx.pages[-1]  # some tasks open a new tab
    for _ in range((secs + 12) // 3):  # browse: scroll a little every 3 s
        try:
            await target.evaluate("window.scrollBy(0, 350)")
        except PlaywrightError:
            pass
        await page.wait_for_timeout(3000)
    for p in ctx.pages[1:]:
        await p.close()


def allowed(m, force):
    """Whether this run may do the task: safe ones always, forced ones unless they are ads."""
    why = skip_reason(m)
    return why is None or (why != "ads" and str(m.get("taskId")) in force)


async def do_tasks(ctx, page, run, force=frozenset()):
    """Do every allowed coin task until none is left; each one done can unlock another."""
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    tries = collections.Counter()
    deadline = time.monotonic() + TASKS_BUDGET
    tasks, new, failures = [], {}, []
    earned_before = sum(run["earned"])
    while time.monotonic() < deadline:
        tasks = await load_task_list(page, run)
        if tasks is None:
            failures.append("The task list did not load")
            break
        for m in tasks:
            tid = str(m.get("taskId"))
            if tid not in seen:
                seen[tid] = task_title(m)
                if not first_run:
                    new[task_title(m)] = skip_reason(m) or "done automatically"
        todo = [m for m in tasks if not task_done(m) and allowed(m, force)
                and tries[m.get("taskId")] < TASK_TRIES]
        if not todo:
            break
        m = todo[0]
        tries[m.get("taskId")] += 1
        try:
            await do_task(ctx, page, m)
        except PlaywrightError as e:
            failures.append(f"{task_title(m)}: {type(e).__name__}")
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, ensure_ascii=False)
    tasks = tasks or []
    run["log"] = failures
    items = [{"id": str(m.get("taskId")), "title": task_title(m), "why": skip_reason(m),
              "state": "done" if task_done(m) else "failed" if allowed(m, force) else "skipped"}
             for m in tasks]
    return {
        "done": [i["title"] for i in items if i["state"] == "done"],
        "skipped": {i["title"]: i["why"] for i in items if i["state"] == "skipped"},
        "failed": [i["title"] for i in items if i["state"] == "failed"],
        "items": items,
        "new": [f"{t} ({why})" for t, why in new.items()],
        "earned": sum(run["earned"]) - earned_before,
    }


async def run_tasks(ctx, page, run, force=frozenset()):
    today = now().date().isoformat()
    st = load_status().get("tasks") or {}
    coins = st.get("coins", 0) if st.get("date") == today else 0
    await save_tasks(status="running", date=today, coins=coins)
    try:
        r = await do_tasks(ctx, page, run, force)
    except Exception as e:  # never let the extras turn a good check-in into an error
        await save_tasks(status="error", detail=f"{type(e).__name__}: {e}"[:500],
                         checked_at=now().isoformat(timespec="seconds"))
        return
    await save_tasks(status="finished", detail="; ".join(run["log"])[:500], coins=coins + r["earned"],
                     done=r["done"], skipped=r["skipped"], failed=r["failed"], items=r["items"],
                     new=r["new"], checked_at=now().isoformat(timespec="seconds"))


async def collect(tasks_only=False, force=frozenset()):
    """Check in (or, with tasks_only, skip straight to the coin tasks).

    Returns (status, detail) for the check-in, or None when tasks_only ran the tasks.
    """
    mtop = []
    run = {"tasks": None, "earned": [], "log": []}

    async def on_response(r):
        if "acs.aliexpress.com/h5/mtop" not in r.url:
            return
        try:
            body = await r.text()
        except Exception:
            body = None
        api = r.url.split("/h5/")[1].split("/")[0]
        try:
            if body and "task.delivery.query" in api:
                run["tasks"] = json.loads(body)["data"]["result"]
            elif body and "task.process.complete" in api:
                res = json.loads(body)["data"]["result"]
                if res.get("completeSuccess"):
                    run["earned"].append(sum(int(i.get("interestNum") or 0)
                                             for i in (res.get("interestIssueResultMap") or {}).values()))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        if "stay.time.report" not in api:  # a heartbeat every few seconds while browsing
            mtop.append({"api": api, "status": r.status, "body": body and body[:3000]})

    async with async_playwright() as pw:
        ctx = await launch(pw)
        page, clicked = None, None
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            ctx.on("response", on_response)  # the context: some tasks open new tabs
            # The logged-in page polls forever, so never wait for networkidle.
            await page.goto(COIN_URL, wait_until="load", timeout=90000)
            await page.wait_for_timeout(12000)
            before = await page.inner_text("body")
            if is_login_wall(page, mtop):
                result = ("session_expired", "The coins page asked for a login")
            elif tasks_only:
                result = None
            elif DONE_RE.search(before):
                result = ("collected", "The coins page shows today's check-in as done")
            else:
                n_before = len(mtop)
                for loc in (page.get_by_role("button", name=BUTTON_RE), page.get_by_text(BUTTON_RE)):
                    if await loc.count():
                        clicked = (await loc.first.inner_text()).strip()
                        # force: the "Collect" button pulses (CSS scale animation), so
                        # Playwright's wait-for-stable check never passes.
                        await loc.first.click(timeout=10000, force=True)
                        await page.wait_for_timeout(8000)
                        break
                after = await page.inner_text("body")
                # Only the sign API counts; stay.time.report also answers SUCCESS every few seconds.
                signed = [c for c in mtop[n_before:] if "sign.execute" in c["api"]
                          and '"issuePrizeSuccess":true' in (c["body"] or "")]
                if clicked and (signed or DONE_RE.search(after)):
                    result = ("collected", f"Clicked '{clicked}'")
                elif clicked:
                    result = ("error", f"Clicked '{clicked}' but got no confirmation")
                else:
                    result = ("button_not_found", "No check-in button on the coins page")
            if result is None:
                await run_tasks(ctx, page, run, force)
            elif EXTRA_TASKS and result[0] == "collected":
                await save_status(*result)  # report the check-in now; the tasks take minutes
                await run_tasks(ctx, page, run)
        finally:
            # Saved on failures too: they are the runs that need debugging.
            if page:
                try:
                    await page.screenshot(path=SHOT_FILE)
                    text = (await page.inner_text("body"))[:5000]
                except Exception as e:
                    text = f"(page unreadable: {e})"
                with open(RUN_FILE, "w") as f:
                    json.dump({"when": now().isoformat(), "url": page.url, "clicked": clicked,
                               "text": text, "task_log": run["log"], "mtop": mtop},
                              f, ensure_ascii=False, indent=1)
            await ctx.close()
    return result


async def run_collect(tasks_only=False, force=frozenset()):
    if lock.locked() and login:
        await stop_login()
    async with lock:
        if tasks_only:
            await save_tasks(status="running")  # the check-in state stays as it is
        else:
            await save_status("running", "")
        try:
            result = await collect(tasks_only, force)
        except Exception as e:
            result = "error", f"{type(e).__name__}: {e}"[:1000]
        if result is None:
            return
        if tasks_only:  # the tasks never started: say why there, not in the check-in state
            await save_tasks(status="error", detail=result[1][:500],
                             checked_at=now().isoformat(timespec="seconds"))
            if result[0] != "session_expired":
                return
        await save_status(*result)


def start_collect(tasks_only=False, force=frozenset()):
    """Start a check-in (or a tasks-only run) in the background; False if a run is going."""
    if lock.locked() and not login:
        return False
    task = asyncio.create_task(run_collect(tasks_only, force))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


async def started(request, ok):
    # Returns at once (HA's shell_command is killed after 60 s); the result
    # lands in the sensor when the run finishes.
    if not ok:
        raise web.HTTPConflict(text="A check-in is already running")
    await asyncio.sleep(0.5)
    if ingress(request):  # the panel's button, not an HA call
        raise web.HTTPFound(f"{ingress(request)}/")
    return web.json_response({"started": True}, status=202)


async def api_collect(request):
    return await started(request, start_collect())


async def api_tasks(request):
    form = await request.post()
    force = frozenset(form.getall("force", []) + request.query.getall("force", []))
    return await started(request, start_collect(tasks_only=True, force=force))


async def stdin_listener():
    # hassio.addon_stdin writes its input JSON-encoded, possibly without a newline.
    loop = asyncio.get_running_loop()
    while data := await loop.run_in_executor(None, os.read, 0, 4096):
        if b"collect" in data.lower():
            print("stdin: collect" if start_collect() else "stdin: a check-in is already running")
        elif b"tasks" in data.lower():
            print("stdin: tasks" if start_collect(tasks_only=True) else "stdin: a check-in is already running")


def ingress(request):
    return request.headers.get("X-Ingress-Path", "")


async def login_start(request):
    async with start_lock:
        if not login:
            if lock.locked():
                raise web.HTTPConflict(text="A check-in is running, try again in a minute")
            await lock.acquire()
            try:
                vnc = subprocess.Popen(["x11vnc", "-display", DISPLAY, "-forever", "-shared",
                                        "-nopw", "-localhost", "-rfbport", "5900", "-quiet"])
                # A plain browser, not driven by Playwright: AliExpress's login
                # captcha rejects automation-controlled browsers (error:aaf1he).
                clear_profile_locks()
                chrome = subprocess.Popen([CHROME, f"--user-data-dir={PROFILE}", "--no-sandbox",
                                           *CHROME_ARGS, COIN_URL],
                                          env={**os.environ, "DISPLAY": DISPLAY})
            except Exception:
                lock.release()
                raise
            login.update(chrome=chrome, vnc=vnc, timer=asyncio.create_task(login_timeout()))
            await asyncio.sleep(3)
    path = f"{ingress(request).lstrip('/')}/websockify"
    raise web.HTTPFound(f"{ingress(request)}/novnc/vnc.html?autoconnect=1&resize=scale"
                        f"&reconnect=1&path={path}")


async def login_stop(request):
    await stop_login()
    raise web.HTTPFound(f"{ingress(request)}/")


async def websockify(request):
    ws = web.WebSocketResponse(protocols=("binary",))
    await ws.prepare(request)
    reader, writer = await asyncio.open_connection("127.0.0.1", 5900)

    async def vnc_to_ws():
        while data := await reader.read(65536):
            await ws.send_bytes(data)
        await ws.close()

    pump = asyncio.create_task(vnc_to_ws())
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            writer.write(msg.data)
            await writer.drain()
    pump.cancel()
    writer.close()
    return ws


def fmt_time(iso):
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


async def index(request):
    lang = "pt" if request.headers.get("Accept-Language", "").lower().startswith("pt") else "en"
    t = TEXT[lang]
    st = load_status()
    status = st.get("status", "never_run")
    tk = st.get("tasks") or {}
    tasks_running = tk.get("status") == "running"
    base = ingress(request)

    def button(path, label, cls="", method="post", hidden=None):
        if method == "get":
            return f'<a class="btn {cls}" href="{base}{path}">{label}</a>'
        fields = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">'
                         for k, v in (hidden or {}).items())
        return (f'<form method="post" action="{base}{path}">{fields}'
                f'<button class="btn {cls}">{label}</button></form>')

    idle = not login and status != "running" and not tasks_running
    if login:
        actions = (f'<p class="note">{t["login_open"]}</p>'
                   + button("/login/start", t["back"], "primary", "get") + button("/login/stop", t["close"]))
    elif not idle:
        actions = ""
    else:
        actions = button("/api/collect", t["collect"], "primary") + button("/login/start", t["login"])
    shot = ""
    if os.path.exists(SHOT_FILE):
        shot = (f'<details><summary>{t["screenshot"]}</summary>'
                f'<img src="{base}/last.png?{html.escape(str(st.get("checked_at")))}" alt=""></details>')
    extra, task_list = "", ""
    if EXTRA_TASKS:
        coins = tk.get("coins", 0) if tk.get("date") == now().date().isoformat() else 0
        extra = f"+{coins} {t['coins']}"
        if tasks_running:
            extra += f" · {t['tasks_running']}"
        elif tk.get("status") == "error":
            extra += f" · {t['tasks_error']}"
        extra = f"<div><dt>{t['extra']}</dt><dd>{html.escape(extra)}</dd></div>"
        items = tk.get("items")
        if items is not None:
            # Not done first, then skipped, then done: what still earns coins on top.
            order = {"failed": 0, "skipped": 1, "done": 2}
            rows = []
            for i in sorted(items, key=lambda i: order.get(i["state"], 0)):
                why = i.get("why")
                note = t["r_ads"] if why == "ads" else f'{t["r_other"]} ({why})' if why else ""
                force = (button("/api/tasks", t["force"], "mini", hidden={"force": i["id"]})
                         if idle and i["state"] == "skipped" and why != "ads" else "")
                rows.append(f'<li><span class="chip {i["state"]}">{t["s_" + i["state"]]}</span>'
                            f'<span class="title">{html.escape(i["title"])}'
                            f'{f"<small>{html.escape(note)}</small>" if note else ""}</span>{force}</li>')
            body = f'<ul class="list">{"".join(rows)}</ul>' if rows else f'<p class="note">{t["no_tasks"]}</p>'
            if tk.get("new"):
                body += f'<h3>{t["t_new"]}</h3><ul>{"".join(f"<li>{html.escape(n)}</li>" for n in tk["new"])}</ul>'
        else:  # status saved by 0.4.0, before items existed
            sections = [(t["t_new"], tk.get("new") or []), (t["t_done"], tk.get("done") or []),
                        (t["t_failed"], tk.get("failed") or []),
                        (t["t_skipped"], [f"{k} ({v})" for k, v in (tk.get("skipped") or {}).items()])]
            body = "".join(f"<h3>{h}</h3><ul>{''.join(f'<li>{html.escape(i)}</li>' for i in items)}</ul>"
                           for h, items in sections if items) or f'<p class="note">{t["no_tasks"]}</p>'
        if tk.get("detail"):
            body += f'<p class="note">{html.escape(tk["detail"])}</p>'
        if any(i.get("why") == "ads" and i["state"] != "done" for i in items or []):
            body += f'<p class="note">{t["ads_note"]}</p>'
        if idle:
            body += (f'<div class="actions">{button("/api/tasks", t["retry"], "primary")}'
                     f'{button("/login/start", t["by_hand"], "", "get")}</div>')
        task_list = f'<section class="card tasks"><h2>{t["tasks"]}</h2>{body}</section>'
    with open(PANEL, encoding="utf-8") as f:
        template = string.Template(f.read())
    page = template.substitute(
        lang="pt-BR" if lang == "pt" else "en",
        refresh=('<meta http-equiv="refresh" content="5">' if status == "running" else
                 '<meta http-equiv="refresh" content="15">' if tasks_running else ""),
        tone=TONE.get(status, "idle"),
        status=html.escape(t.get(status, status)),
        detail=html.escape(st.get("detail") or ""),
        last_success_label=t["last_success"],
        last_success=html.escape(st.get("last_success") or t["never"]),
        checked_at_label=t["checked_at"],
        checked_at=html.escape(fmt_time(st.get("checked_at")) or t["never"]),
        login_expires_label=t["login_expires"],
        login_expires=html.escape((fmt_time(login_expires()) or "")[:10] or t["unknown"]),
        extra=extra,
        actions=actions,
        tasks=task_list,
        shot=shot,
        help=t["help"],
    )
    return web.Response(content_type="text/html", text=page)


async def last_png(request):
    return web.FileResponse(SHOT_FILE)


async def keepalive():
    # HA forgets API-set states on restart; re-push periodically.
    while True:
        await push_state(load_status())
        await asyncio.sleep(600)


async def on_startup(app):
    os.makedirs(PROFILE, exist_ok=True)
    app["xvfb"] = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"])
    app["keepalive"] = asyncio.create_task(keepalive())
    app["stdin"] = asyncio.create_task(stdin_listener())


app = web.Application()
app.on_startup.append(on_startup)
app.router.add_get("/", index)
app.router.add_route("*", "/login/start", login_start)
app.router.add_post("/login/stop", login_stop)
app.router.add_get("/websockify", websockify)
app.router.add_post("/api/collect", api_collect)
app.router.add_post("/api/tasks", api_tasks)
app.router.add_get("/last.png", last_png)
app.router.add_static("/novnc/", "/usr/share/novnc")
web.run_app(app, port=8099)
