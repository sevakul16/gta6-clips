#!/usr/bin/env python3
"""GTA 6 auto-clipper.

Moments from live Twitch streams (chat spikes + viewer clips) -> free quality checks
-> an editor scores each candidate -> the best approved one is rendered as a vertical
1080x1920 video (streamer cam on top, gameplay below, burned-in subtitles) and posted
through upload-post.com (TikTok + YouTube Shorts).

The editor is either a reviewer who reads the `review` branch and writes
state/verdicts.json (judge.mode: review), or the Claude API (judge.mode: api).

Usage:
  python clipper.py                 # normal run
  python clipper.py --dry-run       # everything except posting
  python clipper.py --url <twitch clip url> --streamer xqc   # one clip through the checks
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "posted.json"
REQUEUE_FILE = ROOT / "state" / "requeue.json"   # editor-owned: full candidates of refused posts made before retries existed
OUT_DIR = ROOT / "out"
PREV_DIR = OUT_DIR / "previews"
REVIEW_DIR = OUT_DIR / "review"
VERDICTS_FILE = ROOT / "state" / "verdicts.json"
W, H = 1080, 1920
CAM_H = 760            # height of the cam strip on top
GAME_H = H - CAM_H     # gameplay part below


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- state
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def age_hours(iso):
    return (now_utc() - dt.datetime.fromisoformat(iso)).total_seconds() / 3600


def load_state():
    st = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    st.setdefault("posted", {})      # id -> what was published (or failed)
    st.setdefault("rejected", {})    # id -> why a candidate was thrown away
    st.setdefault("pending", {})     # id -> passed the free checks, waiting for the editor's verdict
    st.setdefault("pool", {})        # id -> approved moments waiting for a publishing slot
    st.setdefault("no_vod", {})      # login -> channel whose recordings cannot be downloaded (subscribers only)
    return st


def save_state(state):
    state["rejected"] = {k: v for k, v in state["rejected"].items() if age_hours(v["at"]) < 72}
    state["pending"] = {k: v for k, v in state["pending"].items() if age_hours(v["at"]) < 8}
    state["no_vod"] = {k: v for k, v in state["no_vod"].items() if age_hours(v["at"]) < 24}
    # approved moments wait for a slot (one post per min_gap_minutes): keep them long enough to get one
    state["pool"] = {k: v for k, v in state["pool"].items() if age_hours(v["at"]) < 48}
    for v in state["posted"].values():      # the copy kept for a retry is only needed until the platforms answer
        if age_hours(v["at"]) > 8:
            v.pop("cand", None)
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


LIMIT_RE = re.compile(r"limit|quota|may upload|too many", re.I)
MAX_ATTEMPTS = 3        # a post refused this many times is given up: an endless retry would only burn uploads
DAY = dt.timedelta(hours=24)


def recent_posts(state, profile, until=None):
    """Times of the profile's posts in the 24 hours before `until` (now by default), oldest first.
    YouTube counts a channel's uploads over a rolling 24 hours: on 19.11 it still refused an upload
    made after midnight Pacific, when the ten before it were all less than a day old."""
    until = until or now_utc()
    return sorted(t for t in (dt.datetime.fromisoformat(v["at"]) for v in state["posted"].values()
                              if v.get("profile") == profile and v.get("ok")) if until - DAY <= t <= until)


def minutes_since_last_post(state, profile):
    ages = [age_hours(v["at"]) * 60 for v in state["posted"].values() if v.get("profile") == profile and v.get("ok")]
    return min(ages) if ages else 10 ** 6


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
                if c["game_id"] != gid or known(state, c["id"]):
                    continue
                if c["view_count"] < cfg["min_views"]:
                    continue
                if not (cfg["min_duration"] <= c["duration"] <= cfg["max_duration"]):
                    continue
                cands.append({"id": c["id"], "url": c["url"], "section": None, "kind": "clip",
                              "views": c["view_count"], "title": c.get("title") or "",
                              "vod": c.get("video_id") or None, "vod_t": c.get("vod_offset"),
                              "group": gname, "login": u["login"], "name": u["display_name"],
                              "source": f"Twitch clip made by a viewer, {c['view_count']} views, "
                                        f"titled \"{c.get('title') or ''}\""})
    cands.sort(key=lambda c: c["views"], reverse=True)
    log(f"New viewer clips to check: {len(cands)}")
    return by_language(cands)


def known(state, cid):
    return any(cid in state[k] for k in ("posted", "rejected", "pending", "pool"))


def find_chat_moments(cfg, state):
    """Listen to live chats of streamers playing the target game; return cut-able moments."""
    import chatspikes
    cc = cfg.get("chat") or {}
    if not cc.get("enabled"):
        return []
    tw = Twitch(os.environ["TWITCH_CLIENT_ID"], os.environ["TWITCH_CLIENT_SECRET"])
    gid = tw.game_id(cfg["game"])
    group_of = {s.lower(): gname for gname, g in cfg["groups"].items() for s in g["streamers"]}
    live = chatspikes.live_targets(tw, list(group_of), gid)
    # plus the biggest live streams in the game right now, separately for each language
    for lang, grp in ((g.get("language"), gname) for gname, g in cfg["groups"].items()):
        found = 0
        for st in tw.get("streams", {"game_id": gid, "language": lang, "first": 30}) if cc.get("top_live") else []:
            login = st["user_login"].lower()
            if found >= cc["top_live"] or st["viewer_count"] < cc.get("top_live_min_viewers", 0):
                break                                    # the list is sorted by viewers
            if login not in live and login not in state["no_vod"]:
                live[login] = st
                group_of[login] = grp
                found += 1
    live = {k: v for k, v in live.items() if k not in state["no_vod"]}
    by_size = sorted(live.values(), key=lambda st: st["viewer_count"], reverse=True)
    log(f"Live in {cfg['game']}: " + (", ".join(
        f"{st['user_login']} {group_of[st['user_login'].lower()]} {st['viewer_count'] / 1000:.1f}k" for st in by_size)
        or "nobody"))
    if not live:
        return []
    minutes = float(os.environ.get("CHAT_LISTEN_MINUTES") or cc.get("listen_minutes", 15))
    spikes, series = chatspikes.listen(list(live), minutes * 60, log=log, ratio=cc.get("ratio", 2.5),
                                       min_msgs=cc.get("min_msgs", 20), z=cc.get("z", 6.0))
    if minutes >= 5:
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / ".listened").write_text("1")
    PREV_DIR.mkdir(parents=True, exist_ok=True)
    (PREV_DIR / "chat_series.json").write_text(json.dumps(
        {"bucket_s": chatspikes.BUCKET, "series": series,
         "spikes": [{**sp, "at": sp["at"].isoformat()} for sp in spikes]}))
    moments = []
    import math
    rank = lambda x: x["score"] * math.log10(max(live[x["login"]]["viewer_count"], 100))
    for sp in sorted(spikes, key=rank, reverse=True):
        st = live[sp["login"]]
        try:
            vod = chatspikes.vod_for(tw, st)
        except Exception as e:  # noqa
            log("  WARN vod lookup", sp["login"], repr(e))
            continue
        if not vod:
            log("  no recording (VOD) for", sp["login"])
            continue
        vstart = dt.datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00"))
        off = (sp["at"] - vstart).total_seconds()
        a0 = max(0, off - cc.get("before", 35))
        a1 = off + cc.get("after", 8)
        mid = f"chat-{vod['id']}-{int(off // 45)}"
        if known(state, mid):
            continue
        moments.append({"id": mid, "url": f"https://www.twitch.tv/videos/{vod['id']}", "section": [a0, a1],
                        "kind": "chat", "views": sp["msgs"], "title": "", "vod": vod["id"], "vod_t": a0,
                        "group": group_of[sp["login"]], "login": st["user_login"], "name": st["user_name"],
                        "viewers": st["viewer_count"], "chat_score": round(sp["score"], 2), "found_at": now_utc().isoformat(),
                        "spike_at": sp["at"].isoformat(), "chat": sp.get("chat") or [],
                        "source": f"cut from the live stream at a chat spike: {sp['msgs']} chatters in 15 s, "
                                  f"{sp['score']:.1f}x the usual pace; the chat reacted about {off - a0:.0f} s "
                                  f"into the clip, so the moment itself is usually 5-20 s before that"})
    log(f"New chat moments: {len(moments)}")
    return by_language(moments)


def by_language(items):
    """Round-robin over the groups (ru, en, ...) so that every language gets checked, best first within each."""
    queues = {}
    for c in items:
        queues.setdefault(c["group"], []).append(c)
    out = []
    while any(queues.values()):
        for q in queues.values():
            if q:
                out.append(q.pop(0))
    return out


def download(url, dest, section=None):
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-o", str(dest)]
    if section:
        cmd += ["-f", "best[height<=1080]/best", "--download-sections", f"*{section[0]:.0f}-{section[1]:.0f}"]
    else:
        cmd += ["-f", "best[ext=mp4]/best"]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=600)
    if r.returncode or not Path(dest).exists():
        raise RuntimeError("yt-dlp: " + (r.stderr.strip() or "no file")[-300:])
    return dest


# ---------------------------------------------------------------- cam detection
def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    return j["streams"][0]["width"], j["streams"][0]["height"], float(j["format"]["duration"])


def detect_cam(path, override=None):
    """Return (cam box (x, y, w, h) in pixels or None, streamer_fills_screen).

    Webcam faces sit still while in-game faces move around, so we keep the
    face position that repeats in most sampled frames.
    """
    import cv2
    vw, vh, dur = probe(path)
    if override:
        x, y, w, h = override
        return (int(x * vw), int(y * vh), int(w * vw), int(h * vh)), False
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    n = 16
    hits = []
    big = 0
    for i in range(n):
        cap.set(cv2.CAP_PROP_POS_MSEC, (dur * (i + 0.5) / n) * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.1, 6, minSize=(int(vh * 0.03), int(vh * 0.03)))
        if any(h > vh * 0.2 for (x, y, w, h) in faces):
            big += 1               # streamer fills the screen (just chatting / full cam)
        for (x, y, w, h) in faces:
            if h > vh * 0.2:
                continue
            hits.append((x, y, w, h))
    cap.release()
    if big >= n * 0.35:
        return None, True
    if not hits:
        return None, False
    grid = vw * 0.06
    key = lambda f: (round((f[0] + f[2] / 2) / grid), round((f[1] + f[3] / 2) / grid))
    (best, cnt), = Counter(key(f) for f in hits).most_common(1)
    if cnt < n * 0.35:
        return None, False
    fs = [f for f in hits if key(f) == best]
    fx = sorted(f[0] + f[2] / 2 for f in fs)[len(fs) // 2]
    fy = sorted(f[1] + f[3] / 2 for f in fs)[len(fs) // 2]
    fw = sorted(f[2] for f in fs)[len(fs) // 2]
    # box around the face with the aspect of the top strip
    bw = min(vw, fw * 4.2)
    bh = bw * CAM_H / W
    bx = min(max(0, fx - bw / 2), vw - bw)
    by = min(max(0, fy - bh * 0.45), vh - bh)
    return (int(bx), int(by), int(bw), int(bh)), False


# ---------------------------------------------------------------- subtitles
# Swear words and slurs are starred out in the burned-in text: platforms cut the reach of videos that show them.
_BAD = re.compile(
    r"(?<![а-яёa-z])(?:"
    r"(?:на|по|за|не|ни|до|от|о|а|при|рас|раз|вы|у)?ху[йяеёию][а-яё]*|хули"
    r"|[а-яё]*пизд[а-яё]*"
    r"|бля(?:ть|дь|д[а-яё]+|т[а-яё]+|ха)?"
    r"|(?:за|на|вы|до|по|у|про|при|пере|недо|долбо|разъ|подъ|объ|отъ|съ|въ|изъ)?[её]б[а-яё]*"
    r"|долбо[её]п[а-яё]*|мудак[а-яё]*|мудил[а-яё]*|сук[аиу]|сучк[а-яё]*|ган?дон[а-яё]*|залуп[а-яё]*"
    r"|[а-яё]*(?:пид[оа]р|педик)[а-яё]*|ниг+ер[а-яё]*|хач(?:и|а|ей|ик|ики)?|чурк[а-яё]*"
    r"|[a-z]*fuck[a-z]*|[a-z]*shit[a-z]*|bitch[a-z]*|nigg[a-z]*|cunt[a-z]*|fagg?ot[a-z]*|retard[a-z]*"
    r")(?![а-яёa-z])", re.I)


def clean(text):
    return _BAD.sub(lambda m: m.group(0)[0] + "*" * min(len(m.group(0)) - 1, 5), text)


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
        text = clean(" ".join(w["word"].strip() for w in ch)).upper().replace("{", "").replace("}", "")
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
    return [{"word": w.word, "start": round(w.start, 2), "end": round(w.end, 2)}
            for s in segs for w in (s.words or [])], audio


# ---------------------------------------------------------------- render
def layout(cam, vw, vh):
    """ffmpeg graph that turns the source frame into the vertical picture [v0]; second value: subtitle height."""
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
        return fc, CAM_H + 150
    fc = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:5,"
          f"eq=brightness=-0.15[bg];"
          f"[0:v]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    return fc, int(H * 0.30)


def peak_time(src, cam, t0, t1):
    """Second of the source where the gameplay moves the most for a while: the default cover frame."""
    import numpy as np
    import quality
    vw, vh, _ = probe(src)
    f = quality._frames(src)
    if len(f) < 12:
        return (t0 + t1) / 2
    mask = np.ones(f.shape[1:], bool)
    if cam:
        x, y, w, h = cam
        mask[int(y / vh * quality.SH):int((y + h) / vh * quality.SH) + 1,
             int(x / vw * quality.SW):int((x + w) / vw * quality.SW) + 1] = False
    d = (np.abs(f[1:] - f[:-1]) > 10)[:, mask].mean(axis=1)
    win = int(quality.FPS * 1.5)
    sm = np.convolve(d, np.ones(win) / win, mode="same")
    a = int((t0 + (t1 - t0) * 0.15) * quality.FPS)
    b = max(a + 1, int((t1 - 1.0) * quality.FPS))
    return (a + int(np.argmax(sm[a:b]))) / quality.FPS


COVER_FONTS = [os.environ.get("COVER_FONT", ""), str(Path.home() / "fonts" / "cover.ttf"),
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def cover_lines(hook, max_lines=3):
    """Headline -> up to 3 short upper-case lines of similar length."""
    words = "".join(ch for ch in hook.upper() if ord(ch) < 0x2000 and ch not in "{}").split()
    if not words:
        return []
    n = 1 if len(" ".join(words)) <= 11 or len(words) == 1 else 2 if len(" ".join(words)) <= 26 or len(words) == 2 \
        else max_lines
    best = None
    # every way to cut the words into n consecutive groups; keep the one with the shortest longest line
    def cuts(k, start):
        if k == 1:
            yield [words[start:]]
            return
        for i in range(start + 1, len(words) - k + 2):
            for rest in cuts(k - 1, i):
                yield [words[start:i]] + rest
    for groups in cuts(min(n, len(words)), 0):
        lines = [" ".join(g) for g in groups]
        if best is None or max(map(len, lines)) < max(map(len, best)):
            best = lines
    return best


def make_cover(src, t, cam, hook, label, dst):
    """Thumbnail: the frame at second t in the vertical layout, punchier colours, big headline on the seam."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    vw, vh, _ = probe(src)
    fc, _ = layout(cam, vw, vh)
    raw = Path(tempfile.mkdtemp()) / "frame.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(src), "-filter_complex", fc,
                    "-map", "[v0]", "-frames:v", "1", str(raw)], check=True)
    im = Image.open(raw).convert("RGB")
    im = ImageEnhance.Color(im).enhance(1.35)
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = im.filter(ImageFilter.UnsharpMask(radius=3, percent=60))
    font_file = next((f for f in COVER_FONTS if f and Path(f).exists()), None)
    lines = cover_lines(hook or "")
    if lines and font_file:
        size = 230
        while size > 80:
            font = ImageFont.truetype(font_file, size)
            if max(font.getlength(ln) for ln in lines) <= W * 0.90:
                break
            size -= 6
        stroke = max(8, size // 9)
        lh = int(size * 1.08)
        bw, bh = W, lh * len(lines) + stroke * 4
        layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for i, ln in enumerate(lines):
            colour = (255, 230, 0) if i == len(lines) - 1 and len(lines) > 1 else (255, 255, 255)
            d.text((bw // 2, stroke * 2 + lh * i + lh // 2), ln, font=font, fill=colour, anchor="mm",
                   stroke_width=stroke, stroke_fill=(0, 0, 0))
        shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 150), mask=layer.split()[3])
        shadow = shadow.filter(ImageFilter.GaussianBlur(14))
        block = Image.alpha_composite(shadow, layer).rotate(3, resample=Image.BICUBIC, expand=True)
        cy = (CAM_H if cam else int(H * 0.30)) + 40          # on the seam between the webcam and the game
        y = min(max(20, cy - block.height // 2), H - block.height - 20)
        # darken the band behind the text a little so it reads on bright frames
        band = Image.new("RGBA", (W, block.height), (0, 0, 0, 0))
        ImageDraw.Draw(band).rectangle([0, int(block.height * 0.12), W, int(block.height * 0.88)], fill=(0, 0, 0, 70))
        im = im.convert("RGBA")
        im.alpha_composite(band.filter(ImageFilter.GaussianBlur(40)), (0, y))
        im.alpha_composite(block, ((W - block.width) // 2, y))
        im = im.convert("RGB")
    if label and font_file:
        d = ImageDraw.Draw(im)
        f2 = ImageFont.truetype(font_file, 44)
        tw = int(f2.getlength(label))
        d.rounded_rectangle([20, 20, 20 + tw + 36, 20 + 70], radius=12, fill=(0, 0, 0))
        d.text((38, 55), label, font=f2, fill=(255, 255, 255), anchor="lm")
    im.save(dst, quality=92)
    return dst


def render(src, dst, cam, label, words, keep=None, cover=None, cover_secs=0.5):
    vw, vh, dur = probe(src)
    t0 = 0.0
    if keep:
        t0, t1 = max(0.0, keep[0]), min(dur, keep[1])
        dur = t1 - t0
        words = [{"word": w["word"], "start": w["start"] - t0, "end": w["end"] - t0}
                 for w in words if w["start"] >= t0 and w["end"] <= t1 + 0.3]
    tmp = tempfile.mkdtemp()
    ass = Path(tmp) / "subs.ass"
    fc, sub_y = layout(cam, vw, vh)
    make_ass(ass, words, label, dur, sub_y)
    inputs = ["-ss", f"{t0:.2f}", "-t", f"{dur:.2f}", "-i", str(src)]
    if cover:       # the cover is the first frames of the video: platforms pick it, viewers see the headline at once
        inputs += ["-i", str(cover)]
        fc += (f";[v0]subtitles='{ass}'[vs];[1:v]scale={W}:{H},setsar=1[cv];"
               f"[vs][cv]overlay=0:0:enable='lt(t,{cover_secs})'[v]")
    else:
        fc += f";[v0]subtitles='{ass}'[v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs,
                    "-filter_complex", fc,
                    "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p", str(dst)], check=True)
    return dst


# ---------------------------------------------------------------- posting
def post(cfg, profile, video, title, caption, yt_title, yt_desc, tags, idem, private=False, cover=None,
         cover_ms=None, platforms=None):
    platforms = platforms or cfg["platforms"]
    data = [("user", profile), ("title", title), ("tiktok_title", caption),
            ("youtube_title", yt_title), ("youtube_description", yt_desc),
            ("privacy_level", "SELF_ONLY" if private else "PUBLIC_TO_EVERYONE"),
            ("privacyStatus", "private" if private else "public"),
            ("categoryId", "20"), ("selfDeclaredMadeForKids", "false"), ("async_upload", "true")]
    data += [("platform[]", p) for p in platforms]
    data += [("tags[]", t) for t in tags]
    if cover_ms is not None:
        data.append(("cover_timestamp", str(int(cover_ms))))      # TikTok: which frame is the cover
    thumb = cover if cover and "youtube" in platforms and (cfg.get("covers") or {}).get(
        "youtube_thumbnail", True) else None

    def send(with_thumb, key):
        files = {"video": (Path(video).name, open(video, "rb"), "video/mp4")}
        if with_thumb:
            files["thumbnail"] = (Path(thumb).name, open(thumb, "rb"), "image/jpeg")
        try:
            return requests.post("https://api.upload-post.com/api/upload",
                                 headers={"Authorization": "Apikey " + os.environ["UPLOAD_POST_KEY"],
                                          "Idempotency-Key": key},
                                 data=data, files=files, timeout=600)
        finally:
            for f in files.values():
                f[1].close()

    r = send(bool(thumb), idem)
    if thumb and 400 <= r.status_code < 500:        # never lose a post because of its thumbnail
        log("   WARN upload with a thumbnail was refused:", r.status_code, "- sending without it")
        r = send(False, idem + "-nothumb")
    r.raise_for_status()
    j = r.json()
    log("upload-post:", r.status_code, {k: j.get(k) for k in ("success", "message", "request_id", "total_platforms")},
        "thumbnail sent" if thumb else "")
    return j


def check_posts(state):
    """Ask upload-post what happened to recent uploads (they are processed in the background)."""
    key = os.environ.get("UPLOAD_POST_KEY")
    for cid, v in state["posted"].items():
        # a finished upload is asked again while its thumbnail is still being set
        waiting_thumb = isinstance(v.get("result"), list) and any(
            isinstance(x, dict) and x.get("thumbnail_pending") for x in v["result"])
        if not key or not v.get("ok") or not v.get("request_id") or (v.get("result") and not waiting_thumb) \
                or age_hours(v["at"]) > 6 or age_hours(v["at"]) < 0.05:
            continue
        try:
            r = requests.get("https://api.upload-post.com/api/uploadposts/status", timeout=30,
                             params={"request_id": v["request_id"]}, headers={"Authorization": "Apikey " + key})
            j = r.json() if r.ok else {}
            # the raw answer carries account details: log only what is needed
            log(f"upload status {v.get('login')} {cid[-12:]}: {r.status_code} {j.get('status')} " + ", ".join(
                f"{x.get('platform')} {'ok' if x.get('success') else 'FAILED ' + str(x.get('error_message'))[:120]}"
                + "".join(f" {k}={str(x[k])[:140]}" for k in ("thumbnail_set", "thumbnail_pending", "thumbnail_note",
                                                              "thumbnail_error", "warnings") if x.get(k) is not None)
                for x in j.get("results") or [] if isinstance(x, dict)))
            if str(j.get("status", "")).lower() in ("completed", "finished", "done", "failed", "error"):
                v["result"] = [{k: x.get(k) for k in ("platform", "success", "post_url", "error_message",
                                                       "thumbnail_set", "thumbnail_pending", "thumbnail_error")
                                if x.get(k) is not None} for x in j.get("results") or [] if isinstance(x, dict)] \
                              or str(j.get("status"))
        except Exception as e:  # noqa
            log("   WARN upload status check failed:", repr(e)[:200])
    settle_failed(state)


def settle_failed(state):
    """A refused post goes back into the queue instead of being lost: for every platform when all refused it,
    or only for the refusing ones when it is already out elsewhere (a second upload would double it there).
    A refusal over a limit also holds the profile until its oldest post of the last 24 hours is a day old:
    that is when the platform's rolling window has room again."""
    try:
        spare = json.loads(REQUEUE_FILE.read_text()) if REQUEUE_FILE.exists() else {}
    except Exception:  # noqa
        spare = {}
    for cid, v in list(state["posted"].items()):
        res = [x for x in v["result"] if isinstance(x, dict)] if isinstance(v.get("result"), list) else []
        if isinstance(v.get("result"), str) and v["result"].lower() in ("failed", "error"):
            res = [{"error_message": "upload " + v["result"]}]      # failed as a whole, no per-platform answer
        failed = [x for x in res if not x.get("success")]
        if not v.get("ok") or not failed:
            if res:
                v.pop("cand", None)
            continue
        why = "; ".join(str(x.get("error_message") or "")[:160] for x in failed)
        at = dt.datetime.fromisoformat(v["at"])
        if LIMIT_RE.search(why) and v.get("profile"):
            before = [t for t in recent_posts(state, v["profile"], at) if t < at]
            until = before[0] + DAY + dt.timedelta(minutes=10) if before else at + dt.timedelta(minutes=135)
            if until > now_utc():
                state.setdefault("hold", {})[v["profile"]] = {"until": until.isoformat(), "why": why[:200]}
        cand = v.get("cand") or spare.get(cid)
        done = [x for x in res if x.get("success")]
        only = sorted({str(x["platform"]) for x in failed if x.get("platform")}) if done else None
        tries = int((cand or {}).get("attempt") or 0) + 1
        if cand and tries <= MAX_ATTEMPTS and (only or not done):
            cand = {k: x for k, x in cand.items() if k != "previewed"}
            cand["attempt"] = tries
            if only:            # out on the other platforms already: the retry goes to the refusing ones alone
                cand["platforms"] = only
                cand["earlier"] = (cand.get("earlier") or []) + done
            state["pool"][cid] = cand
            del state["posted"][cid]
            log(f"   {v.get('login')} {cid[-12:]}: the post was refused ({why[:110]}), back in the queue"
                + (f" for {', '.join(only)}" if only else ""))
        elif done:              # it stays out where it went through
            v.pop("cand", None)
            log(f"   {v.get('login')} {cid[-12:]}: refused on one platform ({why[:110]}), no retry left")
        else:
            v["ok"], v["error"] = False, why[:300]
            v.pop("cand", None)
            log(f"   {v.get('login')} {cid[-12:]}: the post was refused ({why[:110]}) and cannot be retried")


def texts(c, g):
    name, login = c["name"], c["login"]
    ru = g.get("language") == "ru"
    base = (c.get("caption") or "").strip() or (f"{name} в GTA 6" if ru else f"{name} plays GTA 6")
    credit = f"🎥 twitch.tv/{login}"
    caption = f"{base} {credit} {g['hashtags']}"[:2000]
    yt_title = f"{base} | {name} GTA 6 #shorts"
    if len(yt_title) > 100:
        yt_title = f"{base[:100 - len(' #shorts') - 1].rstrip()} #shorts"
    yt_desc = f"{credit}\n\n{('Момент со стрима' if ru else 'A moment from the stream of')} {name}.\n{g['hashtags']}"
    tags = ["gta6", "gta 6", "gta vi", name, "twitch"]
    return base, caption, yt_title, yt_desc, tags


# ---------------------------------------------------------------- previews
def sheet(video, dst, n=5, width=384):
    """Contact sheet: n frames of the video side by side (for reviewing runs without the mp4)."""
    try:
        _, _, dur = probe(video)
        PREV_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                        f"fps={n / max(dur, 1):.4f},scale={width}:-2,tile={n}x1", "-frames:v", "1", "-q:v", "5",
                        str(PREV_DIR / dst)], check=True)
    except Exception as e:  # noqa
        log("  WARN preview failed:", repr(e))


