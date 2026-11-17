"""Pull AI use cases out of the signed-in person's Gmail.

Unlike Drive, this cannot borrow rclone's consent. rclone holds a
`drive.readonly` token and has no Gmail backend at all, so mail needs its own
OAuth client. The flow here is the standard installed-app loopback, written
against the standard library rather than google-auth-oauthlib, because the rest
of this repo installs with nothing heavier than markitdown and that is worth
preserving.

Scope is `gmail.readonly`. You can read your own mailbox and nobody else's,
which is a real limit on the tracker rather than a bug: colleagues' AI use
shows up here only where it reached your inbox.

Everything written here is `visibility: private`, per
docs/MAIL-LAYER-CONVENTIONS.md. That is not a default to be overridden. Mail
reaches a shared folder only as the non-quoting summary publish.py builds, never
as the message text.

Setup, once: create an OAuth client (Desktop app) in a Google Cloud project,
enable the Gmail API, download the JSON to usecase-tooling/gmail-client.json.
Both that file and the token it produces are blocked by .gitignore.
"""

import argparse
import base64
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import aiuse

API = "https://gmail.googleapis.com/gmail/v1"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

HERE = Path(__file__).resolve().parent
CLIENT_FILE = HERE / "gmail-client.json"
TOKEN_FILE = HERE / "gmail-token.json"

# Gmail's search does support OR, so the whole term list goes in one query
# rather than one request per term. Promotions and the spam and trash folders
# are dropped here rather than scored later: it is cheaper, and they are where
# the AI marketing lives.
def query_string(months=24):
    terms = " OR ".join(f'"{t}"' for t in list(aiuse.TOOLS) + ["AI", "LLM", "prompt"])
    return (f"({terms}) newer_than:{months}m "
            "-in:spam -in:trash -category:promotions -category:social")


