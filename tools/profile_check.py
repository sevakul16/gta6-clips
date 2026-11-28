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


def accounts(profile, key):
    """The social accounts of a profile as {platform: dict or ''}; None when the profile does not exist."""
    req = urllib.request.Request(f"https://api.upload-post.com/api/uploadposts/users/{profile}",
                                 headers={"Authorization": "Apikey " + key, "User-Agent": "clips-pipeline"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    acc = (data.get("profile") or {}).get("social_accounts") or {}
    return {str(p).lower(): v for p, v in acc.items()}


def who(v):
    """What tells one account from another, never printed: the handle or id if there is one, else the name."""
    if not isinstance(v, dict):
        return None
    for k in ("handle", "username", "id", "user_id", "channel_id", "display_name", "name"):
        if v.get(k):
            return k, str(v[k]).strip().lower()
    return None


def main():
    profile = os.environ.get("PROFILE", "")
    other = os.environ.get("COMPARE_WITH", "")
    key = os.environ.get("UPLOAD_POST_KEY")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", profile) or not re.fullmatch(r"[A-Za-z0-9_-]{0,40}", other):
        print("bad profile name")
        return 2
    if not key:
        print("no UPLOAD_POST_KEY")
        return 2
    try:
        acc = accounts(profile, key)
        theirs = accounts(other, key) if other and other != profile else None
    except urllib.error.HTTPError as e:
        print(f"{profile}: upload-post answered {e.code}")
        return 1
    except Exception as e:  # noqa
        print(f"{profile}: could not ask upload-post: {type(e).__name__}")
        return 1
    if acc is None:
        print(f"{profile}: the profile does not exist in upload-post yet")
        return 0
    print(f"{profile}: the profile exists")
    for p in sorted(set(WANTED) | {p for p, v in acc.items() if v}):
        v = acc.get(p)
        if not (isinstance(v, dict) and v):
            state = "not connected"
        elif v.get("reauth_required"):
            state = "connected, but upload-post asks to sign in again"
        else:
            state = "connected"
        # the mistake to catch: the account of the other channel connected to this profile by a wrong sign-in
        if state != "not connected" and theirs is not None:
            a, b = who(v), who(theirs.get(p))
            if a is None or b is None or a[0] != b[0]:
                state += f"; same account as in {other}: cannot tell"
            elif a[0] in ("display_name", "name"):     # two channels can carry one name: weaker evidence
                state += f"; same display name as in {other}: {'yes' if a[1] == b[1] else 'no'}"
            else:
                state += f"; same account as in {other}: {'YES' if a[1] == b[1] else 'no'}"
        print(f"{profile}: {p}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