REPORT = []


def report(c, verdict, **extra):
    REPORT.append({"id": c["id"], "kind": c["kind"], "login": c["login"], "views": c.get("views"),
                   "url": c["url"], "section": c.get("section"), "verdict": verdict, **extra})
    PREV_DIR.mkdir(parents=True, exist_ok=True)
    (PREV_DIR / "report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- evaluate / publish
def overlaps(a, b):
    """Same stretch of the same stream recording (so a viewer clip and a chat cut don't double-post)."""
    if not a.get("vod") or a.get("vod") != b.get("vod") or a.get("vod_t") is None or b.get("vod_t") is None:
        return False
    return abs(float(a["vod_t"]) - float(b["vod_t"])) < 60


def reject(state, c, why, **extra):
    log(f"   rejected: {why}")
    state["rejected"][c["id"]] = {"at": now_utc().isoformat(), "why": why, "login": c["login"]}
    report(c, "rejected", why=why, **extra)


def crop_boxes(cam, vw, vh):
    """Source-frame areas that end up in the vertical video: (cam box or None, gameplay box)."""
    if not cam:
        return None, (0, 0, vw, vh)
    gw = min(vw, int(vh * W / GAME_H))
    return tuple(cam), ((vw - gw) // 2, 0, gw, vh)


def review_pack(c, src, dur, cam, words, m, lang):
    """Frames + transcript of a candidate for the editor (pushed to the `review` branch)."""
    import judge
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    vw, vh, _ = probe(src)
    n, cols, fw = (15 if dur > 50 else 12), 3, 512
    fh = int(fw * vh / vw / 2) * 2
    k = fw / vw
    cbox, gbox = crop_boxes(cam, vw, vh)
    draw = ""
    for box, color in ((gbox if cam else None, "yellow"), (cbox, "lime")):
        if box:
            draw += (f",drawbox=x={int(box[0] * k)}:y={int(box[1] * k)}:w={int(box[2] * k)}:h={int(box[3] * k)}"
                     f":color={color}:t=2")
    times = []
    with tempfile.TemporaryDirectory() as td:
        last = None
        for i in range(n):
            t = dur * (i + 0.5) / n
            times.append(round(t, 1))
            f = Path(td) / f"{i:02d}.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={fw}:{fh}{draw}", "-q:v", "4", str(f)])
            if not f.exists() and last:
                f.write_bytes(last.read_bytes())
            if f.exists():
                last = f
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f"{td}/%02d.jpg", "-vf", f"tile={cols}x{n // cols}",
                        "-frames:v", "1", "-q:v", "4", str(REVIEW_DIR / f"{c['id']}.jpg")], check=True)
    (REVIEW_DIR / f"{c['id']}.json").write_text(json.dumps({
        "id": c["id"], "kind": c["kind"], "login": c["login"], "name": c["name"], "language": lang,
        "source": c["source"], "url": c["url"], "section": c.get("section"), "duration": round(dur, 1),
        "frames": f"{cols} columns x {n // cols} rows, left to right then top to bottom",
        "frame_times": times, "video_size": [vw, vh],
        "boxes": "lime = webcam crop (top of the vertical video), yellow = gameplay crop (bottom)" if cam
                 else "no webcam was detected: without a cam box from the editor the clip is rejected",
        "cam_box": [round(cam[0] / vw, 3), round(cam[1] / vh, 3), round(cam[2] / vw, 3), round(cam[3] / vh, 3)]
                   if cam else None,
        "metrics": {a: round(b, 3) for a, b in m.items()}, "found_at": c.get("at"),
        "chat_at_spike": c.get("chat") or None,
        "transcript": judge.transcript(words)}, ensure_ascii=False, indent=1))


