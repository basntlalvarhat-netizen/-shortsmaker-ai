from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return "ShortsMaker AI Backend is Running!"

@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    video_url = data.get('url')
    return jsonify({"status": "success", "message": "Video processing started for: " + video_url})

if __name__ == '__main__':
    app.run(debug=True)
