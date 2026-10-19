"""Conservative extraction warnings, not a guarantee of complete source content."""
import re

MORE_LABEL = re.compile(r"^\s*(?:(?:\u2026|\.{3})\s*more|(?:show|see)\s+more)\b", re.I)
TRUNCATION = re.compile(r"(?:\u2026|\.{3})\s*(?:(?:show|see)\s+)?more\s*$|(?:show|see)\s+more\s*$", re.I)


def assess_description(text, expansion_pending=False):
    if not isinstance(text, str) or not text.strip():
        return "missing"
    if expansion_pending or TRUNCATION.search(text.strip()):
        return "suspected_truncated"
    return "complete_candidate"


def annotate(record, expansion_pending=False):
    text = record.get("description", "")
    inferred = assess_description(text, expansion_pending)
    # Never upgrade a prior explicit extraction warning solely because its suffix is absent.
    status = record.get("description_status")
    if inferred != "complete_candidate" or status not in {"missing", "suspected_truncated"}:
        status = inferred
    record.update(description_status=status, description_length=len(text) if isinstance(text, str) else 0)
    return record
