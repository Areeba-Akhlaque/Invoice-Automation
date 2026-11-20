"""EXECUTION — clean time-entry text before it is summarised.

A recurring meeting is not a deliverable. "Dev Team All Hands", "Pvragon
Coworking" and "Roman : Jaime 1:1" are the same every fortnight, so putting them
in the project cell tells the client nothing and makes two invoices look alike —
which is the opposite of the "fresh and accurate project list every invoice" that
was asked for.

So meeting-only text is stripped here, BEFORE the summariser sees it and before
the missing/repetitive check runs. That ordering is the point: someone whose whole
fortnight is recurring meetings now reads as having nothing substantive, gets
flagged, and their cell is left for review with a note on it — rather than being
described with a list of meeting names that happens to look plausible.

Two shapes of noise, both real:

  1. The whole entry is a meeting      "Dev Team All Hands"
                                       "Victor / Roman"
  2. Meetings prefix the real work     "Echo1 Office Hours; Dev Team All Hands;
                                        Pvragon Coworking. - Echo1 QA pipeline:
                                        finished the draft diagram ..."

Shape 2 must keep its tail, so entries are split into segments first and each
segment judged on its own.

Nothing here invents or rewrites work — it only drops text. If a segment is not
recognisably a meeting it is kept, because wrongly dropping real work is the more
expensive mistake.
"""
from __future__ import annotations

import re

# Entry-internal separators. Deliberately NOT a bare " - ": "Vic - Bradd 1:1" is
# one meeting, and splitting it would hide the pair from the name-pair rule below.
_SEGMENT_SPLIT = re.compile(r";|\||\.\s+-\s+")

# "Roman : Jaime", "Victor / Roman", "Vic - Bradd 1:1", "Dana/Jaime biweekly":
# two people and nothing else. Generic on purpose — a denylist of pairs would need
# a new line every time someone starts a 1:1.
_PAIR = re.compile(r"^(?P<left>[^/:+-]{1,40}?)\s*[/:+-]\s*(?P<right>.{1,40})$")

# Words that make a side of a pair still a pair: the cadence, not the subject.
_CADENCE = re.compile(
    r"\b(1:1|1-1|one[ -]?on[ -]?one|bi[ -]?weekly|weekly|monthly|daily|sync|"
    r"meeting|meetings|call|catch[ -]?up|check[ -]?in)\b"
)


# Pasted invitation boilerplate. A calendar entry carries whatever the organiser
# pasted in — "Let me know if this time doesn't work", a Zoom branding URL, Teams
# dial-in blocks — and this text goes to a client, so it is cut at the earliest
# marker found. Same list as the Apps Script that fills the calendar-sync sheet.
_BOILERPLATE = (
    "____",
    "let me know if this time",
    "microsoft teams",
    "join the meeting",
    "meeting id",
    "passcode",
    "dial in by phone",
    "find a local number",
    "join on a video conferencing device",
    "meeting options",
    "confidentiality notice",
    "this is a reminder",
    "https://",
    "http://",
)


def strip_boilerplate(text: str) -> str:
    """Cut an entry at the earliest piece of pasted invitation text."""
    s = str(text or "")
    cut = len(s)
    low = s.lower()
    for marker in _BOILERPLATE:
        i = low.find(marker)
        if i != -1 and i < cut:
            cut = i
    return s[:cut].strip(" \t-—–:;,.[(")


def _normalize(text: str) -> str:
    """Collapse whitespace, drop list bullets and trailing punctuation, lowercase.

    Trailing non-word characters go too, emoji included: "Pvragon Team Building 🎮"
    is the same standing meeting as "Pvragon Team Building", and matching on the
    exact string meant one of them slipped through.
    """
    s = " ".join(str(text or "").split())
    s = s.lstrip("-•*").strip()
    return re.sub(r"[^\w)\]]+$", "", s).strip().lower()


_JOINERS = {"and", "with", "the", "+", "/", ":", "-", "&", ","}


def strip_people(segment: str, names: set[str]) -> str:
    """Remove a leading run of colleagues' names from a segment.

    "JP:Bradd Crisis Review" is a meeting between two people ABOUT the crisis
    review, and only the subject belongs on an invoice. Stripping the names leaves
    "Crisis Review"; a segment that was nothing but names comes back empty and is
    then treated as noise.

    Only a LEADING run is removed, and only when a name was actually found, so
    "TIPS and Harris bids" (a client, not a colleague) is untouched.
    """
    # "JP:Bradd" is one token until the separators are spaced out, and an unspaced
    # pair was the common shape -- that is how "JP:Bradd Crisis Review" survived.
    words = re.sub(r"\s*([/:+&])\s*", r" \1 ", segment).split()
    i = found = 0
    while i < len(words):
        w = words[i].strip(":/+-&,").lower()
        if w in names:
            found += 1
            i += 1
        elif w in _JOINERS or not w:
            i += 1
        else:
            break
    if not found:
        return segment
    return " ".join(words[i:]).strip(" :/+-&,")


