import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiosqlite
import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import AsyncOpenAI
from rapidfuzz import fuzz

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("latorta")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "byviktoriia_a").strip().lstrip("@")
MANAGER_RAW = os.getenv("MANAGER_CHAT_ID", "").strip()
MANAGER_CHAT_ID = int(MANAGER_RAW) if MANAGER_RAW.lstrip("-").isdigit() else None
SITE_URL = os.getenv("SITE_URL", "https://la-torta.ua/ua/").strip()
MAX_PAGES = max(10, int(os.getenv("MAX_CATALOG_PAGES", "500")))
REFRESH_HOURS = max(1, int(os.getenv("CATALOG_REFRESH_HOURS", "12")))
START_CATALOG_REFRESH = os.getenv("START_CATALOG_REFRESH", "true").lower() in {"1", "true", "yes", "on"}
PORT = int(os.getenv("PORT", "10000"))

DB = Path("data/bot.sqlite3")
KNOWLEDGE = Path("data/knowledge.md")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LaTortaAssistant/2.0; +https://la-torta.ua/)"
}

router = Router()
openai_client = AsyncOpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
catalog_state = {"running": False, "last_count": 0, "last_error": None}

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔎 Знайти товар"),
            KeyboardButton(text="🚚 Доставка й оплата"),
        ],
        [
            KeyboardButton(text="📍 Магазини"),
            KeyboardButton(text="👩‍💼 Покликати менеджера"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напишіть, що ви шукаєте…",
)

WELCOME = """Вітаємо у La-Torta! 🎂

Я — AI-помічник магазину. Допоможу знайти товар, підібрати інгредієнти,
форми, барвники, декор і пакування, зорієнтую за ціною та передам
складне питання менеджеру.

Напишіть, що саме ви шукаєте."""

async def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS products(
            url TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            price TEXT,
            stock TEXT,
            image TEXT,
            description TEXT,
            search_text TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS channel_posts(
            message_id INTEGER PRIMARY KEY,
            text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            chat_id INTEGER,
            payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await db.commit()

async def save_message(chat_id: int, role: str, text: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO messages(chat_id, role, text) VALUES(?,?,?)",
            (chat_id, role, text[:10000]),
        )
        await db.commit()

async def recent_messages(chat_id: int, limit: int = 6) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role,text FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [dict(row) for row in reversed(await cur.fetchall())]

async def upsert_product(product: dict) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        INSERT INTO products(url,title,price,stock,image,description,search_text)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            price=excluded.price,
            stock=excluded.stock,
            image=excluded.image,
            description=excluded.description,
            search_text=excluded.search_text,
            updated_at=CURRENT_TIMESTAMP
        """, (
            product["url"],
            product["title"],
            product.get("price", ""),
            product.get("stock", ""),
            product.get("image", ""),
            product.get("description", ""),
            product["search_text"],
        ))
        await db.commit()

async def product_count() -> int:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM products")
        row = await cur.fetchone()
        return int(row[0])

async def all_products() -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM products")
        return [dict(row) for row in await cur.fetchall()]

def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

async def fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        response = await client.get(url, timeout=25, follow_redirects=True)
        response.raise_for_status()
        return response.text
    except Exception as exc:
        log.warning("Fetch failed %s: %s", url, exc)
        return None

async def sitemap_urls(client: httpx.AsyncClient) -> list[str]:
    pending = [
        urljoin(SITE_URL, "/sitemap.xml"),
        urljoin(SITE_URL, "/sitemap_index.xml"),
        urljoin(SITE_URL, "/ua/sitemap.xml"),
    ]
    seen: set[str] = set()
    urls: set[str] = set()

    while pending and len(seen) < 40:
        sitemap = pending.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)

        body = await fetch(client, sitemap)
        if not body or "<loc>" not in body:
            continue

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            continue

        for element in root.iter():
            if not element.tag.endswith("loc") or not element.text:
                continue
            location = clean(element.text)
            if location.endswith(".xml"):
                pending.append(location)
            elif urlparse(location).netloc.endswith("la-torta.ua") and "/ua/" in location:
                urls.add(location)

    if urls:
        return list(urls)

    home = await fetch(client, SITE_URL)
    if not home:
        return []

    soup = BeautifulSoup(home, "lxml")
    return list({
        urljoin(SITE_URL, link.get("href"))
        for link in soup.select("a[href]")
        if "/ua/" in urljoin(SITE_URL, link.get("href"))
        and urlparse(urljoin(SITE_URL, link.get("href"))).netloc.endswith("la-torta.ua")
    })

def parse_product(url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    product: dict = {}

    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.get_text(strip=True))
        except Exception:
            continue

        queue = data if isinstance(data, list) else [data]
        expanded = []
        for item in queue:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)

        for item in expanded:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            if item_type == "Product" or (
                isinstance(item_type, list) and "Product" in item_type
            ):
                product = item
                break
        if product:
            break

    h1 = soup.select_one("h1")
    title = clean(product.get("name")) or clean(
        h1.get_text(" ", strip=True) if h1 else ""
    )

    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    price = ""
    if isinstance(offers, dict) and offers.get("price"):
        price = f'{offers["price"]} {offers.get("priceCurrency", "UAH")}'

    if not price:
        price_element = soup.select_one(
            '[itemprop="price"], .product-price, .current-price, .price'
        )
        if price_element:
            price = clean(
                price_element.get("content")
                or price_element.get_text(" ", strip=True)
            )

    availability = offers.get("availability", "") if isinstance(offers, dict) else ""
    stock = ""
    if "InStock" in availability:
        stock = "В наявності"
    elif "OutOfStock" in availability:
        stock = "Немає в наявності"

    image = product.get("image", "")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    if not image:
        og_image = soup.select_one('meta[property="og:image"]')
        image = og_image.get("content", "") if og_image else ""

    description = clean(product.get("description"))
    if not description:
        description_element = soup.select_one(
            '[itemprop="description"], .product-description, .description'
        )
        description = clean(
            description_element.get_text(" ", strip=True)
            if description_element else ""
        )

    page_text = clean(soup.get_text(" ", strip=True)).lower()
    product_signal = bool(product) or bool(price) or (
        "купити" in page_text or "додати до кошика" in page_text
    )
    if not title or not product_signal:
        return None

    return {
        "url": url,
        "title": title,
        "price": price,
        "stock": stock,
        "image": urljoin(url, image) if image else "",
        "description": description[:2200],
        "search_text": clean(
            f"{title} {description} {price} {stock}"
        ).lower(),
    }

async def refresh_catalog() -> int:
    if catalog_state["running"]:
        return catalog_state["last_count"]

    catalog_state["running"] = True
    catalog_state["last_error"] = None

    try:
        async with httpx.AsyncClient(headers=HEADERS) as client:
            urls = await sitemap_urls(client)
            excluded = (
                "/blog", "/contacts", "/shipping", "/payment",
                "/about", "/reviews", "/login", "/register",
            )
            urls = [
                url for url in urls
                if not any(part in url.lower() for part in excluded)
            ][:MAX_PAGES]

            semaphore = asyncio.Semaphore(6)

            async def process(url: str) -> int:
                async with semaphore:
                    html = await fetch(client, url)
                    product = parse_product(url, html) if html else None
                    if not product:
                        return 0
                    await upsert_product(product)
                    return 1

            total = 0
            for start in range(0, len(urls), 30):
                total += sum(
                    await asyncio.gather(
                        *(process(url) for url in urls[start:start + 30])
                    )
                )
                log.info(
                    "Catalog progress %s/%s, products=%s",
                    min(start + 30, len(urls)),
                    len(urls),
                    total,
                )

            catalog_state["last_count"] = total
            return total
    except Exception as exc:
        catalog_state["last_error"] = str(exc)
        log.exception("Catalog refresh failed")
        return 0
    finally:
        catalog_state["running"] = False

async def search_products(query: str, limit: int = 4) -> list[dict]:
    products = await all_products()
    normalized = clean(query).lower()
    words = [
        word for word in re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
        if len(word) > 1
    ]

    scored = []
    for product in products:
        haystack = product["search_text"]
        hits = sum(word in haystack for word in words)
        ratio = fuzz.token_set_ratio(normalized, product["title"].lower())
        score = hits * 35 + ratio
        if hits or ratio >= 45:
            scored.append((score, product))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [product for _, product in scored[:limit]]

async def ai_answer(chat_id: int, text: str, products: list[dict]) -> str:
    if not openai_client:
        if products:
            return (
                "Я знайшла кілька відповідних товарів і покажу їх нижче. "
                "Для повної AI-консультації адміністратор має додати OPENAI_API_KEY."
            )
        return (
            "Поки не вдалося знайти точний товар. Напишіть назву, бренд, "
            "розмір або для чого він потрібен."
        )

    knowledge = KNOWLEDGE.read_text("utf-8") if KNOWLEDGE.exists() else ""
    catalog = "\n\n".join(
        f"{index + 1}. {product['title']}\n"
        f"Ціна: {product['price'] or 'не розпізнана'}\n"
        f"Наявність: {product['stock'] or 'потрібно перевірити'}\n"
        f"Опис: {product['description'][:500]}\n"
        f"URL: {product['url']}"
        for index, product in enumerate(products)
    ) or "Релевантних товарів не знайдено."

    history = await recent_messages(chat_id)
    input_messages = [{
        "role": "developer",
        "content": f"""Ти AI-консультант магазину La-Torta.
Відповідай мовою клієнта: українською або російською.
Будь доброзичливою, конкретною та лаконічною.
Не вигадуй товари, ціни, наявність, характеристики, строки чи знижки.
Для точного залишку у фізичному магазині, повернення, претензії,
оптової ціни або зміни замовлення запропонуй менеджера.
Не кажи, що замовлення створене або товар зарезервований.
Картки товарів бот надішле окремо, тому не дублюй довгі URL.

БАЗА ЗНАНЬ:
{knowledge}

ЗНАЙДЕНІ ТОВАРИ:
{catalog}"""
    }]

    for item in history:
        input_messages.append({
            "role": "assistant" if item["role"] == "assistant" else "user",
            "content": item["text"],
        })

    input_messages.append({"role": "user", "content": text})

    response = await openai_client.responses.create(
        model=OPENAI_MODEL,
        input=input_messages,
        max_output_tokens=420,
    )
    return response.output_text.strip()

def product_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Переглянути товар", url=url)
        ]]
    )

def manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Написати Вікторії",
                url=f"https://t.me/{MANAGER_USERNAME}",
            )
        ]]
    )

async def notify_manager(bot: Bot, message: Message) -> bool:
    user = message.from_user
    customer = (
        f"@{user.username}" if user and user.username
        else f"{user.full_name if user else 'Клієнт'} (ID {message.chat.id})"
    )
    notification = (
        "🔔 <b>Запит клієнта</b>\n\n"
        f"Клієнт: {customer}\n\n"
        f"<b>Повідомлення:</b>\n{message.text or '—'}"
    )

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO events(type,chat_id,payload) VALUES('handoff',?,?)",
            (message.chat.id, message.text or ""),
        )
        await db.commit()

    if not MANAGER_CHAT_ID:
        return False

    try:
        await bot.send_message(MANAGER_CHAT_ID, notification)
        return True
    except Exception:
        log.exception("Manager notification failed")
        return False

@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=MAIN_KB)

@router.message(Command("myid"))
async def myid_handler(message: Message) -> None:
    await message.answer(
        f"Ваш Telegram chat ID: <code>{message.chat.id}</code>"
    )

@router.message(Command("delivery"))
@router.message(F.text == "🚚 Доставка й оплата")
async def delivery_handler(message: Message) -> None:
    await message.answer(
        "🚚 Актуальні умови доставки й оплати:\n"
        "https://la-torta.ua/ua/shipping-and-payment/",
        disable_web_page_preview=True,
    )

@router.message(Command("shops"))
@router.message(F.text == "📍 Магазини")
async def shops_handler(message: Message) -> None:
    await message.answer(
        "📍 Актуальні адреси, телефони та графік магазинів:\n"
        "https://la-torta.ua/ua/contacts/",
        disable_web_page_preview=True,
    )

@router.message(Command("catalog"))
@router.message(F.text == "🔎 Знайти товар")
async def catalog_handler(message: Message) -> None:
    await message.answer(
        "Напишіть назву або опишіть завдання. Наприклад:\n"
        "«Потрібен червоний жиророзчинний барвник для шоколаду»."
    )

@router.message(Command("manager"))
@router.message(F.text == "👩‍💼 Покликати менеджера")
async def manager_handler(message: Message, bot: Bot) -> None:
    sent = await notify_manager(bot, message)
    text = (
        "Я передала запит Вікторії."
        if sent
        else "Натисніть кнопку нижче, щоб написати Вікторії."
    )
    await message.answer(text, reply_markup=manager_keyboard())

@router.message(Command("refresh"))
async def refresh_handler(message: Message) -> None:
    if not MANAGER_CHAT_ID or message.chat.id != MANAGER_CHAT_ID:
        await message.answer("Ця команда доступна менеджеру.")
        return
    await message.answer("Оновлення каталогу запущено.")
    count = await refresh_catalog()
    await message.answer(f"Оновлення завершено. Збережено товарів: {count}.")

@router.message(Command("status"))
async def status_handler(message: Message) -> None:
    count = await product_count()
    await message.answer(
        f"Бот працює ✅\n"
        f"Товарів у каталозі: {count}\n"
        f"Оновлення виконується: {'так' if catalog_state['running'] else 'ні'}"
    )

@router.channel_post()
async def channel_post_handler(message: Message) -> None:
    text = message.text or message.caption or ""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO channel_posts(message_id,text) VALUES(?,?)",
            (message.message_id, text),
        )
        await db.commit()

@router.message(F.text)
async def chat_handler(message: Message, bot: Bot) -> None:
    text = message.text.strip()
    await save_message(message.chat.id, "user", text)
    await bot.send_chat_action(message.chat.id, "typing")

    products = await search_products(text)

    try:
        answer = await ai_answer(message.chat.id, text, products)
    except Exception:
        log.exception("AI answer failed")
        answer = (
            "Вибачте, зараз не вдалося сформувати AI-відповідь. "
            "Я покажу знайдені товари або можу передати питання менеджеру."
        )

    await save_message(message.chat.id, "assistant", answer)
    await message.answer(answer)

    for product in products[:3]:
        caption = f"<b>{product['title']}</b>"
        if product.get("price"):
            caption += f"\nЦіна на сторінці: {product['price']}"
        if product.get("stock"):
            caption += f"\nСтатус: {product['stock']}"
        caption += "\nПеревірте актуальні дані в картці товару."

        try:
            if product.get("image"):
                await message.answer_photo(
                    product["image"],
                    caption=caption,
                    reply_markup=product_keyboard(product["url"]),
                )
            else:
                await message.answer(
                    caption,
                    reply_markup=product_keyboard(product["url"]),
                )
        except Exception:
            await message.answer(
                caption,
                reply_markup=product_keyboard(product["url"]),
            )

async def catalog_scheduler() -> None:
    if not START_CATALOG_REFRESH:
        log.info("Automatic catalog refresh is disabled")
        return

    await asyncio.sleep(10)

    while True:
        try:
            await refresh_catalog()
            await asyncio.sleep(REFRESH_HOURS * 3600)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Catalog scheduler crashed")
            await asyncio.sleep(1800)

async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "service": "latorta-assistant-bot",
        "catalog_products": await product_count(),
        "catalog_refresh_running": catalog_state["running"],
        "catalog_last_error": catalog_state["last_error"],
    })

async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("Health server listening on 0.0.0.0:%s", PORT)
    return runner

async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands([
        BotCommand(command="start", description="Почати спілкування"),
        BotCommand(command="catalog", description="Знайти товар"),
        BotCommand(command="delivery", description="Доставка й оплата"),
        BotCommand(command="shops", description="Магазини"),
        BotCommand(command="manager", description="Покликати менеджера"),
        BotCommand(command="myid", description="Показати мій Telegram ID"),
        BotCommand(command="status", description="Статус бота"),
    ])

async def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing. Add it in Render Environment."
        )

    await init_db()
    http_runner = await start_http_server()

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Важно после неудачных webhook-развёртываний:
    # getUpdates/long polling не работает, пока активен webhook.
    await bot.delete_webhook(drop_pending_updates=False)
    await set_commands(bot)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    catalog_task = asyncio.create_task(catalog_scheduler())

    try:
        log.info("Starting Telegram long polling")
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        catalog_task.cancel()
        with suppress(asyncio.CancelledError):
            await catalog_task
        await bot.session.close()
        await http_runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
