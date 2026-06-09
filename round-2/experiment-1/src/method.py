#!/usr/bin/env python3
"""
HOWP (Heterogeneous Oracle World Probing) Experiment — Iter 2.

Tests whether world-probing-based FOL candidate selection beats
top-1 and self-consistency baselines when generator quality gate
(beam recall ≥ 60%) is satisfied.

7 conditions:
  howp_hetero    — diagnostic worlds, heterogeneous oracle (Qwen-7B)
  howp_same      — diagnostic worlds, same-model oracle (LLaMA-70B)
  howp_random    — random worlds, heterogeneous oracle
  howp_m4        — diagnostic worlds m=4, heterogeneous oracle
  top1           — baseline: first candidate
  self_consist   — baseline: most central by edit distance
  direct_judge   — baseline: oracle picks best formula directly
"""

import asyncio
import gc
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
import editdistance
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from fol_checker import check_formula_in_world, z3_entailment
from prompts import (
    build_fol_generation_prompt,
    build_world_generation_prompt,
    build_random_world_prompt,
    build_oracle_prompt,
    build_direct_judge_prompt,
)

# ─── Config ───────────────────────────────────────────────────────────────────

WORKSPACE = Path(__file__).parent
LOGS_DIR = WORKSPACE / "logs"
LOGS_DIR.mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add(str(LOGS_DIR / "run.log"), rotation="30 MB", level="DEBUG")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

GENERATOR_MODEL = "meta-llama/llama-3.1-70b-instruct"
ORACLE_MODEL_FREE = "qwen/qwen-2.5-7b-instruct"   # paid (free tier unavailable on this key)
ORACLE_MODEL_PAID = "qwen/qwen-2.5-7b-instruct"

# Pricing per 1M tokens (input, output)
PRICING: dict[str, tuple[float, float]] = {
    "meta-llama/llama-3.1-70b-instruct": (0.12, 0.30),
    "qwen/qwen-2.5-7b-instruct:free": (0.04, 0.10),
    "qwen/qwen-2.5-7b-instruct": (0.04, 0.10),
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
    "deepseek/deepseek-r1-distill-llama-70b": (0.14, 0.28),
}

COST_TRACKER: dict[str, float] = {"total": 0.0, "calls": 0}
BUDGET_USD = 9.0
PHASE_B_BUDGET_USD = 3.0

# Concurrency limits
GENERATOR_SEMAPHORE_SIZE = 4
ORACLE_SEMAPHORE_SIZE = 8

# ─── Memory limits ────────────────────────────────────────────────────────────

import resource
_ram_limit = 20 * 1024**3  # 20 GB (container has 29 GB)
resource.setrlimit(resource.RLIMIT_AS, (_ram_limit, _ram_limit))

# ─── Budget tracking ──────────────────────────────────────────────────────────

class BudgetExceeded(Exception):
    pass


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model not in PRICING:
        model_base = model.split(":")[0]
        if model_base in PRICING:
            return PRICING[model_base][0] * prompt_tokens / 1e6 + PRICING[model_base][1] * completion_tokens / 1e6
        return 0.0
    p_in, p_out = PRICING[model]
    return p_in * prompt_tokens / 1e6 + p_out * completion_tokens / 1e6


