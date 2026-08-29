import streamlit as st
import asyncio
import threading
import os
import glob
import time
import random
import gc
import collections
from datetime import datetime
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

# ==========================================
# 1. TELEMETRY & PERSISTENT LOG STATE
# ==========================================
class TelemetryState:
    def __init__(self):
        self.log_history = collections.deque(maxlen=50)
        self.current_status = {"task": "Idle", "details": "Waiting for links..."}

    def log(self, text: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {text}"
        self.log_history.append(entry)
        print(entry, flush=True)

    def set_status(self, task: str, details: str):
        self.current_status["task"] = task
        self.current_status["details"] = details

@st.cache_resource
def get_telemetry():
    return TelemetryState()

GLOBAL_STATE = get_telemetry()

# Global state for asynchronous quality selection
USER_EVENTS = {}
USER_CHOICES = {}

# ==========================================
# 2. ANTI-FLOOD PROGRESS TRACKER
# ==========================================
async def progress_callback(current, total, status_msg, action_text, time_tracker):
    now = time.time()
    if now - time_tracker[0] > 4.0:  # Update progress every 4 seconds
        percent = round(current * 100 / total, 1) if total > 0 else 0
        current_mb = current // 1048576
        total_mb = total // 1048576 if total > 0 else "Unknown"
        try:
            await status_msg.edit_text(
                f"⏳ **{action_text}**\n"
                f"📊 Progress: `{percent}%`\n"
                f"📦 Size: `{current_mb} MB` / `{total_mb} MB`"
            )
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass
        time_tracker[0] = now

# ==========================================
# 3. PYROFORK BOT RUNNER (PROPER THREAD LOOP)
# ==========================================
async def run_pyrofork_bot():
    try:
        # Initialize Client strictly inside the async loop
        app = Client(
            "x_downloader_bot",
            api_id=int(st.secrets["API_ID"]),
            api_hash=str(st.secrets["API_HASH"]),
            bot_token=str(st.secrets["BOT_TOKEN"]),
            in_memory=True
        )

        @app.on_message(filters.command("start") & filters.private)
        async def handle_start(client, message):
            welcome_text = (
                "👋 **Welcome to X (Twitter) Video & Media Downloader Bot!**\n\n"
                "**🌟 Features:**\n"
                "• 🎬 **Max 720p Resolution** (Auto-fallback to 480p/360p if > 1.95 GB)\n"
                "• ⚡ **Accelerated Multi-Threaded Engine** (`aria2c` 16-split)\n"
                "• 📸 **Full Media Support** (Videos, Photo Posts, and Tweet Text)\n"
                "• ⏱️ **5-Second Quality Selector** (Auto-defaults to 720p)\n\n"
                "**How to use:**\n"
                "Send one or multiple X post URLs separated by commas (e.g. `https://x.com/... , https://x.com/...`)"
            )
            await message.reply_text(welcome_text)

        @app.on_callback_query(filters.regex(r"^q_(\d+)$"))
        async def handle_quality_choice(client, callback_query):
            user_id = callback_query.from_user.id
            choice = callback_query.matches[0].group(1)
            if user_id in USER_EVENTS:
                USER_CHOICES[user_id] = choice
                USER_EVENTS[user_id].set()
                await callback_query.answer(f"Selected {choice}p")
            else:
                await callback_query.answer("Selection expired or already running.", show_alert=True)

        @app.on_message(filters.text & filters.private & ~filters.command("start"))
        async def handle_urls(client, message):
            urls = [u.strip() for u in message.text.split(",") if u.strip() and ("x.com" in u or "twitter.com" in u)]
            if not urls:
                return await message.reply_text("❌ No valid X (Twitter) links detected. Please check your URLs.")

            user_id = message.from_user.id
            GLOBAL_STATE.log(f"Received {len(urls)} link(s) from user {user_id}")

            # 1. Quality Selection Prompt
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("720p (Default)", callback_data="q_720"),
                    InlineKeyboardButton("480p", callback_data="q_480"),
                    InlineKeyboardButton("360p", callback_data="q_360")
                ]
            ])
            
            status_msg = await message.reply_text(
                "⚙️ **Select maximum video quality:**\n*(Auto-defaults to 720p in 5 seconds)*",
                reply_markup=keyboard
            )

            USER_EVENTS[user_id] = asyncio.Event()
            USER_CHOICES[user_id] = "720"

            try:
                await asyncio.wait_for(USER_EVENTS[user_id].wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            selected_quality = USER_CHOICES.pop(user_id, "720")
            USER_EVENTS.pop(user_id, None)

            try:
                await status_msg.edit_text(f"✅ Quality locked at **{selected_quality}p**.\n🔄 Queued {len(urls)} post(s)...")
            except Exception:
                pass

            # 2. Dynamic yt-dlp format with fallback under 1.95 GB
            if selected_quality == "720":
                fmt = "bestvideo[height<=720][filesize<1950M]+bestaudio/best[height<=720][filesize<1950M]/bestvideo[height<=480][filesize<1950M]+bestaudio/best[height<=480][filesize<1950M]/best"
            elif selected_quality == "480":
                fmt = "bestvideo[height<=480][filesize<1950M]+bestaudio/best[height<=480][filesize<1950M]/bestvideo[height<=360][filesize<1950M]+bestaudio/best[height<=360][filesize<1950M]/best"
            else:
                fmt = "bestvideo[height<=360][filesize<1950M]+bestaudio/best[height<=360][filesize<1950M]/best"

            ydl_opts = {
                'format': fmt,
                'outtmpl': '%(id)s.%(ext)s',
                'writethumbnail': True,
                'external_downloader': 'aria2c',
                'external_downloader_args': {
                    'aria2c': [
                        '--continue=true',
                        '--summary-interval=1',
                        '--console-log-level=error',
                        '--max-connection-per-server=16',
                        '--split=16',
                        '--min-split-size=1M',
                        '--max-tries=10',
                        '--retry-wait=5',
                        '--timeout=60',
                        '--check-certificate=false',
                        '--async-dns=false',
                    ]
                },
                'quiet': True,
                'no_warnings': True
            }

            # 3. Process URLs sequentially
            for idx, url in enumerate(urls):
                try:
                    GLOBAL_STATE.set_status("Processing", f"Link {idx + 1}/{len(urls)}")
                    await status_msg.edit_text(f"🔍 Analyzing Link {idx + 1}/{len(urls)}...")

                    post_text = ""
                    video_id = str(int(time.time())) + f"_{idx}"

                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        post_text = info.get('description', '')
                        video_id = info.get('id', video_id)

                        # Send Tweet text if available
                        if post_text and post_text.strip():
                            await message.reply_text(f"📝 **Post Content:**\n\n{post_text.strip()}")

                        await status_msg.edit_text(f"⬇️ Downloading Media for Link {idx + 1}...")
                        ydl.download([url])

                    # Identify downloaded files
                    downloaded = glob.glob(f"{video_id}.*")
                    media_files = [f for f in downloaded if not f.endswith(('.jpg', '.jpeg', '.webp', '.png'))]
                    image_files = [f for f in downloaded if f.endswith(('.jpg', '.jpeg', '.webp', '.png'))]

                    time_tracker = [time.time()]

                    if media_files:
                        video_file = media_files[0]
                        await status_msg.edit_text(f"⬆️ Uploading Video ({selected_quality}p)...")
                        await client.send_video(
                            chat_id=message.chat.id,
                            video=video_file,
                            caption=f"🎬 **Video ({selected_quality}p)**\n🔗 {url}",
                            progress=progress_callback,
                            progress_args=(status_msg, "Uploading Video", time_tracker)
                        )
                        if os.path.exists(video_file):
                            os.remove(video_file)

                    elif image_files:
                        img_file = image_files[0]
                        await status_msg.edit_text("⬆️ Uploading Photo Post...")
                        await client.send_photo(
                            chat_id=message.chat.id,
                            photo=img_file,
                            caption=f"📸 **Image Post**\n🔗 {url}"
                        )

                    else:
                        if not post_text:
                            await message.reply_text(f"⚠️ No media found for `{url}`.")

                    # Clean up remaining images/thumbnails
                    for f in glob.glob(f"{video_id}.*"):
                        if os.path.exists(f):
                            os.remove(f)

                except Exception as e:
                    GLOBAL_STATE.log(f"Error processing {url}: {e}")
                    await message.reply_text(f"❌ Error downloading `{url}`: {e}")

                finally:
                    gc.collect()
                    if idx < len(urls) - 1:
                        await status_msg.edit_text("⏳ Waiting 6 seconds before next link...")
                        await asyncio.sleep(6)

            try:
                await status_msg.edit_text("✅ All tasks finished successfully!")
            except Exception:
                pass
            GLOBAL_STATE.set_status("Idle", "Ready for next batch")

        # Start listening
        await app.start()
        GLOBAL_STATE.log("Pyrofork Bot connected and listening.")
        await asyncio.Event().wait()

    except Exception as e:
        GLOBAL_STATE.log(f"CRITICAL ERROR: Bot crashed: {e}")
    finally:
        if 'app' in locals() and app.is_initialized:
            await app.stop()

# ==========================================
# 4. STREAMLIT CACHED BOOTSTRAPPER
# ==========================================
@st.cache_resource
def start_bot_thread():
    def run_async_loop():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_pyrofork_bot())
        except Exception as e:
            GLOBAL_STATE.log(f"Async loop error: {e}")

    threading.Thread(target=run_async_loop, daemon=True).start()

# Boot background thread once
start_bot_thread()

# ==========================================
# 5. STREAMLIT UI DASHBOARD
# ==========================================
st.set_page_config(page_title="X Video Downloader Bot", page_icon="⚡", layout="wide")
st.title("⚡ X (Twitter) Downloader Bot Engine")
st.caption("Active & Connected • Anti-Flood & In-Memory Streaming Enabled")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📊 Engine Status")
    st.metric(label="Current Task", value=GLOBAL_STATE.current_status["task"])
    st.info(GLOBAL_STATE.current_status["details"])

with col2:
    st.subheader("📜 Live Process Console")
    log_area = st.empty()
    log_area.code(
        "\n".join(GLOBAL_STATE.log_history) if GLOBAL_STATE.log_history else "System ready. Waiting for links...",
        language="text"
    )

