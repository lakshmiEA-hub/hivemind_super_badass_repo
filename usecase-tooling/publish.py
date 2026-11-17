"""Build the AI use case tracker and put it in a Google Drive folder.

This is the only module here that sends anything outward, so the rules it
enforces are worth stating plainly.

Three kinds of material arrive: Drive documents, Slack messages, and mail. They
do not get the same treatment, because the people in them did not agree to the
same things.

- A public Slack message can be quoted. It was already readable by the
  workspace.
- A DM, a private channel and anything from Gmail cannot be quoted, ever. They
  are reduced to who, when, which tool, and what task. The sentence that proved
  the use case stays in the local vault and does not travel.
- A Drive document is quoted only to recipients who can already open it, which
  is the check teamvault/core.can_access already performs file by file.

On top of that, the sensitivity blocklist in export-tooling/sensitive-excludes.txt
applies to everything regardless of access, exactly as it does for the wiki.

Writing to Drive needs a scope rclone's existing remote does not have, so a
second remote is created with `drive.file`. That scope reaches only files this
tool itself created, which is the least privilege that can still do the job: it
cannot read or touch anything else in the Drive.
"""

import argparse
import json
import mimetypes
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "teamvault"))

import core                                     # noqa: E402  (after sys.path)
import manifest as M                            # noqa: E402

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
WRITE_REMOTE = "hivemind-publish"

EXCLUDES = HERE.parent / "export-tooling" / "sensitive-excludes.txt"


def load_excludes():
    """Case-insensitive substrings that bar a record from any export."""
    if not EXCLUDES.is_file():
        return []
    out = []
    for line in EXCLUDES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.lower())
    return out


def blocked(record, excludes):
    """True when a record is barred outright. Checked against everything we hold.

    The haystack deliberately includes the message text as well as its labels.
    A thread filed in a harmless folder can still be about somebody's salary,
    and that is precisely the case the path-based check misses.
    """
    hay = " ".join(str(record.get(k, "")) for k in
                   ("path", "subject", "channel", "person", "email", "text", "evidence")).lower()
    for term in excludes:
        if term in hay:
            return True, term
    sens, why = M.flag(hay)
    return (True, why) if sens else (False, "")


def quotable(record):
    """Whether this record's words may leave the local vault.

    The default is no. Only a public Slack message and an access-checked Drive
    document earn their way out of it, and the Drive check happens per
    recipient in `gate`, not here.
    """
    if record.get("source") == "slack":
        return record.get("visibility") == "public"
    if record.get("source") == "drive":
        return True            # subject to can_access, applied per recipient
    return False               # gmail, and anything new that forgets to say


def reduce_record(record):
    """Strip a record to who, when, which tool, what task. Nothing else.

    Subject lines and channel names are dropped along with the body. A subject
    is written by a person who assumed one reader, and 'Re: Priya's offer' in a
    shared tracker is the leak this whole module exists to prevent.
    """
    return {
        "source": record.get("source", ""),
        "person": record.get("person", ""),
        "email": record.get("email", ""),
        "date": record.get("date", ""),
        "tools": record.get("tools", []),
        "tasks": record.get("tasks", []),
        "quoted": False,
        "visibility": record.get("visibility", "private"),
    }


def gate(records, recipients, token=None, verbose=True):
    """Split records into what may ship, reduced or whole, and what may not.

    `recipients` are the people the folder will be shared with. A Drive-sourced
    record ships whole only if every one of them can already open the document.
    """
    excludes = load_excludes()
    shipped, dropped = [], []
    for r in records:
        bad, why = blocked(r, excludes)
        if bad:
            dropped.append({**reduce_record(r), "reason": "sensitivity:" + why})
            continue

        if not quotable(r):
            shipped.append(reduce_record(r))
            continue

        if r.get("source") == "drive":
            # An unverifiable document is not a safe one. Without an id, a
            # token and a list of recipients there is no check to run, and
            # quoting it anyway would assert an access guarantee that was
            # never tested. Reduce it instead and say so in the output.
            if not (r.get("file_id") and token and recipients):
                shipped.append({**reduce_record(r), "unverified": True})
                continue
            # every recipient, not any: the folder is one artifact shared with
            # all of them, so the least-privileged reader sets the bar
            ok = True
            for who in recipients:
                dom = who.split("@")[-1].lower()
                if not core.can_access(token, r["file_id"], who, dom):
                    ok = False
                    break
            if not ok:
                shipped.append(reduce_record(r))
                continue

        shipped.append({**r, "quoted": True})

    if verbose:
        whole = sum(1 for s in shipped if s.get("quoted"))
        print(f"  {len(shipped)} entries: {whole} quoted, "
              f"{len(shipped) - whole} reduced to metadata")
        print(f"  {len(dropped)} blocked by the sensitivity list")
    return shipped, dropped


