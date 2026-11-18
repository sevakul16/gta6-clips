"""Find hype moments by chat-activity spikes on live Twitch streams.

Connects anonymously to Twitch IRC, counts chatters per channel in 5-second
buckets, and flags moments where chat speed jumps well above its recent
baseline. Each moment is mapped to an offset inside the stream's in-progress
VOD so it can be cut.
"""
import datetime as dt
import re
import socket
import ssl
import statistics
import time
from collections import Counter, defaultdict, deque

BUCKET = 5  # seconds


class Detector:
    """Spike detector for one channel. Feed closed bucket counts in order."""

    def __init__(self, window=3, ratio=2.5, min_msgs=20, z=6.0, cooldown=90, warm=24, history=72):
        self.window, self.ratio, self.min_msgs, self.z = window, ratio, min_msgs, z
        self.cooldown_buckets = cooldown // BUCKET
        self.warm = warm
        self.counts = deque(maxlen=history)      # bucket counts, newest last
        self.sums = deque(maxlen=history)        # rolling window sums
        self.since_spike = 10 ** 6
        self.max_ratio = 0.0

    def push(self, count):
        """Add one closed bucket. Returns a spike dict or None."""
        self.counts.append(count)
        recent = sum(list(self.counts)[-self.window:])
        base_sums = list(self.sums)[:-self.window] if len(self.sums) > self.window else []
        self.sums.append(recent)
        self.since_spike += 1
        if len(base_sums) < self.warm:
            return None
        base = statistics.median(base_sums)
        mad = statistics.median(abs(s - base) for s in base_sums) * 1.4826
        ratio = recent / max(base, 1.0)
        zscore = (recent - base) / max(mad, 1.0)
        self.max_ratio = max(self.max_ratio, ratio)
        # small chats need a big relative jump; huge chats never triple, so a strong
        # deviation from their own noise level (z-score) with a 1.6x jump also counts
        hit = recent >= self.min_msgs and (ratio >= self.ratio or (ratio >= 1.6 and zscore >= self.z))
        if hit and self.since_spike > self.cooldown_buckets:
            self.since_spike = 0
            return {"msgs": recent, "base": base, "score": ratio, "z": zscore}
        return None


def _tag(tags, name):
    m = re.search(rb"(?:^|;)" + name + rb"=([^; ]*)", tags)
    return m.group(1) if m else b""


def chat_sample(msgs, top=12, extra=8):
    """What the chat was saying: most repeated messages first, then a few others. Text only, no nicknames."""
    norm = []
    for t in msgs:
        words, out = t.split(), []
        for w in words:                      # "KEKW KEKW KEKW" -> "KEKW"
            if not out or out[-1].lower() != w.lower():
                out.append(w)
        norm.append(" ".join(out)[:80])
    cnt = Counter(m.lower() for m in norm if m)
    first = {}
    for m in norm:
        first.setdefault(m.lower(), m)
    common = [k for k, n in cnt.most_common(top) if n > 1]
    rest = [k for k in cnt if k not in common][-extra:]
    return [f"{cnt[k]}x {first[k]}" for k in common] + [first[k] for k in rest]


def listen(channels, seconds, log=print, **params):
    """Listen to chats. Returns (spikes, series): spikes are {login, at, msgs, base, score, z, chat};
    series is {login: [chatters per 5 s bucket]} for calibration.

    Spikes right after a wave of gifted subs or a raid are dropped: the chat reacts to that, not to the game."""
    channels = [c.lower() for c in channels]
    if not channels:
        return [], {}
    ctx = ssl.create_default_context()
    s = ctx.wrap_socket(socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10),
                        server_hostname="irc.chat.twitch.tv")
    s.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")       # tags: gifted subs and raids arrive as USERNOTICE
    s.sendall(b"PASS SCHMOOPIIE\r\nNICK justinfan%d\r\n" % (int(time.time()) % 100000))
    for i in range(0, len(channels), 15):       # Twitch allows 20 joins per 10 seconds
        if i:
            time.sleep(11)
        s.sendall(("JOIN " + ",".join("#" + c for c in channels[i:i + 15]) + "\r\n").encode())
    s.settimeout(1.0)
    start = time.time()
    seen = defaultdict(set)                       # chatters in the open bucket
    det = {c: Detector(**params) for c in channels}
    series = {c: [] for c in channels}
    recent = defaultdict(lambda: deque(maxlen=800))   # (time, text) of the latest messages
    noise = defaultdict(lambda: deque(maxlen=200))    # (time, weight) of gifted subs / raids
    spikes = []
    buf = b""
    cur = 0
    while time.time() - start < seconds:
        try:
            data = s.recv(65536)
            if not data:
                log("  chat: connection closed by Twitch")
                break
            buf += data
        except socket.timeout:
            pass
        except OSError as e:
            log("  chat: socket error", repr(e))
            break
        *lines, buf = buf.split(b"\r\n")
        for ln in lines:
            if ln.startswith(b"PING"):
                s.sendall(b"PONG :tmi.twitch.tv\r\n")
            elif b" USERNOTICE #" in ln:
                tags, rest = ln.split(b" USERNOTICE #", 1)
                ch = rest.split(b" ", 1)[0].decode(errors="ignore")
                kind = _tag(tags, b"msg-id")
                if kind in (b"subgift", b"anonsubgift"):
                    noise[ch].append((time.time(), 1))
                elif kind in (b"submysterygift", b"anonsubmysterygift"):
                    noise[ch].append((time.time(), int(_tag(tags, b"msg-param-mass-gift-count") or 1)))
                elif kind == b"raid":
                    noise[ch].append((time.time(), 100))
            elif b" PRIVMSG #" in ln:
                head, rest = ln.split(b" PRIVMSG #", 1)
                ch, _, msg = rest.partition(b" :")
                if msg.startswith(b"!"):          # bot commands, giveaways
                    continue
                nick = head.rsplit(b" ", 1)[-1][1:].split(b"!", 1)[0]
                ch = ch.decode(errors="ignore")
                seen[ch].add(nick)                # one vote per chatter per bucket
                recent[ch].append((time.time(), msg.decode(errors="ignore")))
        now = time.time()
        b = int((now - start) // BUCKET)
        while cur < b:                             # close finished buckets
            for c in channels:
                n = len(seen[c])
                series[c].append(n)
                sp = det[c].push(n)
                if sp and sum(w for t, w in noise[c] if now - t < 45) >= 5:
                    log(f"  chat spike {c} ignored: gifted subs or a raid just before it")
                    sp = None
                if sp:
                    sp["login"] = c
                    sp["chat"] = chat_sample([m for t, m in recent[c] if now - t < BUCKET * (det[c].window + 1)])
                    sp["at"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=BUCKET * det[c].window / 2)
                    spikes.append(sp)
                    log(f"  chat spike {c}: {sp['msgs']} chatters/{BUCKET * det[c].window}s vs usual {sp['base']:.0f} "
                        f"(x{sp['score']:.1f}, z {sp['z']:.1f})")
            seen.clear()
            cur += 1
    try:
        s.close()
    except OSError:
        pass
    for c in channels:
        if series[c]:
            log(f"  chat {c}: {sum(series[c])} msgs, median {statistics.median(series[c]):.0f}/5s, "
                f"max jump x{det[c].max_ratio:.1f}")
    return spikes, series


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
