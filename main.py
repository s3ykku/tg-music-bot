import os
import asyncio
import logging
import shutil
import subprocess
import re
import json
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, FSInputFile, URLInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
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

def fix_thumb_url(url):
    """Увеличивает качество обложек от Google/YouTube Music."""
    if not url:
        return url
    if "googleusercontent.com" in url or "ggpht.com" in url:
        return re.sub(r'=[sw]\d+.*$', '=w1200-h1200-l90-rj', url)
    return url

def search_ytmusic(query, search_type='songs'):
    """
    Ищет треки или альбомы через YouTube Music API.
    search_type: 'songs' или 'albums'
    """
    try:
        # filter может быть: songs, videos, albums, artists, playlists
        results = ytmusic.search(query, filter=search_type, limit=20)
        parsed_results = []
        
        for item in results:
            # Обработка ТРЕКОВ
            if search_type == 'songs':
                # Формируем строку артистов (Artist1, Artist2)
                artists = ", ".join([a['name'] for a in item.get('artists', [])])
                album = item.get('album', {}).get('name', 'Single')
                thumb = fix_thumb_url(item['thumbnails'][-1]['url']) if item.get('thumbnails') else None
                
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
                thumb = fix_thumb_url(item['thumbnails'][-1]['url']) if item.get('thumbnails') else None
                
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
                thumb = fix_thumb_url(item['thumbnails'][-1]['url']) if item.get('thumbnails') else None
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
        album_thumb = fix_thumb_url(album.get('thumbnails', [{}])[-1].get('url'))
        return tracks, album.get('title', 'Альбом'), album_thumb
    except Exception as e:
        logger.error(f"Ошибка получения альбома: {e}")
        return [], None, None

