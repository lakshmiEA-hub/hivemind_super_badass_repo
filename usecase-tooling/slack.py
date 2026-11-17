"""Pull AI use cases out of Slack.

Searches rather than crawls. Paging every channel's history costs thousands of
calls and most of it is lunch plans; `search.messages` asks Slack to find the
AI-related messages directly, and it searches exactly what the signed-in person
can already see, which is the access model we want anyway. One query per search
term, because Slack's search does not usefully support OR.

Every record carries the visibility of the conversation it came from, and that
field is the whole safety story downstream. A public channel message can be
quoted to colleagues. A DM cannot, whatever it says, because the other person
in it never agreed to appear in a tracker. publish.py enforces that; this
module's job is to record visibility accurately and never guess it.

Needs a user token (xoxp-), not a bot token: `search.messages` is user-token
only. Set SLACK_USER_TOKEN, or put it in usecase-tooling/slack-token.txt, which
.gitignore blocks. Scopes: search:read, users:read, users:read.email,
channels:history, groups:history, im:history, mpim:history.
"""

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import aiuse

API = "https://slack.com/api"

# One query per term. Named products first: they are the terms that find real
# use cases rather than discussion, and Slack ranks exact matches well.
QUERIES = list(aiuse.TOOLS) + [
    "AI", "LLM", "prompt", "generative AI", "automated with",
    "asked it to", "drafted with", "summarised by", "summarized by",
]


def token_from_env():
    """The user token, from the environment or the git-ignored file beside this."""
    tok = (os.environ.get("SLACK_USER_TOKEN") or "").strip()
    if tok:
        return tok
    f = Path(__file__).resolve().parent / "slack-token.txt"
    if f.is_file():
        tok = f.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    raise RuntimeError(
        "No Slack token. Set SLACK_USER_TOKEN, or save the token to "
        f"{f}. It must be a user token (xoxp-), not a bot token: "
        "search.messages does not accept bot tokens. See README.md.")


def _api(method, token, **params):
    """One Slack call, waiting out the rate limiter rather than failing.

    Slack's search tier allows roughly 20 requests a minute and answers an
    overage with 429 and a Retry-After header. Honouring it is the difference
    between a scan that finishes and one that dies two thirds of the way in.
    """
    url = f"{API}/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 5:
                time.sleep(int(e.headers.get("Retry-After", "5")) + 1)
                continue
            raise RuntimeError(f"Slack {method} failed: HTTP {e.code}") from None
        if data.get("ok"):
            return data
        err = data.get("error", "unknown")
        if err == "ratelimited" and attempt < 5:
            time.sleep(5)
            continue
        # the token problems are worth naming, because the fix differs for each
        if err in ("not_allowed_token_type", "invalid_auth", "missing_scope"):
            raise RuntimeError(
                f"Slack rejected the token on {method}: {err}. "
                "search.messages needs a USER token (xoxp-) carrying search:read.")
        raise RuntimeError(f"Slack {method} failed: {err}")
    raise RuntimeError(f"Slack {method} kept rate limiting")


_USERS = {}


def user_info(token, user_id):
    """Display name and email for a user id, cached: ids repeat constantly."""
    if not user_id:
        return {"name": "", "email": ""}
    if user_id not in _USERS:
        try:
            p = _api("users.info", token, user=user_id)["user"]
            prof = p.get("profile") or {}
            _USERS[user_id] = {"name": p.get("real_name") or p.get("name") or user_id,
                               "email": (prof.get("email") or "").lower()}
        except RuntimeError:
            # a deactivated or external user still produced the message, so
            # record the id rather than dropping a real use case
            _USERS[user_id] = {"name": user_id, "email": ""}
    return _USERS[user_id]


def visibility_of(channel):
    """public | private | dm, from the channel object search returns.

    Deliberately pessimistic: anything that is not demonstrably a public
    channel is treated as private, because the cost of being wrong here is
    republishing somebody's DM.
    """
    if not isinstance(channel, dict):
        return "private"
    name = channel.get("name") or ""
    if channel.get("is_im") or channel.get("is_mpim"):
        return "dm"
    # search sometimes returns only an id, and DM ids start with D or G
    if not name and str(channel.get("id", "")).startswith(("D", "G")):
        return "dm"
    if channel.get("is_private"):
        return "private"
    if channel.get("is_channel") or channel.get("is_group") is False:
        return "public"
    return "private"


def search(token, query, pages=2, per_page=100):
    """Matching messages for one query, newest first."""
    out = []
    for page in range(1, pages + 1):
        data = _api("search.messages", token, query=query, count=per_page,
                    page=page, sort="timestamp")
        matches = (data.get("messages") or {}).get("matches") or []
        out.extend(matches)
        paging = (data.get("messages") or {}).get("paging") or {}
        if page >= (paging.get("pages") or 1):
            break
    return out