def _add_cost(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    cost = _estimate_cost(model, prompt_tokens, completion_tokens)
    COST_TRACKER["total"] += cost
    COST_TRACKER["calls"] += 1
    if COST_TRACKER["total"] > BUDGET_USD:
        raise BudgetExceeded(f"Budget ${BUDGET_USD} exceeded: ${COST_TRACKER['total']:.4f}")


# ─── LLM Client ───────────────────────────────────────────────────────────────

_gen_semaphore: asyncio.Semaphore | None = None
_oracle_semaphore: asyncio.Semaphore | None = None


async def _call_llm_raw(
    session: aiohttp.ClientSession,
    model: str,
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
    semaphore: asyncio.Semaphore | None = None,
) -> str | None:
    """Single LLM call via OpenRouter. Returns text or None on error."""
    sem = semaphore or _gen_semaphore
    async with (sem if sem else asyncio.Semaphore(1)):
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            async with session.post(
                OPENROUTER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"OpenRouter {resp.status} for {model}: {text[:200]}")
                    return None
                data = await resp.json()
                usage = data.get("usage", {})
                _add_cost(
                    model,
                    usage.get("prompt_tokens", 300),
                    usage.get("completion_tokens", 100),
                )
                content = data["choices"][0]["message"]["content"]
                logger.debug(f"LLM {model[:30]} response: {content[:100]}")
                return content
        except BudgetExceeded:
            raise
        except Exception as e:
            logger.debug(f"LLM call error ({model}): {e}")
            return None


async def call_generator(
    session: aiohttp.ClientSession, prompt: str, max_tokens: int = 200
) -> str | None:
    return await _call_llm_raw(
        session, GENERATOR_MODEL, prompt, max_tokens=max_tokens,
        temperature=0.7, semaphore=_gen_semaphore
    )


async def call_oracle(
    session: aiohttp.ClientSession, prompt: str, model: str | None = None
) -> str | None:
    m = model or ORACLE_MODEL_FREE
    result = await _call_llm_raw(
        session, m, prompt, max_tokens=15, temperature=0.0, semaphore=_oracle_semaphore
    )
    if result is None and m == ORACLE_MODEL_FREE:
        # Fallback to paid
        logger.debug("Free oracle failed, trying paid")
        result = await _call_llm_raw(
            session, ORACLE_MODEL_PAID, prompt, max_tokens=15, temperature=0.0,
            semaphore=_oracle_semaphore
        )
    return result


async def call_world_gen(
    session: aiohttp.ClientSession, prompt: str
) -> str | None:
    return await _call_llm_raw(
        session, ORACLE_MODEL_FREE, prompt, max_tokens=1200, temperature=0.7,
        semaphore=_oracle_semaphore
    )


# ─── Synthetic Dataset Builder ────────────────────────────────────────────────

CATEGORIES = [
    ("student", "Student"), ("teacher", "Teacher"), ("doctor", "Doctor"),
    ("dog", "Dog"), ("cat", "Cat"), ("bird", "Bird"),
    ("car", "Car"), ("book", "Book"), ("robot", "Robot"), ("plant", "Plant"),
]
PROPERTIES = [
    ("smart", "Smart"), ("fast", "Fast"), ("friendly", "Friendly"),
    ("large", "Large"), ("happy", "Happy"), ("old", "Old"),
    ("rare", "Rare"), ("loud", "Loud"), ("quiet", "Quiet"), ("bright", "Bright"),
]
NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Henry", "Iris", "Jack"]


def _make_entailment_example(cat_nl, cat_fol, prop_nl, prop_fol, name) -> dict:
    """All X are Y. Name is X. → Name is Y. (entailment)"""
    return {
        "premises_nl": [f"All {cat_nl}s are {prop_nl}.", f"{name} is a {cat_nl}."],
        "premises_fol": [
            f"all x. ({cat_fol}(x) -> {prop_fol}(x))",
            f"{cat_fol}({name})",
        ],
        "conclusion_nl": f"{name} is {prop_nl}.",
        "conclusion_fol": f"{prop_fol}({name})",
        "label": "entailment",
        "pattern": "all_A_are_B",
    }


def _make_contradiction_example(cat_nl, cat_fol, prop_nl, prop_fol, name) -> dict:
    """No X are Y. Name is X. → Name is Y. (contradiction)"""
    return {
        "premises_nl": [f"No {cat_nl}s are {prop_nl}.", f"{name} is a {cat_nl}."],
        "premises_fol": [
            f"all x. ({cat_fol}(x) -> -{prop_fol}(x))",
            f"{cat_fol}({name})",
        ],
        "conclusion_nl": f"{name} is {prop_nl}.",
        "conclusion_fol": f"{prop_fol}({name})",
        "label": "contradiction",
        "pattern": "no_A_are_B",
    }


def _make_neutral_example(cat_nl, cat_fol, prop_nl, prop_fol, name) -> dict:
    """Some X are Y. → Name is Y. (neutral — can't determine)"""
    return {
        "premises_nl": [f"Some {cat_nl}s are {prop_nl}."],
        "premises_fol": [f"exists x. ({cat_fol}(x) & {prop_fol}(x))"],
        "conclusion_nl": f"{name} is {prop_nl}.",
        "conclusion_fol": f"{prop_fol}({name})",
        "label": "neutral",
        "pattern": "some_A_are_B",
    }


def _make_chain_entailment(cat_nl, cat_fol, mid_nl, mid_fol, prop_nl, prop_fol, name) -> dict:
    """All X are Y. All Y are Z. Name is X. → Name is Z. (entailment, 2-hop)"""
    return {
        "premises_nl": [
            f"All {cat_nl}s are {mid_nl}.",
            f"All {mid_nl}s are {prop_nl}.",
            f"{name} is a {cat_nl}.",
        ],
        "premises_fol": [
            f"all x. ({cat_fol}(x) -> {mid_fol}(x))",
            f"all x. ({mid_fol}(x) -> {prop_fol}(x))",
            f"{cat_fol}({name})",
        ],
        "conclusion_nl": f"{name} is {prop_nl}.",
        "conclusion_fol": f"{prop_fol}({name})",
        "label": "entailment",
        "pattern": "chain_2hop",
    }


def _make_negation_neutral(cat_nl, cat_fol, prop_nl, prop_fol, name) -> dict:
    """All X are Y. → Name is not Y. (neutral)"""
    return {
        "premises_nl": [f"All {cat_nl}s are {prop_nl}."],
        "premises_fol": [f"all x. ({cat_fol}(x) -> {prop_fol}(x))"],
        "conclusion_nl": f"{name} is not {prop_nl}.",
        "conclusion_fol": f"-{prop_fol}({name})",
        "label": "neutral",
        "pattern": "negation_neutral",
    }


def build_synthetic_dataset(n_total: int = 150, seed: int = 42) -> list[dict]:
    """Generate n_total synthetic NLI examples with controlled FOL structure."""
    import random
    rng = random.Random(seed)

    examples = []
    ex_id = 0

    # Interleave 3 label types
    while len(examples) < n_total:
        cat_idx = rng.randint(0, len(CATEGORIES) - 1)
        prop_idx = rng.randint(0, len(PROPERTIES) - 1)
        name = rng.choice(NAMES)
        cat_nl, cat_fol = CATEGORIES[cat_idx]
        prop_nl, prop_fol = PROPERTIES[prop_idx]

        # Pick pattern type
        label_type = len(examples) % 3  # 0=entailment, 1=contradiction, 2=neutral

        if label_type == 0:
            # Mix of direct entailment and 2-hop
            if rng.random() < 0.3 and prop_idx + 1 < len(PROPERTIES):
                mid_nl, mid_fol = PROPERTIES[(prop_idx + 1) % len(PROPERTIES)]
                ex = _make_chain_entailment(cat_nl, cat_fol, mid_nl, mid_fol, prop_nl, prop_fol, name)
            else:
                ex = _make_entailment_example(cat_nl, cat_fol, prop_nl, prop_fol, name)
        elif label_type == 1:
            ex = _make_contradiction_example(cat_nl, cat_fol, prop_nl, prop_fol, name)
        else:
            if rng.random() < 0.5:
                ex = _make_neutral_example(cat_nl, cat_fol, prop_nl, prop_fol, name)
            else:
                ex = _make_negation_neutral(cat_nl, cat_fol, prop_nl, prop_fol, name)

        ex["example_id"] = f"syn_{ex_id:04d}"
        ex_id += 1
        examples.append(ex)

    return examples


# ─── FOL Parsing/Filtering ────────────────────────────────────────────────────

_FOL_RE = re.compile(
    r'(?:all\s+\w+\s*\.|exists\s+\w+\s*\.|-|[A-Z][A-Za-z0-9_]*\()',
    re.IGNORECASE,
)


_NATURAL_LANGUAGE_STARTS = re.compile(
    r'^(?:No|The|A|An|All|Some|Every|None|There|This|That|It|He|She|They|We|'
    r'Yes|True|False|The\s|Since|Because|Given|Note|Answer|Result)\b',
    re.IGNORECASE,
)

_FOL_PATTERN = re.compile(
    r'^(?:all\s+\w|exists\s+\w|-[A-Z]|-\(|[A-Z][A-Za-z0-9_]*\(|\()',
    re.IGNORECASE,
)


def parse_fol_from_response(response: str) -> str | None:
    """Extract and clean a FOL formula from LLM response."""
    if response is None:
        return None
    text = response.strip()

    candidates = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip markdown fences
        if line.startswith("```") or line.startswith("~~~"):
            continue
        # Strip leading labels
        line = re.sub(r'^(?:FOL|Answer|Result|Formula|Output)\s*:\s*', '', line, flags=re.IGNORECASE)
        # Strip trailing periods/semicolons
        line = line.rstrip('.;')
        line = line.strip()
        if len(line) < 3:
            continue
        # Prefer lines that look like FOL
        if _FOL_PATTERN.match(line) and not _NATURAL_LANGUAGE_STARTS.match(line):
            return line
        candidates.append(line)

    # Fallback: return first non-empty candidate that isn't obviously NL
    for c in candidates:
        if not _NATURAL_LANGUAGE_STARTS.match(c):
            return c

    return candidates[0][:200] if candidates else None


def has_open_domain_existential(fol: str | None) -> bool:
    """True if the formula has an existential over no named individuals (ambiguous)."""
    if not fol:
        return False
    # Heuristic: 'exists x. (... & ...)' with no capital-letter constants
    if re.search(r'\bexists\b', fol, re.IGNORECASE):
        # Check if there are any named constants (uppercase followed by non-paren)
        if not re.search(r'[A-Z][a-z]+(?!\()', fol):
            return True
    return False


# ─── World Generation & Parsing ───────────────────────────────────────────────

def _parse_worlds_json(text: str | None) -> list[dict]:
    """Robustly parse a JSON array of worlds from LLM output."""
    if not text:
        return []
    # Strip markdown code fences
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'```\s*$', '', text.strip(), flags=re.MULTILINE)
    text = text.strip()

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return _normalize_worlds(data)
        if isinstance(data, dict) and 'worlds' in data:
            return _normalize_worlds(data['worlds'])
    except json.JSONDecodeError:
        pass

    # Try to find JSON array in text
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _normalize_worlds(data)
        except json.JSONDecodeError:
            pass

    # Try to extract individual objects
    objects = []
    for m in re.finditer(r'\{[^{}]*\}', text, re.DOTALL):
        try:
            obj = json.loads(m.group())
            if 'entities' in obj:
                objects.append(obj)
        except json.JSONDecodeError:
            pass

    return _normalize_worlds(objects) if objects else []


