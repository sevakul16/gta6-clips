#!/usr/bin/env python3
"""GTA 6 auto-clipper.

Twitch top clips -> vertical 1080x1920 (streamer cam on top, gameplay below)
-> burned-in subtitles -> upload-post.com (TikTok + YouTube Shorts).

Usage:
  python clipper.py                 # normal run (find, render, post)
  python clipper.py --dry-run       # find + render only, no posting
  python clipper.py --input f.mp4 --streamer xqc --dry-run   # render a local file
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "posted.json"
OUT_DIR = ROOT / "out"
W, H = 1080, 1920
CAM_H = 760            # height of the cam strip on top
GAME_H = H - CAM_H     # gameplay part below


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posted": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def posts_last_24h(state, profile):
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    n = 0
    for v in state["posted"].values():
        if v.get("profile") == profile and v.get("ok") and dt.datetime.fromisoformat(v["at"]) > since:
            n += 1
    return n


# ---------------------------------------------------------------- twitch
class Twitch:
    def __init__(self, cid, secret):
        r = requests.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        self.h = {"Client-Id": cid, "Authorization": "Bearer " + r.json()["access_token"]}

    def get(self, path, params):
        r = requests.get("https://api.twitch.tv/helix/" + path, headers=self.h, params=params, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    def game_id(self, name):
        d = self.get("games", {"name": name})
        if not d:
            raise SystemExit(f"Twitch game not found: {name!r}")
        return d[0]["id"]

    def users(self, logins):
        out = {}
        for i in range(0, len(logins), 100):
            for u in self.get("users", [("login", l) for l in logins[i:i + 100]]):
                out[u["login"].lower()] = u
        return out

    def clips(self, broadcaster_id, since):
        return self.get("clips", {"broadcaster_id": broadcaster_id, "first": 50,
                                  "started_at": since.strftime("%Y-%m-%dT%H:%M:%SZ")})


def find_candidates(cfg, state):
    tw = Twitch(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
    gid = tw.game_id(cfg["game"])
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=cfg["lookback_hours"])
    all_logins = [s.lower() for g in cfg["groups"].values() for s in g["streamers"]]
    users = tw.users(all_logins)
    missing = set(all_logins) - set(users)
    if missing:
        log("WARN: Twitch logins not found:", ", ".join(sorted(missing)))
    cands = []
    for gname, g in cfg["groups"].items():
        for login in g["streamers"]:
            u = users.get(login.lower())
            if not u:
                continue
            try:
                clips = tw.clips(u["id"], since)
            except Exception as e:  # noqa
                log("WARN clips", login, e)
                continue
            if clips:
                games = Counter(c["game_id"] for c in clips)
                tg = [c for c in clips if c["game_id"] == gid]
                top = ", ".join(f'{c["view_count"]}v/{c["duration"]:.0f}s' for c in tg[:3])
                log(f"  {login}: {len(clips)} clips, games {dict(games.most_common(3))}, target {gid}: {len(tg)} [{top}]")
            for c in clips:
                if c["game_id"] != gid or c["id"] in state["posted"]:
                    continue
                if c["view_count"] < cfg["min_views"]:
                    continue
                if not (cfg["min_duration"] <= c["duration"] <= cfg["max_duration"]):
                    continue
                c["_group"] = gname
                c["_login"] = u["login"]
                c["_name"] = u["display_name"]
                cands.append(c)
    cands.sort(key=lambda c: c["view_count"], reverse=True)
    log(f"Found {len(cands)} candidate clips")
    return cands


def download(url, dest):
    subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-f", "best[ext=mp4]/best",
                    "-o", str(dest), url], check=True)
    return dest


# ---------------------------------------------------------------- cam detection
def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return j["streams"][0]["width"], j["streams"][0]["height"], float(j["format"]["duration"])


def detect_cam(path, override=None):
    """Return cam box (x, y, w, h) in pixels or None.

    Webcam faces sit still while in-game faces move around, so we keep the
    face position that repeats in most sampled frames.
    """
    import cv2
    vw, vh, dur = probe(path)
    if override:
        x, y, w, h = override
        return int(x * vw), int(y * vh), int(w * vw), int(h * vh)
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    n = 16
    hits = []
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_MSEC, (dur * (i + 0.5) / n) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 6, minSize=(int(vh * 0.05), int(vh * 0.05)))
        for (x, y, w, h) in faces:
            if h > vh * 0.45:      # whole-screen face cam / just chatting -> ignore
                continue
            hits.append((x, y, w, h))
    cap.release()
    if not hits:
        return None
    grid = vw * 0.06
    key = lambda f: (round((f[0] + f[2] / 2) / grid), round((f[1] + f[3] / 2) / grid))
    (best, cnt), = Counter(key(f) for f in hits).most_common(1)
    if cnt < n * 0.35:
        return None
    fs = [f for f in hits if key(f) == best]
    fx = sorted(f[0] + f[2] / 2 for f in fs)[len(fs) // 2]
    fy = sorted(f[1] + f[3] / 2 for f in fs)[len(fs) // 2]
    fw = sorted(f[2] for f in fs)[len(fs) // 2]
    # box around the face with the aspect of the top strip
    bw = min(vw, fw * 4.2)
    bh = bw * CAM_H / W
    bx = min(max(0, fx - bw / 2), vw - bw)
    by = min(max(0, fy - bh * 0.45), vh - bh)
    return int(bx), int(by), int(bw), int(bh)


# ---------------------------------------------------------------- subtitles
def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def make_ass(path, words, label, dur, sub_y):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,DejaVu Sans,82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1
Style: Hi,DejaVu Sans,82,&H0000E5FF,&H0000E5FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,5,60,60,0,1
Style: Tag,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,10,0,7,24,24,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Tag,,0,0,0,,{label}"]
    # group words into chunks of <=3 words / <=1.4s
    chunk = []
    chunks = []
    for w in words:
        if chunk and (len(chunk) >= 3 or w["end"] - chunk[0]["start"] > 1.4):
            chunks.append(chunk)
            chunk = []
        chunk.append(w)
    if chunk:
        chunks.append(chunk)
    for i, ch in enumerate(chunks):
        st = ch[0]["start"]
        en = chunks[i + 1][0]["start"] if i + 1 < len(chunks) and chunks[i + 1][0]["start"] - ch[-1]["end"] < 0.4 else ch[-1]["end"]
        text = " ".join(w["word"].strip() for w in ch).upper().replace("{", "").replace("}", "")
        ev.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Sub,,0,0,0,,{{\\pos({W // 2},{sub_y})\\fad(40,0)}}{text}")
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


_whisper = None


def transcribe(path, model, lang):
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(model, device="cpu", compute_type="int8")
    import numpy as np
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, _ = _whisper.transcribe(audio, language=lang, word_timestamps=True, vad_filter=True)
    return [{"word": w.word, "start": w.start, "end": w.end} for s in segs for w in (s.words or [])]


# ---------------------------------------------------------------- render
def render(src, dst, cam, label, words):
    vw, vh, dur = probe(src)
    tmp = tempfile.mkdtemp()
    ass = Path(tmp) / "subs.ass"
    if cam:
        cx, cy, cw, ch = cam
        # gameplay: centre crop with the aspect of the bottom part
        gw = min(vw, int(vh * W / GAME_H))
        gx = (vw - gw) // 2
        fc = (f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W}:{CAM_H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{CAM_H},setsar=1[cam];"
              f"[0:v]crop={gw}:{vh}:{gx}:0,scale={W}:{GAME_H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{GAME_H},setsar=1[game];"
              f"[cam][game]vstack=inputs=2,drawbox=y={CAM_H - 3}:w=iw:h=6:color=black@0.9:t=fill[v0]")
        sub_y = CAM_H + 150
    else:
        fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5,"
              f"eq=brightness=-0.15[bg];"
              f"[0:v]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
        sub_y = int(H * 0.30)
    make_ass(ass, words, label, dur, sub_y)
    fc += f";[v0]subtitles='{ass}'[v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", fc,
                    "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p", str(dst)], check=True)
    return dst


# ---------------------------------------------------------------- posting
def post(cfg, profile, video, title, caption, yt_title, yt_desc, tags, idem, private=False):
    data = [("user", profile), ("title", title), ("tiktok_title", caption),
            ("youtube_title", yt_title), ("youtube_description", yt_desc),
            ("privacy_level", "SELF_ONLY" if private else "PUBLIC_TO_EVERYONE"),
            ("privacyStatus", "private" if private else "public"),
            ("categoryId", "20"), ("selfDeclaredMadeForKids", "false"), ("async_upload", "true")]
    data += [("platform[]", p) for p in cfg["platforms"]]
    data += [("tags[]", t) for t in tags]
    with open(video, "rb") as f:
        r = requests.post("https://api.upload-post.com/api/upload",
                          headers={"Authorization": "Apikey " + os.environ["UPLOAD_POST_KEY"],
                                   "Idempotency-Key": idem},
                          data=data, files={"video": (Path(video).name, f, "video/mp4")}, timeout=600)
    log("upload-post:", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()


def texts(c, g):
    name, login = c["_name"], c["_login"]
    t = (c.get("title") or "").strip()
    ru = g.get("language") == "ru"
    base = t if t and len(t) < 80 else (f"{name} в GTA 6" if ru else f"{name} plays GTA 6")
    credit = f"🎥 twitch.tv/{login}"
    caption = f"{base} 😳 {credit} {g['hashtags']}"[:2000]
    yt_title = f"{base} | {name} GTA 6 #shorts"[:100]
    yt_desc = f"{credit}\n\n{('Клип со стрима' if ru else 'Clip from the stream of')} {name}.\n{g['hashtags']}"
    tags = ["gta6", "gta 6", "gta vi", name, "twitch"]
    return base, caption, yt_title, yt_desc, tags


# ---------------------------------------------------------------- main
def process(cfg, src, login, group, words_lang):
    over = cfg.get("cam_overrides", {}).get(login.lower())
    cam = detect_cam(src, over)
    log("cam box:", cam)
    words = transcribe(src, cfg["whisper_model"], words_lang) if cfg.get("subtitles") else []
    OUT_DIR.mkdir(exist_ok=True)
    dst = OUT_DIR / (Path(src).stem + "_vertical.mp4")
    return render(src, dst, cam, f"@{login}", words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--input", help="render a local video instead of searching Twitch")
    ap.add_argument("--streamer", default="streamer")
    ap.add_argument("--lang", default=None)
    ap.add_argument("--game", default=None, help="override game from config (for tests)")
    ap.add_argument("--url", help="test: render a Twitch clip URL or a channel clips page (first N clips)")
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--private-post", action="store_true", help="test: post first rendered --url clip privately")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if a.game:
        cfg["game"] = a.game

    if a.input:
        log("rendered:", process(cfg, a.input, a.streamer, None, a.lang))
        return
    if a.url:
        td = Path(tempfile.mkdtemp())
        subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "-f", "best[ext=mp4]/best", "--playlist-items",
                        f"1-{a.n}", "-o", str(td / "%(id)s.%(ext)s"), a.url], check=True)
        for f in sorted(td.glob("*.mp4")):
            out = process(cfg, f, a.streamer, None, a.lang)
            log("rendered:", out)
            if a.private_post:
                g = next(iter(cfg["groups"].values()))
                fake = {"_name": a.streamer, "_login": a.streamer, "title": "TEST private upload"}
                base, caption, yt_title, yt_desc, tags = texts(fake, g)
                post(cfg, g["profile"], out, base, caption, yt_title, yt_desc, tags, "test-" + f.stem, private=True)
                a.private_post = False
        return

    if not os.environ.get("TWITCH_CLIENT_ID") or not os.environ.get("TWITCH_CLIENT_SECRET"):
        log("::warning::TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET secrets are not set yet, skipping run")
        return

    if not a.dry_run and not os.environ.get("UPLOAD_POST_KEY"):
        log("::warning::UPLOAD_POST_KEY secret is not set yet, skipping run")
        return

    state = load_state()
    cands = find_candidates(cfg, state)
    done = 0
    for c in cands:
        if done >= cfg["posts_per_run"]:
            break
        g = cfg["groups"][c["_group"]]
        if posts_last_24h(state, g["profile"]) >= cfg["max_posts_per_day"]:
            log("daily cap reached for", g["profile"])
            continue
        log(f"-> {c['_login']} {c['view_count']} views: {c['title']} {c['url']}")
        try:
            with tempfile.TemporaryDirectory() as td:
                src = download(c["url"], Path(td) / f"{c['id']}.mp4")
                out = process(cfg, src, c["_login"], c["_group"], g.get("language"))
                base, caption, yt_title, yt_desc, tags = texts(c, g)
                if a.dry_run:
                    log("DRY RUN, not posting:", out, "|", caption)
                    done += 1
                    continue
                res = post(cfg, g["profile"], out, base, caption, yt_title, yt_desc, tags, c["id"])
                state["posted"][c["id"]] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "ok": True,
                                            "profile": g["profile"], "login": c["_login"], "url": c["url"],
                                            "views": c["view_count"], "request_id": res.get("request_id")}
                done += 1
        except Exception as e:  # noqa
            log("ERROR", c["id"], repr(e))
            state["posted"][c["id"]] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "ok": False,
                                        "error": repr(e)[:300], "login": c["_login"]}
        save_state(state)
    log(f"done: {done} clip(s)")


if __name__ == "__main__":
    main()
