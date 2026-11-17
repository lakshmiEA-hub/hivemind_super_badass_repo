"""Gate tests: what may be quoted outside the vault, and what may not.

Run: python test_publish.py

These are the tests worth having. Everything else in this folder can be wrong
in ways that cost a missing row; this file is the one that, when wrong, puts
somebody's direct message in a folder shared with their colleagues.
"""

import sys

import publish

PUBLIC_SLACK = {
    "source": "slack", "visibility": "public", "channel": "general",
    "person": "Sarah", "email": "sarah@evidenceaction.org", "date": "2026-10-01",
    "text": "I used Claude to draft the donor report.",
    "evidence": "I used Claude to draft the donor report.",
    "tools": ["Claude"], "tasks": ["drafting"],
}
DM_SLACK = {
    "source": "slack", "visibility": "dm", "channel": "direct message",
    "person": "Amit", "email": "amit@evidenceaction.org", "date": "2026-10-02",
    "text": "honestly I just get ChatGPT to write all my updates",
    "evidence": "honestly I just get ChatGPT to write all my updates",
    "tools": ["ChatGPT"], "tasks": ["drafting"],
}
PRIVATE_SLACK = {**DM_SLACK, "visibility": "private", "channel": "leads-only",
                 "person": "Mary"}
GMAIL = {
    "source": "gmail", "visibility": "private", "subject": "Re: board paper",
    "person": "James", "email": "james@evidenceaction.org", "date": "2026-09-30",
    "text": "Copilot wrote the first draft of the board paper.",
    "evidence": "Copilot wrote the first draft of the board paper.",
    "tools": ["Copilot"], "tasks": ["drafting"],
}
DRIVE = {
    "source": "drive", "visibility": "drive", "path": "Strategy\\AI pilot notes",
    "file_id": "", "person": "Priya", "email": "priya@evidenceaction.org",
    "date": "2026-08-11", "text": "",
    "evidence": "We used Gemini to summarise the field reports.",
    "tools": ["Gemini"], "tasks": ["summarising"],
}
SENSITIVE = {
    "source": "slack", "visibility": "public", "channel": "general",
    "person": "HR", "email": "hr@evidenceaction.org", "date": "2026-10-03",
    "text": "Claude helped me rewrite the performance review for Dan.",
    "evidence": "Claude helped me rewrite the performance review for Dan.",
    "tools": ["Claude"], "tasks": ["drafting"],
}

RECIPIENTS = ["sarah@evidenceaction.org", "james@evidenceaction.org"]


def find(entries, person):
    for e in entries:
        if e.get("person") == person:
            return e
    return None


def main():
    records = [PUBLIC_SLACK, DM_SLACK, PRIVATE_SLACK, GMAIL, DRIVE, SENSITIVE]
    shipped, dropped = publish.gate(records, RECIPIENTS, token=None, verbose=False)
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    # A public channel message may be quoted: the workspace could already read it.
    sarah = find(shipped, "Sarah")
    check("public slack should ship quoted", sarah and sarah.get("quoted"))
    check("public slack should keep its evidence", sarah and sarah.get("evidence"))

    # A DM may not, whatever it says.
    amit = find(shipped, "Amit")
    check("dm should ship reduced", amit and not amit.get("quoted"))
    check("dm must not carry evidence", amit is not None and not amit.get("evidence"))
    check("dm must not carry the message text", amit is not None and not amit.get("text"))
    check("dm must keep the tool", amit and amit.get("tools") == ["ChatGPT"])

    # Nor may a private channel.
    mary = find(shipped, "Mary")
    check("private channel should ship reduced", mary and not mary.get("quoted"))
    check("private channel must not carry evidence",
          mary is not None and not mary.get("evidence"))
    check("private channel name must not travel",
          mary is not None and not mary.get("channel"))

    # Nor mail, ever.
    james = find(shipped, "James")
    check("gmail should ship reduced", james and not james.get("quoted"))
    check("gmail must not carry evidence", james is not None and not james.get("evidence"))
    check("gmail subject must not travel", james is not None and not james.get("subject"))

    # A Drive doc with no id cannot be access checked. It still ships, because
    # the use case is real, but it must not be quoted: there is no check to
    # back the claim that every recipient could already read the original.
    priya = find(shipped, "Priya")
    check("drive without an id should still ship", priya is not None)
    check("unverifiable drive doc must not be quoted",
          priya is not None and not priya.get("quoted"))
    check("unverifiable drive doc must not carry evidence",
          priya is not None and not priya.get("evidence"))
    check("unverifiable drive doc should be labelled",
          priya is not None and priya.get("unverified"))

    # Sensitivity beats access: a public message about a performance review
    # is blocked even though anyone in the workspace could read it.
    check("performance review must be blocked", find(shipped, "HR") is None)
    check("blocked item should be reported", len(dropped) == 1)

    for f in fails:
        print("FAIL " + f)
    total = 16
    print(f"{total - len(fails)}/{total} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
