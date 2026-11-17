"""Decide whether a piece of text records somebody actually using an AI tool.

The tracker's job is narrower than it first looks. A deck about AI strategy, a
newsletter announcing a model release, and a message saying "I got Claude to
draft the donor report" all mention AI, but only the last is a use case. Nearly
everything here exists to separate the third kind from the first two.

No model calls. This runs over every message in a mailbox and every message in
a Slack history, so it has to be cheap enough to be run on all of it. It is a
recall-first filter feeding the review screen, not a final judgement: borderline
items are meant to surface with their evidence so a person decides.

The output carries the sentence that triggered the match. Nothing downstream
should ever assert a use case without it, and publish.py strips that evidence
before anything derived from mail or a DM reaches a shared folder.
"""

import re
from pathlib import Path

# Named products, which are the strongest signal there is: somebody writing
# "Otter" in a sentence about meetings is telling us what they used. The
# meeting-notes tools overlap with core.AI_NOTES_RX on purpose, because a
# transcript in Drive is itself evidence that somebody ran one.
TOOLS = {
    "Claude": r"\bclaude\b|\banthropic\b|\bclaude\.ai\b",
    "ChatGPT": r"\bchat ?gpt\b|\bgpt-?[45]\b|\bopenai\b",
    "Copilot": r"\b(?:github |microsoft |m365 )?copilot\b",
    "Gemini": r"\bgemini\b|\bbard\b",
    "Perplexity": r"\bperplexity\b",
    "NotebookLM": r"\bnotebook ?lm\b",
    "Otter": r"\botter(?:\.ai)?\b",
    "Fireflies": r"\bfireflies(?:\.ai)?\b",
    "Granola": r"\bgranola\b",
    "Read AI": r"\bread\.ai\b",
    "Grammarly": r"\bgrammarly\b",
    "Notion AI": r"\bnotion ai\b",
    "Midjourney": r"\bmidjourney\b",
    "DALL-E": r"\bdall-?e\b",
    "Canva AI": r"\bcanva (?:ai|magic)\b|\bmagic studio\b",
    "ElevenLabs": r"\beleven ?labs\b",
    "Whisper": r"\bwhisper\b",
    "Cursor": r"\bcursor\b",
    "Zapier": r"\bzapier\b",
    "Power Automate": r"\bpower automate\b",
}

# Unnamed references. "AI" is matched case-sensitively and with boundaries
# because the lowercase letters turn up inside ordinary words, and a substring
# match on them flagged about one message in six in testing against a real
# mailbox: "chair", "detail", "maintain", "said", "email" and so on.
GENERIC = [
    (r"\bAI\b", "AI"),
    (r"\bartificial intelligence\b", "artificial intelligence"),
    (r"\bmachine learning\b", "machine learning"),
    (r"\bLLMs?\b", "LLM"),
    (r"\blarge language models?\b", "large language model"),
    (r"\bgen(?:erative)? ?ai\b", "generative AI"),
    (r"\bchat ?bots?\b", "chatbot"),
    (r"\bprompt(?:ing|s| engineering)\b", "prompting"),
]

# Somebody doing something, as opposed to somebody discussing something. This
# is the test that separates a use case from an opinion about AI, so it is
# worth more than the tool name on its own.
USE = [
    (r"\b(?:i|we|she|he|they)\s+(?:just\s+)?(?:used|tried|asked|got|had|ran|fed|prompted)\b", "first-hand use"),
    (r"\b(?:used|using|ran|tried|asked|prompted)\b[^.!?]{0,60}\b(?:to|for)\b", "used it to do something"),
    (r"\b(?:drafted|written|generated|summari[sz]ed|translated|built|created|cleaned|transcribed)\b[^.!?]{0,30}\b(?:with|using|by|via)\b", "output attributed to a tool"),
    (r"\bit (?:wrote|drafted|generated|produced|summari[sz]ed|suggested|gave me)\b", "describing the output"),
    (r"\bhelped me\b|\bsaved me\b|\bcut (?:it|that) down to\b", "reporting a benefit"),
    (r"\bmy (?:prompt|workflow)\b|\bthe prompt i\b", "describing the method"),
]

# What the tool was used for. A use case with no task is still a use case, so
# these only enrich the record; they never gate it.
TASKS = {
    "drafting": r"\bdraft|\bwrote\b|\bwriting\b|\brewrit|\bedit(?:ed|ing)?\b|\bproofread|\bcopy ?edit",
    "summarising": r"\bsummar|\brecap\b|\bdigest\b|\btl;?dr\b|\bkey points\b",
    "analysis": r"\banalys|\banalyz|\bspreadsheet\b|\bpivot\b|\bchart(?:ed|ing)?\b|\bclean(?:ed|ing)? (?:the )?data\b|\bregression\b",
    "coding": r"\bscript\b|\bpython\b|\bcode\b|\bautomat(?:e|ed|ion)\b|\bmacro\b|\bformula\b|\bAPI\b",
    "research": r"\bresearch\b|\blit(?:erature)? review\b|\blandscape\b|\bscan(?:ned|ning)?\b|\bfind sources\b",
    "meetings": r"\btranscri|\bmeeting notes\b|\bminutes\b|\baction items\b",
    "translation": r"\btranslat|\blocali[sz]",
    "fundraising": r"\bdonor\b|\bgrant\b|\bproposal\b|\bfunder\b|\bconcept note\b|\bLOI\b",
    "comms": r"\bnewsletter\b|\bsocial (?:post|media)\b|\bpress release\b|\bblog\b|\btalking points\b",
    "design": r"\bslide|\bdeck\b|\bimage\b|\blogo\b|\bgraphic\b|\bthumbnail\b",
}

