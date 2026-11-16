from homecare.notes.quotes import find_close_quote, find_quote, locate

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


# The spelling-tolerant match. Each text below was written by hand for its case.
TYPED = "Rqeuires a shower with two carers. Carers adminster insulin daily."
LISTED = (
    "Clinical tasks:\n- Insulin delegated by the nursing team.\n- Carers are trained to give it."
)


def test_a_corrected_typo_or_a_joined_list_is_found_in_the_text_s_own_words() -> None:
    assert find_quote("requires a shower with two carers", TYPED) is None
    assert find_close_quote("requires a shower with two carers", TYPED) == (
        "Rqeuires a shower with two carers"
    )
    assert find_close_quote("Carers administer insulin daily", TYPED) == (
        "Carers adminster insulin daily"
    )
    quote = "delegated by the nursing team. Carers are trained"
    assert find_quote(quote, LISTED) is None
    assert find_close_quote(quote, LISTED) == "delegated by the nursing team.\n- Carers are trained"


def test_a_changed_short_word_or_number_is_refused() -> None:
    assert find_close_quote("carers feed her lunch", "carers need her lunch") is None
    text = "Her medicine is due at 09:00 every morning."
    assert find_close_quote("medicine is due at 08:00 every morning", text) is None
    text = "an insulin dose of 10001 units daily"
    assert find_close_quote("an insulin dose of 10000 units daily", text) is None


def test_two_edits_in_a_word_or_two_loose_words_in_four_are_refused() -> None:
    assert find_close_quote("requires two carers daily", "reqiurse two carers daily") is None
    text = "adminster insulln every morning"
    assert find_close_quote("administer insulin every morning", text) is None


def test_a_skipped_spliced_borrowed_or_invented_quote_is_refused() -> None:
    assert (
        find_close_quote("does need insulin at breakfast", "does not need insulin at breakfast")
        is None
    )
    text = "Calls needed at breakfast, lunch and bed."
    assert find_close_quote("Calls needed at lunch", text) is None
    assert find_close_quote("hoisted for all transfers by two carers", TYPED) is None
    assert find_close_quote("not stated", TYPED) is None
    assert find_close_quote("", TYPED) is None
