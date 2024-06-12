import asyncio
from asyncio import Event
import io
from typing import Iterator

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart
from simple_settings import settings

from tgbot.deps import telemetry
from tgbot.repositories import http_openai, invite, sql_users
from tgbot.servicecs import ai
from tgbot.types import URL
from tgbot.utils import tick_iterator

HI_MSG = 'Добро пожаловать!'
CLOSE_MSG = 'Ходу нет!'
AUTH_MSG = 'Требуется авторизация'
ALREADY_MSG = 'И снова добрый день!'

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    if message.text is None or message.from_user is None:
        return None

    if await sql_users.exists(message.from_user.id):
        await message.answer(ALREADY_MSG)
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(CLOSE_MSG)
        return

    code = parts[1]
    payload = invite.get_payload(code)
    if payload is None:
        await message.answer('Невалидный код')
        return
    if await sql_users.exists_code(payload):
        await message.answer('Код не действителен')
        return
    await sql_users.create(
        message.from_user.id,
        message.chat.id,
        payload,
        message.from_user.full_name,
        message.from_user.username or '',
    )
    await message.answer(HI_MSG)


@dp.message()
async def main_handler(message: types.Message) -> None:
    if message.from_user is None:
        return None

    stop = Event()
    try:
        asyncio.create_task(send_typing(message, stop))
        await send_answer(message)
    finally:
        stop.set()


async def send_typing(message: types.Message, stop: Event) -> None:
    assert message.chat
    assert message.bot
    async for _ in tick_iterator(5):
        if stop.is_set():
            break
        await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)


async def send_answer(message: types.Message) -> None:
    assert message.from_user
    assert message.bot
    user = await sql_users.get(message.from_user.id)
    if not user:
        await message.answer(AUTH_MSG)
        return

    telemetry.get().incr('messages')

    if message.voice:
        file_params = await message.bot.get_file(message.voice.file_id)
        assert file_params.file_path
        file_data = io.BytesIO()
        try:
            await message.bot.download_file(file_params.file_path, file_data)
            file_data.name = 'voice.ogg'
            requeset_text = await http_openai.auodo2text(file_data)
        finally:
            file_data.close()
    elif message.text:
        requeset_text = message.text
    else:
        return

    state = await ai.get_chat_state(message, user)
    answer = await state.send(requeset_text)
    if isinstance(answer, URL):
        await message.bot.send_photo(
            message.chat.id,
            answer,
        )
    else:
        for part in _split_answer(answer):
            await message.answer(part)


async def run() -> None:
    bot = Bot(
        settings.TG_TOKEN,
        parse_mode=ParseMode.MARKDOWN,
        session=AiohttpSession(
            api=TelegramAPIServer.from_base(settings.TELEGRAM_BASE_URL),
        ),
    )
    await dp.start_polling(bot)


def _split_answer(answer: str) -> Iterator[str]:
    MAX_LEN = 4096
    while answer:
        if len(answer) <= MAX_LEN:
            break
        part = answer[:MAX_LEN]
        answer = answer[MAX_LEN:]
        yield part
