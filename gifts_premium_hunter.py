import asyncio, os, re, json, logging, time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient
from playwright.async_api import async_playwright, Page, Frame

load_dotenv()

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
SESSION = os.getenv("SESSION_NAME", "user_gifts_session")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))           # сек между сканами
MAX_BUYS_PER_CYCLE = int(os.getenv("MAX_BUYS_PER_CYCLE", "5"))    # покупок за цикл
PREMIUM_WORDS = [w.strip().lower() for w in os.getenv("PREMIUM_WORDS", "premium,премиум").split(",")]

STORAGE = "tg_storage_state.json"       # playwright сессия web.telegram.org
LOG = logging.getLogger("hunter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

# --- селекторы (подогнаны под типовую верстку мини‑аппа подарков).
# Если что-то не совпадёт — пришли скрин из web, поправлю.
CARD_ITEM = "[data-test-id='gift-card'], .gift-card, [class*='giftCard'], [class*='GiftCard']"
CARD_TITLE = ".title, [data-test-id='gift-title'], [class*='Title']"
CARD_BADGE = ".badge, .label, [data-test-id='gift-badge'], [class*='Badge']"
CARD_FRAME = ".card, .frame, .container, [class*='card']"

BUY_BTN_LIST = "button:has-text('Купить'), button:has-text('Buy'), button:has-text('Отправить'), button:has-text('Send')"  # на первом экране внутри карточки
CONFIRM_BUY_BTN = "button:has-text('ОТПРАВИТЬ ПОДАРОК'), button:has-text('Отправить подарок'), button:has-text('Send gift')" # второй экран
ALL_TAB = "button:has-text('Все подарки'), button:has-text('All gifts')"

GIFT_ENTRY_URL = "https://t.me/gifts"   # открываем, жмём "Open" → webview мини‑аппа

# --- антидубль на диск
BOUGHT_FILE = Path("bought_titles.json")
def load_bought():
    if BOUGHT_FILE.exists():
        try:
            return set(json.loads(BOUGHT_FILE.read_text("utf-8")))
        except Exception:
            return set()
    return set()
def save_bought(s):
    BOUGHT_FILE.write_text(json.dumps(sorted(s), ensure_ascii=False), encoding="utf-8")


def looks_premium(title:str, badge:str)->bool:
    t = (title or "").lower()
    b = (badge or "").lower()
    return any(w in t for w in PREMIUM_WORDS) or any(w in b for w in PREMIUM_WORDS)


async def has_colored_border(card) -> bool:
    """Эвристика: «цветная обводка», отличная от дефолтно‑серой.
    Берём цвет рамки/теней у ближайшего контейнера карточки."""
    try:
        elem = card.locator(CARD_FRAME).first
        if await elem.count() == 0:
            elem = card
        color = await elem.evaluate("""(el)=>{
            const s = getComputedStyle(el);
            return (s.borderColor || s.outlineColor || s.boxShadow || '').toString();
        }""")
        # примитивная эвристика: если в строке есть hex или rgb не серого тона
        if not color:
            return False
        color = color.lower()
        # если совсем прозрачное/none
        if "0, 0, 0, 0" in color or "transparent" in color or color.strip() in ("", "none"):
            return False
        # серые тона часто имеют r=g=b, пробуем отсеять
        import re as _re
        m = _re.search(r"rgb\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)", color)
        if m:
            r, g, b = map(int, m.groups())
            return not (abs(r-g)<8 and abs(g-b)<8 and abs(r-b)<8)  # не почти серый
        # если есть hex и он не #aaa/#ccc
        m = _re.search(r"#([0-9a-f]{6})", color)
        if m:
            hexv = m.group(1)
            r = int(hexv[0:2],16); g=int(hexv[2:4],16); b=int(hexv[4:6],16)
            return not (abs(r-g)<8 and abs(g-b)<8 and abs(r-b)<8)
        # если box-shadow строка длинная — вероятно украшенная рамка
        return "box-shadow" in color or "inset" in color or "rgba(" in color
    except:
        return False


async def ensure_login(context):
    page = await context.new_page()
    await page.goto("https://web.telegram.org/k/", wait_until="networkidle")
    if "login" in page.url or "auth" in page.url:
        LOG.info("Откройся в браузере и залогинься (QR/код). Я жду…")
        await page.wait_for_url(re.compile(r".*/k/.*"), timeout=0)
        await context.storage_state(path=STORAGE)
        LOG.info("Сохранил сессию в %s", STORAGE)
    else:
        LOG.info("Сессия Telegram Web уже активна")
    await page.close()


async def open_gifts_webapp(page: Page) -> Frame:
    """Открываем t.me/gifts и жмём Open → получаем webview‑фрейм мини‑аппа."""
    await page.goto(GIFT_ENTRY_URL, wait_until="domcontentloaded")
    # кнопка Open может быть локализована
    try:
        await page.get_by_text("Open").click(timeout=4000)
    except:
        try:
            await page.get_by_text("Открыть").click(timeout=4000)
        except:
            pass
    await page.wait_for_timeout(1200)
    # берём последний фрейм с origin мини‑аппа
    frames = [f for f in page.frames if f != page.main_frame]
    return frames[-1] if frames else page.main_frame


async def refresh_app(webview: Frame):
    """Мягкое обновление содержимого мини‑аппа: кликаем вкладку 'Все подарки' или перезагружаем фрейм."""
    try:
        if await webview.locator(ALL_TAB).count():
            await webview.locator(ALL_TAB).first.click()
            await webview.wait_for_timeout(400)
    except:
        # если вкладки нет—просто подождём, рефреш сделаем изнаружи переоткрытием
        pass


async def scan_and_buy(webview: Frame, bought_titles:set, max_buys:int, client: TelegramClient) -> int:
    """Ищем премиум карточки и покупаем до max_buys штук."""
    buys = 0
    cards = await webview.locator(CARD_ITEM).all()
    LOG.info("Найдено карточек: %d", len(cards))

    for card in cards:
        # заголовок/бэйдж
        title = ""
        badge = ""
        try:
            if await card.locator(CARD_TITLE).count():
                title = (await card.locator(CARD_TITLE).first.inner_text()).strip()
            if await card.locator(CARD_BADGE).count():
                badge = (await card.locator(CARD_BADGE).first.inner_text()).strip()
        except:
            pass

        premium = looks_premium(title, badge)
        if not premium:
            # проверка на «цветную обводку»
            premium = await has_colored_border(card)

        if not premium:
            continue

        key = title or f"id:{await card.evaluate('(e)=>e.outerHTML.slice(0,80)')}"
        if key in bought_titles:
            continue

        LOG.info("🔎 Премиум карточка: %r / %r", title, badge)

        # открыть карточку
        try:
            await card.click()
        except Exception as e:
            LOG.warning("Не смог кликнуть карточку: %s", e)
            continue

        # на экране карточки ищем кнопку покупки
        try:
            await webview.locator(BUY_BTN_LIST).first.click(timeout=4000)
        except Exception as e:
            LOG.warning("Не нашёл кнопку 'Купить/Отправить' на 1-м экране: %s", e)
            await webview.go_back()
            continue

        # второй экран — подтверждение
        try:
            btn = webview.locator(CONFIRM_BUY_BTN).first
            await btn.wait_for(timeout=5000)
            price_txt = await btn.inner_text()
            await btn.click()
            LOG.info("✅ Купил: %s (%s)", key, price_txt)
            bought_titles.add(key)
            save_bought(bought_titles)
            buys += 1

            # уведомление себе в «Избранное»
            try:
                await client.send_message("me", f"✅ Куплен подарок: {key} {('('+price_txt+')') if price_txt else ''}")
            except Exception as e:
                LOG.warning("Не удалось отправить в Избранное: %s", e)

            if buys >= max_buys:
                break
            # лёгкая пауза между покупками, чтобы не выглядеть как бот
            await asyncio.sleep(1.2)

        except Exception as e:
            LOG.warning("Не смог подтвердить покупку: %s", e)
            # пытаемся вернуться назад и продолжать
            try:
                await webview.go_back()
            except:
                pass

    return buys


async def run():
    bought_titles = load_bought()

    # --- Telethon: для уведомлений в «Избранное»
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()  # спросит код/2FA при первом запуске

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=STORAGE if Path(STORAGE).exists() else None)
        await ensure_login(context)

        page = await context.new_page()

        while True:
            try:
                LOG.info("Открываю мини‑апп подарков…")
                webview = await open_gifts_webapp(page)
                await refresh_app(webview)
                bought_now = await scan_and_buy(webview, bought_titles, MAX_BUYS_PER_CYCLE, client)
                LOG.info("Цикл завершён: куплено %d", bought_now)
            except Exception as e:
                LOG.error("Сбой цикла: %s", e)

            # закрываем всё и ждём следующую минуту
            try:
                await page.close()
            except:
                pass
            page = await context.new_page()
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
