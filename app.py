import os
import re
import shutil
import subprocess
import tempfile
import uuid

from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
TMP_ROOT = tempfile.gettempdir()

YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
CLIP_PATH_PATTERN = re.compile(r"^shorts/[A-Za-z0-9_-]{11}/[A-Za-z0-9_.-]+\.mp4$")

CLIP_COUNT = int(os.environ.get("SHORTS_CLIP_COUNT", "3"))
CLIP_SECONDS = int(os.environ.get("SHORTS_CLIP_SECONDS", "30"))
MAX_SOURCE_SECONDS = int(os.environ.get("SHORTS_MAX_SOURCE_SECONDS", str(20 * 60)))
MAX_SOURCE_BYTES = 400 * 1024 * 1024
OUTPUT_WIDTH = 720
OUTPUT_HEIGHT = 1280

# Serverless filesystems are read-only outside /tmp, so point every tool cache there.
os.environ.setdefault("DENO_DIR", os.path.join(TMP_ROOT, "deno"))
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(TMP_ROOT, "cache"))


class ProcessingError(Exception):
    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def extract_video_id(url):
    match = YOUTUBE_ID_PATTERN.search(url)
    return match.group(1) if match else None


def ffmpeg_path():
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def deno_path():
    try:
        import deno

        return deno.find_deno_bin()
    except Exception:
        return shutil.which("deno")


def write_cookies_file(work_dir):
    cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not cookies:
        return None
    path = os.path.join(work_dir, "cookies.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(cookies.replace("\\n", "\n") + "\n")
    return path


def youtube_options(work_dir):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": os.path.join(TMP_ROOT, "yt-dlp-cache"),
        "ffmpeg_location": ffmpeg_path(),
        "retries": 3,
    }
    deno_bin = deno_path()
    if deno_bin:
        options["js_runtimes"] = {"deno": {"path": deno_bin}}
    cookie_file = write_cookies_file(work_dir)
    if cookie_file:
        options["cookiefile"] = cookie_file
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy
    return options


def explain_download_error(error):
    text = str(error)
    if "confirm you" in text and "bot" in text:
        if os.environ.get("YOUTUBE_COOKIES"):
            return "YouTube rejected the server's request even with the configured cookies. Export fresh cookies and update YOUTUBE_COOKIES."
        return (
            "YouTube blocked the download from this server (\"Sign in to confirm you're not a bot\"). "
            "Add a YOUTUBE_COOKIES environment variable containing exported YouTube cookies (cookies.txt format) and try again."
        )
    if "Private video" in text or "members-only" in text:
        return "This video is private or members-only and cannot be downloaded."
    if "Video unavailable" in text:
        return "This video is unavailable."
    if "live event" in text or "is_live" in text:
        return "Live streams cannot be processed. Try again after the stream ends."
    return "Could not download the video from YouTube."


def download_source(video_id, work_dir):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    url = f"https://www.youtube.com/watch?v={video_id}"
    options = youtube_options(work_dir)

    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as error:
        app.logger.warning("yt-dlp metadata failure: %s", error)
        raise ProcessingError(explain_download_error(error), 502)

    if info.get("is_live"):
        raise ProcessingError("Live streams cannot be processed. Try again after the stream ends.", 400)

    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise ProcessingError("Could not determine the video length.", 502)
    if duration > MAX_SOURCE_SECONDS:
        raise ProcessingError(
            f"Video is too long ({int(duration // 60)} min). The limit is {MAX_SOURCE_SECONDS // 60} minutes.",
            400,
        )

    options.update({
        "format": (
            "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
            "bv*[height<=1080]+ba/b[height<=1080]/b"
        ),
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(work_dir, "source.%(ext)s"),
        "max_filesize": MAX_SOURCE_BYTES,
    })

    try:
        with YoutubeDL(options) as ydl:
            ydl.download([url])
    except DownloadError as error:
        app.logger.warning("yt-dlp download failure: %s", error)
        raise ProcessingError(explain_download_error(error), 502)

    for name in os.listdir(work_dir):
        if name.startswith("source.") and not name.endswith((".part", ".ytdl")):
            return os.path.join(work_dir, name), duration, info.get("title") or video_id

    raise ProcessingError("The download finished but no video file was produced.", 502)


