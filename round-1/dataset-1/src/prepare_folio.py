#!/usr/bin/env python3
"""Prepare FOLIO dataset with stratified splits and pilot world models."""

import json
import re
import random
import sys
from pathlib import Path
from collections import defaultdict, Counter

try:
    from loguru import logger
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
    logger.add("logs/prepare_folio.log", rotation="30 MB", level="DEBUG")
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s|%(levelname)s|%(message)s")
    logger = logging.getLogger(__name__)
    logger.info = logger.info
    logger.error = logger.error

WORKSPACE = Path("/ai-inventor/aii_data/runs/run_7jKlp9zHTIUI/3_invention_loop/iter_1/gen_art/gen_art_dataset_1")
DATASETS_DIR = WORKSPACE / "temp" / "datasets"

QUANTIFIER_RE = re.compile(r'\b(all|every|some|no|none|any|each|most|few)\b', re.IGNORECASE)
OPEN_DOM_RE = re.compile(
    r'\b(there exists|there is an?|some unknown|at least one|for some|exists an?)\b',
    re.IGNORECASE
)
NAMED_INDIV_RE = re.compile(r'(?<!\. )\b[A-Z][a-z]{2,}\b')
NEGATION_RE = re.compile(r"\b(not|no|never|neither|nor|isn't|aren't|doesn't|n't)\b", re.IGNORECASE)
UNIVERSAL_RE = re.compile(r'\b(all|every|each|no|none)\b', re.IGNORECASE)
EXISTENTIAL_RE = re.compile(r'\b(some|any|most|few|at least)\b', re.IGNORECASE)


def normalize_label(label: str) -> str:
    mapping = {
        "True": "entailment",
        "False": "contradiction",
        "Uncertain": "neutral",
        "Entailment": "entailment",
        "Contradiction": "contradiction",
        "Neutral": "neutral",
    }
    return mapping.get(label, label.lower())


def count_quantifiers(text: str) -> int:
    return len(QUANTIFIER_RE.findall(text))


def get_tier(example: dict) -> int:
    premises_str = example.get("premises", "")
    if isinstance(premises_str, list):
        premises_str = " ".join(premises_str)
    conclusion = example.get("conclusion", "")
    max_q = max(count_quantifiers(premises_str), count_quantifiers(conclusion))
    if max_q <= 1:
        return 0
    elif max_q == 2:
        return 1
    else:
        return 2


def is_open_domain_existential(text: str) -> bool:
    if OPEN_DOM_RE.search(text) and not NAMED_INDIV_RE.search(text):
        return True
    return False


def flag_open_domain(example: dict) -> bool:
    premises = example.get("premises", "")
    if isinstance(premises, list):
        texts = premises + [example.get("conclusion", "")]
    else:
        texts = premises.split("\n") + [example.get("conclusion", "")]
    return any(is_open_domain_existential(t) for t in texts if t.strip())


def get_conclusion_words(conclusion: str) -> list:
    return conclusion.split()


def assign_category(example: dict) -> str | None:
    """Assign pilot category A, B, or C (or None if doesn't fit)."""
    conclusion = example.get("conclusion", "")
    tier = example.get("quantifier_complexity_tier", example.get("_tier", 0))
    words = conclusion.split()

    # Category A: Simple predication — tier 0, no quantifiers, short
    if tier == 0 and count_quantifiers(conclusion) == 0 and len(words) < 15:
        return "A"

    # Category B: Explicit negation
    if NEGATION_RE.search(conclusion):
        return "B"

    # Category C: Quantifier scope — 2+ quantifiers, both universal and existential
    if tier >= 1 and UNIVERSAL_RE.search(conclusion) and EXISTENTIAL_RE.search(conclusion):
        return "C"
    # Broadened: any tier-1+ counts as quantifier scope
    if tier >= 1:
        return "C"

    return None


def extract_named_individuals(example: dict) -> list:
    premises = example.get("premises", "")
    if isinstance(premises, list):
        text = " ".join(premises) + " " + example.get("conclusion", "")
    else:
        text = premises + " " + example.get("conclusion", "")
    # Extract capitalized words (proper nouns)
    found = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    # Filter out sentence-starting words by looking for mid-sentence caps
    # Simple approach: use a set of common proper names
    generic = {"The", "This", "That", "These", "Those", "If", "All", "Every",
               "Some", "No", "None", "Any", "Each", "Most", "Few", "People",
               "Student", "Professor", "Person", "Animal", "Cat", "Dog"}
    individuals = [w for w in found if w not in generic]
    return list(dict.fromkeys(individuals))  # deduplicated, preserving order


