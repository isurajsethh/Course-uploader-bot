import os
import re
import json
import time
import math
import shutil
import asyncio
import logging
import subprocess
from datetime import datetime
from threading import Thread

import aiohttp
import uvloop
from flask import Flask
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import FloodWait, MessageNotModified

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- Configuration ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "8283878643"))
PORT = int(os.environ.get("PORT", "8080"))

WORK_DIR = "downloads"
os.makedirs(WORK_DIR, exist_ok=True)
STATE_FILE = "bot_state.json"

# --- Keep-Alive Web Server ---
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot is Running 24x7!", 200

def run_flask():
    web_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# --- Persistent State Management ---
DEFAULT_STATE = {
    "target_chat": None,
    "topic_mode": "off",  # off | manual | auto
    "manual_topic_id": None,
    "batch_name": "Target Batch 2026-27",
    "credit_name": "Mr. Sanskari",
    "credit_link": "https://t.me/hello_csc_new",
    "start_index": 1,
    "range_start": 1,
    "range_end": 0,  # 0 means all
    "filter_mode": "all",  # all | video | pdf
    "thumb_path": None,
    "awaiting_input": None,
    "last_txt_path": None,
}

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_STATE.items():
                    data.setdefault(k, v)
                return data
        except Exception:
            pass
    return DEFAULT_STATE.copy()

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

STATE = load_state()
IS_RUNNING = False
STOP_REQUESTED = False
TOPIC_CACHE = {}

# --- Pyrogram Client ---
app = Client(
    "course_uploader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
)

# --- Helper Functions ---
def human_bytes(size):
    if not size or size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    i = int(math.floor(math.log(size, 1024))) if size > 0 else 0
    i = min(i, len(units) - 1)
    return f"{size / (1024 ** i):.2f} {units[i]}"

def format_eta(seconds):
    if not seconds or seconds < 0 or math.isinf(seconds):
        return "--s"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m:02d}m {s:02d}s"
    return f"{s:02d}s"

def progress_bar(percent, length=16):
    percent = max(0.0, min(100.0, percent))
    filled = int(round(length * percent / 100.0))
    return "█" * filled + "░" * (length - filled)

def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] if name else "file"

def styled_button(text, callback_data, style="success"):
    """Creates InlineKeyboardButton with Telegram Bot API style attribute when supported."""
    btn = InlineKeyboardButton(text, callback_data=callback_data)
    try:
        btn.style = style
    except Exception:
        pass
    return btn

def build_home_keyboard():
    f_label = {"all": "All (Vid+PDF)", "video": "Only Video", "pdf": "Only PDF"}.get(
        STATE["filter_mode"], "All (Vid+PDF)"
    )
    t_label = {"off": "Off (Direct)", "manual": f"Manual ({STATE['manual_topic_id']})", "auto": "Auto Topics"}.get(
        STATE["topic_mode"], "Off"
    )
    return InlineKeyboardMarkup([
        [
            styled_button("📤 Upload TXT", "act_upload_txt", "success"),
            styled_button("📝 Extract Tests (/test)", "act_extract_test", "success"),
        ],
        [
            styled_button("📢 Set Channel", "set_channel", "primary"),
            styled_button(f"🗂 Topic: {t_label}", "toggle_topic", "primary"),
        ],
        [
            styled_button("🏷 Set Batch", "set_batch", "primary"),
            styled_button("✍️ Set Credit", "set_credit", "primary"),
        ],
        [
            styled_button(f"🔢 Index ({STATE['start_index']:03d})", "set_index", "primary"),
            styled_button("📏 Set Range", "set_range", "primary"),
        ],
        [
            styled_button(f"🎯 Filter: {f_label}", "toggle_filter", "success"),
            styled_button("🖼 Set Thumbnail", "set_thumb", "primary"),
        ],
        [
            styled_button("🛑 Stop Process", "act_stop", "danger"),
            styled_button("🔄 Reset All", "act_reset", "danger"),
        ],
    ])

def get_dashboard_text():
    ch = STATE["target_chat"] or "Not Set"
    r_end = STATE["range_end"] if STATE["range_end"] > 0 else "End"
    thumb = "Set ✅" if (STATE["thumb_path"] and os.path.exists(STATE["thumb_path"])) else "Not Set"
    return (
        "⚡ **PERSONAL COURSE UPLOADER CONTROL PANEL**\n\n"
        f"📢 **Target Chat:** `{ch}`\n"
        f"🗂 **Topic Mode:** `{STATE['topic_mode'].upper()}`"
        + (f" (ID: `{STATE['manual_topic_id']}`)" if STATE["topic_mode"] == "manual" else "")
        + "\n"
        f"🏷 **Batch:** `{STATE['batch_name']}`\n"
        f"✍️ **Extracted By:** `{STATE['credit_name']}`\n"
        f"🔗 **Credit Link:** {STATE['credit_link']}\n"
        f"🔢 **Next Index:** `{STATE['start_index']:03d}`\n"
        f"📏 **Line Range:** `{STATE['range_start']}` to `{r_end}`\n"
        f"🎯 **Filter Mode:** `{STATE['filter_mode'].upper()}`\n"
        f"🖼 **Thumbnail:** `{thumb}`\n\n"
        "👉 *Neeche buttons se settings karein ya seedha `.txt` file bhejein.*"
    )