class _CodeCatcher(BaseHTTPRequestHandler):
    """Catches the one redirect Google makes back to the loopback address."""

    code = None
    state = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CodeCatcher.code = (q.get("code") or [None])[0]
        _CodeCatcher.state = (q.get("state") or [None])[0]
        body = (b"<html><body style='font-family:system-ui;padding:3rem'>"
                b"<h2>Signed in.</h2><p>You can close this tab and go back to "
                b"the terminal.</p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _client():
    if not CLIENT_FILE.is_file():
        raise RuntimeError(
            f"No Gmail OAuth client at {CLIENT_FILE}. Create one (Desktop app) "
            "in a Google Cloud project with the Gmail API enabled, download the "
            "JSON, and save it there. See README.md.")
    raw = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
    # Google hands the file out under either key depending on the client type
    cfg = raw.get("installed") or raw.get("web") or raw
    if not cfg.get("client_id"):
        raise RuntimeError(f"{CLIENT_FILE} has no client_id in it.")
    return cfg


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error_description", "")
        except Exception:
            pass
        raise RuntimeError(f"Google rejected the token request: {detail or e.code}") from None


def sign_in():
    """Browser consent, once. Stores a refresh token for every run after this."""
    cfg = _client()
    server = HTTPServer(("127.0.0.1", 0), _CodeCatcher)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "redirect_uri": redirect,
        "response_type": "code", "scope": SCOPE, "state": state,
        # offline + consent is what actually returns a refresh token; without
        # them a re-run sends you back through the browser every time
        "access_type": "offline", "prompt": "consent",
    })
    print("Opening your browser to sign in to Gmail (read-only).")
    print("If it does not open, paste this:\n  " + url)
    webbrowser.open(url)
    server.handle_request()
    server.server_close()

    if not _CodeCatcher.code or _CodeCatcher.state != state:
        raise RuntimeError("Sign-in did not complete. Run it again.")
    tok = _post(TOKEN, {
        "code": _CodeCatcher.code, "client_id": cfg["client_id"],
        "client_secret": cfg.get("client_secret", ""), "redirect_uri": redirect,
        "grant_type": "authorization_code",
    })
    tok["obtained"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(tok, indent=1), encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass          # best effort: Windows ACLs do not map onto this cleanly
    return tok


def access_token():
    """A live access token, refreshing or re-authorising as needed."""
    if not TOKEN_FILE.is_file():
        return sign_in()["access_token"]
    tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    fresh_until = tok.get("obtained", 0) + tok.get("expires_in", 3600) - 120
    if time.time() < fresh_until and tok.get("access_token"):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        return sign_in()["access_token"]
    cfg = _client()
    new = _post(TOKEN, {
        "refresh_token": tok["refresh_token"], "client_id": cfg["client_id"],
        "client_secret": cfg.get("client_secret", ""), "grant_type": "refresh_token",
    })
    tok.update(new)
    tok["obtained"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(tok, indent=1), encoding="utf-8")
    return tok["access_token"]


def _get(token, path, **params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            if e.code == 403:
                raise RuntimeError(
                    "Gmail refused the request (403). The Gmail API may not be "
                    "enabled on the project, or the token lacks gmail.readonly."
                ) from None
            raise RuntimeError(f"Gmail API {e.code} on {path}") from None
    raise RuntimeError(f"Gmail API kept failing on {path}")


def _decode(data):
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:
        return ""


def plain_text(payload):
    """The readable body, preferring text/plain and falling back to stripped HTML."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode((payload.get("body") or {}).get("data"))
    if mime.startswith("multipart/"):
        parts = payload.get("parts") or []
        for p in parts:                       # plain wins wherever it exists
            if p.get("mimeType") == "text/plain":
                t = _decode((p.get("body") or {}).get("data"))
                if t.strip():
                    return t
        for p in parts:
            t = plain_text(p)
            if t.strip():
                return t
        return ""
    if mime == "text/html":
        html = _decode((payload.get("body") or {}).get("data"))
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        return re.sub(r"\s{2,}", " ", re.sub(r"(?s)<[^>]+>", " ", html))
    return ""


def strip_quotes(text):
    """Drop quoted reply chains and signatures, per the mail layer conventions.

    A long thread otherwise scores on the same sentence ten times over, and the
    evidence picked out of it is usually somebody else's original message.
    """
    lines = []
    for line in (text or "").splitlines():
        if line.strip().startswith(">"):
            continue
        if re.match(r"^\s*On .{10,80} wrote:\s*$", line):
            break
        if re.match(r"^\s*(--|__|Sent from my)\b", line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def header(msg, name):
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def collect(token, months=24, max_messages=400, verbose=True):
    """AI use cases in the mailbox, newest first."""
    q, out, page = query_string(months), [], None
    scanned = 0
    while scanned < max_messages:
        params = {"q": q, "maxResults": min(100, max_messages - scanned)}
        if page:
            params["pageToken"] = page
        listing = _get(token, "users/me/messages", **params)
        ids = [m["id"] for m in listing.get("messages") or []]
        if not ids:
            break
        for mid in ids:
            scanned += 1
            msg = _get(token, f"users/me/messages/{mid}", format="full")
            body = strip_quotes(plain_text(msg.get("payload")))
            subject = header(msg, "Subject")
            sender = header(msg, "From")
            rec = aiuse.classify(body, sender=sender, subject=subject)
            if not rec["is_use_case"]:
                continue
            try:
                when = datetime.fromtimestamp(
                    int(msg.get("internalDate", "0")) / 1000).date().isoformat()
            except (TypeError, ValueError, OSError):
                when = ""
            addr = re.search(r"[\w.+-]+@[\w.-]+", sender)
            out.append({
                "source": "gmail",
                "visibility": "private",       # never anything else
                "person": re.sub(r"\s*<.*>", "", sender).strip().strip('"'),
                "email": (addr.group(0).lower() if addr else ""),
                "date": when,
                "subject": subject,
                "thread_id": msg.get("threadId", ""),
                "text": body,
                "tools": rec["tools"],
                "tasks": rec["tasks"],
                "evidence": rec["evidence"],
                "score": rec["score"],
                "why": rec["why"],
            })
        if verbose:
            print(f"  scanned {scanned}, {len(out)} use cases so far")
        page = listing.get("nextPageToken")
        if not page:
            break
    return out


def write_notes(records, vault):
    """One note per use case, under `mail/` so sensitive-excludes.txt catches it."""
    base = Path(vault) / "mail" / "threads"
    base.mkdir(parents=True, exist_ok=True)
    written = []
    for r in records:
        uid = re.sub(r"[^a-z0-9]+", "-",
                     f"{r['date']}-{r['subject']}".lower()).strip("-")[:60] or "thread"
        fm = [
            "---",
            "type: mail-thread",
            f"title: {r['subject']}",
            f"uid: {uid}",
            f"date: {r['date']}",
            f"participants: [{r['email']}]",
            "visibility: private",
            "tags: [email, private, ai-use-case]",
            "status: clean",
            f"thread_id: {r['thread_id']}",
            f"tools: [{', '.join(r['tools'])}]",
            f"tasks: [{', '.join(r['tasks'])}]",
            f"score: {r['score']}",
            "---",
            "",
            "> [!warning] Private. Email thread, excluded from all exports.",
            "",
            f"{r['person'] or 'Someone'} used "
            f"{', '.join(r['tools']) or 'an AI tool'}"
            + (f" for {', '.join(r['tasks'])}" if r["tasks"] else "") + ".",
            "",
            "## Evidence",
            "",
            "> " + (r["evidence"] or "")[:600].replace("\n", "\n> "),
        ]
        p = base / (uid + ".md")
        p.write_text("\n".join(fm) + "\n", encoding="utf-8")
        written.append(str(p))
    return written


def main():
    ap = argparse.ArgumentParser(description="Find AI use cases in Gmail.")
    ap.add_argument("--vault", default=str(Path.home() / "Documents" / "TeamVault"))
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--max-messages", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    token = access_token()
    print("searching Gmail")
    records = collect(token, months=a.months, max_messages=a.max_messages)
    print(f"\n{len(records)} use cases found")
    if a.dry_run:
        for r in records[:20]:
            print(f"  {r['date']} {r['person']}: {r['evidence'][:80]}")
        return
    written = write_notes(records, a.vault)
    print(f"wrote {len(written)} notes to {Path(a.vault) / 'mail' / 'threads'}")
    out = Path(a.vault) / "gmail-usecases.json"
    out.write_text(json.dumps(records, indent=1, ensure_ascii=True), encoding="utf-8")
    print(f"wrote {out}")
    print("These are private. They reach a shared folder only as the "
          "non-quoting summary publish.py builds.")


if __name__ == "__main__":
    main()