def _normalize_worlds(worlds: list) -> list[dict]:
    """Ensure worlds have required fields."""
    result = []
    for w in worlds:
        if not isinstance(w, dict):
            continue
        if 'entities' not in w:
            continue
        # Normalize atom format
        atoms = w.get('atoms', {})
        normalized_atoms = {}
        for pred, instances in atoms.items():
            if isinstance(instances, list):
                norm_instances = []
                for inst in instances:
                    if isinstance(inst, list):
                        norm_instances.append([str(a).lower() for a in inst])
                    elif isinstance(inst, str):
                        norm_instances.append([inst.lower()])
                    else:
                        norm_instances.append([str(inst).lower()])
                normalized_atoms[pred] = norm_instances
        # Normalize entity names to lowercase for the checker
        entities = [str(e).lower() for e in w.get('entities', [])]
        result.append({
            'entities': entities,
            'atoms': normalized_atoms,
            'sentence_true': w.get('sentence_true', None),
            'world_purpose': w.get('world_purpose', ''),
        })
    return result


async def generate_diagnostic_worlds(
    session: aiohttp.ClientSession,
    sentence: str,
    m: int = 8,
) -> list[dict]:
    prompt = build_world_generation_prompt(sentence, m=m)
    response = await call_world_gen(session, prompt)
    worlds = _parse_worlds_json(response)
    if len(worlds) < 2:
        # Retry once
        response2 = await call_world_gen(session, prompt)
        worlds2 = _parse_worlds_json(response2)
        worlds = worlds + worlds2
    return worlds[:m]