# --- TXT File Smart Parser ---
def parse_txt_file(filepath):
    """
    Supports both:
    1) ===== Header | Sub | Topic ===== followed by Title:URL
    2) (Home) (Batch) (Subject) (Section) (Topic) Title: https://...
    """
    items = []
    current_topic = "General"

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        lines = [line.strip() for line in f if line.strip()]

    for line in lines:
        # Format 1 Section Header: ===== Home | ... | Topic =====
        if line.startswith("====") and line.endswith("===="):
            clean_hdr = line.strip("= ").strip()
            parts = [p.strip() for p in clean_hdr.split("|") if p.strip()]
            if parts:
                current_topic = parts[-1]
            continue

        # Find URL in line
        url_match = re.search(r"(https?://[^\s]+)", line)
        if not url_match:
            continue

        url = url_match.group(1).strip()
        prefix = line[: url_match.start()].rstrip(": ").strip()

        # Format 2 Bracket Hierarchy: (Home) (Subject) (Topic) Title
        topic = current_topic
        title = prefix

        if prefix.startswith("("):
            brackets = re.findall(r"\(([^()]+)\)", prefix)
            # Remove bracketed groups from the beginning to get title
            remainder = re.sub(r"^(?:\([^()]+\)\s*)+", "", prefix).strip()
            if brackets:
                # Filter out generic navigation labels if possible
                meaningful = [b.strip() for b in brackets if b.strip().lower() not in ("home", "live class", "recorded")]
                if meaningful:
                    topic = meaningful[-1]
                else:
                    topic = brackets[-1].strip()
            if remainder:
                title = remainder

        if not title:
            title = f"Lecture_{len(items) + 1}"

        # Determine Media Type
        lower_url = url.lower()
        lower_title = title.lower()
        if ".m3u8" in lower_url or ".mp4" in lower_url or ".mkv" in lower_url or "play/" in lower_url and not lower_url.endswith(".pdf"):
            media_type = "video"
        elif lower_url.endswith(".pdf") or ".pdf?" in lower_url or "pdf" in lower_title and "http" in lower_url:
            media_type = "pdf"
        elif any(k in lower_title for k in ["test", "quiz", "mock", "exam", "mcq", "practice set"]) or any(
            k in lower_url for k in ["test", "quiz", "exam", "attempt"]
        ):
            media_type = "test"
        else:
            # Default fallback based on URL extension
            if ".pdf" in lower_url:
                media_type = "pdf"
            else:
                media_type = "video"

        items.append({
            "topic": topic,
            "title": title,
            "url": url,
            "type": media_type,
        })

    return items

# --- Dual Live Status Updater ---
class DualProgressTracker:
    def __init__(self, status_msg: Message, batch: str, topic: str, filename: str, q_curr: int, q_total: int, stats: dict):
        self.msg = status_msg
        self.batch = batch
        self.topic = topic
        self.filename = filename
        self.q_curr = q_curr
        self.q_total = q_total
        self.stats = stats

        self.dl_pct = 0.0
        self.dl_done = 0
        self.dl_total = 0
        self.dl_speed = 0.0
        self.dl_eta = 0
        self.dl_status = "⏳"

        self.ul_pct = 0.0
        self.ul_done = 0
        self.ul_total = 0
        self.ul_speed = 0.0
        self.ul_eta = 0
        self.ul_status = "⏸ Waiting"

        self.last_edit = 0.0

    async def render(self, force=False):
        now = time.time()
        if not force and (now - self.last_edit) < 3.5:
            return
        self.last_edit = now

        overall_pct = (self.q_curr / max(1, self.q_total)) * 100.0
        text = (
            "⚡ **BATCH EXTRACTION LIVE STATUS**\n\n"
            f"📂 **Batch:** `{self.batch}`\n"
            f"📌 **Topic:** `{self.topic}`\n"
            f"🎬 **File:** `{self.filename}`\n"
            f"🔢 **Queue:** `{self.q_curr:03d} / {self.q_total:03d}` (Overall: `{overall_pct:.1f}%`)\n\n"
            f"📥 **1. DOWNLOAD / DIRECT FETCH:**\n"
            f"`[{progress_bar(self.dl_pct)}]` `{self.dl_pct:.1f}%` {self.dl_status}\n"
            f"📦 **Size:** `{human_bytes(self.dl_done)} / {human_bytes(self.dl_total)}`\n"
            f"🚀 **Speed:** `{human_bytes(self.dl_speed)}/s`  |  ⏳ **ETA:** `{format_eta(self.dl_eta)}`\n\n"
            f"📤 **2. TELEGRAM UPLOAD:**\n"
            f"`[{progress_bar(self.ul_pct)}]` `{self.ul_pct:.1f}%` {self.ul_status}\n"
            f"📦 **Uploaded:** `{human_bytes(self.ul_done)} / {human_bytes(self.ul_total)}`\n"
            f"🚀 **Speed:** `{human_bytes(self.ul_speed)}/s`  |  ⏳ **ETA:** `{format_eta(self.ul_eta)}`\n\n"
            f"📊 **Total Uploaded:** `{self.stats['success']}`  |  ❌ **Failed:** `{self.stats['failed']}`"
        )
        try:
            await self.msg.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([[styled_button("🛑 Stop Process", "act_stop", "danger")]]),
            )
        except (MessageNotModified, FloodWait):
            pass
        except Exception:
            pass