def tracker_markdown(entries):
    """The index: one table of every use case, plus roll-ups by tool and person."""
    today = date.today().isoformat()
    by_tool, by_person = Counter(), Counter()
    for e in entries:
        for t in e.get("tools") or ["unspecified"]:
            by_tool[t] += 1
        if e.get("person"):
            by_person[e["person"]] += 1

    out = [
        "---",
        "type: moc",
        "title: AI Use Case Tracker",
        "uid: ai-use-case-tracker",
        f"created: {today}",
        f"modified: {today}",
        "tags: [ai-use-case, tracker]",
        "status: clean",
        "---",
        "",
        "# AI Use Case Tracker",
        "",
        f"{len(entries)} use cases, assembled {today} from Google Drive, Slack "
        "and Gmail.",
        "",
        "> [!note] How to read this",
        "> Entries marked `metadata only` came from a direct message, a private",
        "> channel, or email. Who, when and which tool are recorded; the message",
        "> itself is not, because the people in those conversations did not agree",
        "> to be quoted. The evidence stays in the originating vault.",
        "",
        "## By tool",
        "",
        "| Tool | Use cases |",
        "|---|---|",
    ]
    out += [f"| {t} | {n} |" for t, n in by_tool.most_common()]
    out += ["", "## By person", "", "| Person | Use cases |", "|---|---|"]
    out += [f"| {p} | {n} |" for p, n in by_person.most_common()]
    out += ["", "## All use cases", "",
            "| Date | Person | Tool | Task | Source | Evidence |",
            "|---|---|---|---|---|---|"]
    for e in sorted(entries, key=lambda x: x.get("date") or "", reverse=True):
        ev = "metadata only"
        if e.get("quoted") and e.get("evidence"):
            # pipes and newlines would break the row apart
            ev = e["evidence"].replace("|", "\\|").replace("\n", " ")[:160]
        out.append("| {} | {} | {} | {} | {} | {} |".format(
            e.get("date", ""), e.get("person", ""),
            ", ".join(e.get("tools") or []), ", ".join(e.get("tasks") or []),
            e.get("source", ""), ev))
    return "\n".join(out) + "\n"


def build(entries, dropped, out_dir):
    """Write the tracker locally. Always runs before anything is uploaded."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "AI Use Case Tracker.md").write_text(
        tracker_markdown(entries), encoding="utf-8")
    (out / "entries.json").write_text(
        json.dumps(entries, indent=1, ensure_ascii=True), encoding="utf-8")
    # the blocked list ships nothing but counts, so the reviewer can see that
    # the gate ran without the blocked material being written out again
    summary = Counter(d.get("reason", "") for d in dropped)
    (out / "blocked-summary.json").write_text(
        json.dumps(dict(summary), indent=1), encoding="utf-8")
    return out


# ---------------------------------------------------------------- Drive upload

def ensure_write_remote():
    """An rclone remote scoped to drive.file, created on first use.

    Separate from the read-only remote on purpose. The existing one stays
    read-only so nothing in the nightly mirror can ever write to Drive.
    """
    if WRITE_REMOTE in core.list_remotes():
        return WRITE_REMOTE
    print(f"Creating a write-scoped Google connection ('{WRITE_REMOTE}').")
    print("Your browser will open. The scope is drive.file: this tool can only "
          "see files it creates itself, not the rest of your Drive.")
    cmd = [core.rclone_path(), "--config", str(core.config_path()),
           "config", "create", WRITE_REMOTE, "drive", "scope=drive.file"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    if WRITE_REMOTE not in core.list_remotes():
        raise RuntimeError((r.stderr or r.stdout)[:400])
    return WRITE_REMOTE


def _api(token, method, path, body=None, base=DRIVE, **params):
    url = f"{base}/{path}?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        reason, message = core._http_error(e)
        raise RuntimeError(f"Drive {method} {path} failed: {e.code} "
                           f"{reason} {message}".strip()) from None


def create_folder(token, name, parent=None):
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        body["parents"] = [parent]
    return _api(token, "POST", "files", body, fields="id,name,webViewLink")


def upload_file(token, path, parent):
    """Multipart upload of one file. Returns the created file's id."""
    p = Path(path)
    ctype = mimetypes.guess_type(p.name)[0] or "text/markdown"
    meta = json.dumps({"name": p.name, "parents": [parent]}).encode()
    boundary = "-------hivemind" + re.sub(r"\W", "", p.name)[:12]
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        meta, b"\r\n",
        f"--{boundary}\r\nContent-Type: {ctype}\r\n\r\n".encode(),
        p.read_bytes(), f"\r\n--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{UPLOAD}/files?uploadType=multipart&fields=id,name", data=body, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": f"multipart/related; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        reason, message = core._http_error(e)
        raise RuntimeError(f"Upload of {p.name} failed: {e.code} {reason} {message}") from None


