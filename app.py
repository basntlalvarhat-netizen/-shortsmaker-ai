import os
import re

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

YOUTUBE_ID_PATTERN = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url):
    match = YOUTUBE_ID_PATTERN.search(url)
    return match.group(1) if match else None


@app.route("/")
def home():
    return send_file(INDEX_PATH)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "message": "ShortsMaker AI backend is running."})


@app.route("/api/generate", methods=["POST"])
@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json(silent=True) or {}
        video_url = str(data.get("url", "")).strip()

        if not video_url:
            return jsonify({"status": "error", "message": "Please provide a YouTube video URL."}), 400

        if len(video_url) > 2048:
            return jsonify({"status": "error", "message": "URL is too long."}), 400

        video_id = extract_video_id(video_url)
        if not video_id:
            return jsonify({"status": "error", "message": "That does not look like a valid YouTube video link."}), 400

        return jsonify({
            "status": "success",
            "message": "Video link received by the backend. Automatic clip generation is not implemented yet.",
            "url": video_url,
            "video_id": video_id,
            "processing": False,
        })
    except Exception:
        app.logger.exception("Failed to handle /generate request")
        return jsonify({"status": "error", "message": "Something went wrong on the server."}), 500


if __name__ == "__main__":
    app.run(debug=True)
