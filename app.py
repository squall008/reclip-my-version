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
        "--quiet", "--no-warnings",
        # クラウドIP回避: モバイルウェブクライアント偽装
        "--extractor-args", "youtube:player_client=mweb",
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

def download_via_cobalt(url, out_dir, job_id=""):
    """RenderのIPブラックリストを回避するため、Cobalt API（中継局）経由で動画をダウンロードする最終奥義"""
    # コミュニティ公開インスタンス（スコア順、公式はBot保護で弾かれるため除外）
    COBALT_INSTANCES = [
        "https://cobalt-api.meowing.de/",      # 92% スコア
        "https://cobalt-backend.canine.tools/", # 84% スコア
        "https://capi.3kh0.net/",               # 80% スコア
    ]
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    # youtu.be短縮URLをフルURLに正規化（Cobaltが対応しやすい形式に）
    normalized_url = url
    video_id_match = re.search(r'youtu\.be/([0-9A-Za-z_-]{11})', url)
    if video_id_match:
        normalized_url = f"https://www.youtube.com/watch?v={video_id_match.group(1)}"
    
    payload = {
        "url": normalized_url,
        "videoQuality": "1080",
        "youtubeVideoCodec": "h264",
    }

    for instance_url in COBALT_INSTANCES:
        try:
            print(f"[{job_id}] Cobalt Fallback → trying {instance_url}")
            res = requests.post(instance_url, headers=headers, json=payload, timeout=30)

            if res.status_code != 200:
                print(f"[{job_id}]   HTTP {res.status_code}: {res.text[:200]}")
                continue

            data = res.json()
            status = data.get("status")
            print(f"[{job_id}]   Response status: {status}")

            dl_url = None
            if status in ("tunnel", "redirect"):
                dl_url = data.get("url")
            elif status == "picker":
                # 複数アイテムの場合は最初の動画を選択
                picks = data.get("picker", [])
                for p in picks:
                    if p.get("type") in ("video", None):
                        dl_url = p.get("url")
                        break
                if not dl_url and picks:
                    dl_url = picks[0].get("url")
            elif status == "error":
                err = data.get("error", {})
                print(f"[{job_id}]   Cobalt error: {err}")
                continue
            else:
                print(f"[{job_id}]   Unknown status: {data}")
                continue

            if not dl_url:
                print(f"[{job_id}]   No download URL in response")
                continue

            # ストリーミングダウンロード
            print(f"[{job_id}]   Downloading from {dl_url[:80]}...")
            out_path = os.path.join(out_dir, f"{job_id}.mp4")
            r = requests.get(dl_url, stream=True, timeout=120, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
            })
            r.raise_for_status()

            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(out_path)
            if file_size < 1000:  # 1KB未満ならエラー
                print(f"[{job_id}]   File too small ({file_size}B), skipping")
                os.remove(out_path)
                continue

            print(f"[{job_id}]   Cobalt Download SUCCESS! ({file_size / 1024 / 1024:.1f} MB)")
            return out_path

        except Exception as e:
            print(f"[{job_id}]   Instance {instance_url} failed: {e}")
            continue

    print(f"[{job_id}] All Cobalt instances failed.")
    return None

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
            cobalt_file = download_via_cobalt(url, DOWNLOAD_DIR, job_id)
            if cobalt_file:
                # Cobalt成功: ファイル情報を直接セット
                job["status"] = "done"
                job["file"] = cobalt_file
                ext = os.path.splitext(cobalt_file)[1]
                title = job.get("title", "").strip()
                if title:
                    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
                    job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(cobalt_file)
                else:
                    job["filename"] = os.path.basename(cobalt_file)
                return
            else:
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

    video_id = get_video_id(url)

    # === 第1段階: YouTube 公式 Data API v3（APIキーが必要） ===
    API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
    if API_KEY and video_id:
        try:
            api_url = f"https://www.googleapis.com/youtube/v3/videos?id={video_id}&key={API_KEY}&part=snippet"
            res = requests.get(api_url, timeout=10)
            data_api = res.json()
            if "items" in data_api and len(data_api["items"]) > 0:
                snip = data_api["items"][0]["snippet"]
                thumb_info = snip.get("thumbnails", {})
                thumb_url = ""
                for quality in ["maxres", "high", "medium", "default"]:
                    if quality in thumb_info:
                        thumb_url = thumb_info[quality]["url"]
                        break
                print(f"[INFO] YouTube Data API SUCCESS for {video_id}")
                return jsonify({
                    "title": snip.get("title", ""),
                    "thumbnail": thumb_url,
                    "uploader": snip.get("channelTitle", ""),
                    "formats": [],
                })
            else:
                print(f"[INFO] YouTube Data API returned no items for {video_id}")
        except Exception as e:
            print(f"[INFO] YouTube Data API failed: {e}")

    # === 第2段階: YouTube oEmbed（無料・APIキー不要） ===
    if video_id:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            res = requests.get(oembed_url, timeout=10)
            if res.status_code == 200:
                oembed = res.json()
                print(f"[INFO] oEmbed SUCCESS for {video_id}")
                return jsonify({
                    "title": oembed.get("title", ""),
                    "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "uploader": oembed.get("author_name", ""),
                    "formats": [],
                })
        except Exception as e:
            print(f"[INFO] oEmbed failed: {e}")

    # === 第3段階: 最小限カード（URLだけで表示） ===
    # yt-dlpは絶対に呼ばない（Render上では確実にIPブロックされるため）
    if video_id:
        print(f"[INFO] All API methods failed. Returning minimal card for {video_id}")
        return jsonify({
            "title": f"YouTube動画 ({video_id})",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "uploader": "",
            "formats": [],
        })

    # YouTube以外のURLの場合もyt-dlpは使わず、Cobaltに任せる
    print(f"[INFO] Non-YouTube URL or unknown format. Returning minimal card.")
    return jsonify({
        "title": url.split("/")[-1][:50] or "動画",
        "thumbnail": "",
        "uploader": "",
        "formats": [],
    })



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


@app.route("/api/debug")
def debug_info():
    """Render上の環境・接続状況を診断するエンドポイント"""
    results = {"api_key": bool(os.environ.get("YOUTUBE_API_KEY")), "cobalt_tests": [], "cookies": "none"}
    
    # クッキーファイル確認
    current_dir = os.path.dirname(__file__)
    for f in os.listdir(current_dir):
        if "cookie" in f.lower() and f.endswith(".txt"):
            path = os.path.join(current_dir, f)
            results["cookies"] = f"{f} ({os.path.getsize(path)} bytes)"
            break
    
    # 各Cobaltインスタンスのヘルスチェック
    COBALT_INSTANCES = [
        "https://cobalt-api.meowing.de/",
        "https://cobalt-backend.canine.tools/",
        "https://capi.3kh0.net/",
    ]
    test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # テスト用
    for inst in COBALT_INSTANCES:
        try:
            # GETでインスタンス情報を取得
            r = requests.get(inst, timeout=5)
            info = r.json() if r.status_code == 200 else {}
            # POSTでダウンロードテスト(実際にはDLしない)
            r2 = requests.post(inst, json={"url": test_url, "videoQuality": "360"}, 
                             headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=15)
            results["cobalt_tests"].append({
                "instance": inst,
                "get_status": r.status_code,
                "version": info.get("cobalt", {}).get("version", "?"),
                "post_status": r2.status_code,
                "post_body": r2.text[:300],
            })
        except Exception as e:
            results["cobalt_tests"].append({"instance": inst, "error": str(e)})
    
    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
