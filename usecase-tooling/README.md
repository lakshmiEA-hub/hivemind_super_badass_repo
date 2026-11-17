# AI use case tracker

Finds the places where people actually used an AI tool, across Google Drive,
Slack and Gmail, and assembles them into one tracker that can be published to a
Google Drive folder.

The hard part is not collection. It is that the three sources carry different
promises to the people in them, and the tracker has to keep all three.

## What counts as a use case

A deck about AI strategy, a newsletter announcing a model release, and a
message saying "I got Claude to draft the donor report" all mention AI. Only
the last is a use case. `aiuse.py` scores on three things: a named tool,
evidence of somebody doing something rather than discussing something, and the
task it was used for. Marketing and industry news score negative.

Every record carries the sentence that triggered it. Nothing here asserts a use
case without one, and `test_aiuse.py` checks that.

## The privacy model

This is the part to read before running anything.

| Source | Reaches a shared folder as |
|---|---|
| Public Slack channel | The message, quoted. It was already readable by the workspace. |
| Private channel or DM | Who, when, which tool, which task. Never the message. |
| Gmail | Who, when, which tool, which task. Never the message, never the subject. |
| Drive | Quoted, but only if every recipient can already open the document. Otherwise reduced. |

On top of that, `export-tooling/sensitive-excludes.txt` blocks anything matching
it regardless of access, exactly as it does for the wiki. A public Slack message
about somebody's performance review is still blocked.

The reduction is not cosmetic. `publish.py` builds a new record containing only
those four fields, so the body and the subject line are not present in the
uploaded file at all rather than being hidden in it.

Note what metadata still says: that a named colleague used a named tool, derived
from a conversation they had privately. That is a real disclosure even without
the words. Decide whether your recipients should see it before you share.

Mail follows `docs/MAIL-LAYER-CONVENTIONS.md`: notes are written under `mail/`
with `visibility: private`, and the existing blocklist catches them.

## Setup

### Slack

Needs a **user** token (`xoxp-`). `search.messages` does not accept bot tokens.

1. Create a Slack app at api.slack.com/apps, in the Evidence Action workspace.
2. Under **OAuth & Permissions**, add these *user* token scopes:
   `search:read`, `users:read`, `users:read.email`, `channels:history`,
   `groups:history`, `im:history`, `mpim:history`.
3. Install it to the workspace. A workspace admin may have to approve this.
4. Copy the user token into `usecase-tooling/slack-token.txt`, or set
   `SLACK_USER_TOKEN`. Both are git-ignored.

### Gmail

Needs its own OAuth client: rclone holds a `drive.readonly` token and has no
Gmail backend, so there is nothing to borrow.

1. In a Google Cloud project, enable the **Gmail API**.
2. Create an **OAuth client ID**, type **Desktop app**.
3. Download the JSON to `usecase-tooling/gmail-client.json`.

First run opens a browser for consent. The scope is `gmail.readonly`, and it
reads your mailbox only. Nobody else's mail is reachable, which is a real limit
on coverage rather than a bug.

### Drive

Already set up if the pipeline runs. `drive.py` reads the markdown mirror at
`<vault>/drive/` and makes no network calls at all.

Publishing needs a second rclone remote with `drive.file`, created on first use.
That scope reaches only files this tool itself creates, so it cannot read or
change anything else in your Drive. The existing read-only remote stays
read-only.

## Running it

```powershell
cd usecase-tooling
python drive.py  --vault "$env:USERPROFILE\Documents\TeamVault"
python slack.py  --vault "$env:USERPROFILE\Documents\TeamVault"
python gmail.py  --vault "$env:USERPROFILE\Documents\TeamVault"

# build the tracker locally and read it before anybody else does
python publish.py --vault "$env:USERPROFILE\Documents\TeamVault" `
                  --share-with "a@evidenceaction.org,b@evidenceaction.org"

# only when you are happy with it
python publish.py --vault "$env:USERPROFILE\Documents\TeamVault" `
                  --share-with "a@evidenceaction.org,b@evidenceaction.org" --upload
```

`publish.py` never uploads without `--upload`, and refuses to upload without
`--share-with`. Run it without `--upload` first: the tracker it writes locally
is byte for byte what would be sent.

Each source takes `--dry-run`, which reports what it found and writes nothing.

## Tests

```powershell
python test_aiuse.py
python test_publish.py
```

`test_publish.py` is the one that matters. It asserts that a DM, a private
channel and a mail thread cannot be quoted, that subject lines and channel names
do not travel, and that sensitivity beats access. If it fails, do not publish.

## Known limits

- Gmail covers your mailbox only, so other people's use shows up only where it
  reached you. Slack public channels are where colleagues' use is actually
  visible.
- Slack search ranks rather than enumerates. `--pages` trades completeness for
  time; it is a sample of the best matches, not a census.
- `aiuse.py` is regex over text, tuned for recall. Expect to reject some rows
  in review. The score and the reason are on every record so you can see why
  each one surfaced.
- The tool list in `aiuse.TOOLS` is the main thing to keep current. A tool
  nobody listed is a use case nobody finds.
