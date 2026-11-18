"""AI editor: Claude looks at frames + transcript of a candidate clip and decides if it is worth posting.

Needs the ANTHROPIC_API_KEY secret. Returns None when the editor is unavailable
(no key, API error), so the caller can decide what to do without a verdict.
"""
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

SYSTEM = """You are the video editor of a short-form channel (TikTok and YouTube Shorts) that posts highlight \
moments from Twitch streams of GTA 6, laid out with the streamer's webcam reaction above the gameplay.

You receive one candidate clip: frames sampled across it with their timestamps, a speech transcript with \
timestamps, and where the candidate came from. Decide whether it is worth posting.

A clip is worth posting when someone scrolling a feed would stop for it: GTA 6 gameplay or an in-game cutscene \
is clearly on screen, and something specific happens in it, such as a crash, chase, shootout, glitch, fail, \
surprising event or discovery, or a strong genuine reaction from the streamer to what is on screen. \
Talking without gameplay, waiting, menus, loading screens, studio or IRL shots, sponsor reads and uneventful \
driving or walking are not worth posting, however popular the streamer is.

moment_score, 1 to 10: 1-3 nothing happens; 4-5 mildly interesting but would be scrolled past; \
6-7 a clear moment; 8-10 a standout moment people would share. Be strict: most candidates are 5 or lower. \
Nudity or sexual scenes (strip club dances, sex scenes) score 1-2: the platforms remove them.

keep_start and keep_end are seconds within the clip. Keep the build-up, the moment itself and the reaction, \
and cut dead time before and after. If the whole clip is needed, return 0 and the clip duration.

caption is the on-platform caption in the requested language: a short hook that says what happens, \
truthful, at most 80 characters, no hashtags, no streamer handle, at most one emoji."""

SCHEMA = {
    "type": "object",
    "properties": {
        "what_happens": {"type": "string", "description": "One sentence, in English, describing what is on screen and what happens."},
        "gameplay_visible": {"type": "boolean", "description": "GTA 6 gameplay or an in-game cutscene is on screen for most of the clip."},
        "webcam_visible": {"type": "boolean", "description": "The streamer's webcam overlay is visible in the frames."},
        "gambling_ad": {"type": "boolean", "description": "Betting or casino branding is visible in the frames."},
        "moment_score": {"type": "integer"},
        "keep_start": {"type": "number"},
        "keep_end": {"type": "number"},
        "caption": {"type": "string"},
    },
    "required": ["what_happens", "gameplay_visible", "webcam_visible", "gambling_ad", "moment_score",
                 "keep_start", "keep_end", "caption"],
    "additionalProperties": False,
}


def _frames(path, dur, n):
    td = Path(tempfile.mkdtemp())
    out = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        f = td / f"{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=768:-2", "-q:v", "4", str(f)], check=True)
        out.append((t, base64.standard_b64encode(f.read_bytes()).decode()))
    return out


def transcript(words):
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["start"]
        cur.append(w["word"].strip())
        if w["end"] - t0 > 4 or len(cur) >= 14:
            lines.append(f"[{t0:.0f}s] " + " ".join(cur))
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.0f}s] " + " ".join(cur))
    return "\n".join(lines) or "(no speech recognised)"


def available(cfg):
    return bool((cfg.get("judge") or {}).get("enabled", True) and os.environ.get("ANTHROPIC_API_KEY"))


def judge(path, dur, words, meta, cfg, log=print):
    """Return the editor's verdict dict, or None if the editor could not be asked."""
    if not available(cfg):
        return None
    import anthropic
    jc = cfg.get("judge") or {}
    content = [{"type": "text", "text":
                f"Streamer: {meta['name']} (twitch.tv/{meta['login']}). Caption language: {meta['language']}.\n"
                f"Source: {meta['source']}.\nClip duration: {dur:.1f} s.\n\nTranscript:\n{transcript(words)}\n\nFrames:"}]
    for t, b64 in _frames(path, dur, jc.get("frames", 10)):
        content.append({"type": "text", "text": f"t = {t:.1f} s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    kwargs = dict(model=jc.get("model", "claude-opus-5-5"), max_tokens=8000, system=SYSTEM,
                  messages=[{"role": "user", "content": content}],
                  output_config={"effort": jc.get("effort", "low"), "format": {"type": "json_schema", "schema": SCHEMA}})
    client = anthropic.Anthropic()
    try:
        try:
            # a policy decline is re-run on a fallback model inside the same call
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except anthropic.BadRequestError as e:
            log("  editor: retrying without fallbacks:", str(e)[:200])
            resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        log("::warning::AI editor: ANTHROPIC_API_KEY is invalid")
        return None
    except anthropic.RateLimitError:
        log("::warning::AI editor: rate limited, skipping this candidate for now")
        return None
    except anthropic.APIStatusError as e:
        log(f"::warning::AI editor: API error {e.status_code}: {str(e)[:200]}")
        return None
    except anthropic.APIConnectionError:
        log("::warning::AI editor: network error")
        return None
    if resp.stop_reason != "end_turn":
        log("  editor: no verdict, stop_reason =", resp.stop_reason)
        return None
    try:
        v = json.loads(next(b.text for b in resp.content if b.type == "text"))
    except (StopIteration, ValueError) as e:
        log("  editor: unreadable verdict:", repr(e))
        return None
    v["_usage"] = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
    return v
