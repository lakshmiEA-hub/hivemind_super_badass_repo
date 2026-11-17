"""Regression cases for the use case filter. Run: python test_aiuse.py

Plain asserts and no test runner, to match the rest of the tooling: everything
here is startable with a bare `python`. The noise cases matter more than the
positive ones. A filter that misses a use case costs a row in the tracker; a
filter that waves through every newsletter mentioning AI costs the reviewer's
willingness to use the review screen at all.
"""

import sys

import aiuse

# (text, sender, subject, expected)
CASES = [
    ("I used Claude to draft the donor report and it cut it down to an hour.",
     "sarah@evidenceaction.org", "Donor report", True),
    ("We ran the monitoring spreadsheet through ChatGPT to clean the data before the analysis.",
     "amit@evidenceaction.org", "", True),
    ("Quick one: I asked Copilot to write a python script to automate the reconciliation.",
     "lakshmi@evidenceaction.org", "", True),
    ("Granola transcribed the whole partner call, action items came out clean.",
     "james@evidenceaction.org", "Partner call", True),
    ("I got Gemini to translate the field guide into Swahili, saved me two days.",
     "mary@evidenceaction.org", "", True),

    ("Join our webinar on how AI is transforming the nonprofit sector. Register now! Unsubscribe.",
     "news@somevendor.com", "AI for Nonprofits Webinar", False),
    ("This week in AI: OpenAI announces a funding round at a $90B valuation.",
     "newsletter@aidigest.com", "This Week in AI", False),
    # the substring trap: 'detail', 'chair', 'maintain' and 'email' all contain
    # the letters, and a case-insensitive \bAI\b would not save us here either
    ("Please detail the chair arrangements and maintain the email thread.",
     "ops@evidenceaction.org", "Retreat logistics", False),
    ("Your build failed. Do not reply to this message.",
     "no-reply@github.com", "Build failed", False),
    ("Attached is the Q3 budget reforecast for the water program.",
     "finance@evidenceaction.org", "Q3 reforecast", False),
    # discussing AI is not using AI, and this is the distinction the whole
    # tracker rests on
    ("We should discuss our AI strategy at the next leadership meeting.",
     "ceo@evidenceaction.org", "AI strategy", False),
]


def main():
    fails = []
    for text, sender, subject, expected in CASES:
        r = aiuse.classify(text, sender=sender, subject=subject)
        if r["is_use_case"] != expected:
            fails.append((subject or text[:40], expected, r))

    for label, expected, r in fails:
        print(f"FAIL {label!r}: expected {expected}, got {r['is_use_case']} "
              f"(score {r['score']}, why {r['why']})")

    print(f"{len(CASES) - len(fails)}/{len(CASES)} passed")

    # Every positive case must carry the sentence that justifies it: a tracker
    # entry with no quotable evidence is the thing this repo forbids.
    for text, sender, subject, expected in CASES:
        r = aiuse.classify(text, sender=sender, subject=subject)
        if r["is_use_case"]:
            assert r["evidence"], f"use case with no evidence: {subject!r}"

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
