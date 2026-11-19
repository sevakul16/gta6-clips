"""Editor's side of judge.mode: review.

  python tools/editor.py pending DIR        fetch the review branch into DIR, list candidates without a verdict
  python tools/editor.py set ID --score 7 --what "..." --caption "..." [--keep 4 31] [--no-gameplay]
                                [--gambling] [--cam 0.0 0.6 0.25 0.4] [--hook "2-4 WORDS"] [--cover-at 18.5]
                                [--no-subs]
  python tools/editor.py amend ID [--hook "..."] [--cover-at 18.5] [--caption "..."] [--no-subs]   change a verdict
  python tools/editor.py push               commit state/verdicts.json and push it to main

Rubric (same as judge.py): worth posting only when GTA 6 gameplay or an in-game cutscene is clearly on
screen and something specific happens (crash, chase, shootout, glitch, fail, surprise, discovery, or a
strong genuine reaction to what is on screen). 1-3 nothing happens, 4-5 would be scrolled past,
6-7 a clear moment, 8-10 a standout. Be strict. Nudity or sexual scenes (strip club, sex scenes): 1-2,
the platforms remove them. Caption: short truthful hook in the streamer's language,
at most 80 characters, no hashtags, no handle, at most one emoji.

Every approved clip also gets a cover: --hook is its headline (2-4 words, at most ~24 characters, the
streamer's language, no emoji, catchy but true to what happens) and --cover-at is the second of the clip
(as in frame_times) with the strongest picture: the crash, the arrest, the punch, the face. It must lie
inside the kept part. Without --cover-at the frame with the most motion is taken.

--no-subs: the frames already show captions burned into the source (some Twitch clips carry them, white
text with a purple marker near the bottom); our subtitles are then left out so the text is not doubled.
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERDICTS = ROOT / "state" / "verdicts.json"


def load():
    return json.loads(VERDICTS.read_text()) if VERDICTS.exists() else {}


def git(*a, check=True):
    return subprocess.run(["git", *a], cwd=ROOT, check=check, capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("dir")
    s = sub.add_parser("set")
    s.add_argument("id")
    s.add_argument("--score", type=int, required=True)
    s.add_argument("--what", required=True)
    s.add_argument("--caption", default="")
    s.add_argument("--keep", type=float, nargs=2)
    s.add_argument("--no-gameplay", action="store_true")
    s.add_argument("--gambling", action="store_true")
    s.add_argument("--cam", type=float, nargs=4, help="webcam box as fractions of the frame: x y w h")
    s.add_argument("--hook", help="cover headline, 2-4 words")
    s.add_argument("--cover-at", type=float, help="second of the clip for the cover frame")
    s.add_argument("--no-subs", action="store_true", help="source already has burned-in captions")
    m = sub.add_parser("amend")
    m.add_argument("id")
    m.add_argument("--hook")
    m.add_argument("--cover-at", type=float)
    m.add_argument("--caption")
    m.add_argument("--no-subs", action="store_true")
    sub.add_parser("push")
    a = ap.parse_args()

    if a.cmd == "pending":
        d = Path(a.dir)
        d.mkdir(parents=True, exist_ok=True)
        git("fetch", "-q", "--force", "--depth", "1", "origin", "+refs/heads/review:refs/remotes/origin/review")
        tar = subprocess.run(["git", "archive", "origin/review"], cwd=ROOT, check=True, capture_output=True).stdout
        subprocess.run(["tar", "x", "-C", str(d)], input=tar, check=True)
        git("pull", "-q", "--rebase", check=False)
        done = load()
        plist = json.loads((d / "pending.json").read_text())
        todo = [i for i in plist["pending"] if i["id"] not in done]
        print(f"queue updated {plist['updated']}; waiting {len(plist['pending'])}, without a verdict {len(todo)}")
        for i in todo:
            print(f"{i['id']}\t{i['login']}\t{i['kind']}\t{d / (i['id'] + '.jpg')}")
    elif a.cmd == "set":
        v = load()
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        v = {k: x for k, x in v.items() if x.get("at", "") > cutoff}
        v[a.id] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "moment_score": a.score,
                   "what_happens": a.what, "gameplay_visible": not a.no_gameplay, "gambling_ad": a.gambling,
                   "caption": a.caption}
        if a.keep:
            v[a.id].update(keep_start=a.keep[0], keep_end=a.keep[1])
        if a.cam:
            v[a.id]["cam"] = a.cam
        if a.hook:
            v[a.id]["hook"] = a.hook
        if a.cover_at is not None:
            v[a.id]["cover_at"] = a.cover_at
        if a.no_subs:
            v[a.id]["no_subs"] = True
        VERDICTS.write_text(json.dumps(v, ensure_ascii=False, indent=1))
        print("saved", a.id, v[a.id])
    elif a.cmd == "amend":
        v = load()
        if a.id not in v:
            sys.exit(f"no verdict for {a.id}")
        for k, val in (("hook", a.hook), ("cover_at", a.cover_at), ("caption", a.caption)):
            if val is not None:
                v[a.id][k] = val
        if a.no_subs:
            v[a.id]["no_subs"] = True
        VERDICTS.write_text(json.dumps(v, ensure_ascii=False, indent=1))
        print("amended", a.id, v[a.id])
    elif a.cmd == "push":
        git("add", "state/verdicts.json")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("nothing new")
            return
        git("commit", "-qm", "editor: verdicts")
        for _ in range(4):
            git("pull", "-q", "--rebase", check=False)
            if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
                print("pushed")
                return
        sys.exit("push failed")


if __name__ == "__main__":
    main()
