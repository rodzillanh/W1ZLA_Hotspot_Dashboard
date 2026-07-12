"""Bundled Amateur Extra (Element 4) question pool for the License Quiz
card -- unlike every other module in this project, this is NOT a network
client. The pool only changes once every ~4 years when NCVEC republishes
it, so it's loaded once from a static JSON file at startup rather than
polled.

data/extra_2024_2028.json is the current 2024-2028 pool (effective July 1,
2024 - June 30, 2028), sourced from the machine-readable export at
https://github.com/russolsen/ham_radio_question_pool (Apache-2.0), which
itself transcribes NCVEC's official public-domain release
(https://ncvec.org/index.php/2024-2028-extra-class-question-pool-release).
That export's extra-2024-2028.json has 599 questions; NCVEC's own release
notes cite 603 for this pool cycle -- a small, unresolved discrepancy
between snapshots, not something guessed or typed from memory. 27 of the
599 reference a circuit diagram figure ("figure" field) that isn't bundled
here, and are excluded, leaving 572. If this ever needs re-verifying or
updating for the next pool cycle, re-fetch from one of the two sources
above rather than editing questions by hand.
"""
import json
import os
import random

_POOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "extra_2024_2028.json")

# The pool's own subelement codes (first two characters of each question
# id, e.g. "E5" from "E5A01") -- stable across pool cycles even though the
# questions themselves change, so safe to hardcode. E0 (Safety) is a real
# graded subelement in the current pool, not a bonus/appendix section.
SECTION_NAMES = {
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
}


class LicenseQuizPool:
    """Loads the bundled pool once at startup and serves random questions.
    No cache/TTL needed (unlike every network-backed client in this
    project) since the data never changes at runtime."""

    def __init__(self, path: str = _POOL_PATH):
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._questions = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._questions = []

    def random_question(self) -> dict | None:
        if not self._questions:
            return None
        return random.choice(self._questions)

    def count(self) -> int:
        return len(self._questions)