# --- Download Engines ---
async def download_file_aiohttp(url: str, dest_path: str, tracker: DualProgressTracker):
    start_t = time.time()
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"
    }
    timeout = aiohttp.ClientTimeout(total=1800)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            total_size = int(resp.headers.get("Content-Length", 0))
            tracker.dl_total = total_size
            downloaded = 0
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 256):
                    if STOP_REQUESTED:
                        raise Exception("Stopped by user")
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = max(0.1, time.time() - start_t)
                    speed = downloaded / elapsed
                    tracker.dl_done = downloaded
                    tracker.dl_speed = speed
                    if total_size > 0:
                        tracker.dl_pct = (downloaded / total_size) * 100.0
                        tracker.dl_eta = (total_size - downloaded) / max(1, speed)
                    await tracker.render()

    tracker.dl_pct = 100.0
    tracker.dl_done = os.path.getsize(dest_path)
    tracker.dl_total = tracker.dl_done
    tracker.dl_eta = 0
    tracker.dl_status = "✅"
    await tracker.render(force=True)

async def download_video_ytdlp(url: str, dest_path: str, tracker: DualProgressTracker):
    start_t = time.time()
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--newline",
        "-N", "16",
        "--retries", "5",
        "--fragment-retries", "5",
        "--user-agent", "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", dest_path,
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    while True:
        if STOP_REQUESTED:
            proc.terminate()
            raise Exception("Stopped by user")
        line = await proc.stdout.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="ignore").strip()
        # Parse yt-dlp progress output: [download]  45.2% of ~120.50MiB at 12.30MiB/s ETA 00:08
        m_pct = re.search(r"(\d+\.\d+)%", decoded)
        if m_pct:
            tracker.dl_pct = float(m_pct.group(1))
        m_size = re.search(r"of\s+~?([\d\.]+)(KiB|MiB|GiB|KB|MB|GB)", decoded)
        if m_size:
            val = float(m_size.group(1))
            unit = m_size.group(2)
            mult = {"KiB": 1024, "KB": 1024, "MiB": 1024**2, "MB": 1024**2, "GiB": 1024**3, "GB": 1024**3}.get(unit, 1)
            tracker.dl_total = int(val * mult)
            tracker.dl_done = int(tracker.dl_total * (tracker.dl_pct / 100.0))
        m_spd = re.search(r"at\s+([\d\.]+)(KiB|MiB|GiB|KB|MB|GB)/s", decoded)
        if m_spd:
            val = float(m_spd.group(1))
            unit = m_spd.group(2)
            mult = {"KiB": 1024, "KB": 1024, "MiB": 1024**2, "MB": 1024**2, "GiB": 1024**3, "GB": 1024**3}.get(unit, 1)
            tracker.dl_speed = val * mult
        if "Merger" in decoded or "ffmpeg" in decoded:
            tracker.dl_status = "⚙️ Merging"
        await tracker.render()

    await proc.wait()
    if proc.returncode != 0 or not os.path.exists(dest_path):
        raise Exception("Video download/merge failed")

    tracker.dl_pct = 100.0
    tracker.dl_done = os.path.getsize(dest_path)
    tracker.dl_total = tracker.dl_done
    tracker.dl_eta = 0
    tracker.dl_status = "✅"
    await tracker.render(force=True)

def get_video_metadata(filepath):
    duration = 0
    width = 1280
    height = 720
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            filepath,
        ]
        res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode("utf-8")
        info = json.loads(res)
        if "format" in info and "duration" in info["format"]:
            duration = int(float(info["format"]["duration"]))
        if "streams" in info and info["streams"]:
            width = int(info["streams"][0].get("width", 1280))
            height = int(info["streams"][0].get("height", 720))
    except Exception:
        pass
    return duration, width, height

def generate_auto_thumb(video_path, thumb_out):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:03", "-vframes", "1", thumb_out],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        if os.path.exists(thumb_out):
            return thumb_out
    except Exception:
        pass
    return None

