import os
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, FSInputFile
import yt_dlp
from ytmusicapi import YTMusic

# Загрузка переменных из .env
load_dotenv()

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в переменных окружения или .env файле!")
    exit(1)

TEMP_FOLDER = "downloads"

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ytmusic = YTMusic()  # Инициализация API YouTube Music

if not os.path.exists(TEMP_FOLDER):
    os.makedirs(TEMP_FOLDER)

# Пул потоков
executor = ThreadPoolExecutor(max_workers=4)

def search_ytmusic(query, search_type='songs'):
    """
    Ищет треки или альбомы через YouTube Music API.
    search_type: 'songs' или 'albums'
    """
    try:
        # filter может быть: songs, videos, albums, artists, playlists
        results = ytmusic.search(query, filter=search_type, limit=10)
        parsed_results = []
        
        for item in results:
            # Обработка ТРЕКОВ
            if search_type == 'songs':
                # Формируем строку артистов (Artist1, Artist2)
                artists = ", ".join([a['name'] for a in item.get('artists', [])])
                album = item.get('album', {}).get('name', 'Single')
                thumb = item['thumbnails'][-1]['url'] if item.get('thumbnails') else None
                
                parsed_results.append({
                    'id': item['videoId'],
                    'title': item['title'],
                    'subtitle': f"{artists} • {album}",
                    'thumb': thumb,
                    'type': 'TR'
                })
            
            # Обработка АЛЬБОМОВ
            elif search_type == 'albums':
                artists = ", ".join([a['name'] for a in item.get('artists', [])])
                year = item.get('year', '')
                thumb = item['thumbnails'][-1]['url'] if item.get('thumbnails') else None
                
                # У альбомов ID называется browseId
                parsed_results.append({
                    'id': item['browseId'], 
                    'title': item['title'],
                    'subtitle': f"Альбом • {artists} ({year})",
                    'thumb': thumb,
                    'type': 'AL'
                })
                
        return parsed_results
    except Exception as e:
        logger.error(f"Ошибка поиска ytmusic: {e}")
        return []

def get_album_tracks(browse_id):
    """Получает список треков альбома по browseId."""
    try:
        album = ytmusic.get_album(browse_id)
        tracks = []
        for t in album.get('tracks', []):
            tracks.append({
                'id': t['videoId'],
                'title': t['title']
            })
        return tracks, album.get('title', 'Альбом')
    except Exception as e:
        logger.error(f"Ошибка получения альбома: {e}")
        return [], None

def download_task(video_id, filename_prefix):
    """Скачивание и конвертация одного трека."""
    url = f"https://music.youtube.com/watch?v={video_id}"
    filename_base = os.path.join(TEMP_FOLDER, filename_prefix)
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{filename_base}.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'extract_audio': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'flac',
            'preferredquality': '0',
        }, {
            'key': 'FFmpegMetadata',
        }, {
            'key': 'EmbedThumbnail',
        }],
        'writethumbnail': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_filename = f"{filename_base}.flac"
            return final_filename, info.get('title', 'Track'), info.get('duration', 0), info.get('artist', 'Artist')
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, None, None, None

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎧 **YouTube Music FLAC Bot**\n\n"
        "Я ищу музыку напрямую в базе YouTube Music (чистый звук, без клипов).\n\n"
        "🔎 **Как пользоваться:**\n"
        "1. Просто поиск трека: `@botname название`\n"
        "2. Поиск альбома: `@botname alb название`"
    )

@dp.inline_query()
async def inline_search(inline_query: types.InlineQuery):
    text = inline_query.query
    if not text or len(text) < 2:
        return

    # Определение режима: Альбом или Трек
    is_album = False
    clean_query = text
    if text.lower().startswith(('alb ', 'альбом ', 'album ')):
        is_album = True
        # Удаляем префикс (первое слово)
        clean_query = " ".join(text.split()[1:])

    if not clean_query: return

    search_type = 'albums' if is_album else 'songs'
    
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(executor, search_ytmusic, clean_query, search_type)

    articles = []
    for item in results:
        # Формируем скрытое сообщение для отправки
        # Если это трек: TYPE:TR
        # Если это альбом: TYPE:AL
        content_text = f"💿 Загружаю: {item['title']}...\nID: {item['id']} TYPE:{item['type']} #music_load"
        
        article = InlineQueryResultArticle(
            id=item['id'],
            title=item['title'],
            description=item['subtitle'],
            input_message_content=InputTextMessageContent(message_text=content_text),
            thumbnail_url=item['thumb'],
            thumbnail_height=100,
            thumbnail_width=100
        )
        articles.append(article)

    await inline_query.answer(articles, cache_time=60, is_personal=False)

@dp.message(F.text.contains("#music_load"))
async def process_download(message: types.Message):
    # Парсинг данных из сообщения
    id_match = re.search(r"ID: ([\w\.-]+)", message.text) # ID может содержать дефисы и точки
    type_match = re.search(r"TYPE:(\w+)", message.text)
    
    if not id_match or not type_match: return

    content_id = id_match.group(1)
    content_type = type_match.group(1)
    
    # === ВАРИАНТ 1: ОДИНОЧНЫЙ ТРЕК ===
    if content_type == "TR":
        status_msg = await message.reply("⏳ `YouTube Music`: Скачиваю трек во FLAC...")
        loop = asyncio.get_running_loop()
        
        file_path, title, duration, artist = await loop.run_in_executor(
            executor, download_task, content_id, content_id
        )
        
        if file_path and os.path.exists(file_path):
            try:
                audio = FSInputFile(file_path)
                await message.reply_audio(
                    audio, 
                    title=title, 
                    performer=artist, # Теперь у нас есть чистый исполнитель
                    duration=duration, 
                    caption="💾 Format: `FLAC`"
                )
            finally:
                if os.path.exists(file_path): os.remove(file_path)
                await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Ошибка загрузки.")

    # === ВАРИАНТ 2: АЛЬБОМ ===
    elif content_type == "AL":
        status_msg = await message.reply("⏳ `YouTube Music`: Получаю список треков альбома...")
        loop = asyncio.get_running_loop()
        
        # 1. Получаем треки через API (мгновенно)
        tracks, album_title = await loop.run_in_executor(executor, get_album_tracks, content_id)
        
        if not tracks:
            await status_msg.edit_text("❌ Не удалось получить информацию об альбоме.")
            return

        total = len(tracks)
        await status_msg.edit_text(f"💿 Альбом: **{album_title}**\nТреков: {total}. Начинаю загрузку...")
        
        # 2. Качаем по одному
        for i, track in enumerate(tracks, 1):
            # Обновление статуса раз в 3 трека
            if i % 3 == 1 or i == total:
                try:
                    await status_msg.edit_text(f"⏳ Альбом **{album_title}**\nЗагрузка: {i}/{total}\nСейчас: _{track['title']}_")
                except: pass
            
            # Уникальное имя файла
            file_prefix = f"{content_id}_{track['id']}"
            
            file_path, title, duration, artist = await loop.run_in_executor(
                executor, download_task, track['id'], file_prefix
            )
            
            if file_path and os.path.exists(file_path):
                try:
                    audio = FSInputFile(file_path)
                    await message.reply_audio(
                        audio,
                        title=title,
                        performer=artist,
                        duration=duration,
                        caption=f"💿 {i}/{total}"
                    )
                except Exception as e:
                    logger.error(f"Error sending {title}: {e}")
                finally:
                    if os.path.exists(file_path): os.remove(file_path)
            
            await asyncio.sleep(1) # Защита от флуда
            
        await status_msg.edit_text("✅ Альбом загружен.")

# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass