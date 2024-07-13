import asyncio
from asyncio import Event
import io

from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import CommandStart, Command
from aiogram.types import BotCommand, BufferedInputFile, LabeledPrice, ContentType
from asyncpg import UniqueViolationError
from simple_settings import settings

from tgbot.deps import telemetry
from tgbot.repositories import http_openai, invite, sql_chat_messages, sql_users, sql_wallets
from tgbot.servicecs import ai, wallet
from tgbot.utils import get_sign, tick_iterator

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
    try:
        await sql_users.create(
            message.from_user.id,
            message.chat.id,
            payload,
            message.from_user.full_name,
            message.from_user.username or '',
        )
    except UniqueViolationError:
        pass
    await message.answer(HI_MSG)


@dp.message(Command('clean'))
async def cmd_clean(message: types.Message) -> None:
    await sql_chat_messages.clean(message.chat.id)
    await message.answer('Контекст очищен')


@dp.message(Command('balance'))
async def cmd_balance(message: types.Message) -> None:
    assert message.from_user
    microdollars = await sql_wallets.get(message.from_user.id)
    await message.answer('Баланс: {}${:.2f}'.format(get_sign(microdollars), abs(microdollars / 1_000_000)))


@dp.message(Command('buy'))
async def cmd_buy(message: types.Message):
    assert message.from_user
    assert message.bot
    await message.bot.send_invoice(
        message.chat.id,
        title='Пополнение баланса',
        description='Пополнение баланса бота',
        provider_token=settings.TG_PAYMENTS_TOKEN,
        currency='USD',
        prices=[LabeledPrice(label='Пополнение баланса', amount=500*100)],
        start_parameter='add-balance',
        payload=str(message.from_user.id),
    )


@dp.pre_checkout_query(lambda query: True)
async def pre_checkout_query_handler(pre_checkout_q: types.PreCheckoutQuery):
    assert pre_checkout_q.bot
    await pre_checkout_q.bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(content_types=ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment_handler(message: types.Message):
    assert message.bot
    assert message.successful_payment
    payment_info = message.successful_payment
    await wallet.add(int(payment_info.invoice_payload), message.successful_payment.total_amount * 10_000)
    await message.bot.send_message(
        message.chat.id,
        'Платеж на сумму ${:.2f} прошел успешно!'.format(
            message.successful_payment.total_amount // 100,
        ),
    )


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
    if isinstance(answer, bytes):
        await message.bot.send_photo(
            message.chat.id,
            BufferedInputFile(answer, 'answer.jpg'),
        )
    elif isinstance(answer, str):
        await message.answer(answer)


async def run() -> None:
    bot = Bot(
        settings.TG_TOKEN,
        parse_mode=ParseMode.MARKDOWN,
        session=AiohttpSession(
            api=TelegramAPIServer.from_base(settings.TELEGRAM_BASE_URL),
        ),
    )
    await bot.set_my_commands(commands=[
        BotCommand(command='/balance', description='Показать баланс'),
        BotCommand(command='/clean', description='Очистить контекст'),
    ])
    await dp.start_polling(bot)
