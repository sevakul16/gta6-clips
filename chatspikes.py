"""Find hype moments by chat-activity spikes on live Twitch streams.

Connects anonymously to Twitch IRC, counts messages per channel, and flags
moments where chat speed jumps well above its recent baseline. Each moment is
mapped to an offset inside the stream's in-progress VOD so it can be cut.
"""
import datetime as dt
import socket
import ssl
import statistics
import time
from collections import defaultdict, deque

BUCKET = 5  # seconds


def listen(channels, seconds, log=print, warmup=150, window=3, ratio=3.0, min_msgs=25, cooldown=90):
    """Return list of spikes: {login, at (UTC datetime), msgs, base, score}."""
    if not channels:
        return []
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10),
                        server_hostname="irc.chat.twitch.tv")
    s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan%d\r\n" % (int(time.time()) % 100000))
    s.sendall(("JOIN " + ",".join("#" + c.lower() for c in channels) + "\r\n").encode())
    s.settimeout(1.0)
    start = time.time()
    counts = defaultdict(lambda: defaultdict(int))   # login -> bucket index -> msgs
    hist = defaultdict(lambda: deque(maxlen=60))      # closed bucket totals (5 min)
    last_spike = defaultdict(float)
    spikes = []
    buf = b""
    cur = 0
    while time.time() - start < seconds:
        try:
            data = s.recv(65536)
            if not data:
                break
            buf += data
        except socket.timeout:
            pass
        *lines, buf = buf.split(b"\r\n")
        now = time.time()
        b = int((now - start) // BUCKET)
        for ln in lines:
            if ln.startswith(b"PING"):
                s.sendall(b"PONG :tmi.twitch.tv\r\n")
            elif b" PRIVMSG #" in ln:
                ch = ln.split(b" PRIVMSG #", 1)[1].split(b" ", 1)[0].decode(errors="ignore")
                counts[ch][b] += 1
        if b != cur:  # bucket closed -> evaluate
            for ch in [c.lower() for c in channels]:
                c = counts[ch]
                recent = sum(c.get(i, 0) for i in range(cur - window + 1, cur + 1))
                if now - start > warmup and len(hist[ch]) >= 12:
                    base = statistics.median(hist[ch]) * window
                    if recent >= min_msgs and recent >= ratio * max(base, 1) and now - last_spike[ch] > cooldown:
                        last_spike[ch] = now
                        at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=BUCKET * window / 2)
                        spikes.append({"login": ch, "at": at, "msgs": recent, "base": base,
                                       "score": recent / max(base, 1)})
                        log(f"  chat spike {ch}: {recent} msgs/{BUCKET * window}s vs base {base:.0f}")
                hist[ch].append(c.get(cur, 0))
            cur = b
    s.close()
    return spikes


def live_targets(tw, logins, game_id):
    """Live streams of these logins currently in the target game."""
    out = {}
    for i in range(0, len(logins), 100):
        for st in tw.get("streams", [("user_login", l) for l in logins[i:i + 100]] + [("first", "100")]):
            if st["game_id"] == game_id:
                out[st["user_login"].lower()] = st
    return out


def vod_for(tw, stream):
    """In-progress VOD of a live stream (archive whose start matches the stream)."""
    vids = tw.get("videos", {"user_id": stream["user_id"], "type": "archive", "first": 1})
    if not vids:
        return None
    v = vids[0]
    st = dt.datetime.fromisoformat(stream["started_at"].replace("Z", "+00:00"))
    vt = dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
    if abs((vt - st).total_seconds()) > 600:
        return None
    return v