def _is_name_pair(segment: str) -> bool:
    """True for "A / B"-shaped meetings: both sides a name once the cadence is out.

    Two words a side, so "Roman : Jaime 1:1" and "Dana/Jaime biweekly" match while
    "JP:Bradd Crisis Review" and "Humana/RideCare claims discussion" do not — those
    name a subject, which is exactly what belongs on the invoice.
    """
    m = _PAIR.match(segment)
    if not m:
        return False
    sides = []
    for side in (m.group("left"), m.group("right")):
        stripped = _CADENCE.sub(" ", side)
        stripped = re.sub(r"[&+]", " ", stripped)
        words = [w for w in stripped.split() if w not in {"and", "with", "the"}]
        if not words or len(words) > 2:
            return False
        sides.append(words)
    return len(sides) == 2


def is_meeting_noise(
    segment: str, drop_patterns: list[re.Pattern], names: set[str] | None = None
) -> bool:
    """True if this segment is only a meeting, a cadence, names or time off."""
    s = _normalize(segment)
    if not s:
        return True
    if any(p.fullmatch(s) for p in drop_patterns):
        return True
    if _is_name_pair(s):
        return True
    # Names stripped, is there a subject left? "roman : jaime" leaves nothing.
    if names:
        rest = _normalize(strip_people(s, names))
        if not rest or any(p.fullmatch(rest) for p in drop_patterns) or _CADENCE.fullmatch(rest):
            return True
    return False


def scrub_meeting_mentions(
    description: str, phrases: list[re.Pattern], names: set[str] | None = None
) -> str:
    """Last line of defence: take meeting mentions out of a GENERATED description.

    Segment filtering cannot reach a meeting named inside a prose entry — "Daily
    support call and team meetings. Ticket scoping and triage..." is one segment —
    and the model puts it in the cell anyway however firmly the prompt says not to.
    So the rule is enforced on the way out: the phrase is cut, the item is dropped
    if nothing is left of it, and the line is reassembled.

    "Daily support calls and team meetings" -> "Daily support calls" (his support
    calls are the work); "Echo1 Office Hours" -> gone.
    """
    if not description:
        return description
    items, changed = [], False
    for raw in description.rstrip(".").split(","):
        item = raw.strip()
        for p in phrases:
            new = p.sub(" ", item)
            if new != item:
                changed = True
                item = new
        # The model echoes a title verbatim, names and all: "JP:Bradd Crisis
        # Review". Keep the subject, drop who attended.
        if names:
            stripped = strip_people(item, names)
            if stripped != item:
                changed = True
                item = stripped
        item = " ".join(item.split()).strip(" -&/:+")
        item = re.sub(r"^(and|with)\s+", "", item, flags=re.I).strip()
        item = re.sub(r"\s+(and|with)$", "", item, flags=re.I).strip()
        # A leftover like "and" or a bare cadence word is not a deliverable.
        if item and not _CADENCE.fullmatch(item.lower()) and len(item) > 2:
            items.append(item)
    if not changed:
        return description
    if not items:
        return ""
    if len(items) > 1 and not items[-1].lower().startswith("and "):
        items[-1] = f"and {items[-1]}"
    out = ", ".join(items).rstrip(".") + "."
    # Removing the first item can leave the line starting mid-sentence.
    return out[0].upper() + out[1:] if out else out


def compile_patterns(patterns: list[str] | None) -> list[re.Pattern]:
    """Compile the configured drop list once. An unparseable pattern is a config
    error worth failing on: silently ignoring it would quietly stop filtering."""
    out = []
    for raw in patterns or []:
        try:
            out.append(re.compile(raw, re.I))
        except re.error as e:
            raise RuntimeError(
                f"descriptions.drop_segments pattern {raw!r} in settings.yaml "
                f"is not a valid regular expression: {e}"
            ) from None
    return out


def clean_entry(
    entry: str, drop_patterns: list[re.Pattern], names: set[str] | None = None
) -> str:
    """One entry with pasted boilerplate cut and meeting-only segments removed.

    "" when nothing substantive is left, which is what makes the caller flag the
    person rather than describe them.
    """
    text = strip_boilerplate(entry)
    kept = []
    for seg in _SEGMENT_SPLIT.split(text):
        if not seg.strip() or is_meeting_noise(seg, drop_patterns, names):
            continue
        # Keep the subject, drop whoever attended: "JP:Bradd Crisis Review" is
        # about the crisis review, not about JP and Bradd.
        subject = strip_people(seg.strip(), names) if names else seg
        subject = subject.strip(" .;-")
        if subject and not is_meeting_noise(subject, drop_patterns, names):
            kept.append(subject)
    return ". ".join(k for k in kept if k)


def people_names(roster: list[dict], extra: list[str] | None = None) -> set[str]:
    """Colleagues' first names, for stripping out of entry text.

    Built from the roster rather than listed, so onboarding someone keeps working.
    `extra` covers people who appear in entries but are not billed on this invoice
    (Macie, Trever, Toni...), and is configured in settings.yaml.
    """
    names = set()
    for p in roster or []:
        for part in str(p.get("name") or "").split():
            if len(part) > 1:
                names.add(part.lower())
    names.update(n.strip().lower() for n in (extra or []) if n.strip())
    return names


def clean_entries(
    entries: list[str], drop_patterns: list[re.Pattern], names: set[str] | None = None
) -> tuple[list[str], int]:
    """(entries worth summarising, how many were dropped as meetings only).

    The count is reported in the run log so a person whose material shrank to
    nothing is visible rather than just quietly flagged.
    """
    kept, dropped = [], 0
    for e in entries:
        cleaned = clean_entry(e, drop_patterns, names)
        if cleaned:
            kept.append(cleaned)
        else:
            dropped += 1
    return kept, dropped
