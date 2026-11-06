"""The categories a finding can have, and the band each is routed to.

- The ten safeguarding types are paragraph 14.17 of the Care and Support Statutory Guidance
  (DHSC, chapter 14, Open Government Licence v3.0). 14.16 says the list is illustrative, hence
  "other safeguarding concern".
- "Possible deterioration (soft sign)" follows NHS England's description of soft signs
  (Enhanced Health in Care Homes framework, v4, section 11J); no trademarked tool is used.
- "Missed task" and "quality risk" are this project's own definitions.

The band follows from the category in code, never from the model.
"""

ACT_TODAY = "act today"
ACT_THIS_VISIT = "act this visit"
CHECK = "check"
NOTE = "note"
BANDS = (ACT_TODAY, ACT_THIS_VISIT, CHECK, NOTE)

SAFEGUARDING = {
    "physical abuse": "assault, hitting, slapping, pushing, misuse of medication, restraint or "
    "inappropriate physical sanctions",
    "domestic violence": "abuse by a partner or family member, including psychological, "
    "physical, sexual, financial or emotional abuse",
    "sexual abuse": "sexual acts the person has not consented to, inappropriate touching or "
    "looking, sexual harassment, indecent exposure or sexual photography",
    "psychological abuse": "threats of harm or abandonment, humiliation, blaming, controlling, "
    "intimidation, coercion, verbal abuse, isolation or being cut off from contact",
    "financial or material abuse": "theft, fraud, scams, pressure about money, wills or property, "
    "or misuse of the person's possessions or benefits",
    "modern slavery": "forced labour, domestic servitude or human trafficking",
    "discriminatory abuse": "harassment, slurs or worse treatment because of race, gender, age, "
    "disability, sexual orientation or religion",
    "organisational abuse": "poor care practice that comes from how the care service itself is "
    "run, such as routines that ignore people's needs or repeated poor practice by staff",
    "neglect and acts of omission": "ignoring medical, emotional or physical care needs, or "
    "withholding necessities such as medication, adequate food, drink or heating",
    "self-neglect": "the person neglecting their own hygiene, health or surroundings, including "
    "hoarding",
    "other safeguarding concern": "any other sign that the person is being harmed or exploited",
}
OTHERS = {
    "possible deterioration (soft sign)": "an observed change that may mean the person's health "
    "is getting worse, such as new confusion, more sleepy than usual, eating or drinking less, "
    "new breathlessness, new pain, a change in skin, or being less able to move than usual",
    "missed task": "a task in the care plan that was not done on this visit, such as medication, "
    "a meal or a drink, personal care or a check, without the person choosing to decline it",
    "quality risk": "a lapse in the quality of care that is not harm, such as a late or shortened "
    "visit, a missing or wrong record, faulty equipment, or not enough supplies",
}
MEANINGS = SAFEGUARDING | OTHERS
CATEGORIES = tuple(MEANINGS)


def band(category: str) -> str:
    if category in SAFEGUARDING:
        return ACT_TODAY
    if category == "possible deterioration (soft sign)":
        return ACT_THIS_VISIT
    if category == "missed task":
        return CHECK
    if category == "quality risk":
        return NOTE
    raise ValueError(f"unknown category: {category}")