def download_task(video_id, filename_prefix):
    """Скачивание и конвертация одного трека."""
    url = f"https://music.youtube.com/watch?v={video_id}"
    filename_base = os.path.join(TEMP_FOLDER, filename_prefix)

    try:
        # 1. Получаем метаданные через yt-dlp (dump-json)
        # Используем subprocess для вызова внешнего exe
        cmd_info = ['yt-dlp', '--dump-json', '--no-playlist', url]
        proc = subprocess.run(cmd_info, capture_output=True, text=True, encoding='utf-8', check=True)
        info = json.loads(proc.stdout)
        
        title = info.get('title', 'Unknown Track')
        duration = info.get('duration', 0)
        artist = info.get('artist') or info.get('uploader') or 'Unknown Artist'

        # 2. Скачиваем трек
        cmd_dl = [
            'yt-dlp',
            '-f', 'ba[ext=m4a]/bestaudio',
            '--embed-thumbnail',
            '--add-metadata',
            '--no-playlist',
            '--no-cache-dir',
            '--no-check-certificate',
            '-o', f'{filename_base}.%(ext)s',
            url
        ]
        
        # Попытки скачивания (2 попытки)
        success = False
        last_err = ""
        for attempt in range(2):
            result = subprocess.run(cmd_dl, capture_output=True, text=True, encoding='utf-8')
            if result.returncode == 0:
                success = True
                break
            last_err = result.stderr
            logger.warning(f"Попытка {attempt+1} для {video_id} не удалась. Ошибка: {last_err.strip()}")
            if attempt == 0:
                import time
                time.sleep(2)

        if not success:
            logger.error(f"Не удалось скачать {video_id} после всех попыток. Причина: {last_err}")
            return None, None, None, None, None, None

        # Ищем, какой файл в итоге создался (m4a или fallback на webm/opus)
        final_filename = None
        for ext in ['m4a', 'webm', 'mp3', 'opus']:
            p = f"{filename_base}.{ext}"
            if os.path.exists(p):
                final_filename = p
                break
        
        if not final_filename:
            return None, None, None, None, None, None

        final_thumb_url = info.get('thumbnail')
        return final_filename, title, duration, artist, None, final_thumb_url
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, None, None, None, None, None

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
            last_single = None
            if artist_data.get('singles', {}).get('results'):
                last_single = artist_data['singles']['results'][0]['videoId']
            
            last_album = None
            if artist_data.get('albums', {}).get('results'):
                last_album = artist_data['albums']['results'][0]['browseId']
            
            subs["artists"][artist_id] = {
                "name": artist_name,
                "last_single": last_single,
                "last_album": last_album,
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
        # Если подписчиков больше нет, удаляем артиста из базы
        if not subs["artists"][artist_id]["subscribers"]:
            del subs["artists"][artist_id]
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
                
                # Проверка синглов (треков)
                singles = artist_info.get('singles', {}).get('results', [])
                if singles:
                    latest_s = singles[0]
                    # Поддержка миграции со старого поля last_release
                    old_s_id = data.get('last_single') or data.get('last_release')
                    if latest_s['videoId'] != old_s_id:
                        data['last_single'] = latest_s['videoId']
                        data.pop('last_release', None) # Удаляем старый ключ
                        await notify_subscribers(data['subscribers'], data['name'], latest_s['title'], "Трек")
                        changed = True

                # Проверка альбомов
                albums = artist_info.get('albums', {}).get('results', [])
                if albums:
                    latest_a = albums[0]
                    if latest_a['browseId'] != data.get('last_album'):
                        data['last_album'] = latest_a['browseId']
                        await notify_subscribers(data['subscribers'], data['name'], latest_a['title'], "Альбом")
                        changed = True

            except Exception as e:
                logger.error(f"Ошибка при проверке артиста {data['name']}: {e}")

        if changed:
            save_subs(subs)
        
        # Проверяем раз в 12 часов
        await asyncio.sleep(12 * 3600)

async def notify_subscribers(user_ids, artist_name, title, release_type):
    """Вспомогательная функция для рассылки уведомлений."""
    logger.info(f"Новый {release_type} у {artist_name}: {title}")
    notification = (
        f"🔔 **Новый {release_type}!**\n\n"
        f"Исполнитель: {artist_name}\n"
        f"Название: {title}\n\n"
        f"Чтобы скачать, используйте поиск бота."
    )
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, notification, parse_mode="Markdown")
        except Exception:
            pass

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎧 **YouTube Music M4A Bot**\n"
        "Бот для поиска и скачивания музыки в высоком качестве.\n\n"
        "🔎 **Поиск через команды (в личке):**\n"
        "• `/song название` — поиск трека\n"
        "• `/album название` — поиск альбома\n"
        "• `/artist название` — поиск артиста\n\n"
        "✨ **Inline-поиск (в любом чате):**\n"
        "Просто начни писать `@имя_бота` и запрос.\n\n"
        "🔔 **Подписки:**\n"
        "• `/follow имя` — подписаться на новинки\n"
        "• `/unfollow` — управление подписками",
        parse_mode="Markdown"
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ЗАГРУЗКИ ---

async def handle_tr(message: types.Message, content_id: str):
    status_msg = await message.reply("⏳ `YouTube Music`: Скачиваю трек в M4A...")
    loop = asyncio.get_running_loop()
    
    file_path, title, duration, artist, thumb_path, thumb_url = await loop.run_in_executor(
        executor, download_task, content_id, content_id
    )
    
    if file_path and os.path.exists(file_path):
        try:
            if os.path.getsize(file_path) > 50 * 1024 * 1024:
                await status_msg.edit_text("❌ Файл слишком велик (> 50MB). Telegram не позволяет ботам отправлять такие файлы.")
                return

            audio = FSInputFile(file_path)
            
            # Приоритет: локальный файл (лучше для Telegram), затем URL
            thumb = None
            if thumb_path and os.path.exists(thumb_path):
                thumb = FSInputFile(thumb_path)
            elif thumb_url:
                thumb = URLInputFile(thumb_url)

            await message.answer_audio(
                audio, 
                title=title, 
                performer=artist,
                duration=duration, 
                thumbnail=thumb
            )
        finally:
            if os.path.exists(file_path): os.remove(file_path)
            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
            await status_msg.delete()
            if message.text and "#music_load" in message.text:
                try: await message.delete()
                except: pass
    else:
        await status_msg.edit_text("❌ Ошибка загрузки.")
        await asyncio.sleep(3)
        await status_msg.delete()
        if message.text and "#music_load" in message.text:
            try: await message.delete()
            except: pass