def apply_verdict(cfg, state, c, v, dur):
    """Editor's verdict -> approved (pool) or rejected. v=None: no editor configured, accept as 5/10."""
    jc = cfg.get("judge") or {}
    m = c.get("metrics") or {}
    keep, score = None, 5
    if v is not None:
        score = int(v.get("moment_score", 0))
        if v.get("cam") and c.get("size"):          # editor's webcam box: x, y, w, h as fractions of the frame
            vw, vh = c["size"]
            x, y, w, h = [min(max(float(a), 0.0), 1.0) for a in v["cam"]]
            c["cam"] = [int(x * vw), int(y * vh), max(16, int(min(w, 1 - x) * vw)), max(16, int(min(h, 1 - y) * vh))]
            c["cam_src"] = "editor"
        why = None
        if not v.get("gameplay_visible", True):
            why = "editor: no gameplay on screen"
        elif v.get("gambling_ad") and jc.get("block_gambling_ads", True):
            why = "editor: betting/casino branding in the picture"
        elif not c.get("cam") and (cfg.get("quality") or {}).get("require_cam", True):
            why = "editor: no webcam in the picture"
        elif score < jc.get("min_score", 6):
            why = f"editor: {score}/10, {v.get('what_happens', '')}"
        if why:
            reject(state, c, why, metrics=m, editor=v)
            return False
        k0, k1 = float(v.get("keep_start") or 0), float(v.get("keep_end") or dur)
        if 0 <= k0 < k1 <= dur + 1 and k1 - k0 >= 12 and dur - (k1 - k0) >= 3:
            keep = [round(k0, 1), round(min(k1, dur), 1)]
        c["caption"] = (v.get("caption") or "").strip()
        for k in ("hook", "cover_at", "no_subs"):
            if v.get(k) is not None:
                c[k] = v[k]
    c.update({"at": now_utc().isoformat(), "score": score, "keep": keep})
    state["pool"][c["id"]] = c
    report(c, "approved", score=score, keep=keep, caption=c.get("caption"), metrics=m, editor=v)
    log(f"   approved with {score}/10, waiting for a publishing slot")
    return True


