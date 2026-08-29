import streamlit as st
import asyncio
import threading
import os
import glob
import time
import gc
import shutil
import zipfile
import tarfile
import subprocess
import collections
import urllib.request
from datetime import datetime
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified

# ==========================================
# 1. TELEMETRY & PERSISTENT LOG STATE
# ==========================================
class TelemetryState:
    def __init__(self):
        self.log_history = collections.deque(maxlen=60)
        self.current_status = {"task": "Idle", "details": "Waiting for links or commands..."}

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

USER_EVENTS = {}
USER_CHOICES = {}

# ==========================================
# 2. TELEGRAM PROGRESS CARD GENERATORS
# ==========================================
def format_size(size_bytes):
    if not size_bytes or size_bytes < 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    val = float(size_bytes)
    while val >= 1024.0 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.2f} {units[idx]}"

def format_time(seconds):
    if seconds is None or seconds < 0:
        return "Calculating..."
    s = int(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h, {m}m, {s}s"
    elif m > 0:
        return f"{m}m, {s}s"
    else:
        return f"{s}s"

def build_progress_card(action_name: str, current: int, total: int, speed: float, eta: float) -> str:
    if total and total > 0:
        pct = (current / total) * 100
        filled_blocks = max(0, min(10, int(pct / 10)))
        bar = "▪" * filled_blocks + "▫" * (10 - filled_blocks)
        tot_str = format_size(total)
        pct_str = f"{pct:.2f}%"
        eta_str = format_time(eta)
    else:
        bar = "▫" * 10
        pct_str = "In Progress"
        tot_str = "Calculating..."
        eta_str = "Calculating..."

    cur_str = format_size(current)
    spd_str = f"{format_size(speed)}/sec" if speed > 0 else "0 B/sec"

    return (
        f"{action_name}: {pct_str}\n"
        f"[{bar}]\n"
        f"{cur_str} of {tot_str}\n"
        f"Speed: {spd_str}\n"
        f"ETA: {eta_str}"
    )

# Native yt-dlp progress hook (YouTube)
def make_ydl_progress_hook(status_msg, loop, tracker):
    def hook(d):
        if d.get('status') == 'downloading':
            now = time.time()
            if now - tracker['last_update'] > 3.0:
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                speed = d.get('speed', 0) or 0
                eta = d.get('eta', 0) or 0

                text = build_progress_card("Downloading", downloaded, total, speed, eta)
                tracker['last_update'] = now
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(text),
                        loop
                    )
                except Exception:
                    pass
    return hook

# Disk-based monitor for aria2c downloads (X / Twitter)
async def monitor_aria2_download(video_id: str, total_bytes: int, status_msg, stop_event: asyncio.Event):
    start_time = time.time()
    last_update = 0.0
    last_bytes = 0

    while not stop_event.is_set():
        await asyncio.sleep(1.0)
        now = time.time()

        matched_files = glob.glob(f"{video_id}*")
        current_bytes = sum(os.path.getsize(f) for f in matched_files if os.path.isfile(f) and not f.endswith(('.jpg', '.png', '.webp')))

        if now - last_update > 3.0 and current_bytes > 0:
            elapsed = now - start_time
            speed = (current_bytes - last_bytes) / (now - last_update) if last_update > 0 else (current_bytes / elapsed)
            last_bytes = current_bytes

            eta = (total_bytes - current_bytes) / speed if (speed > 0 and total_bytes > current_bytes) else None

            text = build_progress_card("Downloading", current_bytes, total_bytes, speed, eta)
            last_update = now
            try:
                await status_msg.edit_text(text)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except (MessageNotModified, Exception):
                pass

# Upload progress callback for Pyrofork
async def upload_progress_callback(current, total, status_msg, tracker):
    now = time.time()
    if now - tracker['last_update'] > 3.0:
        elapsed = now - tracker['start_time']
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0

        text = build_progress_card("Uploading", current, total, speed, eta)
        tracker['last_update'] = now
        try:
            await status_msg.edit_text(text)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except (MessageNotModified, Exception):
            pass