# Text that mentions AI because it is selling it or reporting on it. These are
# the dominant false positive in a mailbox by a wide margin, so a hit here
# subtracts rather than merely failing to add.
NOISE = [
    (r"\bunsubscribe\b|\bmanage (?:your )?preferences\b|\bview (?:this|in) browser\b", "bulk mail"),
    (r"\bwebinar\b|\bregister (?:now|here|today)\b|\bsave your seat\b", "marketing"),
    (r"\bpress release\b|\bfor immediate release\b", "press release"),
    (r"\bthis week in\b|\bnewsletter\b|\bround-?up\b|\bdigest\b.{0,20}\bissue\b", "newsletter"),
    (r"\bhiring\b|\bjob (?:post|opening|description)\b|\bwe'?re recruiting\b", "recruiting"),
    (r"\bstock\b|\bshare price\b|\bfunding round\b|\bseries [a-d]\b|\bvaluation\b", "industry news"),
]

# Automated senders never describe their own use of a tool, whatever they say.
ROBOT_SENDER = re.compile(
    r"no-?reply|do-?not-?reply|notifications?@|alerts?@|mailer-daemon|"
    r"postmaster@|bounces?@|automated@", re.I)

# A bare newline ends a sentence here as surely as a full stop does: subject
# lines, chat messages and bulleted notes rarely carry terminal punctuation,
# and gluing one to the next line produces unreadable evidence.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Tuned against a hand-labelled sample so that review stays worth doing: below
# this, items are kept with their score but not proposed for the tracker.
THRESHOLD = 3.0


def _hits(text, patterns):
    """Matched (label) pairs for a list of (pattern, label), case-insensitive."""
    out = []
    for pat, label in patterns:
        # \bAI\b is the one pattern whose case carries meaning
        flags = 0 if pat == r"\bAI\b" else re.I
        if re.search(pat, text, flags):
            out.append(label)
    return out


def tools_in(text):
    """Every named AI product mentioned, in a stable order."""
    return [name for name, pat in TOOLS.items() if re.search(pat, text, re.I)]


def tasks_in(text):
    """What the text suggests the tool was used for."""
    return [name for name, pat in TASKS.items() if re.search(pat, text, re.I)]


def evidence_for(text, tools):
    """The one sentence a reviewer should read, verbatim.

    Prefers a sentence naming a tool and showing use over one that merely
    mentions AI, because that is the sentence that justifies the record. The
    text is returned exactly as written: a tracker entry that paraphrases its
    own evidence is worth nothing.
    """
    sentences = [s.strip() for s in SENTENCE_SPLIT.split(text or "") if s.strip()]
    best, best_rank = "", -1
    for s in sentences[:400]:
        rank = 0
        if tools and any(re.search(TOOLS[t], s, re.I) for t in tools):
            rank += 2
        if _hits(s, USE):
            rank += 2
        if _hits(s, GENERIC):
            rank += 1
        if rank > best_rank:
            best, best_rank = s, rank
    return best[:400] if best_rank > 0 else ""


def classify(text, sender="", subject=""):
    """Score a message or document and say why it scored that way.

    `sender` is used only to discard automated mail. `subject` is scored with
    the body because a subject line is where a short message often carries its
    only signal.
    """
    text = text or ""
    full = (subject + "\n" + text) if subject else text

    tools = tools_in(full)
    generic = _hits(full, GENERIC)
    use = _hits(full, USE)
    tasks = tasks_in(full)
    noise = _hits(full, NOISE)

    score, why = 0.0, []

    if tools:
        score += 3.0
        why.append("names " + ", ".join(tools[:3]))
    elif generic:
        # an unnamed mention is weak on its own: most of them are discussion
        score += 1.0
        why.append("mentions " + generic[0])

    if use:
        score += 3.0
        why.append(use[0])

    if tasks:
        score += 1.0
        why.append("task: " + ", ".join(tasks[:2]))

    # A tool plus a task plus evidence of use is the shape of a real use case,
    # and the combination is worth more than its parts.
    if tools and use and tasks:
        score += 2.0

    for n in noise:
        score -= 3.0
        why.append("looks like " + n)

    if sender and ROBOT_SENDER.search(sender):
        score -= 6.0
        why.append("automated sender")

    # Nothing about AI at all: not a near miss, just unrelated.
    if not tools and not generic:
        score = 0.0
        why = ["no AI mention"]

    evidence = evidence_for(full, tools) if score > 0 else ""

    return {
        "score": round(score, 2),
        "is_use_case": score >= THRESHOLD and bool(evidence),
        "tools": tools,
        "tasks": tasks,
        "evidence": evidence,
        "why": why[:4],
    }


def classify_file(path):
    """Classify a document already mirrored to markdown. Returns None if unread."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rec = classify(text, subject=Path(path).stem)
    rec["path"] = str(path)
    return rec