async def handle_al(message: types.Message, content_id: str):
    status_msg = await message.reply("⏳ `YouTube Music`: Получаю список треков альбома...")
    loop = asyncio.get_running_loop()
    
    tracks, album_title, album_thumb = await loop.run_in_executor(executor, get_album_tracks, content_id)
    
    if not tracks:
        await status_msg.edit_text("❌ Не удалось получить информацию об альбоме.")
        await asyncio.sleep(3)
        await status_msg.delete()
        if message.text and "#music_load" in message.text:
            try: await message.delete()
            except: pass
        return

    total = len(tracks)
    await status_msg.edit_text(f"💿 Альбом: **{album_title}**\nТреков: {total}. Начинаю загрузку...")
    
    sem = asyncio.Semaphore(3)
    downloaded_results = [None] * total # Сохраняем порядок треков

    async def download_and_send(track_info, index):
        async with sem:
            file_prefix = f"{content_id}_{track_info['id']}"
            res = await loop.run_in_executor(
                executor, download_task, track_info['id'], file_prefix
            )
            downloaded_results[index] = res

    tasks = [download_and_send(track, i) for i, track in enumerate(tracks)]
    await asyncio.gather(*tasks)

    # Отправка по одному треку (сохраняя порядок)
    for res in downloaded_results:
        if res and res[0]:
            path, title, duration, artist, thumb_path, thumb_url = res
            
            if os.path.getsize(path) > 50 * 1024 * 1024:
                logger.warning(f"Файл {title} слишком велик (> 50MB) и будет пропущен.")
            else:
                try:
                    thumb = None
                    if thumb_path and os.path.exists(thumb_path):
                        thumb = FSInputFile(thumb_path)
                    elif thumb_url:
                        thumb = URLInputFile(thumb_url)
                    elif album_thumb:
                        thumb = URLInputFile(album_thumb)

                    await message.answer_audio(
                        FSInputFile(path),
                        title=title,
                        performer=artist,
                        duration=duration,
                        thumbnail=thumb
                    )
                except Exception as e:
                    logger.error(f"Error sending {title}: {e}")
            
            # Очистка
            if os.path.exists(path): os.remove(path)
            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
            await asyncio.sleep(0.5) # Небольшая пауза между отправками

    await status_msg.delete()
    if message.text and "#music_load" in message.text:
        try: await message.delete()
        except: pass

async def handle_ar(message: types.Message, content_id: str, artist_name: str = None):
    if not artist_name:
        loop = asyncio.get_running_loop()
        artist_data = await loop.run_in_executor(executor, ytmusic.get_artist, content_id)
        artist_name = artist_data.get('name', 'Артист')
        
    keyboard = [[InlineKeyboardButton(text=f"Подписаться на {artist_name}", callback_data=f"sub_artist:{content_id}")]]
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    await message.reply(f"👤 Это профиль артиста **{artist_name}**. Хотите подписаться на уведомления о новых треках?", 
                        reply_markup=markup, parse_mode="Markdown")
    if message.text and "#music_load" in message.text:
        try: await message.delete()
        except: pass

# --- ОБРАБОТЧИКИ ПОИСКА ЧЕРЕЗ КОМАНДЫ ---

# --- ПАГИНАЦИЯ ПОИСКА ---