def to_record(token, m):
    """One search hit, normalised and classified. None when it is not a use case."""
    text = m.get("text") or ""
    channel = m.get("channel") or {}
    who = user_info(token, m.get("user") or "")
    rec = aiuse.classify(text, sender=who["email"])
    if not rec["is_use_case"]:
        return None

    ts = m.get("ts") or "0"
    try:
        when = datetime.fromtimestamp(float(ts), timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        when = ""

    vis = visibility_of(channel)
    return {
        "source": "slack",
        "visibility": vis,
        "channel": channel.get("name") or ("direct message" if vis == "dm" else ""),
        "person": who["name"],
        "email": who["email"],
        "date": when,
        "permalink": m.get("permalink") or "",
        "text": text,
        "tools": rec["tools"],
        "tasks": rec["tasks"],
        "evidence": rec["evidence"],
        "score": rec["score"],
        "why": rec["why"],
    }


def collect(token, queries=None, pages=2, verbose=True):
    """Every AI use case this token can see, deduplicated by permalink."""
    seen, out = set(), []
    for q in (queries or QUERIES):
        try:
            matches = search(token, q, pages=pages)
        except RuntimeError as e:
            if verbose:
                print(f"  {q}: {e}")
            continue
        kept = 0
        for m in matches:
            key = m.get("permalink") or (str(m.get("ts")) + str(m.get("channel")))
            if key in seen:
                continue
            seen.add(key)
            rec = to_record(token, m)
            if rec:
                out.append(rec)
                kept += 1
        if verbose:
            print(f"  {q}: {len(matches)} matches, {kept} use cases")
    return out


def slugify(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()
    return s[:60] or "untitled"


def write_notes(records, vault):
    """One note per use case, under `slack/` in the vault.

    Private and DM material is written with `visibility: private` so that the
    existing export blocklist catches it even if someone later copies these
    notes somewhere they do not belong.
    """
    base = Path(vault) / "slack"
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for r in records:
        vis = "public" if r["visibility"] == "public" else "private"
        uid = slugify(f"{r['date']}-{r['person']}-{'-'.join(r['tools'][:2])}")
        title = f"{r['person'] or 'Unknown'} - {', '.join(r['tools']) or 'AI'}"
        fm = [
            "---",
            "type: ai-use-case",
            f"title: {title}",
            f"uid: {uid}",
            f"source: slack",
            f"channel: {r['channel']}",
            f"conversation_visibility: {r['visibility']}",
            f"visibility: {vis}",
            f"person: {r['person']}",
            f"person_email: {r['email']}",
            f"date: {r['date']}",
            f"tools: [{', '.join(r['tools'])}]",
            f"tasks: [{', '.join(r['tasks'])}]",
            f"permalink: {r['permalink']}",
            f"score: {r['score']}",
            "status: clean",
            "tags: [ai-use-case, slack]",
            "---",
            "",
        ]
        body = []
        if vis == "private":
            body.append("> [!warning] Private. From a DM or private channel, "
                        "excluded from every shared export.")
            body.append("")
        body.append(f"{r['person'] or 'Someone'} used "
                    f"{', '.join(r['tools']) or 'an AI tool'}"
                    + (f" for {', '.join(r['tasks'])}" if r["tasks"] else "") + ".")
        body.append("")
        body.append("## Evidence")
        body.append("")
        body.append("> " + (r["evidence"] or r["text"])[:600].replace("\n", "\n> "))
        if r["permalink"]:
            body.append("")
            body.append(f"[Original message in Slack]({r['permalink']})")

        p = base / (uid + ".md")
        p.write_text("\n".join(fm + body) + "\n", encoding="utf-8")
        written.append(str(p))
    return written


def main():
    ap = argparse.ArgumentParser(description="Find AI use cases in Slack.")
    ap.add_argument("--vault", default=str(Path.home() / "Documents" / "TeamVault"))
    ap.add_argument("--pages", type=int, default=2,
                    help="search pages per term, 100 messages each")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what was found without writing notes")
    a = ap.parse_args()

    token = token_from_env()
    print("searching Slack")
    records = collect(token, pages=a.pages)
    print(f"\n{len(records)} use cases found")
    by_vis = {}
    for r in records:
        by_vis[r["visibility"]] = by_vis.get(r["visibility"], 0) + 1
    for k, v in sorted(by_vis.items()):
        print(f"  {k}: {v}")

    if a.dry_run:
        for r in records[:20]:
            print(f"  [{r['visibility']}] {r['person']}: {r['evidence'][:80]}")
        return
    written = write_notes(records, a.vault)
    print(f"wrote {len(written)} notes to {Path(a.vault) / 'slack'}")
    # publish.py reads this, and applies the visibility rules to it
    out = Path(a.vault) / "slack-usecases.json"
    out.write_text(json.dumps(records, indent=1, ensure_ascii=True), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
