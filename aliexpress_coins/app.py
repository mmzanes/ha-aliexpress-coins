"""AliExpress coins check-in add-on.

One Chromium profile in /data/profile, always headed inside Xvfb. Login runs it
as a plain browser (streamed over noVNC through ingress); the daily check-in
drives the same binary and profile through Playwright.

  GET  /              status page (ingress panel)
  POST /login/start   open the coins page and expose the screen over noVNC
  POST /login/stop    close the login browser
  GET  /websockify    noVNC websocket <-> x11vnc bridge
  POST /api/collect   run the check-in (HA shell_command / rest_command)
  stdin "collect"     same, via the hassio.addon_stdin action
"""
import asyncio
import datetime
import glob
import html
import json
import os
import re
import string
import subprocess

from aiohttp import ClientSession, WSMsgType, web
from playwright.async_api import async_playwright

DATA = "/data"
PROFILE = f"{DATA}/profile"
STATUS_FILE = f"{DATA}/status.json"
RUN_FILE = f"{DATA}/last_run.json"
SHOT_FILE = f"{DATA}/last.png"
PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")
COIN_URL = "https://m.aliexpress.com/p/coin-index/index.html"
DISPLAY = ":99"
W, H = 412, 915  # phone-shaped screen so the noVNC view fits a phone
CHROME = sorted(glob.glob("/ms-playwright/chromium-*/chrome-linux/chrome"))[-1]
LOGIN_TIMEOUT = 15 * 60
SENSOR = "sensor.aliexpress_coins"


def load_options():
    try:
        with open(f"{DATA}/options.json") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


LOCALE = load_options().get("locale") or "en-US"
TIMEZONE = os.environ.get("TZ") or None  # Supervisor passes Home Assistant's time zone
CHROME_ARGS = ["--kiosk", "--no-first-run", f"--lang={LOCALE}", f"--window-size={W},{H}", "--window-position=0,0"]

# Whole-label matches only: the page is full of banners like "Collect, shop and save".
BUTTON_RE = re.compile(r"^\s*((fazer\s+)?check.?in|coletar|resgatar|collect|claim)\s*!?\s*$", re.I)
# Logged-in mobile page after today's check-in: "Today ✓ 10", "Get 15 check-in coins tomorrow!"
DONE_RE = re.compile(r"today\s*✓|hoje\s*✓|check-in coins tomorrow|moedas de check-in amanh", re.I)

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


async def push_state(st):
    attrs = {
        "friendly_name": "AliExpress coins",
        "icon": "mdi:hand-coin",
        "last_success": st.get("last_success"),
        "detail": st.get("detail"),
        "checked_at": st.get("checked_at"),
    }
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


async def collect():
    mtop = []

    async def on_response(r):
        if "acs.aliexpress.com/h5/mtop" not in r.url:
            return
        try:
            body = (await r.text())[:3000]
        except Exception:
            body = None
        mtop.append({"api": r.url.split("/h5/")[1].split("/")[0], "status": r.status, "body": body})

    async with async_playwright() as pw:
        ctx = await launch(pw)
        page, clicked = None, None
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("response", on_response)
            # The logged-in page polls forever, so never wait for networkidle.
            await page.goto(COIN_URL, wait_until="load", timeout=90000)
            await page.wait_for_timeout(12000)
            before = await page.inner_text("body")
            if is_login_wall(page, mtop):
                result = ("session_expired", "The coins page asked for a login")
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
                               "text": text, "mtop": mtop}, f, ensure_ascii=False, indent=1)
            await ctx.close()
    return result


async def run_collect():
    if lock.locked() and login:
        await stop_login()
    async with lock:
        await save_status("running", "")
        try:
            status, detail = await collect()
        except Exception as e:
            status, detail = "error", f"{type(e).__name__}: {e}"[:1000]
        await save_status(status, detail)


def start_collect():
    """Start a check-in in the background; False if one is already running."""
    if lock.locked() and not login:
        return False
    task = asyncio.create_task(run_collect())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


async def api_collect(request):
    # Returns at once (HA's shell_command is killed after 60 s); the result
    # lands in the sensor when the run finishes.
    if not start_collect():
        raise web.HTTPConflict(text="A check-in is already running")
    await asyncio.sleep(0.5)
    if ingress(request):  # the panel's button, not an HA call
        raise web.HTTPFound(f"{ingress(request)}/")
    return web.json_response({"started": True}, status=202)


async def stdin_listener():
    # hassio.addon_stdin writes its input JSON-encoded, possibly without a newline.
    loop = asyncio.get_running_loop()
    while data := await loop.run_in_executor(None, os.read, 0, 4096):
        if b"collect" in data.lower():
            print("stdin: collect" if start_collect() else "stdin: a check-in is already running")


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
    base = ingress(request)

    def button(path, label, cls="", method="post"):
        if method == "get":
            return f'<a class="btn {cls}" href="{base}{path}">{label}</a>'
        return (f'<form method="post" action="{base}{path}">'
                f'<button class="btn {cls}">{label}</button></form>')

    if login:
        actions = (f'<p class="note">{t["login_open"]}</p>'
                   + button("/login/start", t["back"], "primary", "get") + button("/login/stop", t["close"]))
    elif status == "running":
        actions = ""
    else:
        actions = button("/api/collect", t["collect"], "primary") + button("/login/start", t["login"])
    shot = ""
    if os.path.exists(SHOT_FILE):
        shot = (f'<details><summary>{t["screenshot"]}</summary>'
                f'<img src="{base}/last.png?{html.escape(str(st.get("checked_at")))}" alt=""></details>')
    with open(PANEL, encoding="utf-8") as f:
        template = string.Template(f.read())
    page = template.substitute(
        lang="pt-BR" if lang == "pt" else "en",
        refresh='<meta http-equiv="refresh" content="5">' if status == "running" else "",
        tone=TONE.get(status, "idle"),
        status=html.escape(t.get(status, status)),
        detail=html.escape(st.get("detail") or ""),
        last_success_label=t["last_success"],
        last_success=html.escape(st.get("last_success") or t["never"]),
        checked_at_label=t["checked_at"],
        checked_at=html.escape(fmt_time(st.get("checked_at")) or t["never"]),
        actions=actions,
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
app.router.add_get("/last.png", last_png)
app.router.add_static("/novnc/", "/usr/share/novnc")
web.run_app(app, port=8099)