def share(token, file_id, email, role="reader"):
    """Grant one person access. sendNotificationEmail is left on deliberately:
    somebody added to a folder of their colleagues' AI use should be told."""
    return _api(token, "POST", f"files/{file_id}/permissions",
                {"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail="true")


def publish(out_dir, folder_name, recipients, token):
    folder = create_folder(token, folder_name)
    print(f"created Drive folder: {folder.get('name')} ({folder.get('id')})")
    for f in sorted(Path(out_dir).glob("*")):
        if f.is_file():
            upload_file(token, f, folder["id"])
            print(f"  uploaded {f.name}")
    for who in recipients:
        share(token, folder["id"], who)
        print(f"  shared with {who}")
    return folder


# ---------------------------------------------------------------------- CLI

def load_records(vault):
    """Use case records from whatever the source modules have written.

    Read as utf-8-sig rather than utf-8: these files are routinely regenerated
    by hand or by a PowerShell step on the way in, and PowerShell 5.1 writes a
    BOM that plain utf-8 decoding chokes on. utf-8-sig reads both.
    """
    v = Path(vault)
    records = []
    for f in (v / "usecases.json", v / "slack-usecases.json", v / "gmail-usecases.json"):
        if f.is_file():
            records.extend(json.loads(f.read_text(encoding="utf-8-sig")))
    return records


def main():
    ap = argparse.ArgumentParser(
        description="Build the AI use case tracker and optionally publish it to Drive.")
    ap.add_argument("--vault", default=str(Path.home() / "Documents" / "TeamVault"))
    ap.add_argument("--out", default="", help="where to write the tracker locally")
    ap.add_argument("--share-with", default="",
                    help="comma-separated emails. Required to upload.")
    ap.add_argument("--folder-name", default=f"AI Use Case Tracker {date.today().isoformat()}")
    ap.add_argument("--upload", action="store_true",
                    help="actually create and share the Drive folder")
    a = ap.parse_args()

    recipients = [e.strip() for e in a.share_with.split(",") if e.strip()]
    out_dir = a.out or str(Path(a.vault) / "ai-use-case-tracker")

    records = load_records(a.vault)
    if not records:
        raise SystemExit(
            f"No use case records found in {a.vault}. Run slack.py and gmail.py first.")
    print(f"{len(records)} raw records")

    token = None
    if any(r.get("source") == "drive" for r in records) and recipients:
        token = core.access_token(core.list_remotes()[0])

    entries, dropped = gate(records, recipients, token=token)
    build(entries, dropped, out_dir)
    print(f"tracker written to {out_dir}")

    if not a.upload:
        print("\nNothing uploaded. Review the tracker, then re-run with --upload "
              "and --share-with to publish it.")
        return
    if not recipients:
        raise SystemExit("--upload needs --share-with: say who the folder is for.")

    wtoken = core.access_token(ensure_write_remote())
    folder = publish(out_dir, a.folder_name, recipients, wtoken)
    print("\nDone: https://drive.google.com/drive/folders/" + folder["id"])


if __name__ == "__main__":
    main()