async def generate_html_test(url: str, title: str, topic: str, dest_html: str, tracker: DualProgressTracker):
    """Fetches test/quiz JSON or webpage and wraps it into an interactive standalone HTML test file."""
    tracker.dl_status = "🔄 Fetching Test"
    await tracker.render(force=True)
    raw_content = ""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status == 200:
                    raw_content = await resp.text(errors="ignore")
    except Exception as e:
        raw_content = f"Could not fetch live payload ({e}). Direct Test Link: {url}"

    # Try extracting JSON questions if present
    questions_js = "[]"
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict):
            q_list = data.get("data") or data.get("questions") or []
        elif isinstance(data, list):
            q_list = data
        else:
            q_list = []
        if q_list:
            questions_js = json.dumps(q_list, ensure_ascii=False)
    except Exception:
        pass

    html_doc = f"""<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 16px; }}
  .card {{ max-width: 760px; margin: 0 auto; background: #1e293b; border-radius: 14px; padding: 20px; box-shadow: 0 8px 24px rgba(0,0,0,0.35); }}
  h1 {{ font-size: 1.3rem; color: #38bdf8; margin-top: 0; }}
  .meta {{ font-size: 0.9rem; color: #94a3b8; margin-bottom: 16px; }}
  .q-box {{ background: #0f172a; padding: 14px; border-radius: 10px; margin-bottom: 14px; border: 1px solid #334155; }}
  .opt {{ display: block; width: 100%; text-align: left; background: #1e293b; color: #e2e8f0; border: 1px solid #475569; padding: 10px 12px; border-radius: 8px; margin-top: 8px; cursor: pointer; }}
  .opt.correct {{ background: #14532d; border-color: #22c55e; }}
  .opt.wrong {{ background: #7f1d1d; border-color: #ef4444; }}
  .btn {{ display: inline-block; background: #22c55e; color: #052e16; font-weight: 700; padding: 10px 18px; border-radius: 8px; text-decoration: none; margin-top: 10px; }}
</style>
</head>
<body>
<div class="card">
  <h1>{title}</h1>
  <div class="meta">Topic: {topic} | Batch: {STATE['batch_name']} | By: {STATE['credit_name']}</div>
  <div id="quiz-container"></div>
  <a class="btn" href="{url}" target="_blank">Open Original Test Portal ↗</a>
</div>
<script>
const rawQuestions = {questions_js};
const container = document.getElementById('quiz-container');
if (Array.isArray(rawQuestions) && rawQuestions.length > 0) {{
  rawQuestions.forEach((item, idx) => {{
    const qText = item.question || item.title || item.q || ('Question ' + (idx + 1));
    const opts = item.options || [item.option_1, item.option_2, item.option_3, item.option_4].filter(Boolean);
    const ans = item.answer || item.correct_option || '';
    const box = document.createElement('div');
    box.className = 'q-box';
    box.innerHTML = '<div><b>Q' + (idx + 1) + '.</b> ' + qText + '</div>';
    opts.forEach((op, oIdx) => {{
      const b = document.createElement('button');
      b.className = 'opt';
      b.innerHTML = op;
      b.onclick = () => {{
        if (String(oIdx + 1) === String(ans) || op === ans) b.classList.add('correct');
        else b.classList.add('wrong');
      }};
      box.appendChild(b);
    }});
    container.appendChild(box);
  }});
}} else {{
  container.innerHTML = '<div class="q-box">Interactive offline questions unavailable for this token. Use the direct button below to launch the live test.</div>';
}}
</script>
</body>
</html>"""

    with open(dest_html, "w", encoding="utf-8") as f:
        f.write(html_doc)

    tracker.dl_pct = 100.0
    tracker.dl_done = os.path.getsize(dest_html)
    tracker.dl_total = tracker.dl_done
    tracker.dl_status = "✅"
    await tracker.render(force=True)

# --- Forum Topic Resolver ---
async def resolve_topic_thread(chat_id: int, topic_name: str):
    if STATE["topic_mode"] == "off":
        return None
    if STATE["topic_mode"] == "manual":
        return STATE["manual_topic_id"]
    if STATE["topic_mode"] == "auto":
        key = f"{chat_id}:{topic_name}"
        if key in TOPIC_CACHE:
            return TOPIC_CACHE[key]
        try:
            res = await app.create_forum_topic(chat_id, topic_name[:120])
            TOPIC_CACHE[key] = res.id
            return res.id
        except Exception as e:
            logger.warning(f"Could not create forum topic '{topic_name}': {e}")
            return None
    return None

