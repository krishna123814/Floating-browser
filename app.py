import os
import glob
import shutil
import tempfile

from flask import Flask, request, send_file, render_template_string
import yt_dlp

app = Flask(__name__)

# Optional: public URL par misuse rokne ke liye password (Render env var: ACCESS_KEY)
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
# Optional: bot-check aaye to cookies.txt ka poora text yahan daalo (Render env var: YT_COOKIES)
YT_COOKIES = os.environ.get("YT_COOKIES", "")

PAGE = """
<!DOCTYPE html>
<html lang="hi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Downloader</title>
<style>
  :root { --bg:#14181f; --card:#1d232d; --text:#e8ecf2; --muted:#8c97a8; --accent:#4cc38a; --err:#ff6b6b; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--text);
         font-family:system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; }
  main { width:100%; max-width:520px; background:var(--card); border-radius:14px; padding:24px; }
  h1 { margin:0 0 4px; font-size:1.3rem; }
  p { color:var(--muted); font-size:.9rem; margin:0 0 16px; }
  label { display:block; margin:12px 0 6px; font-size:.85rem; color:var(--muted); }
  input, select { width:100%; padding:12px; border-radius:8px; border:1px solid #333c4a; background:#12161c; color:var(--text); font-size:1rem; }
  button { margin-top:18px; width:100%; padding:13px; border:0; border-radius:8px; background:var(--accent); color:#06231a; font-weight:600; font-size:1rem; cursor:pointer; }
  button:disabled { opacity:.6; }
  .err { margin-top:16px; color:var(--err); white-space:pre-wrap; word-break:break-word; font-size:.85rem; }
  #wait { display:none; margin-top:14px; color:var(--muted); font-size:.9rem; }
</style>
</head>
<body>
<main>
  <h1>Video Downloader</h1>
  <p>Link paste karo. Download shuru hone me thoda time lag sakta hai.</p>
  <form method="post" action="/download" onsubmit="document.getElementById('go').disabled=true;document.getElementById('wait').style.display='block';setTimeout(function(){document.getElementById('go').disabled=false;document.getElementById('wait').style.display='none'},60000)">
    <label for="url">YouTube link</label>
    <input id="url" name="url" type="url" required placeholder="https://www.youtube.com/watch?v=...">
    <label for="mode">Type</label>
    <select id="mode" name="mode">
      <option value="video">Video (MP4)</option>
      <option value="audio">Sirf audio (M4A)</option>
    </select>
    <label for="quality">Max quality</label>
    <select id="quality" name="quality">
      <option value="720" selected>720p</option>
      <option value="480">480p</option>
      <option value="360">360p</option>
    </select>
    {% if need_key %}
    <label for="key">Access key</label>
    <input id="key" name="key" type="password" required>
    {% endif %}
    <button id="go" type="submit">Download</button>
    <div id="wait">Server video la raha hai, wait karo...</div>
  </form>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</main>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(PAGE, need_key=bool(ACCESS_KEY), error=None)


@app.get("/health")
def health():
    return "ok"


@app.post("/download")
def download():
    if ACCESS_KEY and request.form.get("key", "") != ACCESS_KEY:
        return render_template_string(PAGE, need_key=True, error="Access key galat hai."), 403

    url = request.form.get("url", "").strip()
    mode = request.form.get("mode", "video")
    quality = request.form.get("quality", "720")
    if not quality.isdigit():
        quality = "720"
    if not url:
        return render_template_string(PAGE, need_key=bool(ACCESS_KEY), error="Link daalo."), 400

    tmp = tempfile.mkdtemp()
    opts = {
        "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        # Render par ffmpeg nahi hota, isliye merge ki zarurat wale formats avoid kiye
        "format": (
            "bestaudio[ext=m4a]/bestaudio"
            if mode == "audio"
            else f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
        ),
    }

    if YT_COOKIES:
        cookie_path = os.path.join(tmp, "cookies.txt")
        with open(cookie_path, "w") as f:
            f.write(YT_COOKIES)
        opts["cookiefile"] = cookie_path

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in glob.glob(os.path.join(tmp, "*")) if not p.endswith("cookies.txt")]
        if not files:
            raise RuntimeError("File nahi bani.")
        path = max(files, key=os.path.getsize)
        resp = send_file(path, as_attachment=True, download_name=os.path.basename(path))
        resp.call_on_close(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return resp
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        msg = str(e)[:600]
        return render_template_string(PAGE, need_key=bool(ACCESS_KEY), error="Error: " + msg), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
