import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)

ALLOWED_HOSTS = {"vk.com", "m.vk.com", "vk.ru", "m.vk.ru", "vkvideo.ru", "vk.video", "vkvideo.com"}


def is_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    return p.scheme in ("http", "https") and host in ALLOWED_HOSTS


def extract(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def progressive_formats(info: dict):
    """Только прямые mp4/webm-файлы (со звуком), без m3u8/dash."""
    out = []
    for f in info.get("formats", []):
        if f.get("protocol") not in ("http", "https"):
            continue
        if not f.get("url") or f.get("vcodec") in (None, "none"):
            continue
        out.append(f)
    out.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    return out


def unique_by_height(formats: list) -> list:
    """Одно качество — одна строка (если у одной высоты несколько файлов, берём лучший по битрейту)."""
    seen, out = set(), []
    for f in formats:  # уже отсортированы: лучший по высоте и битрейту первым
        key = f.get("height") or f.get("format_id")
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def probe_size(f: dict):
    """Реальный размер файла через HEAD (или Range-запрос), если yt-dlp его не знает."""
    headers = dict(f.get("http_headers") or {})
    try:
        r = requests.head(f["url"], headers=headers, allow_redirects=True, timeout=8)
        n = r.headers.get("Content-Length")
        if r.ok and n and n.isdigit() and int(n) > 0:
            return int(n)
    except requests.RequestException:
        pass
    try:  # некоторые CDN не отвечают на HEAD
        headers["Range"] = "bytes=0-0"
        with requests.get(f["url"], headers=headers, stream=True, timeout=8) as r:
            cr = r.headers.get("Content-Range", "")  # bytes 0-0/123456
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                return int(cr.rsplit("/", 1)[1])
            n = r.headers.get("Content-Length")
            if r.status_code == 200 and n and n.isdigit():
                return int(n)
    except requests.RequestException:
        pass
    return None


def resolve_size(f: dict, duration):
    """-> (байты | None, оценка_ли)."""
    n = f.get("filesize") or f.get("filesize_approx")
    if n:
        return int(n), False
    n = probe_size(f)
    if n:
        return n, False
    if f.get("tbr") and duration:  # крайний случай: битрейт * длительность
        return int(f["tbr"] * 1000 / 8 * duration), True
    return None, False


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", s).strip()[:120] or "video"


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not is_allowed(url):
        return jsonify(error="Нужна ссылка на видео VK (vk.com / vkvideo.ru)."), 400
    try:
        info = extract(url)
    except Exception as e:  # noqa: BLE001
        msg = str(e).split("ERROR:")[-1].strip()
        return jsonify(error=f"Не удалось получить видео: {msg}"), 422

    fmts = unique_by_height(progressive_formats(info))
    duration = info.get("duration")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(pool.map(lambda f: resolve_size(f, duration), fmts))

    formats = []
    for f, (size, estimated) in zip(fmts, sizes):
        formats.append({
            "format_id": f["format_id"],
            "height": f.get("height"),
            "quality": f"{f['height']}p" if f.get("height") else f.get("format_note") or "?",
            "ext": f.get("ext", "mp4"),
            "size": size,
            "size_estimated": estimated,
        })
    return jsonify(
        title=info.get("title"),
        thumbnail=info.get("thumbnail"),
        duration=duration,
        uploader=info.get("uploader"),
        formats=formats,
    )


@app.get("/api/download")
def api_download():
    url = request.args.get("url", "").strip()
    fid = request.args.get("format_id", "")
    if not is_allowed(url):
        return "Bad URL", 400
    try:
        info = extract(url)
    except Exception:  # noqa: BLE001
        return "Не удалось получить видео", 422

    fmt = next((f for f in progressive_formats(info) if f["format_id"] == fid), None)
    if not fmt:
        return "Формат не найден", 404

    upstream = requests.get(fmt["url"], stream=True, timeout=30)
    ext = fmt.get("ext", "mp4")
    q = f"{fmt['height']}p" if fmt.get("height") else "video"
    filename = f"{safe_name(info.get('title', 'video'))} [{q}].{ext}"

    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "video/mp4"),
        # RFC 5987, чтобы кириллица в имени работала
        "Content-Disposition": "attachment; filename*=UTF-8''" + requests.utils.quote(filename),
    }
    if upstream.headers.get("Content-Length"):
        headers["Content-Length"] = upstream.headers["Content-Length"]

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=256 * 1024)),
        headers=headers,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
