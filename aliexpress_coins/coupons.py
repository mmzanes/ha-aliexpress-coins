"""Apply a list of AliExpress coupon codes to one product, so they are saved on the account.

The flow, all in the add-on's logged-in Chromium:
  1. open the product page and tap "Add to cart" (choosing the first option of any
     variant the sheet asks for);
  2. tap "Buy now" to reach the order confirmation page with only this product,
     so the cart's other items don't change the order total;
  3. type each code into the promo-code box and tap Apply, recording what
     AliExpress answered.

No order is ever placed: the add-on never taps a place-order or pay button, and
it aborts every request to AliExpress's order-creation APIs as a second guard.
"""
import json
import re

from playwright.async_api import Error as PlaywrightError

# A code: 5-20 letters and digits with at least one of each ("AFFNOV01", "PayPal1").
CODE_RE = re.compile(r"(?<![\w$.,])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{5,20}(?![\w])")
# "para R$ 16 de desconto em compras a partir de R$ 80" / "$5 off orders over $40"
MONEY = r"(?:R\$|US\$|\$|€)\s*([\d.,]+)"
TERMS_RE = re.compile(MONEY + r".{0,40}?(?:a partir de|acima de|over|from|min\.?)\s*" + MONEY, re.I)
MAX_CODES = 80

# Buttons, matched on their whole label.
ADD_TO_CART_RE = re.compile(r"^\s*(adicionar ao carrinho|add to cart|adicionar)\s*$", re.I)
BUY_NOW_RE = re.compile(r"^\s*(comprar agora|buy now|comprar)\s*$", re.I)
SHEET_CONFIRM_RE = re.compile(r"^\s*(adicionar ao carrinho|add to cart|comprar agora|buy now|"
                              r"confirmar|confirm|ok|continuar|continue)\s*$", re.I)
APPLY_RE = re.compile(r"^\s*(aplicar|apply|resgatar|redeem|usar|use)\s*$", re.I)
# Opens the promo-code box on the order page.
CODE_ENTRY_RE = re.compile(r"c[óo]digo\s*(promocional|do cupom|de cupom)|promo\s*code|coupon\s*code|"
                           r"(inserir|digitar|adicionar)\s+c[óo]digo|enter\s+(a\s+)?code|"
                           r"cupons?\s+(do\s+)?aliexpress|aliexpress\s+coupons?", re.I)
CODE_PLACEHOLDER_RE = re.compile(r"c[óo]digo|code|cupom|coupon|promo", re.I)
# Never clicked, whatever else matches.
PLACE_ORDER_RE = re.compile(r"fazer pedido|finalizar pedido|place order|pagar|pay\b|pay now|"
                            r"confirmar (e )?pag|checkout", re.I)
# Requests that would create an order or pay: aborted for the whole coupon run.
ORDER_API_RE = re.compile(r"acs\.aliexpress\.com/h5/[^/]*(place\.?order|order\.create|createorder|"
                          r"cashier|\.pay\.)", re.I)
ORDER_PAGE_RE = re.compile(r"/trade/confirm|/p/trade/|order[-_]?confirm|placeorder", re.I)

# How to read AliExpress's answer to a code.
OK_RE = re.compile(r"aplicad|applied|resgatad|redeemed|sucesso|success|economiz|you saved|"
                   r"desconto de|cupom adicionado|coupon added", re.I)
MIN_RE = re.compile(r"m[íi]nim|minimum|n[ãa]o atinge|not (yet )?(meet|reach)|threshold|"
                    r"compras acima|spend (at least|more)", re.I)
BAD_RE = re.compile(r"inv[áa]lid|n[ãa]o existe|does not exist|not exist|expir|expired|esgotad|"
                    r"used up|sold out|j[áa] (foi )?usad|already (been )?used|limite|limit|"
                    r"n[ãa]o (é )?(aplic|v[áa]lid)|not (applicable|valid|eligible)|"
                    r"incorret|incorrect|wrong|erro|error", re.I)


def parse_codes(text):
    """[{code, discount, minimum}] from pasted text, in order, without repeats.

    Lines like "AFFNOV01 para R$ 16 de desconto em compras a partir de R$ 80" keep
    their terms; bare lists ("HARDMOB1 / estadao1 / GLOBO1") just give the codes.
    """
    out, seen = [], set()
    for line in (text or "").splitlines():
        terms = TERMS_RE.search(line)
        for code in CODE_RE.findall(line):
            if code.lower() in seen:
                continue
            seen.add(code.lower())
            item = {"code": code}
            if terms:
                item.update(discount=terms.group(1), minimum=terms.group(2))
            out.append(item)
    return out[:MAX_CODES]


