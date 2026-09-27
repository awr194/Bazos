# Bazos.cz — аналитика спроса

Инструмент собирает объявления с [Bazos.cz](https://www.bazos.cz) (рубрики `mobil`, `pc`,
`elektro`, `auto`), отслеживает динамику просмотров и скорость снятия объявлений и показывает
метрики спроса в интерактивном дашборде Streamlit.

## Метрики

| Метрика | Определение |
|---|---|
| **VPH** (Views Per Hour) | `(views_now − views_initial) / hours_passed` — между первым и последним снимком просмотров |
| **Время жизни** | `removed_at − first_seen` для снятых объявлений (часы) |
| **Ликвидность** | доля объявлений, ставших неактивными быстрее 24 / 48 часов |
| **Demand score** | `VPH × (1 + log10(1 + просмотры))` — сортировка таблицы активных объявлений |
| **Ценовая эластичность** | доля продаж < 48 ч по ценовым квартилям внутри категории и наклон этой зависимости |

## Структура проекта

```
├── dashboard.py          # Streamlit-дашборд
├── src/
│   ├── config.py         # URL категорий, User-Agent'ы, задержки, пороги
│   ├── db.py             # SQLite: схема, контекстный менеджер соединений, upsert/снимки
│   ├── scraper.py        # сбор (httpx + selectolax), режим --update, --seed-sample
│   └── analytics.py      # VPH, оборачиваемость, ключевые слова, цена/эластичность
├── tests/                # pytest: парсинг на HTML-фикстурах, БД, аналитика
├── data/                 # здесь создаётся bazos.db
├── requirements.txt
└── requirements-dev.txt  # + ruff, pytest
```

База — SQLite с двумя таблицами:

* `listings` — одно объявление: ID, категория, запрос, заголовок, цена (int, CZK), город, PSČ,
  URL, дата публикации, `first_seen` / `last_seen` / `removed_at`, `is_active`,
  `views_initial` / `views_current`;
* `listing_snapshots` — временной ряд: `captured_at`, `views`, `price_czk`, `is_active`.

## Установка

Нужен Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # для разработки: pip install -r requirements-dev.txt
python -m src.scraper --init-db    # создать data/bazos.db
```

Путь к базе можно переопределить переменной окружения `BAZOS_DB_PATH` или флагом `--db`.

## Быстрый старт с демо-данными

```bash
python -m src.scraper --seed-sample   # ~40 правдоподобных объявлений со снимками за 5 дней
streamlit run dashboard.py            # http://localhost:8501
```

Демо-записи получают ID от `900000001`, поэтому не пересекаются с реальными; повторный запуск
перезаписывает только их.

## Сбор реальных данных

```bash
# первые 2 страницы (по 20 объявлений) всех категорий + страницы объявлений для счётчика «Vidělo»
python -m src.scraper

# только телефоны и ПК, 3 страницы, не более 20 карточек на категорию
python -m src.scraper --categories mobil pc --pages 3 --detail-limit 20

# поиск по запросу — запрос сохраняется и участвует в рейтинге ключевых слов
python -m src.scraper --categories mobil --query "iphone 13"

# быстрый сбор без открытия карточек (просмотры берутся из списка, если они там есть)
python -m src.scraper --no-details
```

### Режим обновления

```bash
python -m src.scraper --update            # перепроверить все активные объявления
python -m src.scraper --update --limit 100 --categories auto
```

Для каждого активного объявления запрашивается его страница:

* 404/410, текст «Inzerát byl vymazán» или редирект с карточки — `is_active = 0`, фиксируется `removed_at`;
* иначе — новый снимок просмотров (`Vidělo: X lidí`) и цены.

VPH и время до продажи появляются только при повторных проверках, поэтому `--update` стоит
запускать по расписанию, например каждые 2–3 часа через cron:

```cron
0 */3 * * * cd /path/to/Bazos && .venv/bin/python -m src.scraper --update >> data/update.log 2>&1
30 */6 * * * cd /path/to/Bazos && .venv/bin/python -m src.scraper --pages 2 >> data/scrape.log 2>&1
```

### Бережное отношение к сайту

* Случайная пауза 1,5–3,5 с перед каждым запросом (`MIN_DELAY` / `MAX_DELAY` в `src/config.py`).
* Ротация реалистичных User-Agent'ов, заголовки `Accept-Language: cs-CZ`.
* При `429` / `5xx` — повтор с учётом `Retry-After` или экспоненциальной паузой (до 3 попыток).
* Соблюдайте условия использования Bazos.cz и не уменьшайте задержки.

### Устойчивость парсинга

Парсер не завязан на конкретные CSS-классы: карточка объявления находится как самый широкий
контейнер вокруг ссылки `/inzerat/<id>/`, в котором нет ссылок на другие объявления. Цена, PSČ,
дата `[26.9. 2026]` и просмотры извлекаются регулярными выражениями; классы с фрагментами
`cena`, `lok`, `view`, `popis` используются лишь как подсказка (текст описания исключается при
поиске цены, чтобы не поймать «původně 20 000 Kč»).

## Дашборд

```bash
streamlit run dashboard.py
```

* **Боковая панель:** фильтр категорий, период (по дате первого появления), кнопки
  «Перечитать базу», «Загрузить демо-данные», а также запуск сбора и проверки активных
  объявлений прямо из интерфейса.
* **Карточки:** активные объявления, медианный VPH, самая быстрая категория и её среднее время жизни.
* **График 1:** топ-15 объявлений по VPH (горизонтальные столбцы, цвет — категория).
* **График 2:** гистограмма времени до продажи с отметками 24 и 48 ч.
* **Вкладки:** топ-10 ключевых слов/запросов по среднему VPH, оборачиваемость категорий
  (быстрая — среднее время жизни < 48 ч), медианная цена быстро проданных и ценовая эластичность.
* **Таблица:** поиск по названию/городу/PSČ, сортировка по demand score, ссылки на объявления.

## Аналитика из консоли

```bash
python -m src.analytics
```

```python
from src import analytics
df = analytics.load_listings(categories=["mobil"])
analytics.top_keywords_by_vph(df, n=10)
analytics.category_turnover(df)          # avg_lifetime_h, sold_24h_pct, sold_48h_pct, is_fast
analytics.price_of_fast_sellers(df)      # медианная цена проданных < 48 ч против остальных
analytics.price_elasticity(df)
```

## Проверки

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
```

Тесты не ходят в сеть: HTTP подменяется `httpx.MockTransport`, парсинг проверяется на
HTML-фикстурах (включая вёрстку с «переименованными» классами), БД — во временных файлах.

> `beautifulsoup4` оставлен в зависимостях как запасной парсер; основной — `selectolax`.