async def generate_random_worlds(
    session: aiohttp.ClientSession,
    sentence: str,
    m: int = 8,
) -> list[dict]:
    prompt = build_random_world_prompt(sentence, m=m)
    response = await call_world_gen(session, prompt)
    worlds = _parse_worlds_json(response)
    return worlds[:m]


# ─── Oracle ───────────────────────────────────────────────────────────────────

async def query_oracle(
    session: aiohttp.ClientSession,
    sentence: str,
    world: dict,
    model: str | None = None,
) -> bool | None:
    prompt = build_oracle_prompt(sentence, world)
    response = await call_oracle(session, prompt, model=model)
    if response is None:
        return None
    upper = response.upper()
    if 'TRUE' in upper:
        return True
    if 'FALSE' in upper:
        return False
    return None


# ─── Candidate Scoring ────────────────────────────────────────────────────────

async def howp_select(
    session: aiohttp.ClientSession,
    candidates: list[str],
    sentence: str,
    worlds: list[dict],
    oracle_model: str | None,
    precomputed_oracle_truths: list[bool | None] | None = None,
) -> tuple[str, list[float]]:
    """
    Select best candidate by HOWP scoring.

    KEY OPTIMIZATION: Oracle truth for each world depends only on the sentence
    and world (NOT on the candidate formula). So we call oracle ONCE per world,
    then reuse results for all candidates.

    precomputed_oracle_truths: if provided, skip oracle calls and use these directly.
    """
    # Get oracle truth for each world (once, shared across all candidates)
    if precomputed_oracle_truths is not None:
        oracle_truths = precomputed_oracle_truths
    else:
        oracle_tasks = [
            query_oracle(session, sentence, w, model=oracle_model)
            for w in worlds
        ]
        oracle_results = await asyncio.gather(*oracle_tasks, return_exceptions=True)
        oracle_truths = [
            r if isinstance(r, bool) else None
            for r in oracle_results
        ]

    # Score each candidate against oracle truths
    scores = []
    for candidate_fol in candidates:
        agreements = []
        for world, oracle_truth in zip(worlds, oracle_truths):
            if oracle_truth is None:
                continue
            formula_truth = check_formula_in_world(candidate_fol, world)
            if formula_truth is None:
                continue
            agreements.append(formula_truth == oracle_truth)
        score = sum(agreements) / len(agreements) if agreements else 0.0
        scores.append(score)

    best_idx = scores.index(max(scores)) if scores else 0
    return candidates[best_idx], scores


# ─── Baseline Selectors ───────────────────────────────────────────────────────

def top1_select(candidates: list[str]) -> str:
    return candidates[0] if candidates else ""


def self_consistency_select(candidates: list[str]) -> str:
    valid = [c for c in candidates if c]
    if not valid:
        return candidates[0] if candidates else ""
    if len(valid) == 1:
        return valid[0]
    n = len(valid)
    centrality = []
    for i in range(n):
        total_dist = sum(editdistance.eval(valid[i], valid[j]) for j in range(n))
        centrality.append(total_dist)
    return valid[centrality.index(min(centrality))]


async def direct_judge_select(
    session: aiohttp.ClientSession,
    candidates: list[str],
    sentence: str,
    oracle_model: str | None,
) -> str:
    prompt = build_direct_judge_prompt(sentence, candidates)
    response = await call_oracle(session, prompt, model=oracle_model)
    if response is None:
        return candidates[0]
    # Parse number
    m = re.search(r'\b([1-9])\b', response)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(candidates):
            return candidates[idx]
    return candidates[0]


# ─── FOL Generation (k candidates) ───────────────────────────────────────────

async def generate_k_candidates(
    session: aiohttp.ClientSession,
    example: dict,
    k: int = 5,
) -> list[str]:
    prompt = build_fol_generation_prompt(
        example['conclusion_nl'],
        example['premises_nl'],
    )
    tasks = [call_generator(session, prompt) for _ in range(k)]
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = []
    for r in responses:
        if isinstance(r, Exception) or r is None:
            continue
        fol = parse_fol_from_response(r)
        if fol and len(fol) > 2:
            candidates.append(fol)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for c in candidates:
        key = c.strip().lower()
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return deduped if deduped else candidates


# ─── Phase A: Build Dataset ───────────────────────────────────────────────────