def latest_verdicts():
    """state/verdicts.json as it is on the main branch right now (the editor pushes while a run is going)."""
    try:
        subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", "main"], cwd=ROOT, check=True, timeout=60)
        return subprocess.run(["git", "show", "FETCH_HEAD:state/verdicts.json"], cwd=ROOT, check=True,
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa  (no git, offline, file not there yet)
        return VERDICTS_FILE.read_text() if VERDICTS_FILE.exists() else "{}"


def load_verdicts():
    try:
        return json.loads(latest_verdicts())
    except ValueError as e:
        log("WARN state/verdicts.json is unreadable:", repr(e))
        return None


def apply_reviews(cfg, state, verdicts=None):
    """Pick up verdicts the editor wrote to state/verdicts.json for candidates waiting in 'pending'."""
    if not state["pending"]:
        return
    if verdicts is None:
        verdicts = load_verdicts()
    if verdicts is None:
        return
    n = 0
    for cid, v in verdicts.items():
        c = state["pending"].pop(cid, None)
        if not c:
            continue
        n += 1
        log(f"-> editor's verdict for {c['login']} {cid}: {v.get('moment_score')}/10 {v.get('what_happens', '')}")
        try:
            apply_verdict(cfg, state, c, v, float(c.get("dur") or 0))
        except Exception as e:  # noqa
            log("   ERROR applying verdict:", repr(e)[:300])
    log(f"Editor verdicts applied: {n}; still waiting for a verdict: {len(state['pending'])}")


def amend_pool(state, verdicts):
    """The editor may add a headline, a cover frame or a better caption to a moment that is already approved."""
    for cid, c in state["pool"].items():
        v = (verdicts or {}).get(cid) or {}
        for k in ("hook", "cover_at", "no_subs"):
            if v.get(k) is not None:
                c[k] = v[k]
        if (v.get("caption") or "").strip():
            c["caption"] = v["caption"].strip()


def judge_mode(cfg):
    jc = cfg.get("judge") or {}
    return jc.get("mode", "review") if jc.get("enabled", True) else "off"


def evaluate(cfg, state, c, td):
    """Download a candidate and run the free checks, then hand it to the editor.

    review mode: frames + transcript go to the review branch, the candidate waits in 'pending'.
    api mode: the Claude API gives the verdict right away. Approved candidates go to the pool.
    """
    import judge
    import quality
    g = cfg["groups"][c["group"]]
    jc = cfg.get("judge") or {}
    mode = judge_mode(cfg)
    log(f"-> [{c['kind']}] {c['login']} ({c.get('views')}): {c['title']} {c['url']} {c.get('section') or ''}")
    for other in list(state["posted"].values()) + list(state["pool"].values()) + list(state["pending"].values()):
        if overlaps(c, other):
            return reject(state, c, "same moment is already posted or queued")
    src = Path(td) / f"{c['id']}.mp4"
    if c.get("spike_at"):       # the recording lags behind the live stream: give it time to include the cut
        lag = 120 - (now_utc() - dt.datetime.fromisoformat(c["spike_at"])).total_seconds()
        if lag > 0:
            log(f"   waiting {lag:.0f}s for the recording to catch up")
            time.sleep(lag)
    try:
        download(c["url"], src, c.get("section"))
    except Exception as e:  # noqa
        log("   download failed:", repr(e)[:300])
        if "subscriber-only" in repr(e):      # recordings of this channel are closed: stop listening to it for a day
            state["no_vod"][c["login"].lower()] = {"at": now_utc().isoformat()}
        return reject(state, c, "download failed")
    vw, vh, dur = probe(src)
    cam, fullcam = detect_cam(src, cfg.get("cam_overrides", {}).get(c["login"].lower()))
    m = quality.motion(src, cam, vw, vh)
    tag = f"{c['login']}_{c['id'][-12:]}"
    # in review mode the editor can still point at a webcam the detector missed
    ok, why = quality.verdict(m, cam, fullcam, cfg, cam_required=mode != "review")
    log(f"   {vw}x{vh} {dur:.0f}s, cam {cam}, motion {m['motion']:.2f} (p90 {m['motion_p90']:.2f}), "
        f"static {m['static_share']:.0%}")
    if not ok:
        sheet(src, f"REJ_{tag}.jpg")
        return reject(state, c, why, metrics=m)
    words, pcm = transcribe(src, cfg["whisper_model"], g.get("language")) if cfg.get("subtitles") else ([], None)
    if pcm is not None:
        m.update(quality.loudness(pcm))
    c.update({"at": now_utc().isoformat(), "cam": list(cam) if cam else None, "size": [vw, vh],
              "dur": round(dur, 1), "words": words, "metrics": m})
    if mode == "review":
        review_pack(c, src, dur, cam, words, m, g.get("language", "en"))
        state["pending"][c["id"]] = c
        report(c, "pending", metrics=m)
        log("   passed the free checks, sent to the editor")
        return
    v = None
    if mode == "api":
        v = judge.judge(src, dur, words, {"name": c["name"], "login": c["login"], "language": g.get("language", "en"),
                                           "source": c["source"]}, cfg, log=log)
        if v is None and jc.get("required", True):
            log("   no verdict from the AI editor, leaving this one for a later run")
            sheet(src, f"NOJUDGE_{tag}.jpg")
            report(c, "no_verdict", metrics=m)
            return
        if v:
            log(f"   editor: {v['moment_score']}/10, gameplay {v['gameplay_visible']}, gambling ad {v['gambling_ad']}: "
                f"{v['what_happens']} | caption: {v['caption']} | tokens {v['_usage']}")
    ok = apply_verdict(cfg, state, c, v, dur)
    sheet(src, f"OK{c['score']}_{tag}.jpg" if ok else f"REJ_{tag}.jpg")


def pick(state, profile_of, fresh_only=False):
    """Best approved moment per profile: highest score, then the bigger stream, then the one waiting longest.

    Languages take turns: if the previous post was RU and an EN moment of about the same strength is
    waiting, the EN one goes next (and the other way round)."""
    last = {}
    for v in sorted(state["posted"].values(), key=lambda v: v["at"]):
        if v.get("ok") and v.get("group"):
            last[v.get("profile")] = v["group"]
    key = lambda c: (c["score"], c.get("viewers") or 0, age_hours(c["at"]))
    best = {}
    for prof in set(profile_of.values()):
        cands = [c for c in state["pool"].values()
                 if profile_of[c["group"]] == prof and not (fresh_only and c.get("previewed"))]
        if not cands:
            continue
        top = max(cands, key=key)
        other = [c for c in cands if c["group"] != last.get(prof) and c["score"] >= top["score"] - 1]
        best[prof] = max(other, key=key) if prof in last and other else top
    return best


def build_cover(cfg, c, src, cam):
    """Cover image for the clip and where TikTok finds it in the video (ms). (None, None) when covers are off."""
    if not (cfg.get("covers") or {}).get("enabled", True):
        return None, None
    try:
        from PIL import Image
        _, _, sdur = probe(src)
        k0, k1 = c.get("keep") or [0.0, sdur]
        k1 = min(float(k1), sdur)
        t = c.get("cover_at")
        t = peak_time(src, cam, k0, k1) if t is None else float(t)
        t = min(max(t, k0 + 0.2), k1 - 0.2)
        cover = make_cover(src, t, cam, c.get("hook"), f"@{c['login']}",
                           OUT_DIR / f"{c['login']}_{c['id'][-12:]}_cover.jpg")
        if cover.stat().st_size > 1_900_000:            # YouTube takes thumbnails up to 2 MB
            Image.open(cover).save(cover, quality=80)
        PREV_DIR.mkdir(parents=True, exist_ok=True)
        Image.open(cover).resize((540, 960)).save(PREV_DIR / f"COVER_{c['login']}_{c['id'][-12:]}.jpg", quality=85)
        log(f"   cover: frame at {t:.1f}s, headline: {c.get('hook') or '(none)'}")
        return cover, (200 if c.get("hook") else int((t - k0) * 1000))
    except Exception as e:  # noqa  (a cover is a bonus, never a reason to skip the post)
        log("   WARN cover failed:", repr(e)[:300])
        return None, None


def publish(cfg, state, c, dry):
    g = cfg["groups"][c["group"]]
    log(f"=> publishing [{c['kind']}] {c['login']} {c['score']}/10: {c.get('caption') or c['title']}")
    with tempfile.TemporaryDirectory() as td:
        src = download(c["url"], Path(td) / f"{c['id']}.mp4", c.get("section"))
        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / f"{c['login']}_{c['id'][-12:]}_vertical.mp4"
        ov = (cfg.get("cam_overrides") or {}).get(c["login"].lower())
        if ov and c.get("size") and c.get("cam_src") != "editor":   # the config knows this webcam better
            vw, vh = c["size"]
            c["cam"] = [int(ov[0] * vw), int(ov[1] * vh), int(ov[2] * vw), int(ov[3] * vh)]
        cam = tuple(c["cam"]) if c.get("cam") else None
        cover, cover_ms = build_cover(cfg, c, src, cam)
        # no_subs: the editor saw captions already burned into the source, ours would double them
        words = [] if c.get("no_subs") else (c.get("words") or [])
        # with a headline the cover is also the first half second of the video
        try:
            render(src, out, cam, f"@{c['login']}", words, c.get("keep"),
                   cover=cover if c.get("hook") else None)
        except subprocess.CalledProcessError as e:
            if not (cover and c.get("hook")):
                raise
            log("   WARN render with the cover failed, rendering without it:", repr(e)[:200])
            render(src, out, cam, f"@{c['login']}", words, c.get("keep"))
            cover_ms = None
        sheet(out, f"POST_{c['login']}_{c['id'][-12:]}.jpg", n=6, width=270)
        base, caption, yt_title, yt_desc, tags = texts(c, g)
        if dry:
            log("   DRY RUN, not posting:", out.name, "|", caption)
            c["previewed"] = True          # do not render the same preview again on the next dry run
            report(c, "would_post", caption=caption)
            return
        idem = c["id"] + (f"-r{c['attempt']}" if c.get("attempt") else "")    # a retry must not replay the refusal
        if c.get("platforms"):
            log("   retry for", ", ".join(c["platforms"]), "only: it is already out elsewhere")
        res = post(cfg, g["profile"], out, base, caption, yt_title, yt_desc, tags, idem, cover=cover,
                   cover_ms=cover_ms, platforms=c.get("platforms"))
        state["posted"][c["id"]] = {"at": now_utc().isoformat(), "ok": True, "profile": g["profile"],
                                    "login": c["login"], "group": c["group"], "url": c["url"], "kind": c["kind"],
                                    "score": c["score"],
                                    "caption": c.get("caption"), "vod": c.get("vod"), "vod_t": c.get("vod_t"),
                                    "hook": c.get("hook"), "request_id": res.get("request_id"),
                                    **({"earlier": c["earlier"]} if c.get("earlier") else {}),
                                    "cand": {k: x for k, x in c.items() if k != "previewed"}}
        state["pool"].pop(c["id"], None)
        report(c, "posted", caption=caption)


def publish_due(cfg, state, dry):
    """Apply fresh editor verdicts, then publish the best approved moment of every profile whose slot is open."""
    verdicts = load_verdicts() if state["pending"] or state["pool"] else None
    apply_reviews(cfg, state, verdicts)
    amend_pool(state, verdicts)
    save_state(state)
    profile_of = {gname: g["profile"] for gname, g in cfg["groups"].items()}
    for prof, c in pick(state, profile_of, fresh_only=dry).items():
        hold = (state.get("hold") or {}).get(prof)
        if hold and not dry:
            if dt.datetime.fromisoformat(hold["until"]) > now_utc():
                log(f"{prof}: on hold until {hold['until'][11:16]} UTC: {hold.get('why', '')[:120]}")
                continue
            state["hold"].pop(prof, None)
        recent, cap = recent_posts(state, prof), cfg["max_posts_per_day"]
        if len(recent) >= cap:
            free = recent[len(recent) - cap] + DAY
            log(f"{prof}: daily cap reached ({len(recent)} posts in the last 24 h), next slot {free.isoformat()[11:16]} UTC")
            continue
        gap = minutes_since_last_post(state, prof)
        if gap < cfg.get("min_gap_minutes", 120) and not dry:
            log(f"{prof}: last post {gap:.0f} min ago, next slot in {cfg.get('min_gap_minutes', 120) - gap:.0f} min")
            continue
        try:
            publish(cfg, state, c, dry)
        except Exception as e:  # noqa
            log("   ERROR while publishing:", repr(e)[:400])
            state["pool"].pop(c["id"], None)
            state["posted"][c["id"]] = {"at": now_utc().isoformat(), "ok": False, "error": repr(e)[:300],
                                        "login": c["login"]}
        save_state(state)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--game", default=None, help="override game from config (for tests)")
    ap.add_argument("--url", help="test: evaluate and render one Twitch clip URL (nothing is posted)")
    ap.add_argument("--streamer", default="streamer")
    ap.add_argument("--hook", default=None, help="test: headline for the cover")
    ap.add_argument("--cover-at", type=float, default=None, help="test: second of the clip for the cover frame")
    a = ap.parse_args()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if a.game:
        cfg["game"] = a.game
    import judge
    jc = cfg.get("judge") or {}

    if a.url:   # test mode: one clip through the whole pipeline, state untouched
        state = load_state()
        gname = next((n for n, g in cfg["groups"].items() if a.streamer.lower() in [x.lower() for x in g["streamers"]]),
                     next(iter(cfg["groups"])))
        c = {"id": "test-" + a.url.rstrip("/").split("/")[-1][-24:], "url": a.url, "section": None, "kind": "clip",
             "views": 0, "title": "", "vod": None, "vod_t": None, "group": gname, "login": a.streamer,
             "name": a.streamer, "source": "Twitch clip made by a viewer (manual test)"}
        test_extra = {k: v for k, v in (("hook", a.hook), ("cover_at", a.cover_at)) if v}
        with tempfile.TemporaryDirectory() as td:
            evaluate(cfg, state, c, td)
        c.update(test_extra)
        if c["id"] in state["pool"]:
            publish(cfg, state, c, dry=True)
        elif c["id"] in state["pending"]:   # review mode: show the layout without waiting for the verdict
            c.update({"score": 0, "keep": None})
            publish(cfg, state, c, dry=True)
        for f in REVIEW_DIR.glob("test-*"):  # the review branch is for real candidates only
            f.rename(PREV_DIR / ("REVIEW_" + f.name))
        return

    if not os.environ.get("TWITCH_CLIENT_ID") or not os.environ.get("TWITCH_CLIENT_SECRET"):
        log("::warning::TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET secrets are not set yet, skipping run")
        return
    dry = a.dry_run
    if not cfg.get("publish", True) and not dry:
        log("::warning::publish is off in config.yaml: moments are collected and rendered, nothing is posted")
        dry = True
    if not dry and not os.environ.get("UPLOAD_POST_KEY"):
        log("::warning::UPLOAD_POST_KEY secret is not set yet, skipping run")
        return
    mode = judge_mode(cfg)
    if mode == "api" and not judge.available(cfg):
        log("::warning::ANTHROPIC_API_KEY secret is not set: the AI editor is off"
            + (", so nothing will be approved" if jc.get("required", True) else ""))

    state = load_state()
    check_posts(state)
    publish_due(cfg, state, dry)   # a slot may already be open: do not make it wait for the listening
    new = []
    try:
        new += find_chat_moments(cfg, state)      # listens to live chats first (takes minutes)
    except Exception as e:  # noqa
        log("WARN chat moments failed:", repr(e))
    try:
        clips = find_candidates(cfg, state)
    except Exception as e:  # noqa
        log("WARN viewer clips failed:", repr(e))
        clips = []
    # chat moments first: a spike cannot be found again later, a viewer clip can wait for the next run
    # the editor's time is the scarce resource: stop feeding the queue when enough is already waiting,
    # but always look at the strongest fresh moment of the run
    waiting = len(state["pending"]) + len(state["pool"])
    limit = min(int(jc.get("max_checked_per_run", 8)), max(1, int(jc.get("queue_target", 10)) - waiting))
    order = new[:max(1, limit - 2)] + clips
    log(f"Waiting for the editor or a slot: {waiting}; checking up to {limit} new candidate(s)")
    checked = 0
    for c in order:
        if checked >= limit:
            break
        checked += 1
        try:
            with tempfile.TemporaryDirectory() as td:
                evaluate(cfg, state, c, td)
        except Exception as e:  # noqa
            log("   ERROR while checking:", repr(e)[:400])
        save_state(state)
    log(f"Checked {checked} candidate(s); waiting for the editor: {len(state['pending'])}; "
        f"approved and waiting for a slot: {len(state['pool'])}")
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEW_DIR / "pending.json").write_text(json.dumps(
        {"updated": now_utc().isoformat(),
         "pending": [{"id": k, "login": v["login"], "kind": v["kind"], "at": v["at"]}
                     for k, v in state["pending"].items()]}, indent=1))

    publish_due(cfg, state, dry)   # verdicts that arrived while this run was listening and checking
    save_state(state)


if __name__ == "__main__":
    main()