# --- Main Batch Processor ---
async def process_batch(client: Client, message: Message, txt_path: str, force_test_only=False):
    global IS_RUNNING, STOP_REQUESTED
    if IS_RUNNING:
        await message.reply_text("⚠️ Ek process pehle se chal raha hai! Pehle `/stop` karein.")
        return

    if not STATE["target_chat"]:
        await message.reply_text("⚠️ Pehle `/setchannel -100xxxxxxxxxx` se target channel ya group set karein.")
        return

    items = parse_txt_file(txt_path)
    if not items:
        await message.reply_text("❌ Is `.txt` file mein koi valid link nahi mila.")
        return

    # Apply Line Range
    r_start = max(1, STATE["range_start"])
    r_end = STATE["range_end"] if STATE["range_end"] > 0 else len(items)
    items = items[r_start - 1 : r_end]

    # Apply Filter
    if force_test_only:
        filtered = [x for x in items if x["type"] == "test"]
        if not filtered:
            # Fallback: check any link with test/quiz keyword in title or url
            filtered = [
                x for x in items
                if any(k in (x["title"] + " " + x["url"]).lower() for k in ["test", "quiz", "mock", "mcq", "exam"])
            ]
        items = filtered
    else:
        f_mode = STATE["filter_mode"]
        if f_mode == "video":
            items = [x for x in items if x["type"] == "video"]
        elif f_mode == "pdf":
            items = [x for x in items if x["type"] == "pdf"]

    if not items:
        await message.reply_text("❌ Selected filter/range ke hisaab se koi item nahi mila.")
        return

    IS_RUNNING = True
    STOP_REQUESTED = False
    target_chat = int(STATE["target_chat"])
    curr_index = int(STATE["start_index"])
    stats = {"success": 0, "failed": 0}
    failed_lines = []

    status_msg = await message.reply_text(
        f"🚀 **Starting Extraction...**\nTotal Items: `{len(items)}` | Starting Index: `{curr_index:03d}`"
    )

    for q_idx, item in enumerate(items, start=1):
        if STOP_REQUESTED:
            break

        ext = "mp4" if item["type"] == "video" else ("html" if item["type"] == "test" else "pdf")
        clean_title = sanitize_filename(item["title"])
        filename = f"{clean_title}.{ext}"
        local_path = os.path.join(WORK_DIR, f"{curr_index:03d}_{filename}")
        auto_thumb_path = os.path.join(WORK_DIR, f"{curr_index:03d}_thumb.jpg")

        tracker = DualProgressTracker(
            status_msg=status_msg,
            batch=STATE["batch_name"],
            topic=item["topic"],
            filename=filename,
            q_curr=q_idx,
            q_total=len(items),
            stats=stats,
        )
        await tracker.render(force=True)

        caption = (
            f"**Index:** {curr_index:03d}\n\n"
            f"**Title:** {item['title']}.{ext}\n\n"
            f"**Topic:** {item['topic']}\n\n"
            f"**Batch:** {STATE['batch_name']}\n\n"
            f"**Extracted By:** {STATE['credit_name']}\n"
            f"{STATE['credit_link']}"
        )

        try:
            # 1. Download / Fetch Step
            if item["type"] == "video":
                await download_video_ytdlp(item["url"], local_path, tracker)
            elif item["type"] == "test":
                await generate_html_test(item["url"], item["title"], item["topic"], local_path, tracker)
            else:
                await download_file_aiohttp(item["url"], local_path, tracker)

            if STOP_REQUESTED:
                break

            # 2. Telegram Upload Step
            file_size = os.path.getsize(local_path)
            tracker.ul_total = file_size
            tracker.ul_status = "⏳ Uploading"
            ul_start = time.time()

            async def ul_progress(current, total):
                if STOP_REQUESTED:
                    raise Exception("Stopped by user")
                elapsed = max(0.1, time.time() - ul_start)
                speed = current / elapsed
                tracker.ul_done = current
                tracker.ul_total = total
                tracker.ul_speed = speed
                tracker.ul_pct = (current / max(1, total)) * 100.0
                tracker.ul_eta = (total - current) / max(1, speed)
                await tracker.render()

            thread_id = await resolve_topic_thread(target_chat, item["topic"])
            send_kwargs = {"caption": caption, "progress": ul_progress}
            if thread_id:
                send_kwargs["message_thread_id"] = int(thread_id)

            while True:
                try:
                    if item["type"] == "video":
                        dur, w, h = get_video_metadata(local_path)
                        thumb_to_use = None
                        if STATE["thumb_path"] and os.path.exists(STATE["thumb_path"]):
                            thumb_to_use = STATE["thumb_path"]
                        else:
                            thumb_to_use = generate_auto_thumb(local_path, auto_thumb_path)

                        await client.send_video(
                            chat_id=target_chat,
                            video=local_path,
                            duration=dur,
                            width=w,
                            height=h,
                            thumb=thumb_to_use,
                            supports_streaming=True,
                            **send_kwargs,
                        )
                    else:
                        thumb_to_use = STATE["thumb_path"] if (STATE["thumb_path"] and os.path.exists(STATE["thumb_path"])) else None
                        await client.send_document(
                            chat_id=target_chat,
                            document=local_path,
                            file_name=filename,
                            thumb=thumb_to_use,
                            **send_kwargs,
                        )
                    break
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 2)

            tracker.ul_pct = 100.0
            tracker.ul_done = file_size
            tracker.ul_eta = 0
            tracker.ul_status = "✅"
            stats["success"] += 1
            curr_index += 1
            STATE["start_index"] = curr_index
            save_state(STATE)
            await tracker.render(force=True)

        except Exception as e:
            logger.error(f"Failed [{curr_index:03d}] {item['title']}: {e}")
            stats["failed"] += 1
            failed_lines.append(f"({item['topic']}) {item['title']}: {item['url']}  # Error: {e}")
            curr_index += 1
            STATE["start_index"] = curr_index
            save_state(STATE)
        finally:
            # Zero-storage cleanup
            for p in (local_path, auto_thumb_path):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    IS_RUNNING = False
    summary = (
        f"{'🛑 **PROCESS STOPPED**' if STOP_REQUESTED else '✅ **BATCH COMPLETED**'}\n\n"
        f"🏷 **Batch:** `{STATE['batch_name']}`\n"
        f"✅ **Uploaded:** `{stats['success']}`\n"
        f"❌ **Failed:** `{stats['failed']}`\n"
        f"🔢 **Next Index:** `{STATE['start_index']:03d}`"
    )
    await status_msg.edit_text(summary, reply_markup=build_home_keyboard())

    if failed_lines:
        fail_file = os.path.join(WORK_DIR, "failed_links.txt")
        with open(fail_file, "w", encoding="utf-8") as ff:
            ff.write("\n".join(failed_lines))
        await message.reply_document(fail_file, caption="⚠️ **Failed / Expired Links Report**")
        os.remove(fail_file)

