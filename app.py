import os
import re
import glob
import time
import uuid
import subprocess
import threading

from flask import Flask, request, jsonify, send_from_directory, render_template

import yt_dlp
import imageio_ffmpeg

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
CLIPS_DIR = os.path.join(BASE_DIR, "clips")
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CLIPS_DIR, exist_ok=True)

_FFMPEG_BIN = None


def get_ffmpeg_bin():
    """ffmpeg binary को सिर्फ तभी resolve करता है जब असल में इस्तेमाल हो —
    ताकि इसमें कोई दिक्कत आए तो पूरा ऐप शुरू होने से पहले ही क्रैश न हो,
    बल्कि सिर्फ /generate कॉल करने पर साफ error दिखे।"""
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG_BIN

# ---- Limits (server crash से बचने के लिए) ----
MAX_CLIPS = 5
MAX_SOURCE_MINUTES = 15
CLIP_MIN_SEC = 30
CLIP_MAX_SEC = 60
FILE_TTL_SECONDS = 3600  # 1 घंटे बाद पुरानी फाइलें डिलीट

job_lock = threading.Lock()
job_running = False

HOOK_WORDS = [
    "but", "however", "actually", "secret", "never", "always", "amazing",
    "shocking", "important", "mistake", "warning", "biggest", "worst",
    "best", "truth", "why", "how", "what if", "imagine", "listen",
]


def cleanup_old_files():
    cutoff = time.time() - FILE_TTL_SECONDS
    for folder in (DOWNLOAD_DIR, CLIPS_DIR):
        for f in glob.glob(os.path.join(folder, "*")):
            try:
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
            except OSError:
                pass


def parse_vtt(vtt_path):
    """.vtt सबटाइटल फाइल को (start_sec, end_sec, text) की लिस्ट में बदलता है।"""
    segments = []
    if not vtt_path or not os.path.exists(vtt_path):
        return segments

    time_re = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
    )

    def to_sec(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = time_re.search(line)
        if match:
            start = to_sec(*match.groups()[0:4])
            end = to_sec(*match.groups()[4:8])
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                cleaned = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if cleaned:
                    text_lines.append(cleaned)
                i += 1
            text = " ".join(text_lines)
            if text:
                segments.append((start, end, text))
        else:
            i += 1

    return segments


def score_window(text):
    text_lower = text.lower()
    score = 0
    for word in HOOK_WORDS:
        if word in text_lower:
            score += 2
    score += text_lower.count("?") * 3
    score += text_lower.count("!") * 2
    score += min(len(text) / 20, 10)
    return score


def build_candidate_windows(segments):
    """ट्रांसक्रिप्ट से 30-60 सेकंड के संभावित क्लिप विंडो बनाता है।"""
    if not segments:
        return []

    windows = []
    n = len(segments)
    for i in range(n):
        start_time = segments[i][0]
        text_parts = []
        j = i
        while j < n and segments[j][1] - start_time <= CLIP_MAX_SEC:
            text_parts.append(segments[j][2])
            duration = segments[j][1] - start_time
            if duration >= CLIP_MIN_SEC:
                windows.append((start_time, segments[j][1], " ".join(text_parts)))
            j += 1
    return windows


def pick_top_clips(windows, max_clips=MAX_CLIPS):
    scored = [(score_window(text), s, e, text) for (s, e, text) in windows]
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen = []
    for score, s, e, text in scored:
        overlap = any(s < ce and cs < e for cs, ce, _, _ in chosen)
        if not overlap:
            chosen.append((s, e, score, text))
        if len(chosen) >= max_clips:
            break

    chosen.sort(key=lambda x: x[0])
    return chosen


def even_split_clips(duration_sec, max_clips=MAX_CLIPS):
    """जब ट्रांसक्रिप्ट न मिले, तो बराबर हिस्सों में बांटता है।"""
    clip_len = 45
    clips = []
    t = 0
    while t + CLIP_MIN_SEC <= duration_sec and len(clips) < max_clips:
        end = min(t + clip_len, duration_sec)
        clips.append((t, end, 0, ""))
        t += clip_len
    return clips


def get_duration(path):
    result = subprocess.run(
        [get_ffmpeg_bin(), "-i", path],
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2})\.(\d{2})", result.stderr)
    if not match:
        return 0
    h, m, s, cs = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


def write_srt_for_clip(text, clip_duration, srt_path):
    if not text:
        return
    words = text.split()
    chunk_size = 8
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
    if not chunks:
        return
    per_chunk = max(clip_duration / len(chunks), 1.5)

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, chunk in enumerate(chunks):
            start = idx * per_chunk
            end = min(start + per_chunk, clip_duration)
            f.write(f"{idx + 1}\n{fmt(start)} --> {fmt(end)}\n{chunk}\n\n")