# ==========================================
# 3. VIDEO METADATA & STREAM HELPERS
# ==========================================
def extract_video_thumbnail(video_path: str) -> str:
    thumb_path = f"{video_path}.thumb.jpg"
    try:
        cmd = [
            "ffmpeg", "-y", "-ss", "00:00:01", "-i", video_path,
            "-vframes", "1", "-vf", "scale=320:-1", "-q:v", "2", thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path

        cmd[2] = "00:00:00"
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None

def get_video_specs(video_path: str):
    duration, width, height = 0, 1280, 720
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip()]
        for l in lines:
            try:
                if "." in l:
                    duration = int(float(l))
                elif l.isdigit():
                    if width == 1280:
                        width = int(l)
                    else:
                        height = int(l)
            except Exception:
                pass
    except Exception:
        pass
    return duration, width, height

def ensure_under_telegram_limit(video_path: str, max_bytes: int = 1950 * 1024 * 1024) -> str:
    if not os.path.exists(video_path):
        return video_path

    current_size = os.path.getsize(video_path)
    if current_size <= max_bytes:
        return video_path

    GLOBAL_STATE.log(f"Video ({format_size(current_size)}) exceeds 1.95 GB. Applying disk compression...")
    duration, _, _ = get_video_specs(video_path)
    compressed_path = f"{video_path}.compressed.mp4"

    target_size_bits = 1800 * 1024 * 1024 * 8
    if duration > 0:
        target_bitrate_kbps = max(250, int((target_size_bits / duration) / 1000) - 96)
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{target_bitrate_kbps}k",
            "-maxrate", f"{int(target_bitrate_kbps * 1.2)}k", "-bufsize", f"{int(target_bitrate_kbps * 2)}k",
            "-vf", "scale=-2:480", "-c:a", "aac", "-b:a", "96k",
            compressed_path
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-vf", "scale=-2:480", "-c:a", "aac", "-b:a", "96k",
            compressed_path
        ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 0:
        os.remove(video_path)
        os.rename(compressed_path, video_path)
        GLOBAL_STATE.log(f"Compression complete: {format_size(os.path.getsize(video_path))}")

    return video_path

def get_exact_total_size(info: dict, is_youtube: bool, selected_quality: str) -> int:
    target_h = int(selected_quality) if selected_quality.isdigit() else 720
    formats = info.get('formats', [])

    # Check direct format matches
    for f in formats:
        h = f.get('height') or 0
        if h <= target_h:
            size = f.get('filesize') or f.get('filesize_approx')
            if size and size > 0:
                return size

            # Probe HTTP header for X/Twitter direct MP4s
            url = f.get('url', '')
            if not is_youtube and url.startswith('http') and '.m3u8' not in url:
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}, method='HEAD')
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        cl = resp.headers.get('Content-Length')
                        if cl and cl.isdigit():
                            return int(cl)
                except Exception:
                    pass

    # Fallback to duration * bitrate estimation
    duration = info.get('duration', 0)
    tbr = info.get('tbr', 0)
    if duration and tbr:
        return int((tbr * 1024 / 8) * duration)

    return 0

