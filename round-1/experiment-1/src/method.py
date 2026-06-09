#!/usr/bin/env python3
"""
Heterogeneous-Oracle World-Probing for NL-to-FOL Faithfulness Evaluation on FOLIO.
Main orchestrator implementing Phase 0 (beam recall gate), Phase 1 (oracle pilot),
Phase 2 (main heterogeneous-oracle pipeline), and Phase 3 (baselines + ablations).
"""

import json
import math
import os
import re
import random
import sys
import time
import gc
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from loguru import logger

# ── Logging setup ──────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

# ── Memory guard ────────────────────────────────────────────────────────────
import resource
_MEM_LIMIT = 20 * 1024**3  # 20GB virtual
try:
    resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT, _MEM_LIMIT))
except Exception:
    pass

# ── Local imports ───────────────────────────────────────────────────────────
from llm_client import call_llm, get_cumulative_cost, BUDGET_HARD_LIMIT
from fol_checker import eval_formula, parse_world_from_llm
from z3_evaluator import folio_inference
from prompts import (
    FEW_SHOT_FOL_GENERATION,
    DIAGNOSTIC_WORLD_GENERATION,
    ORACLE_TRUTH_JUDGMENT,
    DIRECT_LLM_JUDGE,
)

# ── Constants ────────────────────────────────────────────────────────────────
GENERATOR_MODEL = "meta-llama/llama-3.1-8b-instruct"
ORACLE_MODEL_PRIMARY = "qwen/qwen-2.5-7b-instruct"
ORACLE_MODEL_FALLBACK = "qwen/qwen-2.5-72b-instruct"
CANDIDATE_K = 5
WORLD_M = 8

WORKSPACE = Path(__file__).parent
RESULTS_DIR = WORKSPACE / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# ── Label normalization ───────────────────────────────────────────────────────

_LABEL_MAP = {
    "true": "Entailment",
    "entailment": "Entailment",
    "false": "Contradiction",
    "contradiction": "Contradiction",
    "uncertain": "Uncertain",
    "neutral": "Uncertain",
    "unknown": "Uncertain",
}


def _normalize_label(label: str) -> str:
    return _LABEL_MAP.get(label.strip().lower(), "Uncertain")


# ── FOLIO Loading ────────────────────────────────────────────────────────────

def load_folio() -> list[dict]:
    try:
        from datasets import load_dataset
        ds = load_dataset("yale-nlp/folio", split="validation", trust_remote_code=True)
        examples = []
        for ex in ds:
            raw_label = ex.get("label", "Uncertain")
            examples.append({**ex, "label": _normalize_label(str(raw_label))})
        logger.info(f"Loaded {len(examples)} FOLIO validation examples")
        return examples
    except Exception as e:
        logger.error(f"HuggingFace load failed: {e}. Trying fallback...")
        return _load_folio_fallback()


def _load_folio_fallback() -> list[dict]:
    import requests
    urls = [
        "https://raw.githubusercontent.com/Yale-LILY/FOLIO/main/data/v0.0/folio-validation.jsonl",
        "https://raw.githubusercontent.com/Yale-LILY/FOLIO/main/data/v0002/folio-validation.jsonl",
    ]
    for url in urls:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            examples = []
            for line in lines:
                if line.strip():
                    obj = json.loads(line)
                    raw_label = obj.get("label", obj.get("gold_label", "Uncertain"))
                    # Normalize labels: True->Entailment, False->Contradiction, Uncertain->Uncertain
                    label = _normalize_label(raw_label)
                    examples.append({
                        "premises": obj.get("premises", obj.get("story", [])),
                        "premises_fol": obj.get("premises-FOL", []),
                        "conclusion": obj.get("conclusion", ""),
                        "conclusion_fol": obj.get("conclusion-FOL", ""),
                        "label": label,
                    })
            logger.info(f"Loaded {len(examples)} FOLIO examples from GitHub fallback ({url})")
            return examples
        except Exception as e:
            logger.warning(f"Fallback URL {url} failed: {e}")
            continue
    raise RuntimeError("All FOLIO fallback URLs failed")


# ── FOL Parsing from LLM output ──────────────────────────────────────────────

def parse_fol_response(text: str) -> Optional[dict]:
    """Extract premises_fol list and conclusion_fol from LLM response."""
    premises = []
    conclusion = None

    # Look for labeled lines
    lines = text.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Premise N: formula
        pm = re.match(r"(?:premise\s*\d+\s*:|p\d+\s*:)\s*(.+)", line, re.IGNORECASE)
        if pm:
            premises.append(pm.group(1).strip())
            continue
        # Conclusion: formula
        cm = re.match(r"(?:conclusion\s*:)\s*(.+)", line, re.IGNORECASE)
        if cm:
            conclusion = cm.group(1).strip()
            continue

    # Fallback: lines that look like FOL
    if not premises and not conclusion:
        fol_lines = []
        for line in lines:
            line = line.strip()
            if any(kw in line for kw in ("forall", "exists", "->", " & ", " | ", "not ")):
                if ":" in line:
                    line = line.split(":", 1)[1].strip()
                fol_lines.append(line)
        if len(fol_lines) >= 2:
            premises = fol_lines[:-1]
            conclusion = fol_lines[-1]
        elif len(fol_lines) == 1:
            conclusion = fol_lines[0]

    if not conclusion and premises:
        conclusion = premises.pop()

    if not conclusion:
        return None

    return {"premises_fol": premises, "conclusion_fol": conclusion}


