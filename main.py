import json
import logging
import os

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ══════════════════════════════════════════════
#  НАСТРОЙКИ — поменяйте токен!
# ══════════════════════════════════════════════
BOT_TOKEN   = "ВАШ_ТОКЕН_ЗДЕСЬ"
CHANNEL_URL = "https://t.me/ВАШ_КАНАЛ"   # ← замените
VPN_URL     = "https://ВАШ_ВПН.com"       # ← замените
DATA_FILE  = "data.json"
URL        = "https://rcoi02.ru/stat.php"
CHECK_INTERVAL = 300  # секунды (5 минут)

logging.basicConfig(format="%(asctime)s  %(levelname)s  %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  ХРАНИЛИЩЕ (простой JSON-файл)
# ══════════════════════════════════════════════

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "statuses": {}}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════
#  ПАРСЕР — обрабатывает rowspan/colspan
# ══════════════════════════════════════════════

def parse_site() -> dict:
    """
    Возвращает dict: "Дата | Форма Кл | Предмет" -> "Статус"
    """
    try:
        resp = requests.get(URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        table = soup.find("table")
        if not table:
            log.warning("Таблица не найдена на странице")
            return {}

        rows = table.find_all("tr")
        carry: dict[int, tuple[str, int]] = {}  # col -> (value, remaining_rowspan)
        result: dict[str, str] = {}
        NUM_COLS = 8  # № | Дата | Класс | Форма | Предмет | Состояние | Заявки | Рассмотрение

        for row in rows[1:]:  # пропускаем заголовок
            cells = row.find_all(["td", "th"])
            cell_idx = 0
            full_row: dict[int, str] = {}
            col = 0

            while col < NUM_COLS:
                if col in carry:
                    full_row[col] = carry[col][0]
                    carry[col] = (carry[col][0], carry[col][1] - 1)
                    if carry[col][1] == 0:
                        del carry[col]
                    col += 1
                elif cell_idx < len(cells):
                    cell = cells[cell_idx]
                    text    = cell.get_text(strip=True)
                    rowspan = int(cell.get("rowspan", 1))
                    colspan = int(cell.get("colspan", 1))
                    full_row[col] = text
                    if rowspan > 1:
                        carry[col] = (text, rowspan - 1)
                    cell_idx += 1
                    col += colspan
                else:
                    full_row[col] = ""
                    col += 1

            date    = full_row.get(1, "").strip()
            cls     = full_row.get(2, "").strip()
            form    = full_row.get(3, "").strip()
            subject = full_row.get(4, "").strip()
            status  = full_row.get(5, "").strip()

            if subject and date:
                key = f"{date} | {form} {cls}кл. | {subject}"
                result[key] = status if status else "—"

        log.info(f"Спарсено позиций: {len(result)}")
        return result

    except Exception as e:
        log.error(f"Ошибка парсинга: {e}")
        return {}

# ══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════

def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Выбрать предметы", callback_data="select")],
        [InlineKeyboardButton("📊 Мои предметы",     callback_data="mine")],
    ])


async def show_select(query, data: dict, uid: str) -> None:
    statuses  = data.get("statuses", {})
    selected  = data["users"].get(uid, [])
    subjects  = sorted(statuses.keys())

    if not subjects:
        await query.edit_message_text(
            "⏳ Данные ещё не загружены. Попробуйте через минуту.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
        )
        return

    kb = []
    for i, subj in enumerate(subjects):
        mark = "✅" if subj in selected else "⬜"
        kb.append([InlineKeyboardButton(f"{mark}  {subj}", callback_data=f"t_{i}")])
    kb.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])

    await query.edit_message_text(
        "📚 Нажмите на предмет для выбора/отмены:\n"
        "<i>(✅ — выбран, ⬜ — не выбран)</i>",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML",
    )


