"""Bundled Technician/General/Extra question pools for the License Quiz
card -- unlike every other module in this project, this is NOT a network
client. Each pool only changes once every ~4 years when NCVEC republishes
it, so all three are loaded once from static JSON files at startup rather
than polled.

technician_2026_2030.json, general_2023_2027.json, extra_2024_2028.json
(repo root, alongside this file -- deliberately NOT in a data/
subdirectory, which would collide with CONFIG_DIR's default of /app/data
and get shadowed by the Docker volume mount at runtime; see CLAUDE.md's
"License Quiz pool file location" gotcha) are each sourced from the
machine-readable export at
https://github.com/russolsen/ham_radio_question_pool (Apache-2.0), which
itself transcribes NCVEC's official public-domain releases. Each pool was
verified question-for-question against NCVEC's own current (most-recent-
errata) PDF before bundling, not trusted from the export alone or from
training-data recall:

- Technician (technician-2026-2030, effective 7/1/2026, supersedes the
  now-expired 2022-2026 cycle): 409 questions in the source export, 12
  reference an unbundled circuit-diagram figure and are excluded, leaving
  397. NCVEC issued a Feb 19, 2026 wording revision to 4 questions
  (T1C01, T5A05, T7A09, T0A10) -- confirmed word-for-word, including
  correct-answer letters, against NCVEC's own revised PDF that the
  source export already has the corrected text, not the original Dec
  2025 wording.
- General (general-2023-2027, effective 7/1/2023 - 6/30/2027): 432
  questions at original release; 9 have since been withdrawn across
  NCVEC's own 6 rounds of errata (most recent Feb 4, 2026 -- G1A04,
  G1C08, G1C09, G1C10, G1E09, G6B09, G8C01, G9C06, G9D13), leaving 423
  active. The full active-question-ID set was diffed against NCVEC's
  6th-errata PDF and matched exactly (zero missing, zero extra). 5 more
  reference an unbundled figure and are excluded, leaving 418.
- Extra (extra-2024-2028, effective 7/1/2024 - 6/30/2028): 603 questions
  at original release; NCVEC's own 4th errata (Feb 4, 2026) confirms 4
  have since been withdrawn (E2A13, E4D05, E6D07, E9E10), leaving the
  599 the source export already reflects -- this resolves what an
  earlier version of this docstring flagged as an "unresolved
  discrepancy" between the export's 599 and NCVEC's originally-cited
  603; both numbers were correct for different points in the errata
  history. 27 of the 599 reference a circuit-diagram figure and are
  excluded, leaving 572.

If any of these ever needs re-verifying or updating for the next pool
cycle, re-fetch from the GitHub export AND cross-check the active-
question-ID set against NCVEC's own current errata PDF the same way --
don't just trust the export snapshot or edit questions by hand.
"""
import json
import os
import random

_DIR = os.path.dirname(os.path.abspath(__file__))

# (json filename, subelement-code -> display-name map). Each class's own
# subelement codes are globally unique (T-/G-/E-prefixed), so a caller
# can't accidentally mix up which class a section code belongs to.
_POOL_DEFS = {
    "technician": ("technician_2026_2030.json", {
        "T0": "Safety",
        "T1": "Commission's Rules",
        "T2": "Operating Procedures",
        "T3": "Radio Wave Propagation",
        "T4": "Amateur Radio Practices",
        "T5": "Electrical Principles",
        "T6": "Electronic and Electrical Components",
        "T7": "Practical Circuits",
        "T8": "Signals and Emissions",
        "T9": "Antennas and Feed Lines",
    }),
    "general": ("general_2023_2027.json", {
        "G0": "Electrical and RF Safety",
        "G1": "Commission's Rules",
        "G2": "Operating Procedures",
        "G3": "Radio Wave Propagation",
        "G4": "Amateur Radio Practices",
        "G5": "Electrical Principles",
        "G6": "Circuit Components",
        "G7": "Practical Circuits",
        "G8": "Signals and Emissions",
        "G9": "Antennas and Feed Lines",
    }),
    # E0 (Safety) is a real graded subelement in the current pool, not a
    # bonus/appendix section.
    "extra": ("extra_2024_2028.json", {
        "E0": "Safety",
        "E1": "Commission's Rules",
        "E2": "Operating Procedures",
        "E3": "Radio Wave Propagation",
        "E4": "Amateur Radio Practices",
        "E5": "Electrical Principles",
        "E6": "Circuit Components",
        "E7": "Practical Circuits",
        "E8": "Signals and Emissions",
        "E9": "Antennas and Transmission Lines",
    }),
}

DEFAULT_CLASS = "extra"  # preserves pre-existing behavior for upgrades

# license_class -> {subelement_code: display_name}, e.g. SECTION_NAMES["extra"]["E0"] == "Safety"
SECTION_NAMES = {cls: names for cls, (_, names) in _POOL_DEFS.items()}


class LicenseQuizPool:
    """Loads all three bundled pools once at startup and serves random
    questions from whichever one is requested. No cache/TTL needed
    (unlike every network-backed client in this project) since the data
    never changes at runtime."""

    def __init__(self, directory: str = _DIR):
        self._questions = {}
        for license_class, (filename, _section_names) in _POOL_DEFS.items():
            path = os.path.join(directory, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._questions[license_class] = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._questions[license_class] = []

    def random_question(self, license_class: str = DEFAULT_CLASS) -> dict | None:
        questions = self._questions.get(license_class) or []
        if not questions:
            return None
        return random.choice(questions)

    def count(self, license_class: str = DEFAULT_CLASS) -> int:
        return len(self._questions.get(license_class) or [])
