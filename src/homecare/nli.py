"""Entailment probabilities from the pinned NLI checkpoint, on the CPU, for the queue-ranking
study.

Laptop only: torch and transformers are in the `rank` group, which the serving image and CI do
not install, so they are imported inside `entailment`. Every file of the checkpoint is checked
against its pin before it loads, and two known pairs are scored first: if the obvious
entailment does not win, or the obvious contradiction does, the run stops. That also catches a
wrong label order.
"""

from homecare import models

NAME = "deberta-v3-large-nli"
BATCH = 8
SANITY = [
    ("The man is asleep in his bed.", "A person is sleeping.", True),
    ("The man is asleep in his bed.", "The man is running a marathon.", False),
]


def entailment(pairs: list[tuple[str, str]]) -> list[float]:
    """P(entailment) for each (premise, hypothesis) pair."""
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    item = models.entry(NAME)
    found = models.problems([item], models.FOLDER, quick=False)
    if found:
        raise SystemExit("; ".join(found))
    folder = models.FOLDER / item["folder"]
    tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
    model = AutoModelForSequenceClassification.from_pretrained(folder, local_files_only=True)
    model.eval()
    label = model.config.label2id["entailment"]

    def score(batch: list[tuple[str, str]]) -> list[float]:
        inputs = tokenizer(
            [p for p, _ in batch], [h for _, h in batch], truncation=True, padding=True,
            return_tensors="pt",
        )  # fmt: skip
        with torch.inference_mode():
            probabilities = model(**inputs).logits.softmax(dim=-1)
        return [float(row[label]) for row in probabilities]

    sanity = score([(premise, hypothesis) for premise, hypothesis, _ in SANITY])
    for (premise, hypothesis, entails), value in zip(SANITY, sanity, strict=True):
        if (value > 0.5) != entails:
            raise SystemExit(f"NLI sanity pair failed ({value:.2f}): {premise!r} => {hypothesis!r}")
    values: list[float] = []
    for start in range(0, len(pairs), BATCH):
        values.extend(score(pairs[start : start + BATCH]))
    return values
