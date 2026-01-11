import os
import asyncio
import logging
import shutil
import re
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import yt_dlp
from ytmusicapi import YTMusic

# Загрузка переменных из .env
load_dotenv()

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не найден в переменных окружения или .env файле!")
    exit(1)

TEMP_FOLDER = "downloads"
SUBS_FILE = "subscriptions.json"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ytmusic = YTMusic()  # Инициализация API YouTube Music

# Загрузка/сохранение подписок
def load_subs():
    if os.path.exists(SUBS_FILE):
        with open(SUBS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"artists": {}}

def save_subs(data):
    with open(SUBS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

if os.path.exists(TEMP_FOLDER):
    shutil.rmtree(TEMP_FOLDER)
os.makedirs(TEMP_FOLDER, exist_ok=True)

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
            
            # Обработка АРТИСТОВ
            elif search_type == 'artists':
                thumb = item['thumbnails'][-1]['url'] if item.get('thumbnails') else None
                parsed_results.append({
                    'id': item['browseId'],
                    'title': item.get('artist', 'Unknown Artist'),
                    'subtitle': "Исполнитель",
                    'thumb': thumb,
                    'type': 'AR'
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
            
            # Улучшенное получение метаданных
            title = info.get('title', 'Unknown Track')
            duration = info.get('duration', 0)
            artist = info.get('artist') or info.get('uploader') or 'Unknown Artist'
            
            return final_filename, title, duration, artist
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, None, None, None

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("follow"))
async def cmd_follow(message: types.Message, command: Command):
    """Поиск артистов для подписки: /follow Название"""
    query = command.args
    if not query:
        await message.answer("Введите имя артиста после команды, например: `/follow Linkin Park`", parse_mode="Markdown")
        return

    loop = asyncio.get_running_loop()
    # Ищем артистов (лимит 5 для выбора)
    results = await loop.run_in_executor(executor, ytmusic.search, query, "artists")
    
    if not results:
        await message.answer("Артист не найден.")
        return

    # Формируем клавиатуру с топ-5 результатами
    keyboard = []
    for artist in results[:5]:
        name = artist.get('artist', 'Unknown Artist')
        b_id = artist.get('browseId')
        keyboard.append([InlineKeyboardButton(text=name, callback_data=f"sub_artist:{b_id}")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer("🔍 **Выберите артиста для подписки:**", reply_markup=markup, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("sub_artist:"))
async def process_sub_artist(callback: CallbackQuery):
    artist_id = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)
    
    loop = asyncio.get_running_loop()
    try:
        artist_data = await loop.run_in_executor(executor, ytmusic.get_artist, artist_id)
        artist_name = artist_data.get('name', 'Артист')
        
        subs = load_subs()
        if artist_id not in subs["artists"]:
            last_id = None
            if artist_data.get('singles', {}).get('results'):
                last_id = artist_data['singles']['results'][0]['videoId']
            
            subs["artists"][artist_id] = {
                "name": artist_name,
                "last_release": last_id,
                "subscribers": []
            }

        if user_id not in subs["artists"][artist_id]["subscribers"]:
            subs["artists"][artist_id]["subscribers"].append(user_id)
            save_subs(subs)
            await callback.message.edit_text(f"✅ Вы подписались на обновления **{artist_name}**!", parse_mode="Markdown")
        else:
            await callback.message.edit_text(f"Вы уже подписаны на {artist_name}.")
    except Exception as e:
        logger.error(f"Error in subscription: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при подписке.")
    
    await callback.answer()

def generate_unsub_markup(user_artists, page):
    """Генерирует клавиатуру для отписки с пагинацией."""
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current_items = user_artists[start:end]
    
    keyboard = []
    for artist in current_items:
        keyboard.append([InlineKeyboardButton(text=f"❌ {artist['name']}", callback_data=f"unsub_art:{artist['id']}:{page}")])
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"unsub_page:{page-1}"))
    if end < len(user_artists):
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"unsub_page:{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("unfollow"))
async def cmd_unfollow(message: types.Message):
    """Вывод списка подписок для отписки."""
    user_id = str(message.from_user.id)
    subs = load_subs()
    user_artists = [{"id": aid, "name": d["name"]} for aid, d in subs["artists"].items() if user_id in d["subscribers"]]
    
    if not user_artists:
        await message.answer("Вы еще не подписаны ни на одного артиста.")
        return

    markup = generate_unsub_markup(user_artists, 0)
    await message.answer("📋 **Ваши подписки:**\nНажмите на артиста, чтобы отписаться:", reply_markup=markup, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("unsub_page:"))
async def process_unsub_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    user_id = str(callback.from_user.id)
    subs = load_subs()
    user_artists = [{"id": aid, "name": d["name"]} for aid, d in subs["artists"].items() if user_id in d["subscribers"]]
    
    if not user_artists:
        await callback.message.edit_text("У вас больше нет подписок.")
        return

    markup = generate_unsub_markup(user_artists, page)
    await callback.message.edit_reply_markup(reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("unsub_art:"))
async def process_unsub_art(callback: CallbackQuery):
    data_parts = callback.data.split(":")
    artist_id = data_parts[1]
    page = int(data_parts[2])
    user_id = str(callback.from_user.id)
    
    subs = load_subs()
    if artist_id in subs["artists"] and user_id in subs["artists"][artist_id]["subscribers"]:
        subs["artists"][artist_id]["subscribers"].remove(user_id)
        save_subs(subs)
        await callback.answer(f"Вы отписались от {subs['artists'][artist_id]['name']}")
    
    user_artists = [{"id": aid, "name": d["name"]} for aid, d in subs["artists"].items() if user_id in d["subscribers"]]
    if not user_artists:
        await callback.message.edit_text("Вы отписались от всех артистов.")
    else:
        if page * 5 >= len(user_artists) and page > 0:
            page -= 1
        markup = generate_unsub_markup(user_artists, page)
        await callback.message.edit_reply_markup(reply_markup=markup)

async def check_artist_updates():
    """Фоновая задача для проверки новых релизов."""
    while True:
        logger.info("Проверка обновлений артистов...")
        subs = load_subs()
        loop = asyncio.get_running_loop()
        changed = False

        for artist_id, data in subs["artists"].items():
            try:
                artist_info = await loop.run_in_executor(executor, ytmusic.get_artist, artist_id)
                singles = artist_info.get('singles', {}).get('results', [])
                
                if singles:
                    latest_track = singles[0]
                    if latest_track['videoId'] != data['last_release']:
                        # Нашли новый трек!
                        data['last_release'] = latest_track['videoId']
                        changed = True
                        
                        notification = (
                            f"🔔 **Новый релиз!**\n\n"
                            f"Исполнитель: {data['name']}\n"
                            f"Трек: {latest_track['title']}\n\n"
                            f"Чтобы скачать, используйте поиск бота."
                        )
                        
                        for user_id in data['subscribers']:
                            try:
                                await bot.send_message(user_id, notification, parse_mode="Markdown")
                            except Exception: pass
            except Exception as e:
                logger.error(f"Ошибка при проверке артиста {data['name']}: {e}")

        if changed:
            save_subs(subs)
        
        # Проверяем раз в 12 часов
        await asyncio.sleep(12 * 3600)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎧 **YouTube Music FLAC Bot**\n\n"
        "Я ищу музыку напрямую в базе YouTube Music (чистый звук, без клипов).\n\n"
        "🔎 **Как пользоваться:**\n"
        "1. Просто поиск трека: `@botname название`\n"
        "2. Поиск альбома: `@botname alb название`\n"
        "3. Поиск артиста: `@botname art имя`\n"
        "4. Подписка на артиста: `/follow имя`\n"
        "5. Список подписок: `/unfollow`"
    )