def classify(msg):
    """accepted, minimum, rejected or unknown, from what the page or API said."""
    if not msg:
        return "unknown"
    if MIN_RE.search(msg):
        return "minimum"
    if BAD_RE.search(msg):
        return "rejected"
    if OK_RE.search(msg):
        return "accepted"
    return "unknown"


async def safe_click(loc, timeout=15000):
    """Click, unless the element's label looks like placing the order or paying."""
    label = ""
    try:
        label = (await loc.inner_text(timeout=3000)).strip()
    except PlaywrightError:
        pass
    if PLACE_ORDER_RE.search(label):
        raise RuntimeError(f"refused to click '{label[:40]}'")
    await loc.click(timeout=timeout, force=True)


async def visible(page, regex):
    loc = page.get_by_text(regex).filter(visible=True)
    return loc if await loc.count() else None


async def pick_variants(page, log):
    """In the SKU sheet, choose the first option of each property that has none chosen."""
    try:
        picked = await page.evaluate("""() => {
            const out = [];
            const groups = document.querySelectorAll('[class*="sku-property"], [class*="skuProperty"], [class*="sku-item"]');
            for (const g of groups) {
                if (g.querySelector('[class*="selected"], [class*="active"], [aria-checked="true"]')) continue;
                const opt = g.querySelector('[class*="value"]:not([class*="disabled"]), [role="radio"]:not([aria-disabled="true"])');
                if (opt) { opt.click(); out.push((opt.innerText || opt.getAttribute('title') || '').trim().slice(0, 40)); }
            }
            return out;
        }""")
    except PlaywrightError:
        picked = []
    if picked:
        log.append("Variant chosen: " + ", ".join(p or "(image)" for p in picked))
        await page.wait_for_timeout(1500)


async def tap_and_confirm(page, regex, log, what):
    """Tap a product-page button; if a variant sheet opens, fill it and confirm."""
    btn = await visible(page, regex)
    if not btn:
        return False
    await safe_click(btn.last)
    await page.wait_for_timeout(3000)
    if ORDER_PAGE_RE.search(page.url):
        log.append(f"Tapped '{what}'")
        return True
    # A sheet asking for the variant and quantity, with its own confirm button.
    await pick_variants(page, log)
    confirm = await visible(page, SHEET_CONFIRM_RE)
    if confirm:
        await safe_click(confirm.last)
        await page.wait_for_timeout(4000)
    log.append(f"Tapped '{what}'")
    return True


async def find_code_input(page):
    """The promo-code text box on the order page, opening its section if needed."""
    for attempt in range(3):
        for inp in await page.locator("input:visible, textarea:visible").all():
            hint = " ".join(filter(None, [await inp.get_attribute("placeholder"),
                                          await inp.get_attribute("aria-label"),
                                          await inp.get_attribute("name")]))
            if CODE_PLACEHOLDER_RE.search(hint):
                return inp
        entry = await visible(page, CODE_ENTRY_RE)
        if entry is None or attempt == 2:
            if attempt == 0:  # the section can be further down the order page
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
                continue
            return None
        await entry.first.scroll_into_view_if_needed(timeout=10000)
        await safe_click(entry.first)
        await page.wait_for_timeout(2500)
    return None


async def body_lines(page):
    try:
        return [l.strip() for l in (await page.inner_text("body")).splitlines() if l.strip()]
    except PlaywrightError:
        return []


WATCH_JS = """() => {
    window.__cpMsgs = [];
    if (window.__cpObs) window.__cpObs.disconnect();
    const grab = n => {
        const el = n.nodeType === 3 ? n.parentElement : n;
        const t = el && el.innerText !== undefined ? el.innerText : (n.textContent || '');
        if (t && t.trim() && t.length < 300) window.__cpMsgs.push(t.trim());
    };
    window.__cpObs = new MutationObserver(ms => { for (const m of ms) {
        if (m.type === 'characterData') grab(m.target);
        m.addedNodes.forEach(grab);
    }});
    window.__cpObs.observe(document.body, {childList: true, subtree: true, characterData: true});
}"""


