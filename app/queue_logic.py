"""Ordering of the jukebox queue. Pure functions, so the rules are easy to test."""
from collections import Counter


def order_queue(
    items: list[dict],
    *,
    fair_rotation: bool,
    allow_consecutive: bool,
    last_user_id: int | None = None,
) -> list[dict]:
    """Return queued items in play order.

    items: dicts with at least id, user_id, added_at, priority.
    last_user_id: who added the song playing / sent to Spotify last, so the
        "no consecutive" rule also applies across that boundary.

    Rules, in order:
      1. Items the admin moved to the top (priority > 0) go first, newest pin first.
      2. Fair rotation: each user's 1st song comes before anyone's 2nd song, and so on
         (round-robin by request time).
      3. No consecutive: avoid the same user twice in a row when another user's song
         is available.
    """
    by_time = sorted(items, key=lambda i: (i["added_at"], i["id"]))
    pinned = sorted((i for i in by_time if i["priority"] > 0), key=lambda i: -i["priority"])
    rest = [i for i in by_time if i["priority"] <= 0]

    if fair_rotation:
        seen: Counter = Counter()
        rounds = {}
        for item in rest:
            rounds[item["id"]] = seen[item["user_id"]]
            seen[item["user_id"]] += 1
        rest.sort(key=lambda i: (rounds[i["id"]], i["added_at"], i["id"]))

    if not allow_consecutive:
        last = pinned[-1]["user_id"] if pinned else last_user_id
        rest = _spread(rest, last)

    return pinned + rest


def _spread(items: list[dict], last_user_id: int | None) -> list[dict]:
    """Greedily pick the earliest item whose user differs from the previous one."""
    remaining = list(items)
    out = []
    while remaining:
        idx = next((k for k, it in enumerate(remaining) if it["user_id"] != last_user_id), 0)
        item = remaining.pop(idx)
        out.append(item)
        last_user_id = item["user_id"]
    return out
