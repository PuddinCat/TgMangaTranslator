from pathlib import Path

from typing import List, Dict, Tuple, Union

import os
import asyncio
import time
import logging
import httpx
import traceback
from telegram import Update, Message
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
    ExtBot,
)

logging.basicConfig(level=logging.INFO)

SAVE_IMAGE_PATH = Path("./saved")
TRANSLATED_IMAGE = Path("./translated")
TRANSLATE_RESULT_TIMEOUT = 600
SHIFT_CORO = 0.01
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MANGA_TRANSLATOR_API = os.environ[
    "MANGA_TRANSLATOR_API"
]  # http://127.0.0.1:5003
os.makedirs(SAVE_IMAGE_PATH, exist_ok=True)
os.makedirs(TRANSLATED_IMAGE, exist_ok=True)

translate_semaphore = asyncio.Lock()
translate_tasks: List[Path] = []
translate_result: Dict[Path, Tuple[asyncio.Task, float]] = {}


class PhotoMissing(Exception):
    """缺失图片"""


class PhotoTooBig(Exception):
    """图片太大"""


class UnknownError(Exception):
    """未知错误"""


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(
        f"""
{update.effective_user.first_name}你好喵，我可以翻译漫画喵
项目在[这里](https://github.com/PuddinCat/TgMangaTranalator)开源喵"""
    )


async def translater_worker():
    while True:
        await asyncio.sleep(SHIFT_CORO)
        if not translate_tasks:
            continue
        photo = translate_tasks.pop(0)
        task = asyncio.create_task(translate_manga_request(photo))
        while not task.done():
            await asyncio.sleep(SHIFT_CORO)
        now = time.perf_counter()
        translate_result[photo] = (task, now)

        timeout_photos = [
            photo
            for photo, (task, t) in translate_result.items()
            if t + TRANSLATE_RESULT_TIMEOUT < now
        ]
        for timeout_photo in timeout_photos:
            await asyncio.sleep(SHIFT_CORO)
            task, _ = translate_result[timeout_photo]
            del translate_result[timeout_photo]
            try:
                await task
            except Exception:
                pass


async def translate_manga_request(src: Path) -> Path:
    name = src.name
    translated_path = TRANSLATED_IMAGE / name
    async with httpx.AsyncClient() as client:
        resp = None

        with open(src, "rb") as file:
            resp = await client.post(
                f"{MANGA_TRANSLATOR_API}/run",
                files={"file": (src.name, file)},
                data={"translator": "gpt3.5-evil", "size": "S"},
                timeout=120,
            )
        photo_task_id = resp.json()["task_id"]
        resp = await client.get(
            f"{MANGA_TRANSLATOR_API}/result/{photo_task_id}",
        )
        with open(translated_path, "wb") as file:
            async for chunk in resp.aiter_bytes():
                file.write(chunk)
    # shutil.copy(src, )
    return translated_path


async def add_translate_task(src: Path) -> int:
    translate_tasks.append(src)
    return len(translate_tasks) - 1


async def translate_task_index(src: Path) -> Union[int, None]:
    try:
        return translate_tasks.index(src)
    except ValueError:
        return None


class PhotoReader:
    async def save_photo(self) -> Path:
        raise NotImplementedError()


class ChatMessageReader(PhotoReader):
    def __init__(self, bot: ExtBot, message: Message):
        self.bot = bot
        self.message = message

    async def save_photo(self):
        assert self.message.from_user is not None
        username, message_id, chat_id, sized_photos = (
            self.message.from_user.username,
            self.message.message_id,
            self.message.chat_id,
            self.message.photo,
        )
        if not sized_photos:
            raise PhotoMissing(self.message.message_id)

        photo = sized_photos[-1]
        file_id = photo.file_id  # 获取图片的file_id
        file = await self.bot.get_file(file_id)  # 根据file_id获取图片文件
        if file.file_size and file.file_size > 1024 * 1024:
            raise PhotoTooBig(file.file_size)

        save_path = SAVE_IMAGE_PATH / f"{username}_{chat_id}_{message_id}.jpg"
        try:
            await file.download_to_drive(save_path)
        except Exception as exp:
            raise UnknownError() from exp
        return save_path


class ResultReplyer:
    async def reply_message(self, message: str):
        raise NotImplementedError()

    async def reply_photo(self, photo):
        raise NotImplementedError()

    async def edit_or_reply_message(self, message: str):
        raise NotImplementedError()

    async def delete_last_message(self):
        raise NotImplementedError()


class MessageReplyer(ResultReplyer):
    def __init__(self, bot: ExtBot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_text_message = None

    async def reply_message(self, message: str):
        self.last_text_message = await self.bot.send_message(
            self.chat_id, message, reply_to_message_id=self.message_id
        )

    async def reply_photo(self, photo):
        await self.bot.send_photo(
            self.chat_id, photo, reply_to_message_id=self.message_id
        )

    async def edit_or_reply_message(self, message: str):
        if not self.last_text_message:
            await self.reply_message(message)
            return
        await self.last_text_message.edit_text(message)

    async def delete_last_message(self):
        if not self.last_text_message:
            return
        await self.last_text_message.delete()
        self.last_text_message = None


class CatGirlService:
    def __init__(self, reader: PhotoReader, replyer: ResultReplyer):
        self.reader = reader
        self.replyer = replyer

    async def run(self):
        saved_file_path, translated_file_path = None, None
        try:
            saved_file_path = await self.reader.save_photo()
        except PhotoMissing:
            await self.replyer.reply_message("我图片呢喵")
            return
        except PhotoTooBig:
            await self.replyer.reply_message("图片太大了喵")
            return
        except UnknownError:
            await self.replyer.reply_message("不知道发生了什么但就是保存不了图片喵")
            return
        if not os.path.exists(saved_file_path):
            await self.replyer.reply_message("我图片呢喵，我看不见图片喵")
            return
        idx = await add_translate_task(saved_file_path)
        if idx != 0:
            await self.replyer.edit_or_reply_message(f"加入队列了喵，前面还有{idx}个任务喵")
        else:
            await self.replyer.edit_or_reply_message("开始处理了喵")
        while idx:
            new_idx = await translate_task_index(saved_file_path)
            if not new_idx:
                await self.replyer.edit_or_reply_message("开始处理了喵")
                break
            if idx != new_idx:
                idx = new_idx
                await self.replyer.edit_or_reply_message(f"现在前面还有{idx}个任务喵")
        while saved_file_path not in translate_result:
            await asyncio.sleep(SHIFT_CORO)
        await self.replyer.delete_last_message()
        try:
            task, _ = translate_result[saved_file_path]
            translated_file_path = await task
        except Exception:
            traceback.print_exc()
            await self.replyer.reply_message("翻译失败了喵")
            return
        with open(translated_file_path, "rb") as file:
            await self.replyer.reply_photo(file)


async def save_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    # 获取并检查所有File对象
    assert update.message is not None
    service = CatGirlService(
        reader=ChatMessageReader(context.bot, update.message),
        replyer=MessageReplyer(
            context.bot, update.message.chat_id, update.message.message_id
        ),
    )
    await service.run()


async def main():
    asyncio.create_task(translater_worker())

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, save_photo))
    app.add_handler(MessageHandler(filters.ALL, hello))

    await app.initialize()
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling()
    while app.running:
        await asyncio.sleep(SHIFT_CORO)
        # print("Fetch")


if __name__ == "__main__":
    asyncio.run(main())
