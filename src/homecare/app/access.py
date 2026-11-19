"""Which of their own records a carer may open, and when, as one pure function on plain data so
the rule can be read and tested on its own. The routes check that a record is the carer's; this
decides by its day.

A carer opens their own day sheet, note forms and notes for the demo clock's day. A note form
also opens for the day before, so a note written after midnight or an addition can still be
written; a Send is never limited by day, so a note kept on a phone without signal is never lost.
Any other day opens once the carer has given a reason that day ("Open another day"), which the
manager sees.
"""

SHEET = "sheet"
FORM = "form"
NOTE = "note"


def carer_may_open(kind: str, day: int, today: int, opened: set[int]) -> bool:
    """Whether a carer may open their own `kind` of record (SHEET, FORM or NOTE) from `day` when
    the clock's day is `today`; `opened` holds the days they opened with a reason today."""
    if day == today or day in opened:
        return True
    return kind == FORM and day == today - 1
