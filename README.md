# La-Torta Assistant — рабочая версия для Render

Эта версия специально исправлена для **Render Web Service**.

## Что было исправлено

1. Приложение слушает переменную `PORT` на `0.0.0.0`.
2. Добавлены адреса `/` и `/health` для проверки Render.
3. Telegram long polling запускается параллельно с HTTP-сервером.
4. Перед запуском удаляется старый webhook.
5. `render.yaml` создаёт Web Service, а не Background Worker.
6. Зафиксированы совместимые версии Python и зависимостей.
7. Индексация каталога запускается через 10 секунд и не блокирует health check.
8. Добавлена команда `/status`.

## Важно

Старый Telegram-токен, который был опубликован в переписке, необходимо отозвать.
Используйте только новый токен.

## Развёртывание через Blueprint

1. Распакуйте архив.
2. Загрузите **содержимое папки** в корень GitHub-репозитория.
   Файл `render.yaml` должен лежать в корне репозитория.
3. В Render выберите:
   `New` → `Blueprint`.
4. Подключите GitHub-репозиторий.
5. Render найдёт `render.yaml`.
6. Введите секреты:
   - `TELEGRAM_BOT_TOKEN`
   - `OPENAI_API_KEY`
   - `MANAGER_CHAT_ID` — можно временно оставить пустым.
7. Нажмите Apply/Deploy.

После запуска адрес вида:

`https://latorta-assistant-bot.onrender.com/health`

должен возвращать JSON с `"ok": true`.

## Развёртывание вручную

Создайте:

- Service Type: **Web Service**
- Runtime: **Python 3**
- Build Command:

```bash
pip install -r requirements.txt
```

- Start Command:

```bash
python -m app.main
```

- Health Check Path:

```text
/health
```

Переменные окружения:

```text
TELEGRAM_BOT_TOKEN = новый токен BotFather
OPENAI_API_KEY = ваш OpenAI API key
OPENAI_MODEL = gpt-4.1-mini
MANAGER_USERNAME = byviktoriia_a
MANAGER_CHAT_ID = числовой ID Виктории
SITE_URL = https://la-torta.ua/ua/
MAX_CATALOG_PAGES = 500
CATALOG_REFRESH_HOURS = 12
START_CATALOG_REFRESH = true
PYTHON_VERSION = 3.12.8
```

## Как получить ID Виктории

1. Виктория открывает бота.
2. Нажимает Start.
3. Отправляет `/myid`.
4. Полученное число вставляется в `MANAGER_CHAT_ID`.
5. В Render выполните Manual Deploy → Deploy latest commit
   или просто перезапустите сервис.

## Проверка после запуска

В Render Logs должны появиться строки:

```text
Health server listening on 0.0.0.0:10000
Starting Telegram long polling
```

Затем:

1. Откройте `/health`.
2. Напишите боту `/start`.
3. Напишите `/status`.
4. Проверьте поиск товара.

## Возможные ошибки

### `Conflict: terminated by other getUpdates request`

Тот же токен одновременно запущен на другом компьютере или сервисе.
Остановите старую копию бота.

### `Unauthorized`

Неверный или отозванный Telegram-токен.

### `TELEGRAM_BOT_TOKEN is missing`

Переменная не добавлена в Render Environment.

### Web service failed to bind to port

Убедитесь, что Start Command:

```bash
python -m app.main
```

и используется именно этот исправленный проект.

### Бот отвечает, но каталог пуст

Подождите несколько минут и отправьте `/status`.
На бесплатном Render каталог SQLite может сбрасываться после redeploy или сна.
Для постоянного каталога позже лучше подключить PostgreSQL.

### OpenAI error

Проверьте `OPENAI_API_KEY`, наличие средств на API-балансе и доступ к модели.
Без OpenAI-ключа бот всё равно запускается и показывает найденные товары.


## Исправление поиска и ссылок

В этой версии:

- AI технически не может отправлять URL;
- ссылки отправляются только отдельными карточками;
- каждая ссылка повторно загружается и проверяется как реальная карточка товара;
- при недоступном sitemap бот обходит категории сайта;
- при пустом локальном индексе используется внутренний поиск сайта;
- добавлена команда `/searchtest шоколад`.

После деплоя выполните:

```text
/status
/searchtest шоколад
/searchtest жиророзчинний барвник
```

Если `/searchtest` не возвращает результаты, откройте Render Logs и найдите
`Fetch failed`, `Fallback crawl` или `Catalog progress`.