async def show_mine(query, data: dict, uid: str) -> None:
    selected = data["users"].get(uid, [])
    statuses = data.get("statuses", {})
    back_kb  = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]])

    if not selected:
        await query.edit_message_text(
            "❌ Вы не выбрали ни одного предмета.\n"
            "Нажмите «Выбрать предметы».",
            reply_markup=back_kb,
        )
        return

    lines = ["📊 <b>Ваши предметы и их статус:</b>\n"]
    for subj in selected:
        status = statuses.get(subj, "нет данных")
        lines.append(f"• <b>{subj}</b>\n  └ {status}")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=back_kb,
        parse_mode="HTML",
    )

# ══════════════════════════════════════════════
#  ОБРАБОТЧИКИ
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = str(update.effective_user.id)
    data = load_data()
    if uid not in data["users"]:
        data["users"][uid] = []
        save_data(data)

    promo = (
        "📣 <b>Пока ждёшь результатов — загляни сюда:</b>\n"
        f"• Мой канал — <a href='{CHANNEL_URL}'>подписаться</a>\n"
        f"• Быстрый VPN — <a href='{VPN_URL}'>попробовать</a>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )

    await update.message.reply_text(
        f"{promo}"
        "👋 Привет! Я слежу за статусами экзаменов на сайте <b>rcoi02.ru</b>.\n\n"
        "Выберите предметы — пришлю уведомление, как только статус изменится.",
        reply_markup=main_menu_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q   = update.callback_query
    await q.answer()
    uid  = str(q.from_user.id)
    data = load_data()

    if uid not in data["users"]:
        data["users"][uid] = []
        save_data(data)

    # ── главное меню ──────────────────────────
    if q.data == "back":
        await q.edit_message_text("Выберите действие:", reply_markup=main_menu_markup())

    # ── список предметов для выбора ───────────
    elif q.data == "select":
        await show_select(q, data, uid)

    # ── мои предметы + текущий статус ─────────
    elif q.data == "mine":
        await show_mine(q, data, uid)

    # ── переключить предмет ───────────────────
    elif q.data.startswith("t_"):
        try:
            idx      = int(q.data[2:])
            subjects = sorted(data["statuses"].keys())
            if 0 <= idx < len(subjects):
                subj = subjects[idx]
                subs = data["users"][uid]
                if subj in subs:
                    subs.remove(subj)
                else:
                    subs.append(subj)
                data["users"][uid] = subs
                save_data(data)
        except (ValueError, IndexError):
            pass
        await show_select(q, load_data(), uid)  # перерисовать меню

# ══════════════════════════════════════════════
#  ФОНОВАЯ ЗАДАЧА — проверка каждые 5 минут
# ══════════════════════════════════════════════

async def check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Проверка обновлений статусов...")
    new_statuses = parse_site()
    if not new_statuses:
        return

    data        = load_data()
    old_statuses = data.get("statuses", {})

    # находим записи, у которых изменился статус (и запись уже была в старых данных)
    changes = {
        subj: new_st
        for subj, new_st in new_statuses.items()
        if subj in old_statuses and old_statuses[subj] != new_st
    }

    data["statuses"] = new_statuses
    save_data(data)

    if not changes:
        log.info("Изменений нет")
        return

    log.info(f"Обнаружены изменения: {list(changes.keys())}")

    for uid, user_subjects in data["users"].items():
        user_changes = {s: v for s, v in changes.items() if s in user_subjects}
        if not user_changes:
            continue

        text = "🔔 <b>Статус экзамена обновлён!</b>\n\n"
        for subj, status in user_changes.items():
            text += f"• <b>{subj}</b>\n  └ {status}\n"

        try:
            await context.bot.send_message(chat_id=int(uid), text=text, parse_mode="HTML")
        except Exception as e:
            log.error(f"Не удалось отправить пользователю {uid}: {e}")

# ══════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))

    # первичный парсинг при старте
    log.info("Первичный парсинг сайта...")
    statuses = parse_site()
    if statuses:
        data = load_data()
        data["statuses"] = statuses
        save_data(data)
        log.info(f"Загружено предметов: {len(statuses)}")
    else:
        log.warning("Первичный парсинг не дал результатов")

    # планировщик: запускать check_job каждые CHECK_INTERVAL секунд
    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL, first=CHECK_INTERVAL)

    log.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
