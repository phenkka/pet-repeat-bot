import asyncio
import logging
import secrets

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import cards, config, render
from .config import Config
from .store import ALL, Store

log = logging.getLogger("repeat-bot")
router = Router()
ALL_LABEL = "Всё вперемешку"


class Access(BaseMiddleware):
    """Пускает админов из .env и добавленных через /adduser.
    Добавленные держатся в памяти, чтобы не ходить в базу на каждое нажатие."""

    def __init__(self, admins: frozenset[int], users: set[int]):
        self.admins = admins
        self.users = users

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or (user.id not in self.admins and user.id not in self.users):
            return None
        data["is_admin"] = user.id in self.admins
        return await handler(event, data)


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def name(theme: str) -> str:
    return ALL_LABEL if theme == ALL else theme


async def load_cards(store: Store, cfg: Config) -> str:
    added, off = await store.sync(cards.load(cfg.cards_dir))
    return f"Новых: {added}, выключено: {off}."


async def show_menu(bot: Bot, chat_id: int, store: Store, head: str = "") -> None:
    rows = await store.themes()
    buttons = [(f"{ALL_LABEL} · {sum(r['n'] for r in rows)}", f"t:{ALL}")]
    buttons += [(f"{r['theme']} · {r['n']}", f"t:{r['theme']}") for r in rows]
    text = "Выбери тему. На кнопке - сколько в ней карточек."
    await bot.send_message(chat_id, f"{head}\n\n{text}" if head else text,
                           reply_markup=kb(*(buttons[i:i + 2] for i in range(0, len(buttons), 2))))


async def session(store: Store, chat_id: int) -> tuple[str, str, int] | None:
    """Проход по колоде: тема, seed перемешивания и номер текущей карточки.
    Хранится как «seed|n|тема»: тема последней, в ней может быть что угодно."""
    raw = await store.get_meta(f"session:{chat_id}") or ""
    seed, _, rest = raw.partition("|")
    n, _, theme = rest.partition("|")
    return (theme, seed, int(n)) if theme else None


async def save_session(store: Store, chat_id: int, theme: str, seed: str, n: int) -> None:
    await store.set_meta(f"session:{chat_id}", f"{seed}|{n}|{theme}")


async def send_card(bot: Bot, chat_id: int, store: Store) -> None:
    s = await session(store, chat_id)
    if s is None:
        await show_menu(bot, chat_id, store)
        return
    theme, seed, n = s
    card = await store.card_at(theme, seed, n)
    if card is None:
        await store.set_meta(f"session:{chat_id}", "")
        await show_menu(bot, chat_id, store, f"Тема «{name(theme)}» пройдена: {n} карточек.")
        return
    label = f"{name(theme)} · {n + 1}/{await store.deck_size(theme)}"
    # в кнопке номер карточки: по нему «Ответ» и «Дальше» в старом сообщении
    # не собьют текущий проход
    await bot.send_message(chat_id, render.question(card, label),
                           reply_markup=kb([("Ответ", f"a:{n}:{card['id']}")]))


@router.message(Command("start", "go"))
async def start(msg: Message, bot: Bot, store: Store):
    await show_menu(bot, msg.chat.id, store,
                    "Карточки из базы знаний. Ответь в голове, потом жми «Ответ» и сверяйся.")


@router.callback_query(F.data.startswith("t:"))
async def pick_theme(cb: CallbackQuery, bot: Bot, store: Store):
    # новый проход: свой порядок карточек
    await cb.answer()
    await save_session(store, cb.message.chat.id, cb.data[2:], secrets.token_hex(4), 0)
    await send_card(bot, cb.message.chat.id, store)


@router.callback_query(F.data.startswith("a:"))
async def show_answer(cb: CallbackQuery, store: Store):
    _, n, card_id = cb.data.split(":", 2)
    card = await store.card(card_id)
    if card is None:
        await cb.answer("Карточки больше нет")
        return
    await cb.answer()
    # подпись вопроса уже в сообщении: первая строка, её и оставляем
    label = (cb.message.text or "").split("\n", 1)[0]
    try:
        await cb.message.edit_text(render.answer(card, label),
                                   reply_markup=kb([("Дальше", f"x:{n}"), ("Меню", "menu")]))
    except TelegramBadRequest:
        # двойное нажатие: ответ уже открыт
        pass


@router.callback_query(F.data.startswith("x:"))
async def next_card(cb: CallbackQuery, bot: Bot, store: Store):
    chat_id = cb.message.chat.id
    s = await session(store, chat_id)
    # «Дальше» из старого сообщения или второй раз: проход уже ушёл вперёд
    if s is None or s[2] != int(cb.data[2:]):
        await cb.answer("Уже дальше")
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    theme, seed, n = s
    await save_session(store, chat_id, theme, seed, n + 1)
    await send_card(bot, chat_id, store)


@router.callback_query(F.data == "menu")
async def to_menu(cb: CallbackQuery, bot: Bot, store: Store):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await store.set_meta(f"session:{cb.message.chat.id}", "")
    await show_menu(bot, cb.message.chat.id, store)


@router.message(Command("reload"))
async def reload(msg: Message, store: Store, cfg: Config, is_admin: bool):
    if not is_admin:
        return
    try:
        report = await load_cards(store, cfg)
    except (ValueError, OSError) as e:
        await msg.answer(f"Карточки не загружены, старые остались:\n<code>{render.text(str(e))}</code>")
        return
    await msg.answer(f"Перечитано. {report}")


@router.message(Command("adduser", "deluser"))
async def manage_user(msg: Message, command: CommandObject, store: Store, access: Access,
                      is_admin: bool):
    if not is_admin:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await msg.answer(f"Формат: /{command.command} 123456789\n"
                         "id пользователь узнаёт у @userinfobot.")
        return
    uid = int(arg)
    if command.command == "adduser":
        await store.add_user(uid)
        access.users.add(uid)
        await msg.answer(f"Добавлен {uid}. Пусть напишет боту /start: первым бот писать не может.")
    else:
        ok = await store.del_user(uid)
        access.users.discard(uid)
        await msg.answer(f"Удалён {uid}." if ok else f"{uid} не было в списке. Админов из .env так не удалить.")


@router.message(Command("users"))
async def list_users(msg: Message, access: Access, is_admin: bool):
    if not is_admin:
        return
    added = ", ".join(map(str, sorted(access.users))) or "нет"
    await msg.answer(f"Админы (.env): {', '.join(map(str, sorted(access.admins)))}\nДобавлены: {added}")


@router.callback_query()
async def stale_button(cb: CallbackQuery, bot: Bot, store: Store):
    # кнопки старой версии бота: «Не помню», «Оспорить», «Хватит»...
    await cb.answer("Кнопка устарела")
    await show_menu(bot, cb.message.chat.id, store)


@router.message()
async def anything(msg: Message, bot: Bot, store: Store):
    # отвечать текстом не нужно: ответ по кнопке
    await show_menu(bot, msg.chat.id, store, "Отвечать текстом не нужно: жми «Ответ» на карточке.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load()
    store = Store(cfg.db)
    await store.open()
    log.info("карточки: %s", await load_cards(store, cfg))

    access = Access(cfg.admins, await store.users())
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(store=store, cfg=cfg, access=access)
    dp.update.outer_middleware(access)
    dp.include_router(router)
    await bot.set_my_commands([BotCommand(command="go", description="меню тем")])
    try:
        await dp.start_polling(bot)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
