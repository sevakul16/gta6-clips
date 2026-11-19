"""Push a run's review material to side branches so it can be read without downloading artifacts.

  review   - frames + transcript of every candidate that waits for the editor's verdict
  previews - contact sheets and report.json of the latest run
  posted   - covers and contact sheets of published clips, the newest 40 files, kept across runs

Each branch is a single commit that is replaced on every run, so the repository does not grow.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{os.environ['GITHUB_REPOSITORY']}.git"


def git(*a, cwd, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"git {a[0]} failed: {r.stderr.replace(os.environ['GH_TOKEN'], '***')[:300]}")
    return r


def push(src, branch):
    with tempfile.TemporaryDirectory() as td:
        shutil.copytree(src, td, dirs_exist_ok=True)
        git("init", "-q", "-b", branch, cwd=td)
        git("add", "-A", cwd=td)
        git("-c", "user.name=clips-bot", "-c", "user.email=clips-bot@users.noreply.github.com",
            "commit", "-qm", f"{branch} from run {os.environ.get('GITHUB_RUN_ID', '?')}", cwd=td)
        for attempt in (1, 2, 3):       # GitHub now and then answers "remote rejected ... (failed)": try again
            r = git("push", "-qf", URL, f"{branch}:{branch}", cwd=td, check=attempt == 3)
            if r.returncode == 0:
                break
            print(f"WARN push to {branch} failed (attempt {attempt}), retrying")
            time.sleep(10 * attempt)
    print(f"pushed {len(list(Path(src).iterdir()))} file(s) to branch {branch}")


def keep_posted(prev):
    """Covers and sheets of published clips survive later runs on the `posted` branch (newest 40 files)."""
    new = sorted(p for p in Path(prev).glob("*.jpg") if p.name.startswith(("POST_", "COVER_")))
    if not new:
        return
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
    with tempfile.TemporaryDirectory() as keep, tempfile.TemporaryDirectory() as old:
        if git("clone", "-q", "--depth", "1", "--branch", "posted", URL, old, cwd=ROOT, check=False).returncode == 0:
            for p in Path(old).glob("*.jpg"):
                shutil.copy(p, Path(keep) / p.name)
        for p in new:
            shutil.copy(p, Path(keep) / f"{stamp}_{p.name}")
        for p in sorted(Path(keep).glob("*.jpg"))[:-40]:
            p.unlink()
        push(keep, "posted")


def main():
    review = ROOT / "out" / "review"
    plist = review / "pending.json"
    if plist.exists():
        # packages of candidates that are still waiting were made by earlier runs: carry them over
        with tempfile.TemporaryDirectory() as old:
            got = git("clone", "-q", "--depth", "1", "--branch", "review", URL, old, cwd=ROOT, check=False).returncode == 0
            for item in json.loads(plist.read_text())["pending"]:
                for ext in ("jpg", "json"):
                    name = f"{item['id']}.{ext}"
                    if got and not (review / name).exists() and (Path(old) / name).exists():
                        shutil.copy(Path(old) / name, review / name)
        push(review, "review")
    prev = ROOT / "out" / "previews"
    if prev.exists() and any(prev.iterdir()):
        for name, step in (("previews", lambda: push(prev, "previews")), ("posted", lambda: keep_posted(prev))):
            try:
                step()
            except (Exception, SystemExit) as e:  # noqa: BLE001 - these sheets are for looking back: never fail the run
                print(f"WARN {name} branch not updated:", str(e)[:200])


if __name__ == "__main__":
    main()