# ==========================================
# 4. PYROFORK BOT RUNNER
# ==========================================
async def run_pyrofork_bot():
    try:
        app = Client(
            "media_downloader_bot",
            api_id=int(st.secrets["API_ID"]),
            api_hash=str(st.secrets["API_HASH"]),
            bot_token=str(st.secrets["BOT_TOKEN"]),
            in_memory=True,
            max_concurrent_transmissions=3
        )

        @app.on_message(filters.command("start") & filters.private)
        async def handle_start(client, message):
            welcome_text = (
                "👋 **Welcome to the Media Downloader & Unpack Bot!** ⚡\n\n"
                "**🌟 Features:**\n"
                "• 🚀 **High-Speed Direct Engine:**\n"
                "   - **X (Twitter):** `aria2c` 16-connection parallel acceleration\n"
                "   - **YouTube:** Multi-fragment stream extraction (Public & Unlisted)\n"
                "• 🎛️ **Resolution Control:** Max 720p with auto 480p/360p fallback under 1.95 GB\n"
                "• 📊 **Accurate Live Telemetry Cards:** Percentage, Speed & ETA\n"
                "• 🖼️ **Native Video Thumbnails:** Screenshot capture & video metadata\n"
                "• 🗜️ **Archive Unpacker (`/unzip`):** Uncompresses `.zip`, `.tar`, `.gz`, etc.\n\n"
                "**Usage:**\n"
                "• Send one or more X/YouTube links separated by commas.\n"
                "• Reply with `/unzip` to any uploaded archive to unpack it."
            )
            await message.reply_text(welcome_text)

        @app.on_callback_query(filters.regex(r"^q_(\d+)$"))
        async def handle_quality_choice(client, callback_query):
            user_id = callback_query.from_user.id
            choice = callback_query.matches[0].group(1)
            if user_id in USER_EVENTS:
                USER_CHOICES[user_id] = choice
                USER_EVENTS[user_id].set()
                await callback_query.answer(f"Quality locked at {choice}p")
            else:
                await callback_query.answer("Selection expired or already running.", show_alert=True)

        # ----------------------------------------------------
        # ARCHIVE UNPACKER (/unzip)
        # ----------------------------------------------------
        @app.on_message(filters.command("unzip") & filters.private)
        async def handle_unzip(client, message):
            if not message.reply_to_message or not message.reply_to_message.document:
                return await message.reply_text("❌ Reply `/unzip` directly to a compressed file (`.zip`, `.tar`, `.gz`).")

            doc = message.reply_to_message.document
            user_id = message.from_user.id
            work_dir = os.path.abspath(f"unzip_{user_id}_{int(time.time())}")
            os.makedirs(work_dir, exist_ok=True)
            archive_path = os.path.join(work_dir, doc.file_name or "archive.zip")

            status_msg = await message.reply_text("📥 Initializing archive download...")
            tracker = {'start_time': time.time(), 'last_update': 0.0}

            try:
                await client.download_media(
                    message=message.reply_to_message,
                    file_name=archive_path,
                    progress=upload_progress_callback,
                    progress_args=(status_msg, tracker)
                )

                await status_msg.edit_text("🗜️ **Extracting archive files...**")
                extract_folder = os.path.join(work_dir, "extracted")
                os.makedirs(extract_folder, exist_ok=True)

                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                        zip_ref.extractall(extract_folder)
                elif tarfile.is_tarfile(archive_path):
                    with tarfile.open(archive_path, 'r:*') as tar_ref:
                        tar_ref.extractall(extract_folder)
                else:
                    try:
                        shutil.unpack_archive(archive_path, extract_folder)
                    except Exception as e:
                        return await status_msg.edit_text(f"❌ Archive extraction error: {e}")

                if os.path.exists(archive_path):
                    os.remove(archive_path)

                all_extracted = []
                for root, _, files in os.walk(extract_folder):
                    for file in files:
                        if not file.startswith(".") and "__MACOSX" not in root:
                            all_extracted.append(os.path.join(root, file))

                if not all_extracted:
                    return await status_msg.edit_text("⚠️ No valid files found inside the archive.")

                await status_msg.edit_text(f"📦 Found **{len(all_extracted)}** file(s). Uploading...")

                for idx, file_path in enumerate(all_extracted):
                    file_name = os.path.basename(file_path)
                    up_tracker = {'start_time': time.time(), 'last_update': 0.0}

                    await status_msg.edit_text(f"⬆️ Uploading ({idx + 1}/{len(all_extracted)}): `{file_name}`")

                    await client.send_document(
                        chat_id=message.chat.id,
                        document=file_path,
                        caption=f"📄 `{file_name}` ({format_size(os.path.getsize(file_path))})",
                        progress=upload_progress_callback,
                        progress_args=(status_msg, up_tracker)
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                    gc.collect()
                    await asyncio.sleep(2)

                await status_msg.edit_text("✅ All archive files extracted and uploaded successfully!")

            except Exception as e:
                GLOBAL_STATE.log(f"Unzip Error: {e}")
                await status_msg.edit_text(f"❌ Extraction error: {e}")
            finally:
                if os.path.exists(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
                gc.collect()

        # ----------------------------------------------------
        # MEDIA URL DOWNLOADER
        # ----------------------------------------------------
        @app.on_message(filters.text & filters.private & ~filters.command(["start", "unzip"]))
        async def handle_media_urls(client, message):
            urls = [u.strip() for u in message.text.split(",") if u.strip() and ("x.com" in u or "twitter.com" in u or "youtube.com" in u or "youtu.be" in u)]
            if not urls:
                return await message.reply_text("❌ No valid X (Twitter) or YouTube links detected.")

            user_id = message.from_user.id
            GLOBAL_STATE.log(f"Queued {len(urls)} URLs from user {user_id}")

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
                await status_msg.edit_text(f"✅ Quality locked at **{selected_quality}p**.\n🔄 Processing {len(urls)} link(s)...")
            except Exception:
                pass

            running_loop = asyncio.get_running_loop()

            for idx, url in enumerate(urls):
                video_id = f"media_{int(time.time())}_{idx}"
                is_youtube = ("youtube.com" in url or "youtu.be" in url)

                try:
                    engine_name = "YouTube Engine" if is_youtube else "aria2c Engine"
                    GLOBAL_STATE.set_status("Processing", f"Link {idx + 1}/{len(urls)} ({engine_name})")
                    await status_msg.edit_text(f"🔍 Analyzing Link {idx + 1}/{len(urls)} via {engine_name}...")

                    # 1. Fetch metadata first
                    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
                        info = await asyncio.to_thread(ydl.extract_info, url, download=False)

                    post_text = info.get('description') or info.get('title') or ""

                    # 2. SEND POST TEXT FIRST AS A SEPARATE MESSAGE
                    if post_text and post_text.strip():
                        await message.reply_text(f"📝 **Post Content:**\n\n{post_text.strip()}")

                    # 3. Calculate exact total size
                    total_bytes = get_exact_total_size(info, is_youtube, selected_quality)

                    # 4. Configure optimized format strings
                    if is_youtube:
                        fmt = (
                            f"bestvideo[height<={selected_quality}][ext=mp4]+bestaudio[ext=m4a]/"
                            f"bestvideo[height<={selected_quality}]+bestaudio/"
                            f"best[height<={selected_quality}]/"
                            f"best[height<=480]/best"
                        )
                        dl_tracker = {'last_update': 0.0}
                        ydl_opts = {
                            'format': fmt,
                            'outtmpl': f'{video_id}.%(ext)s',
                            'writethumbnail': True,
                            'concurrent_fragment_downloads': 8,
                            'progress_hooks': [make_ydl_progress_hook(status_msg, running_loop, dl_tracker)],
                            'quiet': True,
                            'no_warnings': True
                        }
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            await asyncio.to_thread(ydl.download, [url])
                    else:
                        # Prioritize direct progressive MP4 to enable maximum 16-connection aria2c speed
                        fmt = (
                            f"best[height<={selected_quality}][protocol^=http]/"
                            f"best[height<={selected_quality}]/"
                            f"best[height<=480][protocol^=http]/"
                            f"best[height<=480]/"
                            f"best[height<=720]"
                        )
                        ydl_opts = {
                            'format': fmt,
                            'outtmpl': f'{video_id}.%(ext)s',
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
                        stop_monitor = asyncio.Event()
                        monitor_task = asyncio.create_task(
                            monitor_aria2_download(video_id, total_bytes, status_msg, stop_monitor)
                        )
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                await asyncio.to_thread(ydl.download, [url])
                        finally:
                            stop_monitor.set()
                            await monitor_task

                    # 5. Identify downloaded files
                    downloaded = glob.glob(f"{video_id}.*")
                    media_files = [f for f in downloaded if not f.endswith(('.jpg', '.jpeg', '.webp', '.png', '.thumb.jpg'))]
                    image_files = [f for f in downloaded if f.endswith(('.jpg', '.jpeg', '.webp', '.png')) and not f.endswith('.thumb.jpg')]

                    # 6. Upload Video
                    if media_files:
                        video_file = media_files[0]
                        video_file = await asyncio.to_thread(ensure_under_telegram_limit, video_file)

                        thumb_file = extract_video_thumbnail(video_file)
                        duration, width, height = get_video_specs(video_file)

                        caption = f"🎬 **Video ({selected_quality}p)**\n🔗 {url}"

                        up_tracker = {'start_time': time.time(), 'last_update': 0.0}
                        await status_msg.edit_text(f"⬆️ Uploading Video ({selected_quality}p)...")

                        await client.send_video(
                            chat_id=message.chat.id,
                            video=video_file,
                            caption=caption,
                            thumb=thumb_file,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                            progress=upload_progress_callback,
                            progress_args=(status_msg, up_tracker)
                        )

                        if os.path.exists(video_file): os.remove(video_file)
                        if thumb_file and os.path.exists(thumb_file): os.remove(thumb_file)

                    # 7. Upload Photo
                    elif image_files:
                        img_file = image_files[0]
                        img_caption = f"📸 **Image Post**\n🔗 {url}"

                        await status_msg.edit_text("⬆️ Uploading Photo...")
                        await client.send_photo(
                            chat_id=message.chat.id,
                            photo=img_file,
                            caption=img_caption
                        )

                    for f in glob.glob(f"{video_id}.*"):
                        if os.path.exists(f):
                            os.remove(f)

                except Exception as e:
                    GLOBAL_STATE.log(f"Error processing {url}: {e}")
                    await message.reply_text(f"❌ Error downloading `{url}`: {e}")

                finally:
                    gc.collect()
                    if idx < len(urls) - 1:
                        await status_msg.edit_text("⏳ Waiting 4 seconds before next item...")
                        await asyncio.sleep(4)

            try:
                await status_msg.edit_text("✅ All tasks completed successfully!")
            except Exception:
                pass
            GLOBAL_STATE.set_status("Idle", "Ready for next batch")

        await app.start()
        GLOBAL_STATE.log("Pyrofork Bot connected and operational.")
        await asyncio.Event().wait()

    except Exception as e:
        GLOBAL_STATE.log(f"CRITICAL ERROR: Bot crashed: {e}")
    finally:
        if 'app' in locals() and app.is_initialized:
            await app.stop()

# ==========================================
# 5. STREAMLIT BOOTSTRAPPER & UI DASHBOARD
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

start_bot_thread()

st.set_page_config(page_title="Media Downloader & Unpack Bot", page_icon="⚡", layout="wide")
st.title("⚡ Media Downloader & Unpack Engine")
st.caption("Active • aria2c (X) & Multi-Fragment (YouTube) • Strict <1.95GB Fallback • Anti-Flood")

col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("📊 Engine Status")
    st.metric(label="Current Task", value=GLOBAL_STATE.current_status["task"])
    st.info(GLOBAL_STATE.current_status["details"])

with col2:
    st.subheader("📜 Live Telemetry Console")
    log_area = st.empty()
    log_area.code(
        "\n".join(GLOBAL_STATE.log_history) if GLOBAL_STATE.log_history else "System ready. Listening for updates...",
        language="text"
    )

