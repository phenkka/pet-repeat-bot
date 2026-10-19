import asyncio
import logging
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import cards, config, grader, render
from .config import Config
from .store import ALL, Store

log = logging.getLogger("repeat-bot")
# короче этого не ответ, а отписка
MIN_WORDS = 3
router = Router()


class OnlyAllowed(BaseMiddleware):

    def __init__(self, allowed: frozenset[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed:
            return None
        return await handler(event, data)


def kb(*buttons: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons
    ]])


ALL_LABEL = "Всё вперемешку"


def today(cfg: Config):
    return datetime.now(cfg.tz).date()


def name(theme: str) -> str:
    return ALL_LABEL if theme == ALL else theme


async def load_cards(store: Store, cfg: Config) -> str:
    """Карточки и сокращения из файлов. Споры, которые после правок
    засчитываются, закрываются сами: значит, вариант дописан."""
    added, off = await store.sync(cards.load(cfg.cards_dir))
    grader.ALIASES = cards.load_aliases(cfg.cards_dir)
    solved = 0
    for d in await store.open_disputes():
        if grader.check(d["keys"], d["need"], d["answer"])[1]:
            solved += await store.close_dispute(d["id"], "accepted")
    return f"Новых: {added}, выключено: {off}, сокращений: {len(grader.ALIASES)}, споров закрыто: {solved}."


async def menu(store: Store, cfg: Config) -> tuple[str, InlineKeyboardMarkup]:
    """Меню тем. На кнопке: к повторению сегодня + ещё не показано."""
    rows = await store.themes(today(cfg))
    due, fresh = sum(r["due"] for r in rows), sum(r["fresh"] for r in rows)
    buttons = [(f"{ALL_LABEL} · {due}+{fresh}", f"t:{ALL}")]
    buttons += [(f"{r['theme']} · {r['due']}+{r['fresh']}", f"t:{r['theme']}") for r in rows]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons[i:i + 2]]
        for i in range(0, len(buttons), 2)
    ])
    return (f"Выбери тему. На кнопке: к повторению сегодня + ещё не показано.\n"
            f"К повторению сегодня: {due}."), markup


async def show_menu(bot: Bot, chat_id: int, store: Store, cfg: Config, head: str = "") -> None:
    text, markup = await menu(store, cfg)
    await bot.send_message(chat_id, f"{head}\n\n{text}" if head else text, reply_markup=markup)


async def session(store: Store, chat_id: int, d) -> tuple[str, int] | None:
    """Текущая тема и сколько вопросов в ней задано. None - темы нет.
    Хранится как «дата|тема|n»: после полуночи нумерация начинается заново."""
    raw = await store.get_meta(f"session:{chat_id}") or ""
    if not raw:
        return None
    day, _, rest = raw.partition("|")
    theme, _, n = rest.rpartition("|")
    return theme, int(n) if day == d.isoformat() else 0


async def end_session(store: Store, chat_id: int) -> None:
    await store.set_meta(f"session:{chat_id}", "")
    await store.set_meta(f"current:{chat_id}", "")


async def summary(store: Store, cfg: Config, theme: str) -> str:
    ok, fail = await store.theme_today(today(cfg), theme)
    return f"«{name(theme)}» сегодня: знал {ok}, не знал {fail}."


async def ask(bot: Bot, chat_id: int, store: Store, card, label: str) -> None:
    # какой карточке адресован следующий текст из этого чата
    await store.set_meta(f"current:{chat_id}", card["id"])
    await bot.send_message(
        chat_id, f"<b>{label}</b>\n{render.question(card)}\n\n<i>Ответ напиши сообщением.</i>",
        reply_markup=kb(("Не помню", f"n:{card['id']}"), ("Хватит", "stop")))


async def send_next(bot: Bot, chat_id: int, store: Store, cfg: Config) -> None:
    d = today(cfg)
    s = await session(store, chat_id, d)
    if s is None:
        await store.set_meta(f"current:{chat_id}", "")
        await show_menu(bot, chat_id, store, cfg)
        return
    theme, n = s
    card = await store.next_in(d, theme)
    if card:
        await store.set_meta(f"session:{chat_id}", f"{d.isoformat()}|{theme}|{n + 1}")
        await ask(bot, chat_id, store, card, f"{name(theme)} · вопрос {n + 1}")
        return
    head = (f"Тема «{name(theme)}» на сегодня пройдена: возвратов нет, новые кончились.\n"
            + await summary(store, cfg, theme))
    await end_session(store, chat_id)
    await show_menu(bot, chat_id, store, cfg, head)