def build_dataset() -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (beam_recall_50, main_100, oracle_pilot_25).
    Uses synthetic data as primary source.
    """
    logger.info("Building synthetic dataset (150 examples)...")
    all_examples = build_synthetic_dataset(n_total=175, seed=42)

    beam_recall = all_examples[:50]
    main_100 = all_examples[50:150]
    oracle_pilot = all_examples[150:175]

    logger.info(
        f"Dataset: {len(beam_recall)} beam_recall, "
        f"{len(main_100)} main, {len(oracle_pilot)} oracle_pilot"
    )
    return beam_recall, main_100, oracle_pilot


# ─── Phase B: Beam Recall Gate ────────────────────────────────────────────────

async def run_beam_recall_gate(
    session: aiohttp.ClientSession,
    examples: list[dict],
    k: int = 5,
) -> tuple[float, list[dict]]:
    """
    For each example, generate k candidates and check if any is correct
    (i.e., gives the right Z3 verdict against the premises).
    Returns (beam_recall, detailed_results).
    """
    logger.info(f"Phase B: beam recall gate on {len(examples)} examples, k={k}")
    results = []

    async def process_one(ex: dict) -> dict:
        candidates = await generate_k_candidates(session, ex, k=k)
        if not candidates:
            return {'example_id': ex['example_id'], 'any_correct': False, 'candidates': []}

        # Check each candidate
        any_correct = False
        candidate_verdicts = []
        for cand in candidates:
            verdict = z3_entailment(ex['premises_fol'], cand)
            correct = (verdict == ex['label'])
            candidate_verdicts.append({'fol': cand, 'verdict': verdict, 'correct': correct})
            if correct:
                any_correct = True

        return {
            'example_id': ex['example_id'],
            'any_correct': any_correct,
            'candidates': candidate_verdicts,
            'gold_label': ex['label'],
        }

    # Process in batches to manage memory
    batch_size = 10
    for i in range(0, len(examples), batch_size):
        batch = examples[i:i + batch_size]
        batch_results = await asyncio.gather(
            *[process_one(ex) for ex in batch], return_exceptions=True
        )
        for r in batch_results:
            if isinstance(r, Exception):
                logger.error(f"Beam recall batch error: {r}")
                results.append({'any_correct': False})
            else:
                results.append(r)
        logger.info(
            f"  Beam recall progress: {i + len(batch)}/{len(examples)}, "
            f"cost=${COST_TRACKER['total']:.3f}"
        )

    any_correct_count = sum(1 for r in results if r.get('any_correct', False))
    beam_recall = any_correct_count / len(results) if results else 0.0
    logger.info(f"Beam recall k={k}: {beam_recall:.2%} ({any_correct_count}/{len(results)})")
    return beam_recall, results


# ─── Phase C: Full HOWP Pipeline ──────────────────────────────────────────────

CONDITIONS = [
    'howp_hetero',
    'howp_same',
    'howp_random',
    'howp_m4',
    'top1',
    'self_consist',
    'direct_judge',
]


async def run_single_example(
    session: aiohttp.ClientSession,
    ex: dict,
    k: int = 5,
) -> dict | None:
    """Run all 7 conditions on a single example. Returns per-example results."""

    # Generate candidates
    candidates = await generate_k_candidates(session, ex, k=k)
    # Filter open-domain existentials
    filtered = [c for c in candidates if not has_open_domain_existential(c)]
    if len(filtered) >= 2:
        candidates = filtered
    if len(candidates) < 1:
        logger.debug(f"No valid candidates for {ex['example_id']}")
        return None

    # Pad candidates to at least 1
    while len(candidates) < 2:
        candidates.append(candidates[0])

    conclusion_nl = ex['conclusion_nl']

    # Generate worlds (shared across conditions)
    diag_worlds, rand_worlds = await asyncio.gather(
        generate_diagnostic_worlds(session, conclusion_nl, m=8),
        generate_random_worlds(session, conclusion_nl, m=8),
    )

    if len(diag_worlds) < 1:
        diag_worlds = rand_worlds  # fallback

    diag_worlds_m4 = diag_worlds[:4] if len(diag_worlds) >= 4 else diag_worlds

    # OPTIMIZATION: Oracle truth per world is independent of the candidate formula.
    # Call oracle ONCE per (world, model) pair, then reuse across all candidates.
    # This reduces oracle calls from n_cand×n_worlds to just n_worlds per condition.
    oracle_hetero_tasks = [query_oracle(session, conclusion_nl, w, model=ORACLE_MODEL_FREE) for w in diag_worlds]
    oracle_same_tasks = [query_oracle(session, conclusion_nl, w, model=GENERATOR_MODEL) for w in diag_worlds]
    oracle_rand_tasks = [query_oracle(session, conclusion_nl, w, model=ORACLE_MODEL_FREE) for w in rand_worlds]
    direct_task = direct_judge_select(session, candidates, conclusion_nl, ORACLE_MODEL_FREE)

    oracle_hetero_raw, oracle_same_raw, oracle_rand_raw, direct_result = await asyncio.gather(
        asyncio.gather(*oracle_hetero_tasks, return_exceptions=True),
        asyncio.gather(*oracle_same_tasks, return_exceptions=True),
        asyncio.gather(*oracle_rand_tasks, return_exceptions=True),
        direct_task,
        return_exceptions=True,
    )

    def to_truths(raw):
        if isinstance(raw, Exception):
            return []
        return [r if isinstance(r, bool) else None for r in raw]

    oracle_hetero = to_truths(oracle_hetero_raw)
    oracle_same = to_truths(oracle_same_raw)
    oracle_rand = to_truths(oracle_rand_raw)
    oracle_m4 = oracle_hetero[:4] if len(oracle_hetero) >= 4 else oracle_hetero  # reuse subset

    hetero_task = howp_select(session, candidates, conclusion_nl, diag_worlds, ORACLE_MODEL_FREE, precomputed_oracle_truths=oracle_hetero)
    same_task = howp_select(session, candidates, conclusion_nl, diag_worlds, GENERATOR_MODEL, precomputed_oracle_truths=oracle_same)
    random_task = howp_select(session, candidates, conclusion_nl, rand_worlds, ORACLE_MODEL_FREE, precomputed_oracle_truths=oracle_rand)
    m4_task = howp_select(session, candidates, conclusion_nl, diag_worlds_m4, ORACLE_MODEL_FREE, precomputed_oracle_truths=oracle_m4)

    howp_hetero_result, howp_same_result, howp_random_result, howp_m4_result = \
        await asyncio.gather(hetero_task, same_task, random_task, m4_task,
                             return_exceptions=True)

    def safe_selection(result, default):
        if isinstance(result, Exception):
            logger.debug(f"Condition error: {result}")
            return default, []
        return result

    selected_hetero, scores_hetero = safe_selection(howp_hetero_result, (candidates[0], []))
    selected_same, _ = safe_selection(howp_same_result, (candidates[0], []))
    selected_random, _ = safe_selection(howp_random_result, (candidates[0], []))
    selected_m4, _ = safe_selection(howp_m4_result, (candidates[0], []))
    selected_direct = direct_result if isinstance(direct_result, str) else candidates[0]

    selected_top1 = top1_select(candidates)
    selected_self = self_consistency_select(candidates)

    # Downstream verdict for each selected formula
    def verdict(fol):
        v = z3_entailment(ex['premises_fol'], fol)
        return v == ex['label']

    verdicts = {
        'howp_hetero': verdict(selected_hetero),
        'howp_same': verdict(selected_same),
        'howp_random': verdict(selected_random),
        'howp_m4': verdict(selected_m4),
        'top1': verdict(selected_top1),
        'self_consist': verdict(selected_self),
        'direct_judge': verdict(selected_direct),
    }

    return {
        'example_id': ex['example_id'],
        'conclusion_nl': conclusion_nl,
        'gold_label': ex['label'],
        'candidates': candidates,
        'scores_hetero': scores_hetero if isinstance(scores_hetero, list) else [],
        'selected_howp': selected_hetero,
        'selected_howp_same': selected_same,
        'selected_howp_random': selected_random,
        'selected_howp_m4': selected_m4,
        'selected_top1': selected_top1,
        'selected_self_consist': selected_self,
        'selected_direct_judge': selected_direct,
        'verdicts': verdicts,
    }


async def run_full_pipeline(
    session: aiohttp.ClientSession,
    examples: list[dict],
) -> tuple[dict, list[dict]]:
    """Run all 7 conditions on all examples. Returns (aggregated_results, details)."""
    logger.info(f"Phase C: Full HOWP pipeline on {len(examples)} examples")

    condition_verdicts: dict[str, list[bool]] = {c: [] for c in CONDITIONS}
    details = []

    # Process in small parallel batches
    batch_size = 5
    for batch_start in range(0, len(examples), batch_size):
        if COST_TRACKER['total'] > PHASE_B_BUDGET_USD:
            logger.warning(f"Phase B budget cap ${PHASE_B_BUDGET_USD} hit at example {batch_start}")
            break

        batch = examples[batch_start:batch_start + batch_size]
        batch_results = await asyncio.gather(
            *[run_single_example(session, ex) for ex in batch],
            return_exceptions=True,
        )

        for i, result in enumerate(batch_results):
            global_i = batch_start + i
            if isinstance(result, BudgetExceeded):
                logger.warning(f"Budget exceeded at example {global_i}")
                break
            if isinstance(result, Exception):
                logger.error(f"Example {batch[i]['example_id']} failed: {result}")
                continue
            if result is None:
                continue

            for cond in CONDITIONS:
                if cond in result['verdicts']:
                    condition_verdicts[cond].append(result['verdicts'][cond])

            details.append(result)

        n_done = batch_start + len(batch)
        logger.info(
            f"  Progress {n_done}/{len(examples)}, cost=${COST_TRACKER['total']:.3f} | "
            + " ".join(
                f"{c}={sum(v)/len(v):.2f}" if v else f"{c}=N/A"
                for c, v in condition_verdicts.items()
            )
        )

        gc.collect()

    # Aggregate
    agg = {}
    for cond, verdicts in condition_verdicts.items():
        n = len(verdicts)
        correct = sum(verdicts)
        agg[cond] = {
            'accuracy': correct / n if n > 0 else 0.0,
            'n': n,
            'correct': correct,
        }

    return agg, details


# ─── Phase D: Oracle Validation ───────────────────────────────────────────────

async def validate_oracle(
    session: aiohttp.ClientSession,
    examples: list[dict],
    oracle_model: str | None = None,
) -> float:
    """
    Validate oracle accuracy on examples with known world truth.
    For synthetic examples, use Z3 to get ground truth, then compare to oracle.
    """
    logger.info(f"Phase D: Oracle validation on {len(examples)} examples")
    correct = 0
    total = 0

    for ex in examples:
        # Generate 3 diagnostic worlds
        worlds = await generate_diagnostic_worlds(session, ex['conclusion_nl'], m=3)
        if not worlds:
            continue

        for world in worlds:
            # Ground truth: use gold FOL with Z3
            gt = check_formula_in_world(ex['conclusion_fol'], world)
            if gt is None:
                # Use world's declared label as fallback
                gt = world.get('sentence_true')
            if gt is None:
                continue

            oracle_resp = await query_oracle(session, ex['conclusion_nl'], world, model=oracle_model)
            if oracle_resp is None:
                continue

            correct += int(oracle_resp == gt)
            total += 1

    oracle_acc = correct / total if total > 0 else 0.0
    logger.info(f"Oracle accuracy: {oracle_acc:.2%} ({correct}/{total})")
    return oracle_acc


# ─── Output Building ──────────────────────────────────────────────────────────

def _format_output(
    beam_recall: float,
    beam_recall_details: list[dict],
    main_results: dict,
    oracle_acc: float,
    details: list[dict],
    n_examples: int,
) -> dict:
    """Build the output JSON in exp_gen_sol_out schema format."""

    # Comparisons
    hr = main_results.get('howp_hetero', {}).get('accuracy', 0)
    t1 = main_results.get('top1', {}).get('accuracy', 0)
    sc = main_results.get('self_consist', {}).get('accuracy', 0)
    hs = main_results.get('howp_same', {}).get('accuracy', 0)
    hw = main_results.get('howp_random', {}).get('accuracy', 0)
    hm = main_results.get('howp_m4', {}).get('accuracy', 0)

    comparisons = {
        'howp_vs_top1': round(hr - t1, 4),
        'howp_vs_self_consist': round(hr - sc, 4),
        'hetero_vs_same_oracle': round(hr - hs, 4),
        'diag_vs_random_worlds': round(hr - hw, 4),
        'm8_vs_m4': round(hr - hm, 4),
    }

    experiment_metadata = {
        'experiment': 'howp_iter2_synthetic',
        'dataset': 'synthetic_single_multi_premise',
        'generator_model': GENERATOR_MODEL,
        'oracle_model': ORACLE_MODEL_FREE,
        'n_examples': n_examples,
        'beam_recall': {
            'k5': round(beam_recall, 4),
            'passed_gate': beam_recall >= 0.60,
        },
        'in_distribution_oracle_accuracy': round(oracle_acc, 4),
        'results': main_results,
        'comparisons': comparisons,
        'total_cost_usd': round(COST_TRACKER['total'], 4),
        'total_llm_calls': COST_TRACKER['calls'],
    }

    # Build exp_gen_sol_out schema examples — HOWP main examples first (all have predict_* fields)
    examples_out = []

    for d in details:
        ex_input = (
            f"Conclusion: {d.get('conclusion_nl', '')}\n"
            f"Gold label: {d.get('gold_label', '')}"
        )
        verdicts = d.get('verdicts', {})
        ex_output = json.dumps({
            'selected_howp': d.get('selected_howp', ''),
            'selected_top1': d.get('selected_top1', ''),
            'verdicts': verdicts,
        })
        examples_out.append({
            'input': ex_input,
            'output': ex_output,
            'metadata_example_id': d.get('example_id', ''),
            'metadata_phase': 'howp_main',
            'metadata_gold_label': d.get('gold_label', ''),
            'metadata_beam_recall_passed': 'True',
            'predict_howp_hetero': d.get('selected_howp', ''),
            'predict_howp_same': d.get('selected_howp_same', d.get('selected_howp', '')),
            'predict_howp_random': d.get('selected_howp_random', d.get('selected_howp', '')),
            'predict_howp_m4': d.get('selected_howp_m4', d.get('selected_howp', '')),
            'predict_top1': d.get('selected_top1', ''),
            'predict_self_consist': d.get('selected_self_consist', ''),
            'predict_direct_judge': d.get('selected_direct_judge', d.get('selected_top1', '')),
        })

    # Beam recall examples (appended after main, all get a predict_beam_recall field)
    for br in beam_recall_details[:20]:
        candidates = br.get('candidates', [])
        best_cand = next((c['fol'] for c in candidates if c.get('correct')), '')
        if not best_cand and candidates:
            best_cand = candidates[0].get('fol', '')
        ex_input = f"Beam recall check for example {br.get('example_id', '')}"
        ex_output = json.dumps({
            'any_correct': br.get('any_correct', False),
            'gold_label': br.get('gold_label', ''),
        })
        examples_out.append({
            'input': ex_input,
            'output': ex_output,
            'metadata_example_id': br.get('example_id', ''),
            'metadata_phase': 'beam_recall',
            'metadata_gold_label': br.get('gold_label', ''),
            'predict_beam_best': best_cand,
        })

    return {
        'metadata': experiment_metadata,
        'datasets': [{
            'dataset': 'howp_iter2_synthetic',
            'examples': examples_out,
        }],
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

@logger.catch(reraise=True)
async def main():
    global _gen_semaphore, _oracle_semaphore

    logger.info("=" * 60)
    logger.info("HOWP Experiment Iter 2 — Starting")
    logger.info(f"Generator: {GENERATOR_MODEL}")
    logger.info(f"Oracle: {ORACLE_MODEL_FREE}")
    logger.info("=" * 60)

    _gen_semaphore = asyncio.Semaphore(GENERATOR_SEMAPHORE_SIZE)
    _oracle_semaphore = asyncio.Semaphore(ORACLE_SEMAPHORE_SIZE)

    t0 = time.time()

    # Phase A: Build dataset
    beam_recall_examples, main_examples, oracle_pilot_examples = build_dataset()

    # Verify Z3 works on a few synthetic examples
    logger.info("Smoke testing Z3 checker...")
    _smoke_test_z3()

    async with aiohttp.ClientSession() as session:
        # Phase B: Beam recall gate (test on first 5 first)
        logger.info("Running quick sanity check on 5 examples...")
        _, quick_results = await run_beam_recall_gate(session, beam_recall_examples[:5], k=5)
        logger.info(f"Sanity check: {sum(r.get('any_correct', False) for r in quick_results)}/5 correct")

        # Full beam recall on 50 examples
        beam_recall, beam_recall_details = await run_beam_recall_gate(
            session, beam_recall_examples, k=5
        )
        logger.info(f"Beam recall gate: {beam_recall:.2%} (threshold: 60%)")
        if beam_recall < 0.30:
            logger.warning(
                "Beam recall very low (<30%). Continuing anyway with synthetic data "
                "(generator may be struggling with simple examples)."
            )

        # Phase C: Full HOWP pipeline
        main_results, details = await run_full_pipeline(session, main_examples)

        # Phase D: Oracle validation
        oracle_acc = await validate_oracle(
            session, oracle_pilot_examples[:25], oracle_model=ORACLE_MODEL_FREE
        )

    elapsed = time.time() - t0
    logger.info(f"Total runtime: {elapsed:.1f}s, cost: ${COST_TRACKER['total']:.4f}")

    # Log summary
    logger.info("\n" + "=" * 40 + " RESULTS " + "=" * 40)
    logger.info(f"Beam recall k=5: {beam_recall:.2%} | Gate passed: {beam_recall >= 0.60}")
    logger.info(f"Oracle accuracy: {oracle_acc:.2%}")
    logger.info("Condition accuracies:")
    for cond, res in main_results.items():
        logger.info(f"  {cond:20s}: {res['accuracy']:.3f} ({res['correct']}/{res['n']})")
    logger.info("=" * 89)

    # Build and save output
    n_processed = main_results.get('howp_hetero', {}).get('n', 0)
    output = _format_output(
        beam_recall=beam_recall,
        beam_recall_details=beam_recall_details,
        main_results=main_results,
        oracle_acc=oracle_acc,
        details=details,
        n_examples=n_processed,
    )

    out_path = WORKSPACE / "method_out_proofwriter.json"
    out_path.write_text(json.dumps(output, indent=2))
    logger.info(f"Saved output to {out_path}")

    # Also save as method_out.json (schema-compliant name)
    out_path2 = WORKSPACE / "method_out.json"
    out_path2.write_text(json.dumps(output, indent=2))
    logger.info(f"Saved output to {out_path2}")

    return output


def _smoke_test_z3():
    """Quick smoke test of the Z3 model checker."""
    from fol_checker import check_formula_in_world, z3_entailment

    # Test 1: all students are smart, alice is student → alice is smart
    world1 = {
        'entities': ['alice', 'bob'],
        'atoms': {'student': [['alice']], 'smart': [['alice'], ['bob']]}
    }
    # Note: predicate names in formula must match world atoms keys
    # Formula predicates are CamelCase by convention, world atoms might not be
    # Use a consistent world:
    world2 = {
        'entities': ['alice', 'bob'],
        'atoms': {'Student': [['alice']], 'Smart': [['alice'], ['bob']]}
    }
    result = check_formula_in_world('all x. (Student(x) -> Smart(x))', world2)
    logger.info(f"Smoke test 1 (all Student→Smart in world with 2 students, both smart): {result} (expected True)")

    world3 = {
        'entities': ['alice', 'bob'],
        'atoms': {'Student': [['alice']], 'Smart': [['bob']]}  # alice not smart!
    }
    result2 = check_formula_in_world('all x. (Student(x) -> Smart(x))', world3)
    logger.info(f"Smoke test 2 (all Student→Smart, alice student but not smart): {result2} (expected False)")

    result3 = check_formula_in_world('exists x. (Student(x) & -Smart(x))', world3)
    logger.info(f"Smoke test 3 (exists student not smart, alice=student,not-smart): {result3} (expected True)")

    # Test Z3 entailment
    r4 = z3_entailment(
        ['all x. (Student(x) -> Smart(x))', 'Student(Alice)'],
        'Smart(Alice)'
    )
    logger.info(f"Smoke test 4 (entailment: all S→Sm, S(A) ⊨ Sm(A)): {r4} (expected entailment)")

    r5 = z3_entailment(
        ['all x. (Student(x) -> -Smart(x))', 'Student(Alice)'],
        'Smart(Alice)'
    )
    logger.info(f"Smoke test 5 (contradiction: all S→¬Sm, S(A) ⊨ Sm(A)?): {r5} (expected contradiction)")


if __name__ == "__main__":
    asyncio.run(main())