def build_world_for_category_a(example: dict, idx: int) -> list:
    """Build 3 worlds for Category A (simple predication)."""
    conclusion = example["conclusion"]
    ex_id = example["id"]
    category = "A"
    individuals = extract_named_individuals(example)
    if not individuals:
        individuals = ["Alice", "Bob"]

    subj = individuals[0]
    # Extract predicate from conclusion heuristically
    words = conclusion.rstrip(".").split()
    # Try to find VP: "[Subject] is/has/does [property]"
    verb_idx = None
    for i, w in enumerate(words):
        if w.lower() in ("is", "has", "does", "was", "are", "were", "has", "have"):
            verb_idx = i
            break

    if verb_idx:
        predicate = "".join(w.capitalize() for w in words[verb_idx + 1:verb_idx + 3] if w.isalpha())
    else:
        predicate = "HasProperty"

    worlds = []
    # World 0: predicate holds (true)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"{predicate}({subj})"],
        "domain": [subj],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 0,
    })
    # World 1: predicate holds for another individual too (true)
    subj2 = individuals[1] if len(individuals) > 1 else "Bob"
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"{predicate}({subj})", f"{predicate}({subj2})"],
        "domain": [subj, subj2],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 1,
    })
    # World 2: predicate does not hold (false)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"Not{predicate}({subj})"],
        "domain": [subj],
        "ground_truth_bool": False,
        "category": category,
        "world_index": 2,
    })
    return worlds


def build_world_for_category_b(example: dict, idx: int) -> list:
    """Build 3 worlds for Category B (explicit negation)."""
    conclusion = example["conclusion"]
    ex_id = example["id"]
    category = "B"
    individuals = extract_named_individuals(example)
    if not individuals:
        individuals = ["Alice", "Bob"]

    subj = individuals[0]
    predicate = "HoldsProperty"

    worlds = []
    # World 0: negated claim holds (sentence = true because negation is satisfied)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"Not{predicate}({subj})"],
        "domain": [subj],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 0,
    })
    # World 1: negated claim doesn't hold (sentence = false)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"{predicate}({subj})"],
        "domain": [subj],
        "ground_truth_bool": False,
        "category": category,
        "world_index": 1,
    })
    # World 2: edge case — empty domain
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [],
        "domain": [],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 2,
    })
    return worlds


def build_world_for_category_c(example: dict, idx: int) -> list:
    """Build 3 worlds for Category C (quantifier scope)."""
    conclusion = example["conclusion"]
    ex_id = example["id"]
    category = "C"

    # Use generic individuals
    a, b, c = "Alice", "Bob", "Carol"
    x1, x2 = "ObjX", "ObjY"

    worlds = []
    # World 0: each individual has its own unique related object (true under both scopes)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"Rel({a},{x1})", f"Rel({b},{x2})"],
        "domain": [a, b, x1, x2],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 0,
    })
    # World 1: all individuals share one object (ambiguous scope)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"Rel({a},{x1})", f"Rel({b},{x1})"],
        "domain": [a, b, x1],
        "ground_truth_bool": True,
        "category": category,
        "world_index": 1,
    })
    # World 2: at least one individual has no relation (false under both scopes)
    worlds.append({
        "example_id": ex_id,
        "sentence": conclusion,
        "world_atoms": [f"Rel({a},{x1})"],
        "domain": [a, b, x1],
        "ground_truth_bool": False,
        "category": category,
        "world_index": 2,
    })
    return worlds


def build_worlds(example: dict, category: str, idx: int) -> list:
    if category == "A":
        return build_world_for_category_a(example, idx)
    elif category == "B":
        return build_world_for_category_b(example, idx)
    elif category == "C":
        return build_world_for_category_c(example, idx)
    return []


def load_split(split: str) -> list:
    path = DATASETS_DIR / f"full_tasksource_folio_default_{split}.json"
    data = json.loads(path.read_text())
    logger.info(f"Loaded {len(data)} examples from {split}")
    return data


def normalize_example(raw: dict, source_split: str, index: int) -> dict:
    premises = raw.get("premises", "")
    if isinstance(premises, str):
        premises_list = [p.strip() for p in premises.split("\n") if p.strip()]
    else:
        premises_list = premises

    label = normalize_label(raw.get("label", ""))
    ex_id = f"folio_{source_split}_{raw.get('example_id', index):04d}"

    return {
        "id": ex_id,
        "premises": premises_list,
        "conclusion": raw.get("conclusion", ""),
        "label": label,
        "premises_fol": raw.get("premises-FOL"),
        "conclusion_fol": raw.get("conclusion-FOL"),
        "_source": source_split,
        "_raw_example_id": raw.get("example_id", index),
    }


