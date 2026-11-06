from homecare.notes.quotes import find_quote, locate

NOTE = (
    "Helped Doreen wash.\nSaw finger-shaped bruises on both upper arms – she said “he gets rough”."
)


def test_finds_a_quote_across_a_line_break_and_typographic_marks() -> None:
    assert find_quote("wash. Saw finger-shaped bruises", NOTE) == "wash.\nSaw finger-shaped bruises"
    assert find_quote('she said "he gets rough"', NOTE) == "she said “he gets rough”"


def test_case_and_a_dropped_comma_do_not_matter() -> None:
    assert find_quote("BRUISES ON BOTH UPPER ARMS", NOTE) == "bruises on both upper arms"
    assert find_quote("Hello, world", "hello world") == "hello world"


def test_a_quote_that_is_not_in_the_note_is_not_found() -> None:
    assert find_quote("bruises on her legs", NOTE) is None
    assert find_quote("", NOTE) is None


def test_quotes_must_start_and_end_at_word_edges() -> None:
    assert find_quote("the rapist", "the therapist came") is None
    assert find_quote("3 to 6", "3 to 60 mm") is None


def test_locate_gives_the_span_in_the_original_text() -> None:
    span = locate("upper arms", NOTE)
    assert span is not None
    assert NOTE[span[0] : span[1]] == "upper arms"
