#!/usr/bin/env python3
"""Load FOLIO dataset from temp/datasets/ and produce full_data_out.json."""

import json
import sys
from pathlib import Path

try:
    from loguru import logger
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
    logger.add("logs/data.log", rotation="30 MB", level="DEBUG")
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

WORKSPACE = Path("/ai-inventor/aii_data/runs/run_7jKlp9zHTIUI/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")
DATASETS_DIR = WORKSPACE / "temp" / "datasets"


def load_folio_split(split: str) -> list:
    path = DATASETS_DIR / f"full_tasksource_folio_default_{split}.json"
    data = json.loads(path.read_text())
    logger.info(f"Loaded {len(data)} rows from folio/{split}")
    return data


def normalize_label(label: str) -> str:
    mapping = {"True": "entailment", "False": "contradiction", "Uncertain": "neutral",
               "Entailment": "entailment", "Contradiction": "contradiction", "Neutral": "neutral"}
    return mapping.get(label, label.lower())


def make_folio_example(raw: dict, split: str, idx: int) -> dict:
    premises = raw.get("premises", "")
    if isinstance(premises, str):
        premises_list = [p.strip() for p in premises.split("\n") if p.strip()]
    else:
        premises_list = list(premises)

    conclusion = raw.get("conclusion", "")
    label = normalize_label(raw.get("label", ""))
    example_id = raw.get("example_id", idx)
    story_id = raw.get("story_id", "")

    # Input: all premises as numbered list + conclusion question
    premises_text = "\n".join(f"{i+1}. {p}" for i, p in enumerate(premises_list))
    input_text = f"Premises:\n{premises_text}\n\nConclusion: {conclusion}\n\nDoes the conclusion follow from the premises? (entailment/contradiction/neutral)"

    # Output: the label
    output_text = label

    # Metadata
    premises_fol = raw.get("premises-FOL", None)
    conclusion_fol = raw.get("conclusion-FOL", None)

    example = {
        "input": input_text,
        "output": output_text,
        "metadata_example_id": str(example_id),
        "metadata_story_id": str(story_id),
        "metadata_split": split,
        "metadata_row_index": idx,
        "metadata_task_type": "classification",
        "metadata_n_classes": 3,
        "metadata_conclusion": conclusion,
        "metadata_num_premises": len(premises_list),
    }

    if premises_fol:
        example["metadata_premises_fol"] = str(premises_fol)
    if conclusion_fol:
        example["metadata_conclusion_fol"] = str(conclusion_fol)

    return example


def main():
    logger.info("=== Building full_data_out.json from FOLIO ===")
    Path("logs").mkdir(exist_ok=True)

    val_raw = load_folio_split("validation")
    train_raw = load_folio_split("train")

    val_examples = [make_folio_example(r, "validation", i) for i, r in enumerate(val_raw)]
    train_examples = [make_folio_example(r, "train", i) for i, r in enumerate(train_raw)]

    all_examples = val_examples + train_examples
    logger.info(f"Total FOLIO examples: {len(all_examples)} ({len(val_examples)} val + {len(train_examples)} train)")

    output = {
        "datasets": [
            {
                "dataset": "folio",
                "examples": all_examples,
            }
        ]
    }

    out_path = WORKSPACE / "full_data_out.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    logger.info(f"Saved full_data_out.json with {len(all_examples)} examples")


if __name__ == "__main__":
    main()