@dp.inline_query()
async def inline_search(inline_query: types.InlineQuery):
    text = inline_query.query
    if not text or len(text) < 2:
        return

    # Определение режима: Альбом, Артист или Трек
    is_album = False
    is_artist = False
    clean_query = text
    if text.lower().startswith(('alb ', 'альбом ', 'album ')):
        is_album = True
        # Удаляем префикс (первое слово)
        clean_query = " ".join(text.split()[1:])
    elif text.lower().startswith(('art ', 'artist ', 'артист ')):
        is_artist = True
        clean_query = " ".join(text.split()[1:])

    if not clean_query: return

    if is_album:
        search_type = 'albums'
    elif is_artist:
        search_type = 'artists'
    else:
        search_type = 'songs'
    
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(executor, search_ytmusic, clean_query, search_type)

    articles = []
    for item in results:
        # Формируем скрытое сообщение для отправки
        # Если это трек: TYPE:TR
        # Если это альбом: TYPE:AL
        # Если это артист: TYPE:AR
        content_text = f"💿 Выбрано: {item['title']}...\nID: {item['id']} TYPE:{item['type']} #music_load"
        
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

    # === ВАРИАНТ 3: АРТИСТ (ПОДПИСКА) ===
    elif content_type == "AR":
        # Вместо скачивания предлагаем подписаться
        name_match = re.search(r"Выбрано: (.*)\.\.\.", message.text)
        artist_name = name_match.group(1) if name_match else "Артист"
        
        keyboard = [[InlineKeyboardButton(text=f"Подписаться на {artist_name}", callback_data=f"sub_artist:{content_id}")]]
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
        
        await message.reply(f"👤 Это профиль артиста **{artist_name}**. Хотите подписаться на уведомления о новых треках?", 
                            reply_markup=markup, parse_mode="Markdown")

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
        
        # 2. Параллельная загрузка с ограничением (Semaphore)
        sem = asyncio.Semaphore(3) # Качаем по 3 трека одновременно

        async def download_and_send(track_info, index):
            async with sem:
                file_prefix = f"{content_id}_{track_info['id']}"
                file_path, title, duration, artist = await loop.run_in_executor(
                    executor, download_task, track_info['id'], file_prefix
                )
                
                if file_path and os.path.exists(file_path):
                    try:
                        audio = FSInputFile(file_path)
                        await message.reply_audio(
                            audio,
                            title=title,
                            performer=artist,
                            duration=duration,
                            caption=f"💿 {index}/{total}"
                        )
                    except Exception as e:
                        logger.error(f"Error sending {title}: {e}")
                    finally:
                        if os.path.exists(file_path): os.remove(file_path)
                
                # Небольшая пауза, чтобы Telegram не забанил за флуд сообщениями
                await asyncio.sleep(1)

        # Запускаем задачи
        tasks = [download_and_send(track, i) for i, track in enumerate(tracks, 1)]
        await asyncio.gather(*tasks)
        
        await status_msg.edit_text("✅ Альбом загружен.")

# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_artist_updates())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass