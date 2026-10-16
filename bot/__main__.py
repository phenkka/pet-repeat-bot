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
from .store import Store

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


GO = kb(("Продолжить", "go"))


def today(cfg: Config):
    return datetime.now(cfg.tz).date()


async def left(store: Store, cfg: Config) -> int:
    return await store.to_do(today(cfg), cfg.new_per_day, cfg.max_per_day)


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


async def send_next(bot: Bot, chat_id: int, store: Store, cfg: Config) -> None:
    d = today(cfg)
    card = await store.next_card(d, cfg.new_per_day, cfg.max_per_day)
    if card:
        # какой карточке адресован следующий текст из этого чата
        await store.set_meta(f"current:{chat_id}", card["id"])
        await bot.send_message(chat_id, render.question(card) + "\n\n<i>Ответ напиши сообщением.</i>",
                               reply_markup=kb(("Не помню", f"n:{card['id']}"), ("Хватит", "stop")))
        return
    await store.set_meta(f"current:{chat_id}", "")
    s = await store.stats(d)
    await bot.send_message(
        chat_id,
        f"На сегодня всё.\nЗнал: {s['ok_today']}, не знал: {s['fail_today']}.\n"
        f"Завтра к повторению: {s['due_tomorrow']}.",
    )


@router.message(Command("start"))
async def start(msg: Message, store: Store, cfg: Config):
    n = await left(store, cfg)
    await msg.answer(
        "Карточки из базы знаний.\n"
        "Ответ пишешь сообщением, своими словами. Бот ищет в нём ключевые пункты,\n"
        "опечатки и сокращения прощает. Не помнишь - «Не помню».\n\n"
        f"Сегодня карточек: {n}.",
        reply_markup=GO if n else None,
    )


@router.message(Command("go"))
async def go_cmd(msg: Message, bot: Bot, store: Store, cfg: Config):
    await send_next(bot, msg.chat.id, store, cfg)


@router.callback_query(F.data == "go")
async def go_btn(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    await cb.answer()
    await send_next(bot, cb.message.chat.id, store, cfg)


@router.callback_query(F.data == "stop")
async def stop(cb: CallbackQuery, store: Store, cfg: Config):
    # карточка без оценки остаётся в сегодняшней норме
    await cb.answer()
    await store.set_meta(f"current:{cb.message.chat.id}", "")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"Остановлено. Осталось сегодня: {await left(store, cfg)}. /go - продолжить.")


@router.callback_query(F.data.startswith("n:"))
async def dont_know(cb: CallbackQuery, bot: Bot, store: Store, cfg: Config):
    card = await store.card(cb.data[2:])
    days = await store.grade(card["id"], False, today(cfg), cfg.intervals) if card else None
    if days is None:
        await cb.answer("Уже оценено")
        return
    await cb.answer()
    await cb.message.edit_text(f"{render.answer(card)}\n\n<b>не помню</b>, снова через {days} дн.")
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
        await msg.answer("Сейчас нет вопроса. /go - начать.")
        return
    if len(grader.tokens(msg.text)) < MIN_WORDS:
        await msg.answer("Коротко. Ответь, как на собесе: что происходит и почему.")
        return
    keys = card["keys"]
    hits, ok = grader.check(keys, card["need"], msg.text)
    days = await store.grade(card["id"], ok, today(cfg), cfg.intervals, answer=msg.text)
    if days is None:
        await msg.answer("Эта карточка сегодня уже оценена.")
    else:
        # засчитанное можно только понизить. незасчитанное оспорить:
        # оценка не меняется, ответ уходит на разбор формулировок
        button = ("Всё равно не знал", f"d:{card['id']}") if ok else ("Оспорить", f"a:{card['id']}")
        await msg.answer(render.verdict(card, [grader.label(k) for k in keys], hits, ok, days),
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
            d = now.date().isoformat()
            # в REMIND_AT серия начинается сама, с первой карточки
            if now.time() >= cfg.remind_at and await store.get_meta("reminded") != d:
                n = await left(store, cfg)
                for uid in cfg.allowed if n else ():
                    if await notify(bot, uid, f"Норма на сегодня: {n}."):
                        await send_next(bot, uid, store, cfg)
                await store.set_meta("reminded", d)
                # бот поднят после NUDGE_AT: второй раз в ту же минуту не дёргать
                if now.time() >= cfg.nudge_at:
                    await store.set_meta("nudged", d)
            # в NUDGE_AT, если норма не добита
            if now.time() >= cfg.nudge_at and await store.get_meta("nudged") != d:
                n = await left(store, cfg)
                for uid in cfg.allowed if n else ():
                    await notify(bot, uid, f"Норма не добита, осталось: {n}.", reply_markup=GO)
                await store.set_meta("nudged", d)
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
