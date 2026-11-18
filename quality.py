"""Cheap quality signals for a candidate clip: is there gameplay, does anything happen.

No ML here, just motion outside the webcam box and loudness dynamics. The
optional LLM editor (judge.py) makes the final call when an API key is set.
"""
import subprocess

import numpy as np

SW, SH, FPS = 160, 90, 4


def _frames(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={FPS},scale={SW}:{SH},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (SW * SH)
    return np.frombuffer(raw[:n * SW * SH], np.uint8).reshape(n, SH, SW).astype(np.int16)


def motion(path, cam, vw, vh):
    """Share of the non-webcam picture that changes between frames (0..1): median and p90."""
    f = _frames(path)
    if len(f) < 3:
        return {"motion": 0.0, "motion_p90": 0.0, "static_share": 1.0}
    mask = np.ones((SH, SW), bool)
    if cam:
        x, y, w, h = cam
        mask[int(y / vh * SH):int((y + h) / vh * SH) + 1, int(x / vw * SW):int((x + w) / vw * SW) + 1] = False
    d = (np.abs(f[1:] - f[:-1]) > 10)[:, mask].mean(axis=1)
    return {"motion": float(np.median(d)), "motion_p90": float(np.percentile(d, 90)),
            "static_share": float((d < 0.02).mean())}


def loudness(pcm):
    """Loudness dynamics from 16 kHz float PCM: how far the peaks rise above the usual level (dB)."""
    win = 4000  # 0.25 s
    n = len(pcm) // win
    if n < 8:
        return {"peak_db": 0.0, "level_db": -90.0}
    rms = np.sqrt((pcm[:n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-10)
    db = 20 * np.log10(rms)
    return {"peak_db": float(np.percentile(db, 95) - np.median(db)), "level_db": float(np.median(db))}


def verdict(m, cam, fullcam, cfg, cam_required=True):
    """Return (ok, reason). Thresholds live in config.yaml -> quality."""
    q = cfg.get("quality") or {}
    if fullcam:
        return False, "streamer fills the screen, no gameplay"
    if cam_required and q.get("require_cam", True) and not cam:
        return False, "no webcam found"
    if m["motion"] < q.get("min_motion", 0.08):
        return False, f"picture is almost static (motion {m['motion']:.2f})"
    if m["static_share"] > q.get("max_static_share", 0.5):
        return False, f"static for {m['static_share']:.0%} of the clip"
    return True, "ok"