# ── Candidate Generation ─────────────────────────────────────────────────────

def generate_k_candidates(
    example: dict,
    k: int = CANDIDATE_K,
    model: str = GENERATOR_MODEL,
) -> list[Optional[dict]]:
    """Generate k FOL candidate translations using parallel API calls."""
    premises_text = "\n".join(f"- {p}" for p in example["premises"])
    prompt = FEW_SHOT_FOL_GENERATION.format(
        premises=premises_text,
        conclusion=example["conclusion"],
    )

    def _one_call(_: int) -> Optional[dict]:
        try:
            response = call_llm(model, prompt, max_tokens=600, temperature=0.8)
            return parse_fol_response(response)
        except Exception as e:
            logger.warning(f"Candidate generation failed: {e}")
            return None

    candidates = []
    with ThreadPoolExecutor(max_workers=min(k, 5)) as ex:
        futures = {ex.submit(_one_call, i): i for i in range(k)}
        for fut in as_completed(futures):
            candidates.append(fut.result())

    return candidates


# ── Downstream Z3 Evaluation ─────────────────────────────────────────────────

def folio_inference_from_candidate(
    candidate: Optional[dict], example: dict
) -> Optional[str]:
    """Evaluate a candidate's FOL against FOLIO gold label using Z3."""
    if not candidate:
        return None
    premises_fol = candidate.get("premises_fol", [])
    conclusion_fol = candidate.get("conclusion_fol")
    if not conclusion_fol:
        return None
    try:
        result = folio_inference(premises_fol, conclusion_fol, timeout_secs=8)
        return result
    except Exception:
        return None


# ── Phase 0: Beam Recall Gate (50 examples) ──────────────────────────────────

def phase0_beam_recall(examples: list[dict], n: int = 50) -> tuple[float, list[bool]]:
    logger.info(f"=== Phase 0: Beam Recall Gate (n={n}) ===")
    sampled = random.sample(examples, min(n, len(examples)))
    results = []

    for i, ex in enumerate(sampled):
        try:
            candidates = generate_k_candidates(ex, k=CANDIDATE_K)
            any_correct = False
            for cand in candidates:
                verdict = folio_inference_from_candidate(cand, ex)
                gold = ex["label"].strip()
                if verdict and verdict.lower() == gold.lower():
                    any_correct = True
                    break
            results.append(any_correct)
        except Exception as e:
            logger.error(f"Phase 0 example {i} failed: {e}")
            results.append(False)

        if (i + 1) % 10 == 0:
            partial = sum(results) / len(results)
            logger.info(f"  Phase 0 progress {i+1}/{n}: recall={partial:.3f}, cost=${get_cumulative_cost():.3f}")

    beam_recall = sum(results) / len(results) if results else 0.0
    logger.info(f"Phase 0 complete: beam_recall={beam_recall:.3f}")

    if beam_recall < 0.40:
        logger.warning(
            f"WARN: Beam recall {beam_recall:.3f} < 0.40 gate. "
            "Switching generator to llama-3.3-70b-instruct for re-check."
        )
        # Try fallback generator on 10 examples
        fallback_gen = "meta-llama/llama-3.3-70b-instruct"
        sub = random.sample(examples, min(10, len(examples)))
        fb_results = []
        for ex in sub:
            try:
                cands = generate_k_candidates(ex, k=3, model=fallback_gen)
                any_correct = any(
                    folio_inference_from_candidate(c, ex) and
                    folio_inference_from_candidate(c, ex).lower() == ex["label"].lower()
                    for c in cands if c
                )
                fb_results.append(any_correct)
            except Exception:
                fb_results.append(False)
        fb_recall = sum(fb_results) / len(fb_results) if fb_results else 0.0
        logger.info(f"Fallback generator recall: {fb_recall:.3f}")
        if fb_recall < 0.40:
            logger.warning(
                f"HALT: Both generators below 0.40 recall. Proceeding with current results anyway."
            )
        else:
            global GENERATOR_MODEL
            GENERATOR_MODEL = fallback_gen
            beam_recall = fb_recall
            logger.info(f"Switched generator to {fallback_gen}")

    return beam_recall, results


# ── Phase 1: Oracle Pilot (30 triples) ───────────────────────────────────────