# --- Owner Filter ---
owner_only = filters.user(OWNER_ID)

# --- Command Handlers ---
@app.on_message(filters.command(["start", "settings"]) & owner_only)
async def cmd_start(client: Client, message: Message):
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(get_dashboard_text(), reply_markup=build_home_keyboard())

@app.on_message(filters.command("id"))
async def cmd_id(client: Client, message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else "N/A"
    thread_id = getattr(message, "message_thread_id", None)
    fwd_id = message.forward_from_chat.id if message.forward_from_chat else None

    text = (
        "🆔 **ID DETAILS**\n\n"
        f"👤 **Your User ID:** `{user_id}`\n"
        f"💬 **Current Chat ID:** `{chat_id}`\n"
    )
    if thread_id:
        text += f"🧵 **Topic / Thread ID:** `{thread_id}`\n"
    if fwd_id:
        text += f"📢 **Forwarded Channel ID:** `{fwd_id}`\n"
    await message.reply_text(text)

@app.on_message(filters.command("setchannel") & owner_only)
async def cmd_setchannel(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        STATE["awaiting_input"] = "channel"
        save_state(STATE)
        await message.reply_text("📢 Apne Target Channel ya Group ki ID bhejein (jaise `-1001234567890`):")
        return
    STATE["target_chat"] = parts[1].strip()
    STATE["topic_mode"] = "off"
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(f"✅ Target Channel set ho gaya: `{STATE['target_chat']}`", reply_markup=build_home_keyboard())

@app.on_message(filters.command("grouptopictransfer") & owner_only)
async def cmd_grouptopic(client: Client, message: Message):
    parts = message.text.split()
    if len(parts) == 1:
        STATE["awaiting_input"] = "grouptopic"
        save_state(STATE)
        await message.reply_text(
            "🗂 **Group Topic Transfer Setup:**\n"
            "• Specific Topic ke liye bhejein: `-100xxxxxxxxxx 45`\n"
            "• Chapter-wise Auto Topic banane ke liye bhejein: `-100xxxxxxxxxx auto`"
        )
        return
    if len(parts) >= 2:
        STATE["target_chat"] = parts[1].strip()
        if len(parts) >= 3 and parts[2].lower() != "auto":
            STATE["topic_mode"] = "manual"
            STATE["manual_topic_id"] = int(parts[2])
        else:
            STATE["topic_mode"] = "auto"
            STATE["manual_topic_id"] = None
        STATE["awaiting_input"] = None
        save_state(STATE)
        await message.reply_text(
            f"✅ Group Topic Transfer configured!\nGroup: `{STATE['target_chat']}` | Mode: `{STATE['topic_mode'].upper()}`",
            reply_markup=build_home_keyboard(),
        )

@app.on_message(filters.command("setbatch") & owner_only)
async def cmd_setbatch(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        STATE["awaiting_input"] = "batch"
        save_state(STATE)
        await message.reply_text("🏷 Naya **Batch Name** likh kar bhejein:")
        return
    STATE["batch_name"] = parts[1].strip()
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(f"✅ Batch set: `{STATE['batch_name']}`", reply_markup=build_home_keyboard())

@app.on_message(filters.command("setcredit") & owner_only)
async def cmd_setcredit(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        STATE["awaiting_input"] = "credit"
        save_state(STATE)
        await message.reply_text("✍️ Credit naam aur link bhejein (`Name | https://t.me/link`):")
        return
    raw = parts[1].strip()
    if "|" in raw:
        name, link = [x.strip() for x in raw.split("|", 1)]
        STATE["credit_name"] = name
        STATE["credit_link"] = link
    else:
        STATE["credit_name"] = raw
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(f"✅ Credit updated: `{STATE['credit_name']}`", reply_markup=build_home_keyboard())

@app.on_message(filters.command("setindex") & owner_only)
async def cmd_setindex(client: Client, message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        STATE["awaiting_input"] = "index"
        save_state(STATE)
        await message.reply_text("🔢 Starting Index number bhejein (jaise `1` ya `30`):")
        return
    STATE["start_index"] = int(parts[1].strip())
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(f"✅ Starting Index set: `{STATE['start_index']:03d}`", reply_markup=build_home_keyboard())

@app.on_message(filters.command("range") & owner_only)
async def cmd_range(client: Client, message: Message):
    parts = message.text.split()
    if len(parts) < 3:
        STATE["awaiting_input"] = "range"
        save_state(STATE)
        await message.reply_text("📏 Start aur End line number bhejein (jaise `1 50`, ya poori file ke liye `1 0`):")
        return
    STATE["range_start"] = int(parts[1])
    STATE["range_end"] = int(parts[2])
    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text(
        f"✅ Range set: `{STATE['range_start']}` to `{STATE['range_end'] or 'End'}`",
        reply_markup=build_home_keyboard(),
    )

@app.on_message(filters.command("test") & owner_only)
async def cmd_test(client: Client, message: Message):
    if message.reply_to_message and message.reply_to_message.document:
        doc = message.reply_to_message.document
        if doc.file_name.endswith(".txt"):
            txt_path = os.path.join(WORK_DIR, f"test_{int(time.time())}_{doc.file_name}")
            await message.reply_to_message.download(txt_path)
            STATE["last_txt_path"] = txt_path
            save_state(STATE)
            await process_batch(client, message, txt_path, force_test_only=True)
            return

    if STATE.get("last_txt_path") and os.path.exists(STATE["last_txt_path"]):
        await process_batch(client, message, STATE["last_txt_path"], force_test_only=True)
    else:
        STATE["awaiting_input"] = "test_txt"
        save_state(STATE)
        await message.reply_text("📝 Woh `.txt` file bhejein jisse saare Tests `.html` ban kar nikalne hain:")

@app.on_message(filters.command("stop") & owner_only)
async def cmd_stop(client: Client, message: Message):
    global STOP_REQUESTED
    if IS_RUNNING:
        STOP_REQUESTED = True
        await message.reply_text("🛑 Stopping process... Current step khatam hote hi ruk jayega.")
    else:
        await message.reply_text("ℹ️ Abhi koi process nahi chal raha hai.")

@app.on_message(filters.command("reset") & owner_only)
async def cmd_reset(client: Client, message: Message):
    global STOP_REQUESTED, TOPIC_CACHE
    STOP_REQUESTED = True
    TOPIC_CACHE.clear()
    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        os.makedirs(WORK_DIR, exist_ok=True)
    STATE.clear()
    STATE.update(DEFAULT_STATE.copy())
    save_state(STATE)
    await message.reply_text("🔄 Saari settings aur temporary files reset kar di gayi hain.", reply_markup=build_home_keyboard())

# --- Inline Button Callbacks ---
@app.on_callback_query(owner_only)
async def on_callback(client: Client, query: CallbackQuery):
    global STOP_REQUESTED
    data = query.data

    if data == "act_upload_txt":
        STATE["awaiting_input"] = "upload_txt"
        save_state(STATE)
        await query.message.reply_text("📤 Apni `.txt` file bhejein, upload turant shuru ho jayega:")
    elif data == "act_extract_test":
        STATE["awaiting_input"] = "test_txt"
        save_state(STATE)
        await query.message.reply_text("📝 Tests nikalne ke liye `.txt` file bhejein (`.html` roop mein upload honge):")
    elif data == "set_channel":
        STATE["awaiting_input"] = "channel"
        save_state(STATE)
        await query.message.reply_text("📢 Target Channel ya Group ID bhejein (e.g., `-1001234567890`):")
    elif data == "toggle_topic":
        modes = ["off", "auto", "manual"]
        nxt = modes[(modes.index(STATE["topic_mode"]) + 1) % len(modes)]
        if nxt == "manual" and not STATE["manual_topic_id"]:
            STATE["awaiting_input"] = "grouptopic"
            save_state(STATE)
            await query.message.reply_text("🗂 Group ID aur Topic ID bhejein (`-100xxxxxxxxxx 45`):")
            await query.answer()
            return
        STATE["topic_mode"] = nxt
        save_state(STATE)
        await query.message.edit_text(get_dashboard_text(), reply_markup=build_home_keyboard())
    elif data == "set_batch":
        STATE["awaiting_input"] = "batch"
        save_state(STATE)
        await query.message.reply_text("🏷 Batch ka naya naam bhejein:")
    elif data == "set_credit":
        STATE["awaiting_input"] = "credit"
        save_state(STATE)
        await query.message.reply_text("✍️ Credit naam aur link bhejein (`Name | https://t.me/link`):")
    elif data == "set_index":
        STATE["awaiting_input"] = "index"
        save_state(STATE)
        await query.message.reply_text("🔢 Starting Index number bhejein (e.g., `1` ya `30`):")
    elif data == "set_range":
        STATE["awaiting_input"] = "range"
        save_state(STATE)
        await query.message.reply_text("📏 Start aur End line bhejein (e.g., `1 50` ya `1 0`):")
    elif data == "toggle_filter":
        f_modes = ["all", "video", "pdf"]
        STATE["filter_mode"] = f_modes[(f_modes.index(STATE["filter_mode"]) + 1) % len(f_modes)]
        save_state(STATE)
        await query.message.edit_text(get_dashboard_text(), reply_markup=build_home_keyboard())
    elif data == "set_thumb":
        STATE["awaiting_input"] = "thumb"
        save_state(STATE)
        await query.message.reply_text("🖼 Custom Thumbnail ke liye koi Photo bhejein (ya hatane ke liye `clear` likhein):")
    elif data == "act_stop":
        STOP_REQUESTED = True
        await query.answer("🛑 Stop request bhej di gayi hai!", show_alert=True)
    elif data == "act_reset":
        STOP_REQUESTED = True
        STATE.clear()
        STATE.update(DEFAULT_STATE.copy())
        save_state(STATE)
        await query.message.edit_text(get_dashboard_text(), reply_markup=build_home_keyboard())
        await query.answer("🔄 Reset Done!", show_alert=True)

    await query.answer()

# --- Document, Photo & Interactive Text Handlers ---
@app.on_message(filters.document & owner_only)
async def on_document(client: Client, message: Message):
    doc = message.document
    if not doc.file_name.lower().endswith(".txt"):
        await message.reply_text("⚠️ Kripya sirf `.txt` file hi bhejein.")
        return

    txt_path = os.path.join(WORK_DIR, f"batch_{int(time.time())}_{doc.file_name}")
    await message.download(txt_path)
    STATE["last_txt_path"] = txt_path
    mode = STATE.get("awaiting_input")
    STATE["awaiting_input"] = None
    save_state(STATE)

    force_test = (mode == "test_txt") or (message.caption and "/test" in message.caption.lower())
    await process_batch(client, message, txt_path, force_test_only=force_test)

@app.on_message(filters.photo & owner_only)
async def on_photo(client: Client, message: Message):
    if STATE.get("awaiting_input") == "thumb":
        thumb_file = os.path.join(WORK_DIR, "custom_thumb.jpg")
        await message.download(thumb_file)
        STATE["thumb_path"] = thumb_file
        STATE["awaiting_input"] = None
        save_state(STATE)
        await message.reply_text("✅ Custom Thumbnail save ho gaya!", reply_markup=build_home_keyboard())

@app.on_message(filters.text & ~filters.command(["start", "settings", "id", "setchannel", "grouptopictransfer", "setbatch", "setcredit", "setindex", "range", "test", "stop", "reset"]) & owner_only)
async def on_text_input(client: Client, message: Message):
    awaiting = STATE.get("awaiting_input")
    if not awaiting:
        return

    txt = message.text.strip()
    if awaiting == "channel":
        STATE["target_chat"] = txt
        STATE["topic_mode"] = "off"
    elif awaiting == "grouptopic":
        parts = txt.split()
        STATE["target_chat"] = parts[0]
        if len(parts) >= 2 and parts[1].lower() != "auto":
            STATE["topic_mode"] = "manual"
            STATE["manual_topic_id"] = int(parts[1])
        else:
            STATE["topic_mode"] = "auto"
            STATE["manual_topic_id"] = None
    elif awaiting == "batch":
        STATE["batch_name"] = txt
    elif awaiting == "credit":
        if "|" in txt:
            n, l = [x.strip() for x in txt.split("|", 1)]
            STATE["credit_name"] = n
            STATE["credit_link"] = l
        else:
            STATE["credit_name"] = txt
    elif awaiting == "index" and txt.isdigit():
        STATE["start_index"] = int(txt)
    elif awaiting == "range":
        parts = txt.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            STATE["range_start"] = int(parts[0])
            STATE["range_end"] = int(parts[1])
    elif awaiting == "thumb" and txt.lower() == "clear":
        STATE["thumb_path"] = None

    STATE["awaiting_input"] = None
    save_state(STATE)
    await message.reply_text("✅ Setting update ho gayi!", reply_markup=build_home_keyboard())

# --- Startup ---
async def main():
    Thread(target=run_flask, daemon=True).start()
    await app.start()
    logger.info("Bot Started Successfully!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    app.run(main())