async def apply_code(page, code, mtop):
    """Type one code and tap Apply. Returns {state, msg}."""
    inp = await find_code_input(page)
    if inp is None:
        return {"state": "unknown", "msg": "Promo code box not found on the order page"}
    before = set(await body_lines(page))
    n = len(mtop)
    await inp.fill("")
    await inp.fill(code)
    try:
        await page.evaluate(WATCH_JS)  # catches toasts, and a message repeated from the last code
    except PlaywrightError:
        pass
    apply = await visible(page, APPLY_RE)
    if apply:
        await safe_click(apply.last)
    else:
        await inp.press("Enter")
    await page.wait_for_timeout(5000)
    # A toast or inline message that wasn't there before, plus what the APIs said.
    try:
        watched = await page.evaluate("() => window.__cpMsgs || []")
    except PlaywrightError:
        watched = []
    seen_lines = [l.strip() for t in watched for l in t.splitlines() if l.strip()]
    new = [l for l in dict.fromkeys(seen_lines + [l for l in await body_lines(page) if l not in before])
           if code.lower() not in l.lower() and len(l) < 200 and not APPLY_RE.match(l)]
    # Prefer lines that read like an answer to the code; a re-rendered order summary is noise.
    new = [l for l in new if classify(l) != "unknown"] or new
    api_msgs = []
    for c in mtop[n:]:
        try:
            j = json.loads(c["body"] or "{}")
        except ValueError:
            continue
        data = j.get("data") or {}
        for k in ("errorMsg", "errorMessage", "message", "msg", "toast", "tips"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, str) and v.strip():
                api_msgs.append(v.strip())
        api_msgs += [r.split("::", 1)[-1] for r in j.get("ret") or [] if "SUCCESS" not in r]
    msg = " · ".join(dict.fromkeys(api_msgs + new[:2]))[:200]
    return {"state": classify(msg), "msg": msg}


async def open_page(page, url, log, ready=None, what="page"):
    """Go to url without waiting for "load": product and order pages keep loading ads and
    trackers for minutes. Waits for the DOM, then for a `ready` element if given."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except PlaywrightError as e:
        if "Timeout" not in str(e):
            raise
        log.append(f"{what} slow to load, carrying on")
    if ready is not None:
        for _ in range(45):
            if await visible(page, ready):
                return True
            await page.wait_for_timeout(1000)
        return False
    await page.wait_for_timeout(5000)
    return True


async def run(ctx, page, url, codes, mtop, on_progress):
    """Add the product to the cart, open its order page and apply each code.

    on_progress(items) is awaited after every code. Returns (status, detail, items).
    """
    log = []
    items = [{**c, "state": "pending", "msg": ""} for c in codes]
    try:
        return await _run(ctx, page, url, items, mtop, on_progress, log)
    except (PlaywrightError, RuntimeError) as e:
        first = (str(e).splitlines() or [""])[0][:200]
        return "error", "; ".join([f"{type(e).__name__}: {first}"] + log + [f"at {page.url[:120]}"])[:500], items


async def _run(ctx, page, url, items, mtop, on_progress, log):
    async def block_order(route):
        log.append("Blocked an order request")
        await route.abort()

    await ctx.route(ORDER_API_RE, block_order)
    buttons = re.compile(ADD_TO_CART_RE.pattern + "|" + BUY_NOW_RE.pattern, re.I)
    found = await open_page(page, url, log, buttons, "Product page")
    if "login" in page.url:
        return "session_expired", "AliExpress asked for a login", items
    if not found:
        return "error", "; ".join(log + ["No 'Add to cart' or 'Buy now' button appeared on the product page",
                                          f"at {page.url[:120]}"])[:500], items
    if not await tap_and_confirm(page, ADD_TO_CART_RE, log, "Add to cart"):
        log.append("No 'Add to cart' button on the product page")
    await open_page(page, url, log, BUY_NOW_RE, "Product page")  # closes the sheet / cart popup
    if not await tap_and_confirm(page, BUY_NOW_RE, log, "Buy now"):
        return "error", "; ".join(log + ["No 'Buy now' button on the product page"]), items
    for _ in range(40):
        if await find_code_input(page) or (ORDER_PAGE_RE.search(page.url) and await visible(page, CODE_ENTRY_RE)):
            break
        await page.wait_for_timeout(1500)
    else:
        return "error", "; ".join(log + [f"The order page did not open or has no promo-code box (at {page.url[:120]})"])[:500], items
    log.append("Order page open")
    order_url = page.url
    for it in items:
        try:
            it.update(await apply_code(page, it["code"], mtop))
        except (PlaywrightError, RuntimeError) as e:
            it.update(state="unknown", msg=f"{type(e).__name__}: {str(e).splitlines()[0][:150]}")
        if not ORDER_PAGE_RE.search(page.url) and ORDER_PAGE_RE.search(order_url):
            await open_page(page, order_url, log, None, "Order page")  # a sheet navigated away
        await on_progress(items)
    counts = {s: sum(i["state"] == s for i in items) for s in ("accepted", "minimum", "rejected", "unknown")}
    detail = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
    return "finished", "; ".join([detail] + log)[:500], items