def make_vertical_clip(source_path, start, end, text, out_path):
    duration = end - start
    srt_path = out_path + ".srt"
    write_srt_for_clip(text, duration, srt_path)

    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

    has_captions = os.path.exists(srt_path) and os.path.getsize(srt_path) > 0
    if has_captions:
        escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")
        vf += (
            f",subtitles='{escaped_srt}':force_style="
            "'Fontsize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=3,Outline=2,Alignment=2,MarginV=80'"
        )

    cmd = [
        get_ffmpeg_bin(), "-y",
        "-ss", str(start), "-to", str(end),
        "-i", source_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    if os.path.exists(srt_path):
        os.remove(srt_path)


def is_direct_media_url(url):
    return bool(re.search(r"\.(mp4|mov|webm|mkv)(\?|$)", url, re.IGNORECASE))


def download_direct(url, job_id):
    ext_match = re.search(r"\.(mp4|mov|webm|mkv)(\?|$)", url, re.IGNORECASE)
    ext = ext_match.group(1) if ext_match else "mp4"
    out_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.{ext}")
    cmd = [get_ffmpeg_bin(), "-y", "-i", url, "-c", "copy", out_path]
    subprocess.run(cmd, check=True, capture_output=True)
    duration = get_duration(out_path)
    return out_path, None, duration


def download_source(url, job_id):
    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
    }

    if os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = info.get("duration", 0)

    video_path = None
    vtt_path = None
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        if f.endswith(".vtt"):
            vtt_path = f
        elif f.endswith((".mp4", ".mkv", ".webm")):
            video_path = f

    return video_path, vtt_path, duration


@app.route("/")
def index():
    try:
        return render_template("index.html")
    except Exception as exc:
        app.logger.exception("Homepage render failed")
        return f"होमपेज लोड करने में दिक्कत: {exc}", 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/generate", methods=["POST"])
def generate():
    global job_running
    cleanup_old_files()

    with job_lock:
        if job_running:
            return jsonify({"error": "एक और वीडियो पहले से प्रोसेस हो रहा है, कृपया थोड़ी देर बाद कोशिश करें।"}), 429
        job_running = True

    try:
        url = request.form.get("url") or ((request.json or {}).get("url") if request.is_json else None)
        if not url:
            return jsonify({"error": "कोई वीडियो लिंक नहीं मिला।"}), 400

        cookies_file = request.files.get("cookies")
        if cookies_file and cookies_file.filename:
            cookies_file.save(COOKIES_PATH)

        job_id = uuid.uuid4().hex[:10]

        try:
            if is_direct_media_url(url):
                video_path, vtt_path, duration = download_direct(url, job_id)
            else:
                video_path, vtt_path, duration = download_source(url, job_id)
        except Exception as exc:
            message = str(exc)
            if "bot" in message.lower() or "sign in" in message.lower():
                return jsonify({
                    "error": "YouTube bot-verification मांग रहा है। कृपया अपनी cookies.txt अपलोड करें, या कोई डायरेक्ट MP4 लिंक आज़माएं।"
                }), 400
            return jsonify({"error": f"वीडियो डाउनलोड नहीं हो सका: {message}"}), 400

        if not video_path:
            return jsonify({"error": "वीडियो डाउनलोड फेल हो गया।"}), 400

        if duration and duration > MAX_SOURCE_MINUTES * 60:
            os.remove(video_path)
            return jsonify({
                "error": f"वीडियो बहुत लंबा है। कृपया {MAX_SOURCE_MINUTES} मिनट से छोटा वीडियो इस्तेमाल करें।"
            }), 400

        segments = parse_vtt(vtt_path)
        windows = build_candidate_windows(segments)
        chosen = pick_top_clips(windows, MAX_CLIPS) if windows else even_split_clips(duration, MAX_CLIPS)

        if not chosen:
            return jsonify({"error": "इस वीडियो से कोई क्लिप नहीं बन पाई (शायद बहुत छोटा है)।"}), 400

        clip_urls = []
        for idx, (start, end, score, text) in enumerate(chosen):
            out_name = f"{job_id}_clip{idx + 1}.mp4"
            out_path = os.path.join(CLIPS_DIR, out_name)
            try:
                make_vertical_clip(video_path, start, end, text, out_path)
                clip_urls.append({
                    "name": out_name,
                    "url": f"/downloads/{out_name}",
                    "start": round(start, 1),
                    "end": round(end, 1),
                })
            except subprocess.CalledProcessError:
                continue

        if os.path.exists(video_path):
            os.remove(video_path)
        if vtt_path and os.path.exists(vtt_path):
            os.remove(vtt_path)

        if not clip_urls:
            return jsonify({"error": "क्लिप बनाने में दिक्कत आई, कृपया दूसरा वीडियो आज़माएं।"}), 400

        return jsonify({"clips": clip_urls})

    finally:
        with job_lock:
            job_running = False


@app.route("/downloads/<path:filename>")
def downloads(filename):
    return send_from_directory(CLIPS_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
