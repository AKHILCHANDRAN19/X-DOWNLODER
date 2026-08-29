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

# Streamlit UI
st.set_page_config(page_title="X Downloader Bot", page_icon="🤖")
st.title("X Downloader Telegram Bot")
st.success("Bot is successfully running in the background. Anti-Flood & Memory streaming active.")

# 1. Cache the Client so Streamlit doesn't create a new bot on every UI refresh
@st.cache_resource
def get_bot():
    return Client(
        "x_bot_session",
        in_memory=True, # Prevents Streamlit's ephemeral storage from wiping the session
        bot_token=st.secrets["BOT_TOKEN"],
        api_id=st.secrets["API_ID"],
        api_hash=st.secrets["API_HASH"]
    )

app = get_bot()

# 2. Cache global dictionaries so they survive Streamlit background reruns
@st.cache_resource
def get_state():
    return {"user_events": {}, "user_choices": {}}

state = get_state()
user_events = state["user_events"]
user_choices = state["user_choices"]

# 3. Anti-Flood Progress Callback (Updates every 5 seconds)
async def progress_callback(current, total, status_msg, action_text, time_tracker):
    now = time.time()
    if now - time_tracker[0] > 5:
        percent = round(current * 100 / total, 1) if total > 0 else 0
        current_mb = current // 1048576
        total_mb = total // 1048576 if total > 0 else "Unknown"
        try:
            await status_msg.edit_text(f"⏳ **{action_text}**\nProgress: {percent}%\nSize: {current_mb}MB / {total_mb}MB")
        except FloodWait as e:
            await asyncio.sleep(e.value) # Respects Telegram's strict flood limits
        except Exception:
            pass
        time_tracker[0] = now

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    welcome_text = (
        "👋 **Welcome to the X (Twitter) Downloader Bot!**\n\n"
        "**Features:**\n"
        "• Selectable Quality (Max 720p fallback)\n"
        "• Supports Videos, Images, and Text posts\n"
        "• Anti-Flood & Memory Optimized\n\n"
        "Just send me X/Twitter URLs (comma separated)."
    )
    await message.reply_text(welcome_text)

# Handles the inline keyboard button presses
@app.on_callback_query()
async def handle_quality_selection(client, callback_query):
    user_id = callback_query.from_user.id
    if user_id in user_events:
        user_choices[user_id] = callback_query.data
        user_events[user_id].set() # Triggers the waiting process_urls thread to continue
        await callback_query.answer(f"Selected {callback_query.data}p")
    else:
        await callback_query.answer("Session expired or already processing.", show_alert=True)

@app.on_message(filters.text & ~filters.command("start"))
async def process_urls(client, message):
    urls = [url.strip() for url in message.text.split(",") if url.strip()]
    if not urls:
        return
        
    user_id = message.from_user.id
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("720p", callback_data="720"),
            InlineKeyboardButton("480p", callback_data="480"),
            InlineKeyboardButton("360p", callback_data="360")
        ]
    ])
    
    status_msg = await message.reply_text(
        "⚙️ **Select video quality:**\n*(Automatically defaults to 720p in 5 seconds)*", 
        reply_markup=keyboard
    )

    user_events[user_id] = asyncio.Event()
    user_choices[user_id] = "720" # The default fallback

    try:
        # Pauses script for up to 5 seconds waiting for the user's click
        await asyncio.wait_for(user_events[user_id].wait(), timeout=5.0)
    except asyncio.TimeoutError:
        pass 

    selected_quality = user_choices.pop(user_id, "720")
    user_events.pop(user_id, None)

    try:
        await status_msg.edit_text(f"✅ Quality locked at **{selected_quality}p**.\n🔄 Processing {len(urls)} link(s)...")
    except Exception:
        pass

    # Dynamic yt-dlp format strict cap based on selection
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

            for f in image_files:
                if os.path.exists(f):
                    os.remove(f)

        except Exception as e:
            await message.reply_text(f"❌ Error processing {url}: {e}")
            
        finally:
            gc.collect() 
            
            if index < len(urls) - 1:
                await status_msg.edit_text("⏳ Pausing briefly to prevent rate limits...")
                await asyncio.sleep(random.randint(5, 8))

    try:
        await status_msg.edit_text("✅ All tasks completed.")
    except Exception:
        pass

# 4. The PROPER Threading Bypass (Now correctly awaiting app.start)
async def boot_bot():
    await app.start()
    await asyncio.Event().wait()

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(boot_bot())

@st.cache_resource
def start_bot_thread():
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    return True

# Boot the thread once per server deployment
start_bot_thread()

