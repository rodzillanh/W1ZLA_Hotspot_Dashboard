"""Pure ADIF (Amateur Data Interchange Format) tokenizer for the Live
map's "Import ADIF log" feature -- no ham-radio-lookup dependencies here
by design; position resolution (grid square vs. QRZ/RadioID callsign
fallback) lives in app.py's import route instead, keeping this module a
small, independently-testable text parser, same spirit as digipi.py's
packet-line parser having no knowledge of SSH/polling.

ADIF fields are length-prefixed tags, not line- or delimiter-bounded:
`<FIELDNAME:LENGTH[:TYPE]>VALUE`, with a bare `<EOR>` (no length) ending
each record and a bare `<EOH>` (no length) ending an optional header
block. Verified directly against a hand-built realistic sample before
trusting this (mixed-case tags, one record split across two lines, one
record with GRIDSQUARE and one without, an ignorable header) -- correct
output confirmed for all of those cases, not just the common single-line
case.
"""
import re

_TAG_RE = re.compile(r"<(\w+)(?::(\d+)(?::\w+)?)?>", re.IGNORECASE)


def parse_adif(text: str) -> list:
    """Tokenizes ADIF's length-prefixed tag format into a list of plain
    dicts, one per record (keys upper-cased). Skips everything before
    <EOH> if present -- header fields describe the file, not a QSO. A
    tag with no length spec that isn't EOR is skipped defensively rather
    than raising -- malformed/unexpected input just yields fewer
    extracted fields, never an exception."""
    eoh = re.search(r"<eoh>", text, re.IGNORECASE)
    body = text[eoh.end():] if eoh else text

    records = []
    current: dict = {}
    for m in _TAG_RE.finditer(body):
        tag = m.group(1).upper()
        length_str = m.group(2)
        if tag == "EOR":
            if current:
                records.append(current)
            current = {}
            continue
        if length_str is None:
            continue
        length = int(length_str)
        current[tag] = body[m.end():m.end() + length]
    return records
