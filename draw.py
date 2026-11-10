"""Verifiable seeded draw for DropStack.

Algorithm (documented for audit):
  1. At draw time the server generates seed = secrets.token_hex(32) (CSPRNG)
     and stores it in the draws table BEFORE any selection logic.
  2. Candidate list = sorted ascending list of entry IDs for the drop
     (stored as a JSON snapshot in draws.entry_ids).
  3. Full Fisher-Yates (Knuth) shuffle of the candidate list using
     Python's random.Random(seed) — Mersenne Twister, randrange-based.
  4. The first `quantity` shuffled IDs are winners; the next
     `DRAW_ALTERNATES` IDs are alternates, in order.
  5. Anyone can re-verify: re-run perform_draw(seed, entry_ids) and
     compare against draws.winners / draws.alternates. The
     /drops/<id>/verify-draw endpoint does this automatically.

Determinism holds for CPython's random module (stable across versions
for seed-based Random + randrange shuffles). Record the Python version
used for the draw in the audit log.
"""
import random
from typing import List, Tuple, Optional


def perform_draw(seed: str, entry_ids: List[int], quantity: int = 1,
                 alternates: int = 3) -> Tuple[Optional[int], List[int], List[int]]:
    ids = sorted(int(i) for i in entry_ids)
    rng = random.Random(seed)
    for i in range(len(ids) - 1, 0, -1):
        j = rng.randrange(i + 1)
        ids[i], ids[j] = ids[j], ids[i]
    if not ids:
        return None, [], []
    q = max(1, int(quantity))
    winners = ids[:q]
    alts = ids[q:q + max(0, int(alternates))]
    return (winners[0] if len(winners) == 1 else None), winners, alts


def verify(seed: str, entry_ids: List[int], quantity: int,
           alternates: int, expected_winners: List[int],
           expected_alts: List[int]) -> bool:
    _, winners, alts = perform_draw(seed, entry_ids, quantity, alternates)
    return winners == list(expected_winners) and alts == list(expected_alts)