def generate_search_markup(results, query, stype, page):
    """Генерирует клавиатуру для результатов поиска с пагинацией."""
    items_per_page = 5
    start = page * items_per_page
    end = start + items_per_page
    current_items = results[start:end]
    
    keyboard = []
    for item in current_items:
        btn_text = f"{item['title']} ({item['subtitle']})"
        if len(btn_text) > 50: btn_text = btn_text[:47] + "..."
            
        keyboard.append([InlineKeyboardButton(
            text=btn_text, 
            callback_data=f"select_{item['type']}:{item['id']}"
        )])
    
    nav_row = []
    # Ограничиваем длину запроса для callback_data (лимит 64 байта)
    safe_query = query[:40]
    
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"sp:{stype}:{page-1}:{safe_query}"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"sp:{stype}:{page+1}:{safe_query}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.callback_query(F.data.startswith("sp:"))
async def process_search_pagination(callback: CallbackQuery):
    parts = callback.data.split(":")
    stype = parts[1]
    page = int(parts[2])
    query = ":".join(parts[3:])
    
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(executor, search_ytmusic, query, stype)
    
    if not results:
        await callback.answer("Ничего не найдено.")
        return

    markup = generate_search_markup(results, query, stype, page)
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except Exception:
        pass
    await callback.answer()

@dp.message(Command("song", "album", "artist"))
async def cmd_search(message: types.Message, command: Command):
    query = command.args
    cmd = command.command.lower()
    
    if not query:
        hints = {"song": "трека", "album": "альбома", "artist": "артиста"}
        await message.answer(f"Введите название {hints.get(cmd)}: `/{cmd} Название`", parse_mode="Markdown")
        return

    search_types = {"song": "songs", "album": "albums", "artist": "artists"}
    stype = search_types[cmd]
    
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(executor, search_ytmusic, query, stype)
    
    if not results:
        await message.answer("Ничего не найдено.")
        return

    markup = generate_search_markup(results, query, stype, 0)
    await message.answer(f"🔍 Результаты поиска {cmd}:", reply_markup=markup)

@dp.callback_query(F.data.startswith("select_"))
async def process_select_callback(callback: CallbackQuery):
    parts = callback.data.split(":")
    ctype = parts[0].split("_")[1]
    cid = parts[1]
    await callback.answer()
    if ctype == "TR":
        await handle_tr(callback.message, cid)
    elif ctype == "AL":
        await handle_al(callback.message, cid)
    elif ctype == "AR":
        await handle_ar(callback.message, cid)

@dp.message(F.text.contains("#music_load"))
async def process_download(message: types.Message):
    # Парсинг данных из сообщения
    id_match = re.search(r"ID: ([\w\.-]+)", message.text) # ID может содержать дефисы и точки
    type_match = re.search(r"TYPE:(\w+)", message.text)
    
    if not id_match or not type_match: return

    content_id = id_match.group(1)
    content_type = type_match.group(1)
    
    if content_type == "TR":
        await handle_tr(message, content_id)
    elif content_type == "AR":
        name_match = re.search(r"Выбрано: (.*)\.\.\.", message.text)
        artist_name = name_match.group(1) if name_match else None
        await handle_ar(message, content_id, artist_name)
    elif content_type == "AL":
        await handle_al(message, content_id)

# --- НАСТРОЙКА МЕНЮ КОМАНД ---
async def set_main_menu(bot: Bot):
    main_menu_commands = [
        BotCommand(command="song", description="🔍 Поиск трека"),
        BotCommand(command="album", description="💿 Поиск альбома"),
        BotCommand(command="artist", description="👤 Поиск артиста"),
        BotCommand(command="follow", description="🔔 Подписаться"),
        BotCommand(command="unfollow", description="🔕 Отписаться"),
        BotCommand(command="start", description="📖 Инструкция")
    ]
    await bot.set_my_commands(main_menu_commands)

# --- ЗАПУСК ---
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await set_main_menu(bot)
    asyncio.create_task(check_artist_updates())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass