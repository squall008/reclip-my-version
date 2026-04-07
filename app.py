import os
import uuid
import glob
import json
import subprocess
import threading
import sys
import time
import re
from flask import Flask, request, jsonify, send_file, render_template
import requests
from googleapiclient.discovery import build

app = Flask(__name__)

try:
    import imageio_ffmpeg
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    ffmpeg_path = None

# 環境変数から設定を取得
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

youtube_service = None
if YOUTUBE_API_KEY:
    try:
        youtube_service = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    except Exception as e:
        print(f"Failed to init YouTube API: {e}")

jobs = {}

def get_ydl_base_opts():
    """ボット回避とクッキーの全設定を生成して返す最強の盾"""
    cmd = [
        "--cache-dir", CACHE_DIR,
        "--no-check-certificates",
        "--quiet", "--no-warnings", # ログをクリーンに
    ]
    
    # 候補となるファイル名 (巨大ファイル「cookies (1).txt」等にも対応)
    candidates = ["cookies.txt", "www.youtube.com_cookies.txt", "youtube.com_cookies.txt", "cookies (1).txt"]
    current_dir = os.path.dirname(__file__)
    for filename in os.listdir(current_dir):
        if filename in candidates or filename.endswith("_cookies.txt"):
            path = os.path.join(current_dir, filename)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                # --- 自動翻訳コンニャク（パッチ） ---
                processed_path = os.path.join(current_dir, "processed_cookies.txt")
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        raw_cookies = f.read()
                    
                    # 海外製yt-dlpが認識できるように、日本版ドメインを海外基準の .google.com へ変換
                    fixed_cookies = raw_cookies.replace(".google.co.jp\t", ".google.com\t")
                    fixed_cookies = fixed_cookies.replace("google.co.jp\t", "google.com\t")
                    
                    with open(processed_path, "w", encoding="utf-8") as f:
                        f.write(fixed_cookies)
                        
                    print(f"Applied TRANSLATED bot-evasion shield with cookies: {filename} -> processed_cookies.txt")
                    cmd += ["--cookies", processed_path]
                except Exception as e:
                    print(f"Cookie translation failed: {e}")
                    # 万一失敗した場合は保険としてそのまま使う
                    cmd += ["--cookies", path]
                break
    return cmd

def download_via_cobalt(url, out_path, job_id=""):
    """RenderのIPブラックリストを回避するため、Cobalt API（中継局）経由で動画をダウンロードする最終奥義"""
    try:
        print(f"[{job_id}] Cobalt API Fallback Initiative Started...")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
        }
        payload = {
            "url": url,
            "videoQuality": "1080",
        }
        res = requests.post("https://api.cobalt.tools/api/json", headers=headers, json=payload, timeout=30)
        res.raise_for_status()
        data = res.json()
        
        if data.get("status") in ["stream", "redirect"]:
            dl_url = data["url"]
            print(f"[{job_id}] Cobalt DL streaming from {dl_url[:50]}...")
            r = requests.get(dl_url, stream=True, timeout=60)
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            print(f"[{job_id}] Cobalt Fallback Download SUCCESS!")
            return True
        else:
            print(f"[{job_id}] Cobalt API rejected or failed to process: {data}")
            return False
    except Exception as e:
        print(f"[{job_id}] Cobalt Fallback Error: {e}")
        return False

def get_video_id(url):
    """URLから動画IDを抽出"""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"youtube\.com\/shorts\/([0-9A-Za-z_-]{11})"
    ]
    for p in patterns:
        m = re.search(p, url)
        if m: return m.group(1)
    return None

def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-o", out_template]
    # 共通のボット回避設定を適用
    cmd += get_ydl_base_opts()

    if ffmpeg_path:
        cmd += ["--ffmpeg-location", ffmpeg_path]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        # もしyt-dlpが失敗した場合はフォールバック発動
        if result.returncode != 0:
            print(f"[{job_id}] yt-dlp failed (Err: {result.stderr.splitlines()[-1] if result.stderr else 'unknown'}). Initiating Cobalt API fallback...")
            fallback_success = download_via_cobalt(url, out_template, job_id)
            if not fallback_success:
                job["status"] = "error"
                job["error"] = "YouTube側と中継サーバーの双方で保存が拒否されました"
                return
        else:
            print(f"[{job_id}] yt-dlp Exec SUCCESS")
            
        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URLを入力してください"}), 400

    # まず公式APIで情報を取得
    video_id = get_video_id(url)
    if youtube_service and video_id:
        try:
            req = youtube_service.videos().list(part="snippet,contentDetails", id=video_id)
            res = req.execute()
            if res.get("items"):
                item = res["items"][0]
                snip = item["snippet"]
                
                # サムネの最高画質を柔軟に取得
                thumb_info = snip.get("thumbnails", {})
                thumb_url = ""
                if "maxres" in thumb_info:
                    thumb_url = thumb_info["maxres"]["url"]
                elif "high" in thumb_info:
                    thumb_url = thumb_info["high"]["url"]
                elif "medium" in thumb_info:
                    thumb_url = thumb_info["medium"]["url"]
                elif "default" in thumb_info:
                    thumb_url = thumb_info["default"]["url"]
                    
                print(f"Official YouTube API OK! Skipping yt-dlp pre-check to avoid early ban!")
                return jsonify({
                    "title": snip.get("title", ""),
                    "thumbnail": thumb_url,
                    "uploader": snip.get("channelTitle", ""),
                    "formats": [],
                    "auth_needed": False
                })
        except Exception as e:
            print(f"API Error: {e}")

    # API失敗またはフォールバック
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-j"]
    cmd += get_ydl_base_opts()
    cmd += [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            err = result.stderr.strip()
            print(f"FATAL: yt-dlp extraction failed: {err}") # 詳細ログをRenderに出力
            if "Sign in" in err or "confirm you're not a bot" in err:
                return jsonify({"error": "authentication_required", "msg": "ボット判定を回避するため、シークレット窓でクッキーを取得し直してプッシュしてください（cookies.txt）"}), 401
            return jsonify({"error": err.split("\n")[-1]}), 400

        info = json.loads(result.stdout)
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({"id": f["format_id"], "label": f"{height}p", "height": height})
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title, "created_at": time.time()}


    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
