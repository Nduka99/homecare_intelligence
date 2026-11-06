"""The continuity sweep's rule, on hand-worked numbers."""

from homecare.matching import sweep


def test_the_knee_is_where_doubling_stops_paying() -> None:
    # 1→2 cuts 40 to 20 (half); 2→4 cuts 20 to 16 (a fifth, under a quarter): m = 2.
    assert sweep.knee({1: 40, 2: 20, 4: 16, 8: 15}) == 2


def test_every_doubling_paying_gives_the_largest() -> None:
    assert sweep.knee({1: 40, 2: 20, 4: 10, 8: 5}) == 8


def test_exactly_a_quarter_still_pays() -> None:
    # 1→2 cuts 40 to 30, exactly a quarter, which is not "less than": go on to 2→4.
    assert sweep.knee({1: 40, 2: 30, 4: 29, 8: 29}) == 2


def test_no_breaks_left_stops_at_once() -> None:
    assert sweep.knee({1: 0, 2: 0, 4: 0, 8: 0}) == 1


def test_totals_add_the_days_and_count_problems() -> None:
    day = {measure: 1 for measure in sweep.MEASURES} | {"problems": ["x"]}
    assert sweep.totals([day, day]) == {measure: 2 for measure in sweep.MEASURES} | {"problems": 2}
