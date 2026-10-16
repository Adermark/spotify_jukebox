from app.queue_logic import order_queue


def item(id, user, t, priority=0):
    return {"id": id, "user_id": user, "added_at": t, "priority": priority}


def ids(items):
    return [i["id"] for i in items]


def test_plain_fifo():
    items = [item(1, "a", 1), item(2, "a", 2), item(3, "b", 3)]
    assert ids(order_queue(items, fair_rotation=False, allow_consecutive=True)) == [1, 2, 3]


def test_fair_rotation_round_robin():
    # A adds three songs, then B and C add one each: B and C jump ahead of A's 2nd and 3rd.
    items = [item(1, "a", 1), item(2, "a", 2), item(3, "a", 3), item(4, "b", 4), item(5, "c", 5)]
    assert ids(order_queue(items, fair_rotation=True, allow_consecutive=True)) == [1, 4, 5, 2, 3]


def test_no_consecutive_spreads_users():
    items = [item(1, "a", 1), item(2, "a", 2), item(3, "b", 3), item(4, "b", 4)]
    assert ids(order_queue(items, fair_rotation=False, allow_consecutive=False)) == [1, 3, 2, 4]


def test_no_consecutive_respects_last_played_user():
    items = [item(1, "a", 1), item(2, "b", 2)]
    assert ids(order_queue(items, fair_rotation=False, allow_consecutive=False, last_user_id="a")) == [2, 1]


def test_no_consecutive_falls_back_when_unavoidable():
    items = [item(1, "a", 1), item(2, "a", 2)]
    assert ids(order_queue(items, fair_rotation=False, allow_consecutive=False)) == [1, 2]


def test_pinned_items_go_first():
    items = [item(1, "a", 1), item(2, "b", 2), item(3, "c", 3, priority=1), item(4, "d", 4, priority=2)]
    assert ids(order_queue(items, fair_rotation=True, allow_consecutive=True)) == [4, 3, 1, 2]
