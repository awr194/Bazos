"""Интерактивный дашборд спроса на Bazos.cz.

Запуск:  streamlit run dashboard.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src import analytics, config, db, scraper

st.set_page_config(page_title="Bazos: аналитика спроса", page_icon="📈", layout="wide")

# Рубрик 20 — различимых цветов на всех не хватит, поэтому столбцы одного цвета,
# а рубрика видна в подписи/подсказке.
ACCENT = "#2a78d6"


@st.cache_data(ttl=600)
def load_taxonomy() -> dict[tuple[str, str], str]:
    """Названия рубрик и подкатегорий с сайта (заполняется через --discover)."""
    db.init_db()
    return {(r["category"], r["subcategory"]): r["name"] for r in db.list_taxonomy() if r["name"]}


def cat_label(code: str) -> str:
    """'mobil' -> 'Мобильные телефоны'; 'mobil/apple' -> 'Мобильные телефоны / Apple'."""
    cat, _, sub = str(code).partition("/")
    names = load_taxonomy()
    label = config.CATEGORY_LABELS.get(cat) or names.get((cat, "")) or cat
    return f"{label} / {names.get((cat, sub), sub)}" if sub else label


@st.cache_data(ttl=300, show_spinner="Загружаю данные из SQLite…")
def load_data() -> pd.DataFrame:
    db.init_db()
    return analytics.load_listings()


def fmt_hours(h: float | None) -> str:
    if h is None or pd.isna(h):
        return "—"
    return f"{h:.0f} ч" if h < 72 else f"{h / 24:.1f} дн"


@st.cache_data(ttl=300)
def load_market_counts() -> pd.DataFrame:
    db.init_db()
    return analytics.market_counts_latest()


def fmt_int(x: float | int | None) -> str:
    return "—" if x is None or pd.isna(x) else f"{int(x):,}".replace(",", " ")


def render_demand(data: pd.DataFrame) -> None:
    """Вкладка «Спрос»: объявления покупателей против предложения."""
    st.caption(
        "Bazos не публикует, что люди вводят в поиск, поэтому спрос измеряется двумя способами: "
        "**явный** — объявления покупателей «Koupím / Sháním / Hledám» (`python -m src.scraper --demand`), "
        "**скрытый** — просмотры (VPH) объявлений о продаже. Перекупщики («Vykoupím») учитываются отдельно."
    )
    o, w = analytics.offers(data), analytics.wanted(data)
    buyouts = int((data["listing_type"] == "buyout").sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Объявлений «продаю»", fmt_int(len(o)))
    c2.metric("Объявлений «куплю / ищу»", fmt_int(len(w)))
    c3.metric(
        "«Куплю» на 100 «продаю»",
        f"{len(w) / len(o) * 100:.1f}" if len(o) else "—",
        help="По собранной выборке. Общий объём рынка — в блоке ниже (замер --count).",
    )
    c4.metric("Перекупщиков", fmt_int(buyouts))

    # --- Объём рынка по замерам --count ------------------------------------------
    st.subheader("Объём рынка на Bazos.cz")
    balance = analytics.market_balance(load_market_counts())
    if categories:
        balance = balance[balance["category"].isin(categories)]
    if balance.empty:
        st.info(
            "Замеров общего объёма ещё нет. Выполните `python -m src.scraper --count` — "
            "это 4 запроса на рубрику (≈ 1 минута на все рубрики)."
        )
    else:
        balance["category"] = balance["category"].map(cat_label)
        st.dataframe(
            balance.rename(
                columns={
                    "category": "Категория",
                    "offers_total": "Всего объявлений в рубрике",
                    "demand_hits": "Найдено по «koupím/sháním/hledám»",
                    "demand_per_1000": "Спрос на 1000 объявлений",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )

    if w.empty:
        st.info(
            "В выборке нет объявлений покупателей. Соберите их: `python -m src.scraper --demand --pages 3`."
        )
        return

    # --- Что ищут: ключевые слова ----------------------------------------------------
    st.subheader("Что ищут покупатели — и сколько на это предложений")
    kw = analytics.supply_demand_by_keyword(data, n=15)
    left, right = st.columns([3, 2])
    with left:
        long = kw.melt(
            id_vars="keyword", value_vars=["wanted", "offers"], var_name="side", value_name="count"
        )
        long["side"] = long["side"].map({"wanted": "Ищут (куплю)", "offers": "Продают"})
        fig = px.bar(
            long,
            x="count",
            y="keyword",
            color="side",
            barmode="group",
            orientation="h",
            color_discrete_map={"Продают": "#2a78d6", "Ищут (куплю)": "#eb6834"},
            category_orders={"keyword": list(kw["keyword"]), "side": ["Ищут (куплю)", "Продают"]},
            labels={"count": "Объявлений", "keyword": "", "side": ""},
        )
        fig.update_traces(marker_line_width=0, hovertemplate="%{y}: %{x} объявл.<extra></extra>")
        fig.update_layout(
            height=max(320, 28 * len(kw) + 80),
            margin=dict(l=0, r=10, t=10, b=0),
            legend=dict(orientation="h", y=-0.12, title=None),
            bargap=0.25,
            bargroupgap=0.1,
        )
        st.plotly_chart(fig, use_container_width=True)
    with right:
        st.dataframe(
            kw.rename(
                columns={
                    "keyword": "Слово",
                    "wanted": "Ищут",
                    "offers": "Продают",
                    "wanted_per_offer": "Ищут / продают",
                    "offer_vph": "VPH предложений",
                    "median_offer_price": "Медиана цены, Kč",
                    "median_budget": "Медиана бюджета, Kč",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "«Ищут / продают» > 1 — дефицит: покупателей больше, чем предложений. "
            "Пусто — предложений в выборке нет совсем."
        )

    # --- По категориям -------------------------------------------------------------------
    st.subheader("Спрос и предложение по " + ("рубрикам" if group_col == "category" else "подкатегориям"))
    sd = analytics.supply_demand_by_category(data, by=group_col).rename(columns={group_col: "category"})
    sd["category"] = sd["category"].map(cat_label)
    st.dataframe(
        sd.rename(
            columns={
                "category": "Категория",
                "offers": "Продают",
                "wanted": "Ищут",
                "buyouts": "Перекупщики",
                "demand_per_100_offers": "Ищут на 100 продаж",
                "median_offer_price": "Медиана цены, Kč",
                "median_budget": "Медиана бюджета, Kč",
                "budget_to_price": "Бюджет / цена",
            }
        ),
        hide_index=True,
        use_container_width=True,
    )
    st.caption("«Бюджет / цена» < 1 — покупатели готовы платить меньше, чем просят продавцы.")

    # --- Список объявлений покупателей -------------------------------------------------
    st.subheader("Объявления покупателей")
    q = st.text_input("Поиск по объявлениям «куплю»", placeholder="например: iphone, octavia", key="wanted_q")
    table = w.copy()
    if q:
        table = table[table["title"].str.lower().str.contains(q.strip().lower(), regex=False)]
    table = table.sort_values("first_seen", ascending=False)
    table["category"] = table["tag"].map(cat_label)
    st.dataframe(
        table[
            ["title", "category", "price_czk", "location", "views_current", "is_active", "posted_at", "url"]
        ],
        hide_index=True,
        use_container_width=True,
        column_config={
            "title": "Что ищут",
            "category": "Категория",
            "price_czk": st.column_config.NumberColumn("Бюджет", format="%d Kč"),
            "location": "Город",
            "views_current": st.column_config.NumberColumn("Просмотров"),
            "is_active": st.column_config.CheckboxColumn("Активно"),
            "posted_at": "Опубликовано",
            "url": st.column_config.LinkColumn("Ссылка", display_text="открыть"),
        },
    )


# --- Боковая панель -----------------------------------------------------------------
st.sidebar.title("Фильтры")
all_data = load_data()

category_options = list(config.CATEGORY_LABELS) + sorted(
    set(all_data["category"]) - set(config.CATEGORY_LABELS) if not all_data.empty else set()
)
selected = st.sidebar.multiselect(
    f"Рубрики (всего {len(category_options)})",
    options=category_options,
    format_func=cat_label,
    placeholder="Все рубрики",
)
categories = selected or category_options

tag_options = (
    sorted(
        all_data.loc[all_data["category"].isin(categories) & all_data["subcategory"].notna(), "tag"].unique()
    )
    if not all_data.empty
    else []
)
tags = st.sidebar.multiselect(
    "Подкатегории",
    options=tag_options,
    format_func=cat_label,
    placeholder="Все подкатегории",
    help="Подкатегории появляются после `--discover` и `--by-subcategory`.",
)
group_col = st.sidebar.radio(
    "Группировать таблицы",
    options=["category", "tag"],
    format_func=lambda v: "по рубрикам" if v == "category" else "по подкатегориям",
    horizontal=True,
)

if all_data.empty:
    min_d = max_d = date.today()
else:
    min_d = all_data["first_seen"].min().date()
    max_d = all_data["first_seen"].max().date()
date_range = st.sidebar.date_input(
    "Период (дата первого появления)",
    value=(min_d, max_d),
    min_value=min_d - timedelta(days=365),
    max_value=max(max_d, date.today()),
)
date_from, date_to = date_range if isinstance(date_range, tuple) and len(date_range) == 2 else (min_d, max_d)

st.sidebar.divider()
st.sidebar.subheader("Обновление данных")
if st.sidebar.button("🔄 Перечитать базу", use_container_width=True):
    load_data.clear()
    load_market_counts.clear()
    load_taxonomy.clear()
    st.rerun()

if st.sidebar.button(
    "🧪 Загрузить демо-данные",
    use_container_width=True,
    help="Вставляет 50 демо-объявлений (продажа и «куплю») (python -m src.scraper --seed-sample)",
):
    n = scraper.seed_sample()
    load_data.clear()
    load_market_counts.clear()
    st.sidebar.success(f"Добавлено {n} демо-объявлений")
    st.rerun()

with st.sidebar.expander("Сбор с Bazos.cz (медленно)"):
    st.caption(
        f"Между запросами пауза {config.MIN_DELAY}–{config.MAX_DELAY} с. "
        "Для регулярного сбора используйте CLI и cron."
    )
    pages = st.number_input("Страниц на категорию", 1, 10, 1)
    detail_limit = st.number_input("Макс. карточек на категорию", 0, 200, 10)
    if st.button("⬇️ Собрать новые", use_container_width=True, disabled=not categories):
        with st.spinner("Собираю объявления…"):
            stats = scraper.scrape(
                categories, int(pages), fetch_details=detail_limit > 0, detail_limit=int(detail_limit)
            )
        load_data.clear()
        load_market_counts.clear()
        st.success(f"Готово: {stats}")
    upd_limit = st.number_input("Макс. проверок активных", 1, 500, 30)
    if st.button("♻️ Проверить активные", use_container_width=True):
        with st.spinner("Проверяю активные объявления…"):
            stats = scraper.update_active(categories, int(upd_limit))
        load_data.clear()
        load_market_counts.clear()
        st.success(f"Готово: {stats}")
    st.divider()
    if st.button("🛒 Собрать спрос («koupím…»)", use_container_width=True, disabled=not categories):
        with st.spinner("Собираю объявления покупателей…"):
            stats = scraper.scrape_demand(categories, int(pages))
        load_data.clear()
        load_market_counts.clear()
        st.success(f"Готово: {stats}")
    if st.button("🗂 Обновить справочник рубрик", use_container_width=True):
        with st.spinner("Читаю рубрики и подкатегории с сайта…"):
            res = scraper.discover(selected or None)
        load_taxonomy.clear()
        n_subs = sum(len(v) for v in res["subcategories"].values())
        st.success(f"Рубрик: {len(res['subcategories'])}, подкатегорий: {n_subs}")
    if st.button("🏷 Собрать по подкатегориям", use_container_width=True):
        with st.spinner("Обхожу подкатегории…"):
            stats = scraper.scrape_by_subcategory(categories, int(pages))
        load_data.clear()
        load_market_counts.clear()
        st.success(f"Готово: {stats}")
    if st.button("📊 Замерить объём рынка", use_container_width=True, disabled=not categories):
        with st.spinner("Считаю объявления в рубриках…"):
            scraper.count_market(categories)
        load_market_counts.clear()
        st.success("Замер сохранён")

# --- Данные с учётом фильтров ------------------------------------------------------
df = all_data[all_data["category"].isin(categories)] if categories else all_data.iloc[0:0]
if tags:
    df = df[df["tag"].isin(tags)]
df = df[(df["first_seen"].dt.date >= date_from) & (df["first_seen"].dt.date <= date_to)]

st.title("📈 Bazos.cz — спрос и предложение")
st.caption(
    "VPH = (просмотры сейчас − просмотры при первом снимке) / часы наблюдения. "
    "Ликвидность — доля объявлений, снятых быстрее 24/48 ч."
)

if all_data.empty:
    st.info(
        "База пуста. Нажмите **«Загрузить демо-данные»** в боковой панели или выполните "
        "`python -m src.scraper --seed-sample`."
    )
    st.stop()
if df.empty:
    st.warning("Под выбранные фильтры не попало ни одного объявления.")
    st.stop()

df_all = df
df = analytics.offers(df_all)  # продажные объявления — основа метрик предложения

tab_supply, tab_demand = st.tabs(["📦 Предложение (продаю)", "🛒 Спрос: что ищут люди"])

with tab_supply:
    # --- Карточки метрик ----------------------------------------------------------------
    m = analytics.summary_metrics(df)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Активных объявлений",
        f"{m['active']:,}".replace(",", " "),
        help=f"Всего в выборке: {m['total']}, снято: {m['removed']}",
    )
    c2.metric(
        "Медианный VPH",
        "—" if m["median_vph"] is None else f"{m['median_vph']:.1f}",
        help="Медиана просмотров в час по объявлениям с ≥ 2 снимками",
    )
    c3.metric(
        "Самая быстрая категория",
        "—" if m["fastest_category"] is None else cat_label(m["fastest_category"]),
        help="Минимальное среднее время жизни снятых объявлений (среди рубрик, где снято ≥ 3)",
    )
    c4.metric("Среднее время жизни в ней", fmt_hours(m["fastest_lifetime_h"]))

    # --- Графики ---------------------------------------------------------------------
    left, right = st.columns(2)

    with left:
        st.subheader("Топ-15 «горячих» объявлений по VPH")
        hot = analytics.hottest_items(df, 15).copy()
        if hot.empty:
            st.info("Недостаточно снимков для расчёта VPH.")
        else:
            hot["label"] = hot["title"].str.slice(0, 40) + " · " + hot["category"]
            hot["Категория"] = hot["tag"].map(cat_label)
            fig = px.bar(
                hot.iloc[::-1],
                x="vph",
                y="label",
                orientation="h",
                color_discrete_sequence=[ACCENT],
                custom_data=["title", "Категория", "price_czk", "views_current"],
                labels={"vph": "Просмотров в час", "label": ""},
            )
            fig.update_traces(
                marker_line_width=0,
                hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<br>"
                "VPH: %{x:.1f}<br>Цена: %{customdata[2]:,.0f} Kč<br>"
                "Просмотров: %{customdata[3]}<extra></extra>",
            )
            fig.update_layout(
                height=520,
                margin=dict(l=0, r=10, t=10, b=0),
                bargap=0.25,
                showlegend=False,
                yaxis=dict(categoryorder="array", categoryarray=list(hot["label"][::-1])),
            )
            st.plotly_chart(fig, use_container_width=True)

    with right:
        st.subheader("Распределение времени до продажи")
        sold = df[df["lifetime_h"].notna()]
        if sold.empty:
            st.info("Пока нет снятых объявлений — запустите режим `--update` позже.")
        else:
            fig = px.histogram(
                sold,
                x="lifetime_h",
                nbins=20,
                color_discrete_sequence=[ACCENT],
                labels={"lifetime_h": "Часов от появления до снятия", "count": "Объявлений"},
            )
            fig.update_traces(
                marker_line_width=2,
                marker_line_color="rgba(0,0,0,0)",
                hovertemplate="%{x} ч: %{y} объявл.<extra></extra>",
            )
            for h in config.FAST_SALE_HOURS:
                fig.add_vline(
                    x=h,
                    line_dash="dot",
                    line_width=1,
                    line_color="gray",
                    annotation_text=f"{h} ч",
                    annotation_position="top",
                )
            fig.update_layout(
                height=520, margin=dict(l=0, r=10, t=30, b=0), yaxis_title="Объявлений", bargap=0.05
            )
            st.plotly_chart(fig, use_container_width=True)
            s24, s48 = sold["sold_24h"].mean() * 100, sold["sold_48h"].mean() * 100
            st.caption(
                f"Снято за < 24 ч: **{s24:.0f}%**, за < 48 ч: **{s48:.0f}%** "
                f"(из {len(sold)} снятых объявлений)."
            )

    # --- Аналитические таблицы -----------------------------------------------------------
    t1, t2, t3 = st.tabs(["🔑 Ключевые слова", "⚡ Оборачиваемость категорий", "💰 Цена и скорость"])
    with t1:
        kw = analytics.top_keywords_by_vph(df, 10)
        st.dataframe(
            kw.rename(
                columns={
                    "keyword": "Ключевое слово / запрос",
                    "avg_vph": "Средний VPH",
                    "median_vph": "Медианный VPH",
                    "listings": "Объявлений",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
    with t2:
        turn = analytics.category_turnover(df, by=group_col).rename(columns={group_col: "category"})
        turn["category"] = turn["category"].map(cat_label)
        st.dataframe(
            turn.rename(
                columns={
                    "category": "Категория",
                    "listings": "Объявлений",
                    "removed": "Снято",
                    "avg_lifetime_h": "Ср. жизнь, ч",
                    "median_lifetime_h": "Медиана жизни, ч",
                    "sold_24h_pct": "< 24 ч, %",
                    "sold_48h_pct": "< 48 ч, %",
                    "median_vph": "Медианный VPH",
                    "is_fast": "Быстрая (< 48 ч)",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
    with t3:
        pf = analytics.price_of_fast_sellers(df)
        pf["category"] = pf["category"].map(cat_label)
        st.markdown("**Медианная цена: проданные быстрее 48 ч против остальных**")
        st.dataframe(
            pf.rename(
                columns={
                    "category": "Категория",
                    "fast_median_price": "Медиана (быстрые), Kč",
                    "other_median_price": "Медиана (остальные), Kč",
                    "fast_count": "Быстрых",
                    "other_count": "Остальных",
                    "price_ratio": "Отношение цен",
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
        el = analytics.price_elasticity(df)
        if not el.empty:
            st.markdown("**Ценовая эластичность: доля быстрых продаж по ценовым квартилям**")
            st.caption("Отрицательный наклон — чем дороже, тем медленнее продаётся (эластичный спрос).")
            el["category"] = el["category"].map(cat_label)
            st.dataframe(
                el.rename(
                    columns={
                        "category": "Категория",
                        "bucket": "Квартиль",
                        "price_from": "Цена от",
                        "price_to": "Цена до",
                        "median_price": "Медиана цены",
                        "listings": "Объявлений",
                        "fast_sale_pct": "Продано < 48 ч, %",
                        "median_vph": "Медианный VPH",
                        "slope_pct_per_bucket": "Наклон, п.п./квартиль",
                    }
                ),
                hide_index=True,
                use_container_width=True,
            )

    # --- Таблица активных объявлений ---------------------------------------------------
    st.subheader("Активные объявления по текущему спросу")
    search = st.text_input("Поиск по названию, городу или PSČ", placeholder="например: iphone, Praha, 602")
    active = df[df["is_active"]].copy()
    if search:
        needle = search.strip().lower()
        hay = (
            active["title"].fillna("") + " " + active["location"].fillna("") + " " + active["psc"].fillna("")
        ).str.lower()
        active = active[hay.str.contains(needle, regex=False)]
    active = active.sort_values("demand_score", ascending=False, na_position="last")
    active["category"] = active["tag"].map(cat_label)
    st.dataframe(
        active[
            [
                "title",
                "category",
                "price_czk",
                "location",
                "psc",
                "views_current",
                "vph",
                "demand_score",
                "posted_at",
                "url",
            ]
        ],
        hide_index=True,
        use_container_width=True,
        column_config={
            "title": "Название",
            "category": "Категория",
            "price_czk": st.column_config.NumberColumn("Цена", format="%d Kč"),
            "location": "Город",
            "psc": "PSČ",
            "views_current": st.column_config.NumberColumn("Просмотров"),
            "vph": st.column_config.NumberColumn("VPH", format="%.1f"),
            "demand_score": st.column_config.ProgressColumn(
                "Demand score",
                format="%.1f",
                min_value=0,
                max_value=max(float(active["demand_score"].fillna(0).max()), 1.0) if len(active) else 1.0,
            ),
            "posted_at": "Опубликовано",
            "url": st.column_config.LinkColumn("Ссылка", display_text="открыть"),
        },
    )
    st.caption(
        f"Показано {len(active)} активных объявлений. Demand score = VPH × (1 + log10(1 + просмотры))."
    )

with tab_demand:
    render_demand(df_all)
