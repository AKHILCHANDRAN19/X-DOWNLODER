import streamlit as st
import asyncio
import threading
import os
import glob
import time
import random
import gc
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

# Streamlit UI (Keeps the app active)
st.set_page_config(page_title="X Downloader Bot", page_icon="🤖")
st.title("X Downloader Telegram Bot")
st.success("Bot is running. Memory streaming and Anti-Flood enabled.")

# FIX: Added in_memory=True to prevent Streamlit from wiping the .session file
app = Client(
    "x_bot_session",
    in_memory=True,
    bot_token=st.secrets["BOT_TOKEN"],
    api_id=st.secrets["API_ID"],
    api_hash=st.secrets["API_HASH"]
)

# Global dictionaries to handle asynchronous quality selection
user_events = {}
user_choices = {}

# Anti-Flood Progress Bar (Updates every 5 seconds)
async def progress_callback(current, total, status_msg, action_text, time_tracker):
    now = time.time()
    if now - time_tracker[0] > 5:
        percent = round(current * 100 / total, 1) if total > 0 else 0
        current_mb = current // 1048576
        total_mb = total // 1048576 if total > 0 else "Unknown"
        try:
            await status_msg.edit_text(f"⏳ **{action_text}**\nProgress: {percent}%\nSize: {current_mb}MB / {total_mb}MB")
        except FloodWait as e:
            await asyncio.sleep(e.value) # Respect Telegram rate limits
        except Exception:
            pass
        time_tracker[0] = now

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    welcome_text = (
        "👋 **Welcome to the X (Twitter) Downloader Bot!**\n\n"
        "**Features:**\n"
        "• Selectable Quality (Max 720p)\n"
        "• Supports Videos, Images, and Text posts\n"
        "• Anti-Flood & Memory Optimized\n\n"
        "Just send me X/Twitter URLs (comma separated)."
    )
    await message.reply_text(welcome_text)

@app.on_callback_query()
async def handle_quality_selection(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in user_events:
        user_choices[user_id] = callback_query.data
        user_events[user_id].set() # Trigger the background thread to continue
        await callback_query.answer(f"Selected {callback_query.data}p")
    else:
        await callback_query.answer("Session expired or invalid.", show_alert=True)

@app.on_message(filters.text & ~filters.command("start"))
async def process_urls(client, message):
    urls = [url.strip() for url in message.text.split(",") if url.strip()]
    if not urls:
        return
        
    user_id = message.from_user.id
    
    # 1. Ask for quality preference
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("720p", callback_data="720"),
            InlineKeyboardButton("480p", callback_data="480"),
            InlineKeyboardButton("360p", callback_data="360")
        ]
    ])
    
    status_msg = await message.reply_text(
        "⚙️ **Select video quality:**\n*(Defaults to 720p automatically in 5 seconds)*", 
        reply_markup=keyboard
    )

    # 2. Setup 5-second wait trigger
    user_events[user_id] = asyncio.Event()
    user_choices[user_id] = "720" # Default fallback

    try:
        # Wait up to 5 seconds for the user to press a button
        await asyncio.wait_for(user_events[user_id].wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass # 5 seconds passed without interaction, default remains 720p

    selected_quality = user_choices.pop(user_id, "720")
    user_events.pop(user_id, None)

    try:
        await status_msg.edit_text(f"✅ Quality locked at **{selected_quality}p**.\n🔄 Processing {len(urls)} link(s)...")
    except Exception:
        pass

    # 3. Dynamic yt-dlp format based on choice (Strictly capping at selection, <1.95GB)
    format_string = f'bestvideo[height<={selected_quality}][filesize<1950M]+bestaudio/best[height<={selected_quality}][filesize<1950M]/best'
    
    ydl_opts = {
        'format': format_string,
        'outtmpl': '%(id)s.%(ext)s',
        'writethumbnail': True,
        'external_downloader': 'aria2c',
        'external_downloader_args': {
            'aria2c': [
                '--continue=true', '--summary-interval=1', '--console-log-level=error', 
                '--max-connection-per-server=16', '--split=16', '--min-split-size=1M', 
                '--max-tries=10', '--retry-wait=5', '--timeout=60', 
                '--check-certificate=false', '--async-dns=false'
            ]
        },
        'quiet': True,
        'no_warnings': True
    }

    # 4. Process each URL sequentially
    for index, url in enumerate(urls):
        try:
            await status_msg.edit_text(f"🔍 Analyzing Link {index + 1}/{len(urls)}...")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                post_text = info.get('description', '')
                video_id = info.get('id', 'unknown')
                
                if post_text:
                    await message.reply_text(f"📝 **Post Text:**\n\n{post_text}")

                await status_msg.edit_text(f"⬇️ Downloading Media {index + 1}...")
                ydl.download([url])

            downloaded_files = glob.glob(f"{video_id}.*")
            media_files = [f for f in downloaded_files if not f.endswith('.jpg') and not f.endswith('.webp')]
            image_files = [f for f in downloaded_files if f.endswith('.jpg') or f.endswith('.webp')]

            time_tracker = [time.time()]

            if media_files:
                video_path = media_files[0]
                await status_msg.edit_text(f"⬆️ Uploading Video ({selected_quality}p)...")
                await client.send_video(
                    chat_id=message.chat.id,
                    video=video_path,
                    progress=progress_callback,
                    progress_args=(status_msg, "Uploading Video", time_tracker)
                )
                os.remove(video_path)
            
            elif image_files:
                image_path = image_files[0]
                await status_msg.edit_text(f"⬆️ Uploading Image...")
                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=image_path
                )
            else:
                await message.reply_text(f"⚠️ No media found for {url}. It might be a text-only post.")

            # Clean up residual thumbnails
            for f in image_files:
                if os.path.exists(f):
                    os.remove(f)

        except Exception as e:
            await message.reply_text(f"❌ Error processing {url}: {e}")
            
        finally:
            gc.collect() # Aggressively free RAM to stay under Streamlit limits
            
            if index < len(urls) - 1:
                await status_msg.edit_text("⏳ Pausing briefly to prevent rate limits...")
                await asyncio.sleep(random.randint(5, 8))

    await status_msg.edit_text("✅ All tasks completed.")

# Threading Bypass
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.start()
    loop.run_until_complete(asyncio.Event().wait())

if "bot_started" not in st.session_state:
    st.session_state.bot_started = True
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