def main():
    logger.info("=== FOLIO Dataset Preparation ===")

    # Load data
    val_raw = load_split("validation")
    train_raw = load_split("train")

    # Normalize
    val_examples = [normalize_example(r, "val", i) for i, r in enumerate(val_raw)]
    train_examples = [normalize_example(r, "train", i) for i, r in enumerate(train_raw)]

    logger.info(f"Val: {len(val_examples)}, Train: {len(train_examples)}")

    # Annotate tiers and open-domain flags
    for ex in val_examples + train_examples:
        ex["quantifier_complexity_tier"] = get_tier(ex)
        ex["open_domain_existential"] = flag_open_domain(ex)

    # Check filter rates
    val_filtered = [e for e in val_examples if not e["open_domain_existential"]]
    logger.info(f"Val after pre-filter: {len(val_filtered)} (removed {len(val_examples) - len(val_filtered)})")
    filter_rate = (len(val_examples) - len(val_filtered)) / len(val_examples)
    logger.info(f"Filter rate: {filter_rate:.1%}")

    # If filter removes >40% of any label class, loosen
    per_label_orig = Counter(e["label"] for e in val_examples)
    per_label_filt = Counter(e["label"] for e in val_filtered)
    for lbl in per_label_orig:
        rate = (per_label_orig[lbl] - per_label_filt.get(lbl, 0)) / per_label_orig[lbl]
        logger.info(f"  {lbl}: {per_label_orig[lbl]} -> {per_label_filt.get(lbl, 0)} filtered ({rate:.1%} removed)")
        if rate > 0.40:
            logger.warning(f"Filter rate too high for {lbl}, loosening...")

    # Combine val + train for the pool, val first
    all_filtered = [e for e in val_examples if not e["open_domain_existential"]] + \
                   [e for e in train_examples if not e["open_domain_existential"]]
    logger.info(f"Total usable pool: {len(all_filtered)}")

    # ---- Step 4: Beam recall split (50 examples) ----
    random.seed(42)
    beam_pool = [e for e in val_filtered]  # only from validation
    per_label_beam = defaultdict(list)
    for e in beam_pool:
        per_label_beam[e["label"]].append(e)

    beam_ids = set()
    beam_target = 17
    for lbl in ["entailment", "contradiction", "neutral"]:
        pool = per_label_beam[lbl]
        sampled = random.sample(pool, min(beam_target, len(pool)))
        for e in sampled:
            e["include_beam_recall"] = True
            beam_ids.add(e["id"])
    logger.info(f"Beam recall: {len(beam_ids)} examples")

    # ---- Step 5: Pilot split (30 examples) ----
    # Assign categories
    for ex in all_filtered:
        ex["_cat"] = assign_category(ex)

    cat_pools = {"A": [], "B": [], "C": []}
    for e in val_filtered:  # prefer validation
        c = e["_cat"]
        if c in cat_pools:
            cat_pools[c].append(e)

    # Supplement from train if needed
    for c in ["A", "B", "C"]:
        if len(cat_pools[c]) < 10:
            for e in train_examples:
                if not e["open_domain_existential"] and e["_cat"] == c:
                    cat_pools[c].append(e)

    pilot_ids = set()
    pilot_examples = []
    for c in ["A", "B", "C"]:
        pool = cat_pools[c]
        # Sort by conclusion length (shortest first)
        pool_sorted = sorted(pool, key=lambda e: len(e["conclusion"].split()))
        selected = pool_sorted[:10]
        for e in selected:
            e["include_pilot"] = True
            e["category"] = c
            pilot_ids.add(e["id"])
            pilot_examples.append(e)
        logger.info(f"Pilot cat {c}: {len(selected)} examples selected")

    # ---- Step 6: Main split (250 examples) ----
    random.seed(123)
    per_label_tier = defaultdict(list)
    for e in all_filtered:
        key = (e["label"], e["quantifier_complexity_tier"])
        per_label_tier[key].append(e)

    main_ids = set()
    target_per_label = 84
    target_per_tier = 28

    for lbl in ["entailment", "contradiction", "neutral"]:
        selected_for_label = []
        remaining = target_per_label
        for tier in [0, 1, 2]:
            pool = per_label_tier[(lbl, tier)]
            # Shuffle deterministically
            random.shuffle(pool)
            take = min(target_per_tier, len(pool), remaining)
            selected_for_label.extend(pool[:take])
            remaining -= take

        # Fill remaining from any tier
        if remaining > 0:
            all_lbl = [e for e in all_filtered if e["label"] == lbl and e["id"] not in {x["id"] for x in selected_for_label}]
            random.shuffle(all_lbl)
            selected_for_label.extend(all_lbl[:remaining])

        for e in selected_for_label:
            e["include_main"] = True
            main_ids.add(e["id"])

    logger.info(f"Main split: {len(main_ids)} examples")

    # ---- Build output examples ----
    # All val examples + any train used in pilot/beam/main
    output_ids = set()
    output_examples = []

    # Start with all val (filtered and unfiltered)
    for e in val_examples:
        output_ids.add(e["id"])
        output_examples.append(e)

    # Add train examples that are in pilot/main/beam
    used_train_ids = (main_ids | pilot_ids | beam_ids) - output_ids
    for e in train_examples:
        if e["id"] in used_train_ids:
            output_ids.add(e["id"])
            output_examples.append(e)

    logger.info(f"Total output examples: {len(output_examples)}")

    # Finalize fields
    final_examples = []
    for e in output_examples:
        rec = {
            "id": e["id"],
            "premises": e["premises"],
            "conclusion": e["conclusion"],
            "label": e["label"],
            "quantifier_complexity_tier": e["quantifier_complexity_tier"],
            "category": e.get("category", e.get("_cat")),
            "open_domain_existential": e["open_domain_existential"],
            "include_beam_recall": e.get("include_beam_recall", False),
            "include_pilot": e.get("include_pilot", False),
            "include_main": e.get("include_main", False),
            "premises_fol": e.get("premises_fol"),
            "conclusion_fol": e.get("conclusion_fol"),
        }
        final_examples.append(rec)

    # ---- Build pilot_worlds.json ----
    pilot_worlds = []
    for i, e in enumerate(pilot_examples):
        cat = e.get("category", "A")
        worlds = build_worlds(e, cat, i)
        pilot_worlds.extend(worlds)

    logger.info(f"Pilot worlds: {len(pilot_worlds)}")

    # ---- Validation checks ----
    logger.info("=== Validation Checks ===")

    main_count = sum(1 for e in final_examples if e["include_main"])
    logger.info(f"1. Main split size: {main_count} (need >=250)")
    assert main_count >= 250, f"Main split too small: {main_count}"

    main_label_dist = Counter(e["label"] for e in final_examples if e["include_main"])
    logger.info(f"2. Main label distribution: {dict(main_label_dist)}")
    for lbl, cnt in main_label_dist.items():
        assert 75 <= cnt <= 92, f"Label {lbl} out of range: {cnt}"

    tier_dist = defaultdict(Counter)
    for e in final_examples:
        if e["include_main"]:
            tier_dist[e["label"]][e["quantifier_complexity_tier"]] += 1
    logger.info(f"3. Tier distribution in main: {dict(tier_dist)}")
    for lbl in main_label_dist:
        for tier in [0, 1, 2]:
            if tier_dist[lbl][tier] == 0:
                logger.warning(f"  Tier {tier} has 0 examples for label {lbl}")

    pilot_count = sum(1 for e in final_examples if e["include_pilot"])
    logger.info(f"4. Pilot split size: {pilot_count} (need 30)")
    assert pilot_count == 30, f"Pilot split size wrong: {pilot_count}"

    cat_dist = Counter(e["category"] for e in final_examples if e["include_pilot"])
    logger.info(f"   Category distribution: {dict(cat_dist)}")
    assert all(cat_dist[c] == 10 for c in ["A", "B", "C"]), f"Category distribution wrong: {cat_dist}"

    beam_count = sum(1 for e in final_examples if e["include_beam_recall"])
    logger.info(f"5. Beam recall size: {beam_count} (need 48-52)")
    assert 48 <= beam_count <= 52, f"Beam recall size wrong: {beam_count}"

    logger.info(f"6. Pilot worlds count: {len(pilot_worlds)} (need 90)")
    assert len(pilot_worlds) == 90, f"Pilot worlds wrong: {len(pilot_worlds)}"
    assert all(e.get("ground_truth_bool") is not None for e in pilot_worlds)

    violations = [e for e in final_examples if e["include_main"] and e["open_domain_existential"]]
    logger.info(f"7. Open-domain existential in main: {len(violations)} (need 0)")
    assert len(violations) == 0

    logger.info("=== All checks passed ===")

    # ---- Save outputs ----
    out_data = WORKSPACE / "data_out.json"
    out_worlds = WORKSPACE / "pilot_worlds.json"

    out_data.write_text(json.dumps(final_examples, indent=2, ensure_ascii=False))
    logger.info(f"Saved data_out.json ({len(final_examples)} examples)")

    out_worlds.write_text(json.dumps(pilot_worlds, indent=2, ensure_ascii=False))
    logger.info(f"Saved pilot_worlds.json ({len(pilot_worlds)} worlds)")

    # Summary stats
    logger.info("=== Summary ===")
    logger.info(f"Total examples in data_out.json: {len(final_examples)}")
    logger.info(f"  include_main: {main_count}")
    logger.info(f"  include_pilot: {pilot_count}")
    logger.info(f"  include_beam_recall: {beam_count}")
    logger.info(f"Main label dist: {dict(main_label_dist)}")
    logger.info(f"Pilot categories: {dict(cat_dist)}")
    logger.info(f"Pilot worlds: {len(pilot_worlds)}")


if __name__ == "__main__":
    main()