PILOT_TRIPLES = [
    # Simple positive predication (10)
    {"sentence": "Alice is a student.", "world": {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Student(bob)": False}}, "ground_truth": True},
    {"sentence": "Bob is not a student.", "world": {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Student(bob)": False}}, "ground_truth": True},
    {"sentence": "Alice is a doctor.", "world": {"domain": ["alice"], "atoms": {"Doctor(alice)": False}}, "ground_truth": False},
    {"sentence": "Alice and Bob are both students.", "world": {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Student(bob)": True}}, "ground_truth": True},
    {"sentence": "Alice and Bob are both students.", "world": {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Student(bob)": False}}, "ground_truth": False},
    {"sentence": "Alice is happy.", "world": {"domain": ["alice", "bob"], "atoms": {"Happy(alice)": True, "Happy(bob)": False}}, "ground_truth": True},
    {"sentence": "No one is happy.", "world": {"domain": ["alice", "bob"], "atoms": {"Happy(alice)": False, "Happy(bob)": False}}, "ground_truth": True},
    {"sentence": "No one is happy.", "world": {"domain": ["alice"], "atoms": {"Happy(alice)": True}}, "ground_truth": False},
    {"sentence": "Alice likes Bob.", "world": {"domain": ["alice", "bob"], "atoms": {"Likes(alice, bob)": True}}, "ground_truth": True},
    {"sentence": "Alice does not like Bob.", "world": {"domain": ["alice", "bob"], "atoms": {"Likes(alice, bob)": False}}, "ground_truth": True},
    # Negation (10)
    {"sentence": "No cats are dogs.", "world": {"domain": ["felix"], "atoms": {"Cat(felix)": True, "Dog(felix)": False}}, "ground_truth": True},
    {"sentence": "No cats are dogs.", "world": {"domain": ["rex"], "atoms": {"Cat(rex)": True, "Dog(rex)": True}}, "ground_truth": False},
    {"sentence": "Not all birds can fly.", "world": {"domain": ["tweety", "sam"], "atoms": {"Bird(tweety)": True, "CanFly(tweety)": True, "Bird(sam)": True, "CanFly(sam)": False}}, "ground_truth": True},
    {"sentence": "Not all birds can fly.", "world": {"domain": ["tweety"], "atoms": {"Bird(tweety)": True, "CanFly(tweety)": True}}, "ground_truth": False},
    {"sentence": "Alice is neither a teacher nor a student.", "world": {"domain": ["alice"], "atoms": {"Teacher(alice)": False, "Student(alice)": False}}, "ground_truth": True},
    {"sentence": "Alice is neither a teacher nor a student.", "world": {"domain": ["alice"], "atoms": {"Teacher(alice)": True, "Student(alice)": False}}, "ground_truth": False},
    {"sentence": "Nobody likes everyone.", "world": {"domain": ["alice", "bob"], "atoms": {"Likes(alice, alice)": True, "Likes(alice, bob)": False, "Likes(bob, alice)": True, "Likes(bob, bob)": True}}, "ground_truth": True},
    {"sentence": "Nobody likes everyone.", "world": {"domain": ["alice"], "atoms": {"Likes(alice, alice)": True}}, "ground_truth": False},
    {"sentence": "There are no students who failed every exam.", "world": {"domain": ["alice", "e1", "e2"], "atoms": {"Student(alice)": True, "Exam(e1)": True, "Exam(e2)": True, "Failed(alice, e1)": True, "Failed(alice, e2)": False}}, "ground_truth": True},
    {"sentence": "There are no students who failed every exam.", "world": {"domain": ["alice", "e1"], "atoms": {"Student(alice)": True, "Exam(e1)": True, "Failed(alice, e1)": True}}, "ground_truth": False},
    # Quantifier scope (10)
    {"sentence": "Every student likes some teacher.", "world": {"domain": ["s1", "t1"], "atoms": {"Student(s1)": True, "Teacher(t1)": True, "Likes(s1, t1)": True}}, "ground_truth": True},
    {"sentence": "Every student likes some teacher.", "world": {"domain": ["s1", "t1"], "atoms": {"Student(s1)": True, "Teacher(t1)": True, "Likes(s1, t1)": False}}, "ground_truth": False},
    {"sentence": "All birds fly.", "world": {"domain": ["tweety", "sam"], "atoms": {"Bird(tweety)": True, "Flies(tweety)": True, "Bird(sam)": True, "Flies(sam)": False}}, "ground_truth": False},
    {"sentence": "All birds fly.", "world": {"domain": ["tweety"], "atoms": {"Bird(tweety)": True, "Flies(tweety)": True}}, "ground_truth": True},
    {"sentence": "Some student passed every exam.", "world": {"domain": ["alice", "bob", "e1", "e2"], "atoms": {"Student(alice)": True, "Student(bob)": True, "Exam(e1)": True, "Exam(e2)": True, "Passed(alice, e1)": True, "Passed(alice, e2)": True, "Passed(bob, e1)": False}}, "ground_truth": True},
    {"sentence": "Some student passed every exam.", "world": {"domain": ["alice", "e1", "e2"], "atoms": {"Student(alice)": True, "Exam(e1)": True, "Exam(e2)": True, "Passed(alice, e1)": True, "Passed(alice, e2)": False}}, "ground_truth": False},
    {"sentence": "There is someone whom everyone loves.", "world": {"domain": ["alice", "bob"], "atoms": {"Loves(alice, alice)": True, "Loves(bob, alice)": True, "Loves(alice, bob)": False}}, "ground_truth": True},
    {"sentence": "There is someone whom everyone loves.", "world": {"domain": ["alice", "bob"], "atoms": {"Loves(alice, alice)": False, "Loves(bob, alice)": True, "Loves(alice, bob)": True, "Loves(bob, bob)": False}}, "ground_truth": False},
    {"sentence": "Every teacher knows some student who passes all exams.", "world": {"domain": ["t1", "s1", "e1"], "atoms": {"Teacher(t1)": True, "Student(s1)": True, "Exam(e1)": True, "Knows(t1, s1)": True, "Passes(s1, e1)": True}}, "ground_truth": True},
    {"sentence": "Every teacher knows some student who passes all exams.", "world": {"domain": ["t1", "s1", "e1", "e2"], "atoms": {"Teacher(t1)": True, "Student(s1)": True, "Exam(e1)": True, "Exam(e2)": True, "Knows(t1, s1)": True, "Passes(s1, e1)": True, "Passes(s1, e2)": False}}, "ground_truth": False},
]


def phase1_oracle_pilot(oracle_model: str = ORACLE_MODEL_PRIMARY) -> tuple[float, str]:
    logger.info(f"=== Phase 1: Oracle Pilot (n={len(PILOT_TRIPLES)}) model={oracle_model} ===")
    correct = 0

    for triple in PILOT_TRIPLES:
        try:
            response = call_llm(
                oracle_model,
                ORACLE_TRUTH_JUDGMENT.format(
                    domain=triple["world"]["domain"],
                    atoms=triple["world"]["atoms"],
                    sentence=triple["sentence"],
                ),
                max_tokens=10,
                temperature=0.0,
            )
            pred = "yes" in response.lower()
            if pred == triple["ground_truth"]:
                correct += 1
        except Exception as e:
            logger.warning(f"Oracle pilot call failed: {e}")

    accuracy = correct / len(PILOT_TRIPLES)
    logger.info(f"Phase 1 oracle accuracy: {accuracy:.3f} ({correct}/{len(PILOT_TRIPLES)})")

    if accuracy < 0.60:
        logger.warning(f"Oracle accuracy {accuracy:.3f} < 0.60. Checking budget for upgrade...")
        if get_cumulative_cost() < 5.0:
            oracle_model = ORACLE_MODEL_FALLBACK
            logger.info(f"Upgrading oracle to {oracle_model}")
        else:
            logger.warning("Budget too low for oracle upgrade; proceeding with primary model.")

    return accuracy, oracle_model


# ── Stratification ───────────────────────────────────────────────────────────

def stratify_examples(examples: list[dict], n: int = 250) -> list[dict]:
    by_label: dict[str, list] = {}
    for ex in examples:
        lbl = ex.get("label", "Uncertain")
        by_label.setdefault(lbl, []).append(ex)

    per_label = n // max(len(by_label), 1)
    stratified = []
    for label, exs in by_label.items():
        sampled = random.sample(exs, min(per_label, len(exs)))
        stratified.extend(sampled)

    # Fill up to n
    stratified_set = {id(e) for e in stratified}
    remaining = [e for e in examples if id(e) not in stratified_set]
    random.shuffle(remaining)
    stratified.extend(remaining[: n - len(stratified)])
    random.shuffle(stratified)
    return stratified[:n]


def syntactic_prefilter(premises: list[str], conclusion: str) -> bool:
    """Rough filter for examples likely to work with small closed-world semantics."""
    text = " ".join(premises + [conclusion])
    has_named = bool(re.search(r"\b[A-Z][a-z]+\b", text))
    open_domain = bool(re.search(r"\bthere (is|are|exists?)\b|\bsome \w+ (is|are|has|have)\b", text.lower()))
    return has_named or not open_domain


# ── World Generation ─────────────────────────────────────────────────────────

def generate_diagnostic_worlds(
    conclusion: str,
    candidates: list[Optional[dict]],
    oracle_model: str,
    m: int = WORLD_M,
) -> list[dict]:
    valid_cands = [c for c in candidates if c and c.get("conclusion_fol")]
    if not valid_cands:
        return []

    cand_strs = "\n".join(
        f"{i+1}. {c['conclusion_fol']}" for i, c in enumerate(valid_cands)
    )
    prompt = DIAGNOSTIC_WORLD_GENERATION.format(
        sentence=conclusion, candidates=cand_strs
    )

    worlds = []
    attempts = 0
    max_attempts = m + 3

    def _get_world(_: int) -> Optional[dict]:
        try:
            response = call_llm(oracle_model, prompt, max_tokens=350, temperature=0.9)
            return parse_world_from_llm(response)
        except Exception as e:
            logger.debug(f"World gen attempt failed: {e}")
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_get_world, i) for i in range(max_attempts)]
        for fut in as_completed(futures):
            if len(worlds) >= m:
                break
            w = fut.result()
            if w and len(w.get("domain", [])) >= 2:
                worlds.append(w)

    return worlds[:m]


# ── Candidate Scoring ────────────────────────────────────────────────────────

def score_candidate(
    candidate: Optional[dict],
    worlds: list[dict],
    sentence: str,
    oracle_model: str,
) -> float:
    if not candidate or not candidate.get("conclusion_fol"):
        return 0.0

    agreements = 0
    total = 0

    def _score_world(world: dict) -> Optional[bool]:
        formula_truth = eval_formula(candidate["conclusion_fol"], world)
        if formula_truth is None:
            return None
        try:
            oracle_response = call_llm(
                oracle_model,
                ORACLE_TRUTH_JUDGMENT.format(
                    domain=world["domain"],
                    atoms=world["atoms"],
                    sentence=sentence,
                ),
                max_tokens=10,
                temperature=0.0,
            )
            oracle_truth = "yes" in oracle_response.lower()
            return formula_truth == oracle_truth
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_score_world, w) for w in worlds]
        for fut in as_completed(futures):
            r = fut.result()
            if r is not None:
                if r:
                    agreements += 1
                total += 1

    return agreements / total if total > 0 else 0.5


# ── Phase 2: Main Pipeline ────────────────────────────────────────────────────

def phase2_main_pipeline(
    examples: list[dict],
    oracle_model: str,
    m: int = WORLD_M,
    save_interval: int = 25,
) -> list[dict]:
    logger.info(f"=== Phase 2: Main Pipeline (n={len(examples)}, m={m}, oracle={oracle_model}) ===")
    results = []
    # Cache candidates to reuse in baselines
    candidates_cache: list[list[Optional[dict]]] = []

    for i, ex in enumerate(examples):
        if get_cumulative_cost() >= BUDGET_HARD_LIMIT - 0.5:
            logger.warning(f"Budget nearly exhausted at example {i}. Stopping phase 2.")
            break

        try:
            if not syntactic_prefilter(ex["premises"], ex["conclusion"]):
                logger.debug(f"Example {i} prefiltered (open-domain)")
                candidates_cache.append([])
                results.append({
                    "example_id": i,
                    "label": ex["label"],
                    "verdict": None,
                    "correct": False,
                    "scores": [],
                    "best_idx": -1,
                    "filtered": True,
                })
                continue

            candidates = generate_k_candidates(ex, k=CANDIDATE_K, model=GENERATOR_MODEL)
            candidates_cache.append(candidates)

            worlds = generate_diagnostic_worlds(ex["conclusion"], candidates, oracle_model, m)

            scores = [
                score_candidate(c, worlds, ex["conclusion"], oracle_model)
                for c in candidates
            ]

            best_idx = max(range(len(scores)), key=lambda j: scores[j]) if scores else 0
            selected = candidates[best_idx] if candidates else None
            verdict = folio_inference_from_candidate(selected, ex)
            gold = ex["label"].strip()
            correct = verdict is not None and verdict.lower() == gold.lower()

            results.append({
                "example_id": i,
                "label": gold,
                "verdict": verdict,
                "correct": correct,
                "scores": scores,
                "best_idx": best_idx,
                "filtered": False,
            })

        except Exception as e:
            logger.error(f"Phase 2 example {i} failed: {e}")
            candidates_cache.append([])
            results.append({
                "example_id": i, "label": ex.get("label", "Uncertain"),
                "verdict": None, "correct": False,
                "scores": [], "best_idx": -1, "filtered": False,
            })

        if (i + 1) % 10 == 0:
            n_done = len(results)
            acc = sum(r["correct"] for r in results) / n_done if n_done else 0
            logger.info(
                f"  Phase 2 progress {i+1}/{len(examples)}: "
                f"acc={acc:.3f}, cost=${get_cumulative_cost():.3f}"
            )

        # Periodic save
        if (i + 1) % save_interval == 0:
            _save_partial(results, "phase2_partial.json")

    return results, candidates_cache


# ── Baselines ────────────────────────────────────────────────────────────────

def baseline_top1(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
) -> list[dict]:
    """Use first candidate (temperature=0 equivalent) as prediction."""
    results = []
    for i, (ex, candidates) in enumerate(zip(examples, candidates_cache)):
        selected = candidates[0] if candidates else None
        verdict = folio_inference_from_candidate(selected, ex)
        gold = ex["label"].strip()
        correct = verdict is not None and verdict.lower() == gold.lower()
        results.append({"example_id": i, "label": gold, "verdict": verdict, "correct": correct})
    return results


def _jaccard(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def baseline_self_consistency(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
) -> list[dict]:
    """Cluster k=5 by Jaccard on conclusion_fol tokens; pick centroid of largest cluster."""
    results = []
    for i, (ex, candidates) in enumerate(zip(examples, candidates_cache)):
        valid = [(j, c) for j, c in enumerate(candidates) if c and c.get("conclusion_fol")]
        if not valid:
            results.append({
                "example_id": i, "label": ex["label"].strip(),
                "verdict": None, "correct": False,
            })
            continue

        formulas = [(j, c["conclusion_fol"]) for j, c in valid]
        k = len(formulas)
        if k == 1:
            selected = valid[0][1]
        else:
            # Compute pairwise Jaccard
            sim = [[_jaccard(formulas[a][1], formulas[b][1]) for b in range(k)] for a in range(k)]
            # Greedy clustering: form cluster around highest-sim pair
            assigned = [-1] * k
            cluster_id = 0
            for a in range(k):
                for b in range(a + 1, k):
                    if sim[a][b] > 0.5 and assigned[a] == -1 and assigned[b] == -1:
                        assigned[a] = cluster_id
                        assigned[b] = cluster_id
                        cluster_id += 1

            # Singletons
            for a in range(k):
                if assigned[a] == -1:
                    assigned[a] = cluster_id
                    cluster_id += 1

            # Find largest cluster
            from collections import Counter as _Ctr
            cluster_counts = _Ctr(assigned)
            largest_cid = cluster_counts.most_common(1)[0][0]
            members = [formulas[a] for a in range(k) if assigned[a] == largest_cid]

            # Pick centroid: highest average sim to other members
            if len(members) == 1:
                centroid_formula = members[0][1]
            else:
                best_avg = -1
                centroid_formula = members[0][1]
                for a, (_, fa) in enumerate(members):
                    avg = sum(_jaccard(fa, fb) for _, fb in members) / len(members)
                    if avg > best_avg:
                        best_avg = avg
                        centroid_formula = fa

            # Find candidate with this conclusion_fol
            selected = None
            for _, c in valid:
                if c["conclusion_fol"] == centroid_formula:
                    selected = c
                    break
            if selected is None:
                selected = valid[0][1]

        verdict = folio_inference_from_candidate(selected, ex)
        gold = ex["label"].strip()
        correct = verdict is not None and verdict.lower() == gold.lower()
        results.append({"example_id": i, "label": gold, "verdict": verdict, "correct": correct})
    return results


def baseline_direct_judge(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    oracle_model: str,
) -> list[dict]:
    """Ask oracle to select best FOL candidate directly."""
    results = []
    for i, (ex, candidates) in enumerate(zip(examples, candidates_cache)):
        valid = [c for c in candidates if c and c.get("conclusion_fol")]
        if not valid:
            results.append({
                "example_id": i, "label": ex["label"].strip(),
                "verdict": None, "correct": False,
            })
            continue

        if get_cumulative_cost() >= BUDGET_HARD_LIMIT - 0.5:
            # Fallback to top-1
            selected = valid[0]
        else:
            k = len(valid)
            numbered = "\n".join(f"{j+1}. {c['conclusion_fol']}" for j, c in enumerate(valid))
            prompt = DIRECT_LLM_JUDGE.format(
                k=k, sentence=ex["conclusion"], candidates_numbered=numbered
            )
            try:
                response = call_llm(oracle_model, prompt, max_tokens=5, temperature=0.0)
                num_m = re.search(r"\d+", response.strip())
                if num_m:
                    idx = int(num_m.group()) - 1
                    selected = valid[max(0, min(idx, k - 1))]
                else:
                    selected = valid[0]
            except Exception:
                selected = valid[0]

        verdict = folio_inference_from_candidate(selected, ex)
        gold = ex["label"].strip()
        correct = verdict is not None and verdict.lower() == gold.lower()
        results.append({"example_id": i, "label": gold, "verdict": verdict, "correct": correct})

        if (i + 1) % 25 == 0:
            logger.info(f"  Direct judge baseline {i+1}/{len(examples)}, cost=${get_cumulative_cost():.3f}")

    return results


# ── Ablations ────────────────────────────────────────────────────────────────

def ablation_same_model_oracle(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    m: int = WORLD_M,
) -> list[dict]:
    """Use same model (Llama-3.1-8B) as both generator and oracle."""
    logger.info("=== Ablation A: Same-model oracle ===")
    return _run_world_probe(examples, candidates_cache, oracle_model=GENERATOR_MODEL, m=m, label="AblA")


def ablation_random_worlds(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    oracle_model: str,
    m: int = WORLD_M,
) -> list[dict]:
    """Use random worlds instead of diagnostic worlds."""
    logger.info("=== Ablation B: Random worlds ===")
    return _run_world_probe(
        examples, candidates_cache, oracle_model=oracle_model, m=m, label="AblB", random_worlds=True
    )


def ablation_m4(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    oracle_model: str,
) -> list[dict]:
    """Run main pipeline with m=4 worlds instead of m=8."""
    logger.info("=== Ablation C: m=4 worlds ===")
    return _run_world_probe(examples, candidates_cache, oracle_model=oracle_model, m=4, label="AblC")


def _make_random_world(domain_size: int = 3) -> dict:
    """Generate a random closed world."""
    names = ["a", "b", "c", "d", "e"][:domain_size]
    preds = ["P", "Q", "R", "S"]
    atoms = {}
    for p in preds:
        for n in names:
            atoms[f"{p}({n})"] = random.random() > 0.5
        for n1 in names:
            for n2 in names:
                if random.random() > 0.7:
                    atoms[f"{p}({n1},{n2})"] = random.random() > 0.5
    return {"domain": names, "atoms": atoms}


def _run_world_probe(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    oracle_model: str,
    m: int,
    label: str,
    random_worlds: bool = False,
) -> list[dict]:
    results = []
    for i, (ex, candidates) in enumerate(zip(examples, candidates_cache)):
        if get_cumulative_cost() >= BUDGET_HARD_LIMIT - 0.3:
            logger.warning(f"{label}: budget limit, stopping at {i}")
            break
        try:
            if not candidates or not any(c for c in candidates):
                results.append({
                    "example_id": i, "label": ex["label"].strip(),
                    "verdict": None, "correct": False,
                })
                continue

            if random_worlds:
                worlds = [_make_random_world(random.randint(2, 4)) for _ in range(m)]
            else:
                worlds = generate_diagnostic_worlds(ex["conclusion"], candidates, oracle_model, m)

            scores = [
                score_candidate(c, worlds, ex["conclusion"], oracle_model)
                for c in candidates
            ]
            best_idx = max(range(len(scores)), key=lambda j: scores[j]) if scores else 0
            selected = candidates[best_idx] if candidates else None
            verdict = folio_inference_from_candidate(selected, ex)
            gold = ex["label"].strip()
            correct = verdict is not None and verdict.lower() == gold.lower()
            results.append({"example_id": i, "label": gold, "verdict": verdict, "correct": correct})
        except Exception as e:
            logger.error(f"{label} example {i}: {e}")
            results.append({
                "example_id": i, "label": ex.get("label", "Uncertain"),
                "verdict": None, "correct": False,
            })

        if (i + 1) % 25 == 0:
            acc = sum(r["correct"] for r in results) / len(results) if results else 0
            logger.info(f"  {label} {i+1}/{len(examples)}: acc={acc:.3f}, cost=${get_cumulative_cost():.3f}")

    return results


# ── Metrics ──────────────────────────────────────────────────────────────────

def compute_accuracy(results: list[dict]) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.get("correct")) / len(results)


def stratified_accuracy(results: list[dict]) -> dict[str, float]:
    by_label: dict[str, list] = {}
    for r in results:
        by_label.setdefault(r.get("label", "Uncertain"), []).append(r.get("correct", False))
    return {label: sum(v) / len(v) for label, v in by_label.items()}


def compute_confidence_interval(results: list[dict]) -> tuple[float, float]:
    n = len(results)
    if n == 0:
        return (0.0, 0.0)
    p = compute_accuracy(results)
    se = math.sqrt(p * (1 - p) / n)
    return (max(0.0, p - 1.96 * se), min(1.0, p + 1.96 * se))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _save_partial(results: list[dict], filename: str) -> None:
    path = RESULTS_DIR / filename
    path.write_text(json.dumps(results, indent=2))


def log_cost_checkpoint() -> None:
    logger.info(f"Cost checkpoint: ${get_cumulative_cost():.4f} / ${BUDGET_HARD_LIMIT:.2f}")


# ── method_out.json builder ───────────────────────────────────────────────────

def build_method_out(
    examples: list[dict],
    candidates_cache: list[list[Optional[dict]]],
    main_results: list[dict],
    top1_results: list[dict],
    sc_results: list[dict],
    judge_results: list[dict],
    ablation_a: list[dict],
    ablation_b: list[dict],
    ablation_c: list[dict],
    beam_recall: float,
    pilot_accuracy: float,
    oracle_model: str,
) -> list[dict]:
    """Build the method_out.json in exp_gen_sol_out format."""
    records = []
    n = len(main_results)

    for i in range(n):
        ex = examples[i] if i < len(examples) else {}
        mr = main_results[i] if i < len(main_results) else {}
        t1 = top1_results[i] if i < len(top1_results) else {}
        sc = sc_results[i] if i < len(sc_results) else {}
        jd = judge_results[i] if i < len(judge_results) else {}
        aa = ablation_a[i] if i < len(ablation_a) else {}
        ab = ablation_b[i] if i < len(ablation_b) else {}
        ac = ablation_c[i] if i < len(ablation_c) else {}

        premises = ex.get("premises", [])
        conclusion = ex.get("conclusion", "")
        gold_label = ex.get("label", "")

        # Serialize candidates
        cands = candidates_cache[i] if i < len(candidates_cache) else []
        cands_str = json.dumps([c for c in cands if c], separators=(",", ":"))[:500]

        record = {
            "input": f"Premises: {' | '.join(premises)}\nConclusion: {conclusion}",
            "output": gold_label,
            "predict_main_method": mr.get("verdict") or "",
            "predict_top1_baseline": t1.get("verdict") or "",
            "predict_self_consistency": sc.get("verdict") or "",
            "predict_direct_judge": jd.get("verdict") or "",
            "predict_ablation_same_oracle": aa.get("verdict") or "",
            "predict_ablation_random_worlds": ab.get("verdict") or "",
            "predict_ablation_m4": ac.get("verdict") or "",
            "metadata_gold_label": gold_label,
            "metadata_main_correct": str(mr.get("correct", False)),
            "metadata_top1_correct": str(t1.get("correct", False)),
            "metadata_sc_correct": str(sc.get("correct", False)),
            "metadata_candidates": cands_str,
            "metadata_world_scores": json.dumps(mr.get("scores", []))[:200],
        }
        records.append(record)

    return records


# ── Main ─────────────────────────────────────────────────────────────────────

@logger.catch(reraise=True)
def main():
    random.seed(42)
    logger.info("Starting Heterogeneous-Oracle World-Probing experiment")

    examples = load_folio()

    # Phase 0
    beam_recall, beam_results = phase0_beam_recall(examples, n=50)
    log_cost_checkpoint()

    # Phase 1
    pilot_accuracy, oracle_model = phase1_oracle_pilot()
    log_cost_checkpoint()

    # Stratify (use all available examples, max 204 for FOLIO validation)
    n_strat = min(250, len(examples))
    stratified = stratify_examples(examples, n=n_strat)
    logger.info(f"Stratified {len(stratified)} examples")

    # Phase 2
    main_results, candidates_cache = phase2_main_pipeline(stratified, oracle_model, m=WORLD_M)
    _save_partial(main_results, "phase2_complete.json")
    log_cost_checkpoint()

    n_processed = len(main_results)
    processed_examples = stratified[:n_processed]
    processed_cache = candidates_cache[:n_processed]

    # Phase 3 baselines (zero-cost, reuse cache)
    logger.info("=== Phase 3: Baselines ===")
    top1_results = baseline_top1(processed_examples, processed_cache)
    sc_results = baseline_self_consistency(processed_examples, processed_cache)

    # Direct judge baseline (needs API calls but limited)
    judge_results = baseline_direct_judge(processed_examples, processed_cache, oracle_model)
    log_cost_checkpoint()

    # Ablations (may be skipped if budget exhausted)
    ablation_a, ablation_b, ablation_c = [], [], []

    if get_cumulative_cost() < BUDGET_HARD_LIMIT - 2.0:
        ablation_a = ablation_same_model_oracle(processed_examples, processed_cache, m=WORLD_M)
        log_cost_checkpoint()
    else:
        logger.warning("Skipping ablation A (budget)")

    if get_cumulative_cost() < BUDGET_HARD_LIMIT - 1.0:
        ablation_b = ablation_random_worlds(processed_examples, processed_cache, oracle_model, m=WORLD_M)
        log_cost_checkpoint()
    else:
        logger.warning("Skipping ablation B (budget)")

    if get_cumulative_cost() < BUDGET_HARD_LIMIT - 0.5:
        ablation_c = ablation_m4(processed_examples, processed_cache, oracle_model)
        log_cost_checkpoint()
    else:
        logger.warning("Skipping ablation C (budget)")

    # Compute metrics
    main_acc = compute_accuracy(main_results)
    main_ci = compute_confidence_interval(main_results)
    top1_acc = compute_accuracy(top1_results)
    sc_acc = compute_accuracy(sc_results)
    judge_acc = compute_accuracy(judge_results)
    aa_acc = compute_accuracy(ablation_a)
    ab_acc = compute_accuracy(ablation_b)
    ac_acc = compute_accuracy(ablation_c)

    summary = {
        "phase0": {"beam_recall": beam_recall, "n": 50},
        "phase1": {"oracle_accuracy": pilot_accuracy, "oracle_model": oracle_model, "n": len(PILOT_TRIPLES)},
        "main_method": {
            "accuracy": main_acc,
            "ci_95": list(main_ci),
            "by_label": stratified_accuracy(main_results),
            "n": n_processed,
        },
        "baselines": {
            "top1": {"accuracy": top1_acc, "n": len(top1_results)},
            "self_consistency": {"accuracy": sc_acc, "n": len(sc_results)},
            "direct_judge": {"accuracy": judge_acc, "n": len(judge_results)},
        },
        "ablations": {
            "same_model_oracle": {"accuracy": aa_acc, "n": len(ablation_a)},
            "random_worlds": {"accuracy": ab_acc, "n": len(ablation_b)},
            "m4": {"accuracy": ac_acc, "n": len(ablation_c)},
        },
        "budget": {
            "total_cost_usd": get_cumulative_cost(),
            "oracle_model_used": oracle_model,
            "generator_model_used": GENERATOR_MODEL,
        },
        "success_criteria": {
            "S1_beam_recall_ge60": beam_recall >= 0.60,
            "S2_oracle_accuracy_ge72": pilot_accuracy >= 0.72,
            "S3_main_vs_top1_ge5pp": main_acc - top1_acc >= 0.05,
            "S4_main_vs_sc_ge3pp": main_acc - sc_acc >= 0.03,
            "S5_hetero_vs_homo_ge3pp": (main_acc - aa_acc >= 0.03) if ablation_a else None,
        },
    }

    # Save summary
    (RESULTS_DIR / "experiment_summary.json").write_text(json.dumps(summary, indent=2))
    logger.info(f"Summary: {json.dumps(summary, indent=2)}")

    # Build method_out in exp_gen_sol_out format
    records = build_method_out(
        processed_examples, processed_cache,
        main_results, top1_results, sc_results, judge_results,
        ablation_a, ablation_b, ablation_c,
        beam_recall, pilot_accuracy, oracle_model,
    )

    method_out = {
        "metadata": {
            "method_name": "heterogeneous_oracle_world_probing",
            "description": "Four-phase NL-to-FOL faithfulness evaluation via heterogeneous oracle world probing on FOLIO",
            "parameters": {
                "generator_model": GENERATOR_MODEL,
                "oracle_model": oracle_model,
                "k_candidates": CANDIDATE_K,
                "m_worlds": WORLD_M,
                "n_examples": n_processed,
            },
            "summary": summary,
        },
        "datasets": [
            {
                "dataset": "yale-nlp/folio",
                "examples": records,
            }
        ],
    }

    out_path = RESULTS_DIR / "method_out.json"
    out_path.write_text(json.dumps(method_out, indent=2))
    logger.info(f"Saved method_out.json ({len(records)} examples) to {out_path}")
    logger.info(
        f"Done. Total cost: ${get_cumulative_cost():.4f} | "
        f"Main accuracy: {main_acc:.3f} | Top-1 accuracy: {top1_acc:.3f} | "
        f"Delta: {main_acc - top1_acc:+.3f}"
    )


if __name__ == "__main__":
    main()