def plan_clips(duration):
    clip_length = min(CLIP_SECONDS, duration)
    count = max(1, min(CLIP_COUNT, int(duration // CLIP_SECONDS)))
    if count == 1:
        return [(0.0, clip_length)]
    span = duration - clip_length
    return [(round(span * (index + 1) / (count + 1), 2), clip_length) for index in range(count)]


def render_clip(source_path, start, length, output_path):
    vertical_filter = (
        "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:flags=lanczos,setsar=1"
    )
    command = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", source_path, "-t", str(length),
        "-vf", vertical_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode != 0 or not os.path.isfile(output_path):
        app.logger.error("ffmpeg failed: %s", result.stderr[-2000:])
        raise ProcessingError("Video encoding failed while creating a clip.", 500)


def upload_clip(video_id, index, file_path):
    import vercel.blob as blob

    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        raise ProcessingError("Blob storage is not configured (missing BLOB_READ_WRITE_TOKEN).", 500)

    with open(file_path, "rb") as handle:
        result = blob.put(
            f"shorts/{video_id}/short-{index + 1}.mp4",
            handle.read(),
            access="private",
            content_type="video/mp4",
            add_random_suffix=True,
        )
    return result.pathname


def format_timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


@app.route("/")
def home():
    return send_file(INDEX_PATH)


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "message": "ShortsMaker AI backend is running.",
        "blob_configured": bool(os.environ.get("BLOB_READ_WRITE_TOKEN")),
        "youtube_cookies_configured": bool(os.environ.get("YOUTUBE_COOKIES")),
    })


@app.route("/api/generate", methods=["POST"])
@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    video_url = str(data.get("url", "")).strip()

    if not video_url:
        return jsonify({"status": "error", "message": "Please provide a YouTube video URL."}), 400
    if len(video_url) > 2048:
        return jsonify({"status": "error", "message": "URL is too long."}), 400

    video_id = extract_video_id(video_url)
    if not video_id:
        return jsonify({"status": "error", "message": "That does not look like a valid YouTube video link."}), 400

    work_dir = os.path.join(TMP_ROOT, f"shorts-{uuid.uuid4().hex}")
    os.makedirs(work_dir, exist_ok=True)

    try:
        source_path, duration, title = download_source(video_id, work_dir)

        clips = []
        for index, (start, length) in enumerate(plan_clips(duration)):
            output_path = os.path.join(work_dir, f"short-{index + 1}.mp4")
            render_clip(source_path, start, length, output_path)
            size = os.path.getsize(output_path)
            pathname = upload_clip(video_id, index, output_path)
            os.remove(output_path)
            clips.append({
                "index": index + 1,
                "start": start,
                "end": round(start + length, 2),
                "label": f"{format_timestamp(start)} - {format_timestamp(start + length)}",
                "size_bytes": size,
                "download_url": f"/api/clips/{pathname}",
            })

        return jsonify({
            "status": "success",
            "message": f"Created {len(clips)} vertical {OUTPUT_WIDTH}x{OUTPUT_HEIGHT} MP4 shorts.",
            "video_id": video_id,
            "title": title,
            "processing": True,
            "clips": clips,
        })
    except ProcessingError as error:
        return jsonify({"status": "error", "message": error.message}), error.status_code
    except Exception:
        app.logger.exception("Failed to process /generate request")
        return jsonify({"status": "error", "message": "Something went wrong while processing the video."}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.route("/api/clips/<path:pathname>")
def download_clip(pathname):
    if not CLIP_PATH_PATTERN.match(pathname):
        return jsonify({"status": "error", "message": "Invalid clip path."}), 400

    import vercel.blob as blob

    try:
        result = blob.get(pathname, access="private")
    except Exception:
        app.logger.exception("Failed to fetch clip %s", pathname)
        return jsonify({"status": "error", "message": "Clip not found."}), 404

    filename = pathname.rsplit("/", 1)[-1]
    return Response(
        result.content,
        mimetype="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


if __name__ == "__main__":
    app.run(debug=True)
