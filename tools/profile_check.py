"""Which platforms have an account connected to an upload-post profile right now.

Run by the `profile check` workflow (manual). It asks the same question the pipeline asks before a post from
the shelf, at any time instead of only in a posting slot. The answer of upload-post carries account names and
an e-mail, and the logs of this repository are public: only platform names and yes/no go to the output.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

WANTED = ("tiktok", "youtube")


def main():
    profile = os.environ.get("PROFILE", "")
    key = os.environ.get("UPLOAD_POST_KEY")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile):
        print("bad profile name")
        return 2
    if not key:
        print("no UPLOAD_POST_KEY")
        return 2
    req = urllib.request.Request(f"https://api.upload-post.com/api/uploadposts/users/{profile}",
                                 headers={"Authorization": "Apikey " + key, "User-Agent": "clips-pipeline"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"{profile}: the profile does not exist in upload-post yet")
            return 0
        print(f"{profile}: upload-post answered {e.code}")
        return 1
    except Exception as e:  # noqa
        print(f"{profile}: could not ask upload-post: {type(e).__name__}")
        return 1
    acc = (data.get("profile") or {}).get("social_accounts") or {}
    acc = {str(p).lower(): v for p, v in acc.items()}
    print(f"{profile}: the profile exists")
    for p in sorted(set(WANTED) | {p for p, v in acc.items() if v}):
        v = acc.get(p)
        if not (isinstance(v, dict) and v):
            state = "not connected"
        elif v.get("reauth_required"):
            state = "connected, but upload-post asks to sign in again"
        else:
            state = "connected"
        print(f"{profile}: {p}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
