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

# Streamlit UI
st.set_page_config(page_title="X Downloader Bot", page_icon="🤖")
st.title("X Downloader Telegram Bot")
st.success("Bot is running in the background thread. Memory streaming enabled.")

# Initialize Pyrofork Client
app = Client(
    "x_bot_session",
    bot_token=st.secrets["BOT_TOKEN"],
    api_id=st.secrets["API_ID"],
    api_hash=st.secrets["API_HASH"]
)

async def progress_callback(current, total, status_msg, action_text, time_tracker):
    now = time.time()
    if now - time_tracker[0] > 3:
        percent = round(current * 100 / total, 1) if total > 0 else 0
        current_mb = current // 1048576
        total_mb = total // 1048576 if total > 0 else "Unknown"
        try:
            await status_msg.edit_text(f"⏳ **{action_text}**\nProgress: {percent}%\nSize: {current_mb}MB / {total_mb}MB")
        except Exception:
            pass
        time_tracker[0] = now

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    welcome_text = (
        "👋 **Welcome to the X (Twitter) Downloader Bot!**\n\n"
        "**Features:**\n"
        "• Max 720p resolution (Auto-fallback to 480p if > 1.95GB)\n"
        "• Supports Videos, Images, and Text posts\n"
        "• Accelerated downloads via aria2\n\n"
        "**How to use:**\n"
        "Just send me X/Twitter URLs. You can send multiple URLs separated by commas."
    )
    await message.reply_text(welcome_text)

@app.on_message(filters.text & ~filters.command("start"))
async def process_urls(client, message):
    urls = [url.strip() for url in message.text.split(",") if url.strip()]
    
    if not urls:
        return
        
    status_msg = await message.reply_text(f"🔄 Queued {len(urls)} link(s) for processing...")
    
    ydl_opts = {
        'format': 'bestvideo[height<=720][filesize<1950M]+bestaudio/best[height<=720][filesize<1950M]/bestvideo[height<=480][filesize<1950M]+bestaudio/best[height<=480][filesize<1950M]/best',
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
                post_text = info.get('description', 'No text found.')
                video_id = info.get('id', 'unknown')
                
                if post_text:
                    await message.reply_text(f"📝 **Post Text:**\n\n{post_text}")

                await status_msg.edit_text(f"⬇️ Downloading Media for Link {index + 1}...")
                ydl.download([url])

            downloaded_files = glob.glob(f"{video_id}.*")
            media_files = [f for f in downloaded_files if not f.endswith('.jpg') and not f.endswith('.webp')]
            image_files = [f for f in downloaded_files if f.endswith('.jpg') or f.endswith('.webp')]

            time_tracker = [time.time()]

            if media_files:
                video_path = media_files[0]
                await status_msg.edit_text(f"⬆️ Uploading Video...")
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
                await asyncio.sleep(random.randint(7, 10))

    await status_msg.edit_text("✅ All tasks completed.")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.start()
    loop.run_until_complete(asyncio.Event().wait())

if "bot_started" not in st.session_state:
    st.session_state.bot_started = True
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