@router.message(Command("start"))
async def start(msg: Message, bot: Bot, store: Store, cfg: Config):
    await show_menu(
        bot, msg.chat.id, store, cfg,
        "Карточки из базы знаний.\n"
        "Ответ пишешь сообщением, своими словами. Бот ищет в нём ключевые пункты,\n"
        "опечатки и сокращения прощает. Не помнишь - «Не помню».\n"
        "В теме сначала возвраты, у которых подошёл срок, потом новые.")


@router.message(Command("go"))
async def go_cmd(msg: Message, bot: Bot, store: Store, cfg: Config):
    await show_menu(bot, msg.chat.id, store, cfg)


@router.callback_query(F.data == "go")
async def go_btn(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    # кнопка «Продолжить» из сообщений до меню тем
    await cb.answer()
    await show_menu(bot, cb.message.chat.id, store, cfg)


@router.callback_query(F.data.startswith("t:"))
async def pick_theme(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    # другая тема посреди серии: прошлая карточка без ответа просто не оценивается
    await cb.answer()
    await store.set_meta(f"session:{cb.message.chat.id}", f"{today(cfg).isoformat()}|{cb.data[2:]}|0")
    await send_next(bot, cb.message.chat.id, store, cfg)


@router.callback_query(F.data == "stop")
async def stop(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    # карточка без ответа не засчитывается и остаётся, где была
    await cb.answer()
    chat_id = cb.message.chat.id
    s = await session(store, chat_id, today(cfg))
    await end_session(store, chat_id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await show_menu(bot, chat_id, store, cfg,
                    "Остановлено. " + (await summary(store, cfg, s[0]) if s else ""))


@router.callback_query(F.data.startswith("n:"))
async def dont_know(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    card = await store.card(cb.data[2:])
    days = await store.grade(card["id"], False, today(cfg), cfg.intervals) if card else None
    if days is None:
        await cb.answer("Уже оценено")
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(render.verdict(card, "Не помню", days))
    await send_next(bot, cb.message.chat.id, store, cfg)


@router.callback_query(F.data.startswith("d:"))
async def downgrade(cb: CallbackQuery, store: Store, cfg: Config):
    days = await store.downgrade(cb.data[2:], today(cfg), cfg.intervals)
    await cb.answer("Переведено в «не знал»" if days else "Уже не засчитано")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("a:"))
async def appeal(cb: CallbackQuery, store: Store, cfg: Config):
    dispute_id = await store.dispute(cb.data[2:], today(cfg))
    await cb.answer(f"Спор #{dispute_id} записан" if dispute_id else "Уже оспорено")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.message(Command("stats"))
async def stats(msg: Message, store: Store, cfg: Config):
    s = await store.stats(today(cfg))
    boxes = ", ".join(f"ступень {b}: {n}" for b, n in s["boxes"]) or "пусто"
    await msg.answer(
        f"Карточек: {s['active']}, ещё не показаны: {s['unseen']}.\n"
        f"Сегодня знал: {s['ok_today']}, не знал: {s['fail_today']}.\n"
        f"Завтра к повторению: {s['due_tomorrow']}.\n"
        f"По ступеням: {boxes}.\n"
        f"Открытых споров: {s['disputes']}."
    )


@router.message(Command("disputes"))
async def disputes(msg: Message, store: Store):
    rows = await store.open_disputes()
    if not rows:
        await msg.answer("Открытых споров нет.")
        return
    parts = []
    for d in rows:
        hits, _ = grader.check(d["keys"], d["need"], d["answer"])
        missed = "; ".join(grader.label(k) for k, h in zip(d["keys"], hits) if not h)
        parts.append(f"<b>#{d['id']}</b> {d['card_id']} {d['day']}\n"
                     f"ответ: {render.text(d['answer'])}\nне найдено: {render.text(missed)}")
    # одним сообщением: его удобно переслать целиком на разбор
    await msg.answer("\n\n".join(parts)[:4000])


@router.message(Command("drop"))
async def drop(msg: Message, command: CommandObject, store: Store):
    # спор разобран, ответ правда неверный
    if not (command.args or "").strip().isdigit():
        await msg.answer("Формат: /drop 12")
        return
    ok = await store.close_dispute(int(command.args), "rejected")
    await msg.answer("Закрыт как неверный." if ok else "Такого открытого спора нет.")


@router.message(Command("reload"))
async def reload(msg: Message, store: Store, cfg: Config):
    try:
        report = await load_cards(store, cfg)
    except (ValueError, OSError) as e:
        await msg.answer(f"Карточки не загружены, старые остались:\n<code>{render.text(str(e))}</code>")
        return
    await msg.answer(f"Перечитано. {report}")


@router.message(F.text & ~F.text.startswith("/"))
async def typed_answer(msg: Message, bot: Bot, store: Store, cfg: Config):
    card = await store.card(await store.get_meta(f"current:{msg.chat.id}") or "")
    if card is None:
        await show_menu(bot, msg.chat.id, store, cfg, "Сейчас нет вопроса.")
        return
    if len(grader.tokens(msg.text)) < MIN_WORDS:
        await msg.answer("Коротко. Ответь, как на собесе: что происходит и почему.")
        return
    _, ok = grader.check(card["keys"], card["need"], msg.text)
    days = await store.grade(card["id"], ok, today(cfg), cfg.intervals, answer=msg.text)
    if days is None:
        await msg.answer("Эта карточка сегодня уже оценена.")
    else:
        # засчитанное можно только понизить. незасчитанное оспорить:
        # оценка не меняется, ответ уходит на разбор формулировок
        button = ("Всё равно не знал", f"d:{card['id']}") if ok else ("Оспорить", f"a:{card['id']}")
        await msg.answer(render.verdict(card, "Засчитано" if ok else "Не засчитано", days),
                         reply_markup=kb(button))
    await send_next(bot, msg.chat.id, store, cfg)


async def notify(bot: Bot, uid: int, text: str, **kw) -> bool:
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except TelegramAPIError as e:
        # не нажимал /start: Telegram не даёт боту писать первым
        log.warning("сообщение %s не ушло: %s", uid, e)
        return False


async def reminder(bot: Bot, store: Store, cfg: Config) -> None:
    while True:
        try:
            now = datetime.now(cfg.tz)
            d = now.date()
            # в REMIND_AT меню всегда: даже без возвратов есть новые
            if now.time() >= cfg.remind_at and await store.get_meta("reminded") != d.isoformat():
                n = await store.due_count(d)
                head = f"Пора повторять. К повторению сегодня: {n}." if n else "Пора повторять."
                for uid in cfg.allowed:
                    text, markup = await menu(store, cfg)
                    await notify(bot, uid, f"{head}\n\n{text}", reply_markup=markup)
                await store.set_meta("reminded", d.isoformat())
                # бот поднят после NUDGE_AT: второй раз в ту же минуту не дёргать
                if now.time() >= cfg.nudge_at:
                    await store.set_meta("nudged", d.isoformat())
            # в NUDGE_AT, если возвраты не пройдены
            if now.time() >= cfg.nudge_at and await store.get_meta("nudged") != d.isoformat():
                n = await store.due_count(d)
                for uid in cfg.allowed if n else ():
                    text, markup = await menu(store, cfg)
                    await notify(bot, uid, f"Не повторено сегодня: {n}.\n\n{text}", reply_markup=markup)
                await store.set_meta("nudged", d.isoformat())
        except Exception:
            # база перезапускается: напоминание не должно умереть насовсем
            log.exception("напоминание упало, следующая попытка через минуту")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load()
    store = Store(cfg.db)
    await store.open()
    log.info("карточки: %s", await load_cards(store, cfg))

    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(store=store, cfg=cfg)
    dp.update.outer_middleware(OnlyAllowed(cfg.allowed))
    dp.include_router(router)
    await bot.set_my_commands([
        BotCommand(command="go", description="повторять"),
        BotCommand(command="stats", description="сколько и что"),
        BotCommand(command="disputes", description="открытые споры"),
        BotCommand(command="reload", description="перечитать карточки"),
    ])
    task = asyncio.create_task(reminder(bot, store, cfg))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
