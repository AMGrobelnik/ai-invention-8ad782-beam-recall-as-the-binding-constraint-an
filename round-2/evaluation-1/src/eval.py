#!/usr/bin/env python3
"""
Statistical re-analysis of HOWP FOLIO Experiment (iter_1).
Computes: bootstrap CIs, McNemar tests, normalized SC, Uncertain decomposition,
cross-world AUC, inter-model agreement.
"""
import gc
import json
import math
import os
import re
import resource
import sys
from pathlib import Path

import numpy as np
from loguru import logger

logger.remove()
logger.add(sys.stdout, level="INFO", format="{time:HH:mm:ss}|{level:<7}|{message}")
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

# ── hardware / memory limits ───────────────────────────────────────────────────
CGROUP_MEM = Path("/sys/fs/cgroup/memory.max")
try:
    raw = CGROUP_MEM.read_text().strip()
    TOTAL_RAM = int(raw) if raw != "max" else 4 * 1024**3
except Exception:
    TOTAL_RAM = 4 * 1024**3

RAM_BUDGET = min(int(TOTAL_RAM * 0.6), 16 * 1024**3)
resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))
logger.info(f"RAM budget: {RAM_BUDGET/1e9:.1f} GB")

# ── paths ─────────────────────────────────────────────────────────────────────
WORKSPACE = Path(__file__).parent
FULL_METHOD = Path(
    "/ai-inventor/aii_data/runs/run_7jKlp9zHTIUI"
    "/3_invention_loop/iter_1/gen_art/gen_art_experiment_1/full_method_out.json"
)

CONDITIONS = [
    "predict_main_method",
    "predict_top1_baseline",
    "predict_self_consistency",
    "predict_direct_judge",
    "predict_ablation_same_oracle",
    "predict_ablation_random_worlds",
    "predict_ablation_m4",
]
SHORT_NAMES = {
    "predict_main_method": "Hetero-Oracle",
    "predict_top1_baseline": "Top-1",
    "predict_self_consistency": "Self-Consist",
    "predict_direct_judge": "Direct-Judge",
    "predict_ablation_same_oracle": "Same-Oracle",
    "predict_ablation_random_worlds": "Rand-Worlds",
    "predict_ablation_m4": "m=4 Worlds",
}

N_BOOTSTRAP = 10_000
RNG = np.random.default_rng(42)

# ── helpers ───────────────────────────────────────────────────────────────────

def is_correct(pred: str, gold: str) -> bool:
    return pred != "" and pred == gold


def bootstrap_ci(correct_arr: np.ndarray, n: int = N_BOOTSTRAP) -> tuple[float, float]:
    """Return (lo, hi) 95% CI via bootstrap resampling."""
    means = np.empty(n)
    size = len(correct_arr)
    for i in range(n):
        idx = RNG.integers(0, size, size)
        means[i] = correct_arr[idx].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def cohen_h(p1: float, p2: float) -> float:
    return 2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2))


def bh_correction(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR correction."""
    n = len(pvalues)
    order = np.argsort(pvalues)
    pvalues_arr = np.array(pvalues)
    adjusted = np.zeros(n)
    for rank, idx in enumerate(order, start=1):
        adjusted[idx] = pvalues_arr[idx] * n / rank
    # enforce monotonicity (running minimum from largest)
    min_so_far = 1.0
    for idx in reversed(order):
        if adjusted[idx] > min_so_far:
            adjusted[idx] = min_so_far
        else:
            min_so_far = adjusted[idx]
    return [min(1.0, float(v)) for v in adjusted]


def alpha_rename_fol(formula: str) -> str:
    """
    Alpha-rename all bound variables to x0, x1, x2... in order of first binding.
    Handles: forall X. P, exists X. P, lambda X. P, plus parenthesized binders.
    """
    # Find all binder occurrences: 'forall X', 'exists X', 'lambda X'
    binder_re = re.compile(r'\b(forall|exists|lambda)\s+([A-Za-z_][A-Za-z0-9_]*)\b')
    # Map from original var name -> renamed var
    mapping: dict[str, str] = {}
    counter = [0]

    def replace_binder(m: re.Match) -> str:
        quant, var = m.group(1), m.group(2)
        if var not in mapping:
            mapping[var] = f"x{counter[0]}"
            counter[0] += 1
        return f"{quant} {mapping[var]}"

    renamed = binder_re.sub(replace_binder, formula)

    # Replace free occurrences of each original variable
    def replace_free(text: str, old: str, new: str) -> str:
        return re.sub(r'\b' + re.escape(old) + r'\b', new, text)

    for old, new in mapping.items():
        renamed = replace_free(renamed, old, new)

    return renamed.strip().lower()


def normalize_fol_candidate(candidate: dict) -> str:
    """Normalize a candidate dict to a single comparable string."""
    parts = []
    for p in candidate.get("premises_fol", []):
        parts.append(alpha_rename_fol(str(p)))
    parts.append(alpha_rename_fol(str(candidate.get("conclusion_fol", ""))))
    s = " ".join(parts)
    return re.sub(r'\s+', '', s)


def jaccard_similarity(a: str, b: str) -> float:
    ta, tb = set(a), set(b)
    if not ta and not tb:
        return 1.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union > 0 else 0.0


def cluster_and_pick(candidates_str: str) -> int:
    """
    Parse candidates JSON, cluster by Jaccard token overlap, return index of
    centroid of largest cluster. Returns -1 if parsing fails.
    """
    try:
        candidates = json.loads(candidates_str)
    except Exception:
        return -1
    n = len(candidates)
    if n == 0:
        return -1

    norms = [normalize_fol_candidate(c) for c in candidates]

    # Similarity matrix
    sim = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            s = jaccard_similarity(norms[i], norms[j])
            sim[i, j] = s
            sim[j, i] = s

    # Greedy single-link clustering: threshold = 0.3 (same as original approach)
    threshold = 0.3
    clusters: list[list[int]] = []
    assigned = [-1] * n
    for i in range(n):
        best_cluster = -1
        for ci, cl in enumerate(clusters):
            if any(sim[i, j] >= threshold for j in cl):
                best_cluster = ci
                break
        if best_cluster == -1:
            clusters.append([i])
            assigned[i] = len(clusters) - 1
        else:
            clusters[best_cluster].append(i)
            assigned[i] = best_cluster

    # Largest cluster (ties broken by lowest cluster index → lowest member index)
    largest_ci = max(range(len(clusters)), key=lambda ci: (len(clusters[ci]), -ci))
    cluster_members = clusters[largest_ci]

    # Centroid = member with highest average similarity to rest of cluster
    if len(cluster_members) == 1:
        return cluster_members[0]

    best_idx = min(cluster_members, key=lambda i: -np.mean([sim[i, j] for j in cluster_members]))
    return best_idx


# ── figures ───────────────────────────────────────────────────────────────────

def plot_ci_forest(results: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    conditions = list(results["per_condition"].keys())
    accuracies = [results["per_condition"][c]["accuracy"] for c in conditions]
    lows = [results["per_condition"][c]["ci_95_lo"] for c in conditions]
    highs = [results["per_condition"][c]["ci_95_hi"] for c in conditions]
    names = [SHORT_NAMES.get(c, c) for c in conditions]

    order = np.argsort(accuracies)
    conditions = [conditions[i] for i in order]
    accuracies = [accuracies[i] for i in order]
    lows = [lows[i] for i in order]
    highs = [highs[i] for i in order]
    names = [names[i] for i in order]

    top1_acc = results["per_condition"]["predict_top1_baseline"]["accuracy"]

    fig, ax = plt.subplots(figsize=(8, 5))
    y = np.arange(len(conditions))
    xerr_lo = [accuracies[i] - lows[i] for i in range(len(conditions))]
    xerr_hi = [highs[i] - accuracies[i] for i in range(len(conditions))]

    colors = ["#2196F3" if c == "predict_main_method" else "#90CAF9" for c in conditions]
    ax.barh(y, accuracies, xerr=[xerr_lo, xerr_hi], align='center',
            color=colors, ecolor='black', capsize=4, height=0.6)
    ax.axvline(top1_acc, color='red', linestyle='--', linewidth=1.5, label=f'Top-1 baseline ({top1_acc:.3f})')
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=10)
    ax.set_xlabel("Accuracy ± 95% Bootstrap CI", fontsize=11)
    ax.set_title("Condition Accuracy Forest Plot (FOLIO, n=204)", fontsize=12)
    ax.legend(fontsize=9)
    ax.set_xlim(0, 0.5)
    ax.grid(axis='x', alpha=0.3)
    plt.tight_layout()
    path = WORKSPACE / "figures" / "ci_forest_plot.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"Saved {path}")


def plot_agreement_score_violin(examples: list[dict]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    correct_scores = []
    wrong_scores = []
    for ex in examples:
        try:
            ws = json.loads(ex["metadata_world_scores"])
        except Exception:
            continue
        max_ws = max(ws)
        pred = ex["predict_main_method"]
        gold = ex["metadata_gold_label"]
        if pred != "" and pred == gold:
            correct_scores.append(max_ws)
        else:
            wrong_scores.append(max_ws)

    fig, ax = plt.subplots(figsize=(6, 5))
    data = [correct_scores, wrong_scores]
    labels = [f"Correct\n(n={len(correct_scores)})", f"Incorrect\n(n={len(wrong_scores)})"]
    parts = ax.violinplot(data, positions=[1, 2], showmedians=True, showextrema=True)
    for pc, color in zip(parts['bodies'], ['#4CAF50', '#F44336']):
        pc.set_facecolor(color)
        pc.set_alpha(0.7)
    ax.set_xticks([1, 2])
    ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylabel("Max World Agreement Score", fontsize=11)
    ax.set_title("World Agreement Score: Correct vs Incorrect Selections", fontsize=11)
    ax.set_ylim(-0.05, 1.05)
    ax.grid(axis='y', alpha=0.3)
    plt.tight_layout()
    path = WORKSPACE / "figures" / "agreement_score_violin.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"Saved {path}")


def plot_mcnemar_heatmap(mcnemar_matrix: dict, conditions: list[str]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors

    n = len(conditions)
    pmat = np.full((n, n), np.nan)
    hmat = np.full((n, n), np.nan)

    for key, vals in mcnemar_matrix.items():
        a, b = key.split("__vs__")
        if a in conditions and b in conditions:
            i, j = conditions.index(a), conditions.index(b)
            p_adj = vals.get("p_value_adjusted", 1.0)
            h = vals.get("cohen_h", 0.0)
            pmat[i, j] = -math.log10(max(p_adj, 1e-10))
            hmat[i, j] = h
            pmat[j, i] = pmat[i, j]
            hmat[j, i] = -h

    short = [SHORT_NAMES.get(c, c) for c in conditions]
    fig, ax = plt.subplots(figsize=(9, 7))
    im = ax.imshow(pmat, cmap='YlOrRd', vmin=0, aspect='auto')
    plt.colorbar(im, ax=ax, label="-log10(adjusted p-value)")
    ax.set_xticks(range(n))
    ax.set_xticklabels(short, rotation=45, ha='right', fontsize=9)
    ax.set_yticks(range(n))
    ax.set_yticklabels(short, fontsize=9)
    ax.set_title("McNemar Pairwise Test: -log10(BH-adjusted p-value)\nAnnotated with Cohen's h", fontsize=11)

    for i in range(n):
        for j in range(n):
            if not np.isnan(hmat[i, j]):
                ax.text(j, i, f"{hmat[i, j]:.2f}", ha='center', va='center',
                        fontsize=7, color='black')

    plt.tight_layout()
    path = WORKSPACE / "figures" / "mcnemar_pvalue_heatmap.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"Saved {path}")


# ── main computation ──────────────────────────────────────────────────────────

@logger.catch(reraise=True)
def main() -> None:
    logger.info("Loading full_method_out.json")
    data = json.loads(FULL_METHOD.read_text())
    examples = data["datasets"][0]["examples"]
    n_total = len(examples)
    logger.info(f"Loaded {n_total} examples")

    # ── 1. Per-condition accuracy + bootstrap CIs ─────────────────────────────
    logger.info("Computing per-condition accuracy + bootstrap CIs")
    per_condition: dict[str, dict] = {}

    for cond in CONDITIONS:
        preds = [ex.get(cond, "") for ex in examples]
        golds = [ex["metadata_gold_label"] for ex in examples]
        correct = np.array([int(is_correct(p, g)) for p, g in zip(preds, golds)])
        acc = float(correct.mean())
        ci_lo, ci_hi = bootstrap_ci(correct)
        # empty string rate
        empty_rate = float(sum(1 for p in preds if p == "") / n_total)
        per_condition[cond] = {
            "accuracy": acc,
            "ci_95_lo": ci_lo,
            "ci_95_hi": ci_hi,
            "n": n_total,
            "n_correct": int(correct.sum()),
            "empty_string_rate": empty_rate,
        }
        logger.info(f"  {SHORT_NAMES[cond]}: acc={acc:.4f} CI=[{ci_lo:.4f},{ci_hi:.4f}] empty={empty_rate:.3f}")

    # Per-label breakdown for main method
    main_pred = [ex.get("predict_main_method", "") for ex in examples]
    golds_all = [ex["metadata_gold_label"] for ex in examples]
    labels = ["Entailment", "Contradiction", "Uncertain"]
    per_label_main: dict[str, dict] = {}
    for lbl in labels:
        idx = [i for i, g in enumerate(golds_all) if g == lbl]
        if idx:
            correct_lbl = [int(is_correct(main_pred[i], golds_all[i])) for i in idx]
            per_label_main[lbl] = {
                "accuracy": float(np.mean(correct_lbl)),
                "n": len(idx),
                "n_correct": int(sum(correct_lbl)),
            }
    per_condition["predict_main_method"]["per_label"] = per_label_main

    # ── 2. McNemar's test — all 15 pairwise comparisons ───────────────────────
    logger.info("Computing McNemar pairwise tests")
    from itertools import combinations
    from scipy.stats import chi2

    mcnemar_results: dict[str, dict] = {}
    raw_pvalues: list[float] = []
    pair_keys: list[str] = []

    golds_arr = np.array(golds_all)

    for cA, cB in combinations(CONDITIONS, 2):
        pA_arr = np.array([ex.get(cA, "") for ex in examples])
        pB_arr = np.array([ex.get(cB, "") for ex in examples])

        # Exclude rows where either prediction is empty
        mask = (pA_arr != "") & (pB_arr != "")
        pA_m = pA_arr[mask]
        pB_m = pB_arr[mask]
        gA_m = golds_arr[mask]

        corrA = pA_m == gA_m
        corrB = pB_m == gA_m
        n00 = int(((~corrA) & (~corrB)).sum())
        n01 = int(((~corrA) & corrB).sum())   # B only correct
        n10 = int((corrA & (~corrB)).sum())    # A only correct
        n11 = int((corrA & corrB).sum())

        b, c = n10, n01  # discordant cells
        n_m = int(mask.sum())
        accA = float(corrA.mean()) if n_m > 0 else 0.0
        accB = float(corrB.mean()) if n_m > 0 else 0.0

        if b + c == 0:
            stat, pval = 0.0, 1.0
        elif b + c >= 25:
            # chi-squared approximation
            stat = (abs(b - c) - 1) ** 2 / (b + c)
            pval = float(1 - chi2.cdf(stat, df=1))
        else:
            # exact binomial (sign test)
            from scipy.stats import binom
            p_exact = binom.pmf(min(b, c), b + c, 0.5)
            # two-tailed
            pval = min(1.0, 2 * float(p_exact))
            stat = float((b - c) ** 2 / max(b + c, 1))

        h = cohen_h(accA, accB)
        key = f"{cA}__vs__{cB}"
        mcnemar_results[key] = {
            "condition_A": cA,
            "condition_B": cB,
            "n_valid": n_m,
            "n00": n00, "n01": n01, "n10": n10, "n11": n11,
            "chi2_stat": float(stat),
            "p_value_raw": float(pval),
            "cohen_h": float(h),
            "acc_A": accA,
            "acc_B": accB,
        }
        raw_pvalues.append(pval)
        pair_keys.append(key)

    # BH correction
    adjusted = bh_correction(raw_pvalues)
    for key, adj_p in zip(pair_keys, adjusted):
        mcnemar_results[key]["p_value_adjusted"] = adj_p

    # Log primary comparisons
    primary = [
        ("predict_main_method", "predict_top1_baseline", "hetero-oracle vs top-1"),
        ("predict_main_method", "predict_self_consistency", "hetero-oracle vs self-consistency"),
        ("predict_main_method", "predict_ablation_same_oracle", "hetero-oracle vs same-model-oracle"),
    ]
    for cA, cB, label in primary:
        key = f"{cA}__vs__{cB}"
        r = mcnemar_results[key]
        logger.info(f"  [{label}] chi2={r['chi2_stat']:.3f} p_raw={r['p_value_raw']:.4f} "
                    f"p_adj={r['p_value_adjusted']:.4f} h={r['cohen_h']:.3f}")

    # ── 3. Normalized self-consistency ────────────────────────────────────────
    logger.info("Computing normalized self-consistency")
    norm_sc_preds: list[str] = []
    orig_sc_preds = [ex.get("predict_self_consistency", "") for ex in examples]
    n_changed = 0
    n_parse_fail = 0

    for i, ex in enumerate(examples):
        cands_str = ex.get("metadata_candidates", "[]")
        try:
            candidates = json.loads(cands_str)
        except Exception:
            norm_sc_preds.append("")
            n_parse_fail += 1
            continue

        best_idx = cluster_and_pick(cands_str)
        if best_idx < 0 or best_idx >= len(candidates):
            norm_sc_preds.append("")
            n_parse_fail += 1
            continue

        # Simulate downstream prediction: we need to know what the original SC selected
        # We replicate the original cluster_and_pick on raw strings to find orig_idx
        # Then compare with best_idx from normalized version
        # For the normalized version, just use best_idx's original (un-executed) prediction
        # Since we don't have Z3 results per-candidate, we approximate:
        # If the best candidate index matches what orig SC would select, use orig SC prediction
        # Otherwise we can't know the downstream result without re-running Z3.
        # Strategy: if normalization changed the selected index, mark as "unknown" (empty)
        # but report the fraction changed.
        # Actually — the original SC index can be inferred from metadata_candidates and metadata_world_scores.
        # The original SC used raw Jaccard on premise+conclusion strings concatenated.
        # We re-cluster raw to find original index.

        norms = [normalize_fol_candidate(c) for c in candidates]
        # Original (raw) clustering — same as original code but without alpha-renaming
        raw_norms = []
        for c in candidates:
            parts = []
            for p in c.get("premises_fol", []):
                parts.append(str(p))
            parts.append(str(c.get("conclusion_fol", "")))
            raw_norms.append(re.sub(r'\s+', '', " ".join(parts).lower()))

        # cluster on raw
        threshold = 0.3
        clusters_raw: list[list[int]] = []
        for idx2 in range(len(candidates)):
            placed = False
            for cl in clusters_raw:
                if any(jaccard_similarity(raw_norms[idx2], raw_norms[j]) >= threshold for j in cl):
                    cl.append(idx2)
                    placed = True
                    break
            if not placed:
                clusters_raw.append([idx2])
        largest_ci_raw = max(range(len(clusters_raw)), key=lambda ci: (len(clusters_raw[ci]), -ci))
        orig_idx = min(clusters_raw[largest_ci_raw])  # centroid = lowest index in largest cluster

        if best_idx != orig_idx:
            n_changed += 1
            # Can't evaluate without Z3; use empty to be conservative
            norm_sc_preds.append("")
        else:
            norm_sc_preds.append(orig_sc_preds[i])

    norm_sc_correct = np.array([int(is_correct(p, g)) for p, g in zip(norm_sc_preds, golds_all)])
    norm_sc_acc = float(norm_sc_correct.mean())
    orig_sc_correct = np.array([int(is_correct(p, g)) for p, g in zip(orig_sc_preds, golds_all)])
    orig_sc_acc = float(orig_sc_correct.mean())
    frac_changed = float(n_changed / n_total)

    normalized_sc = {
        "normalized_sc_accuracy": norm_sc_acc,
        "original_sc_accuracy": orig_sc_acc,
        "delta_vs_original_sc": float(norm_sc_acc - orig_sc_acc),
        "fraction_changed": frac_changed,
        "n_changed": n_changed,
        "n_parse_fail": n_parse_fail,
    }
    logger.info(f"Normalized SC: acc={norm_sc_acc:.4f} orig={orig_sc_acc:.4f} "
                f"delta={norm_sc_acc-orig_sc_acc:.4f} changed={frac_changed:.3f}")

    # ── 4. Uncertain-class decomposition ─────────────────────────────────────
    logger.info("Computing Uncertain-class decomposition")
    gold_uncertain_idx = [i for i, g in enumerate(golds_all) if g == "Uncertain"]
    n_gold_unc = len(gold_uncertain_idx)

    main_preds = [ex.get("predict_main_method", "") for ex in examples]
    unc_predicted_as_uncertain = sum(1 for i in gold_uncertain_idx if main_preds[i] == "Uncertain")
    unc_predicted_as_empty = sum(1 for i in gold_uncertain_idx if main_preds[i] == "")
    unc_predicted_as_other = sum(1 for i in gold_uncertain_idx
                                  if main_preds[i] not in ("Uncertain", "") and main_preds[i] != "Uncertain")

    # Coincidence hits: gold=Uncertain AND pred=Uncertain
    coincidence_hits = unc_predicted_as_uncertain
    total_main_correct_unc = per_condition["predict_main_method"]["per_label"].get("Uncertain", {}).get("n_correct", 0)

    uncertain_decomp = {
        "n_gold_uncertain": n_gold_unc,
        "main_predicted_uncertain_given_gold_uncertain": unc_predicted_as_uncertain,
        "main_predicted_empty_given_gold_uncertain": unc_predicted_as_empty,
        "main_predicted_wrong_given_gold_uncertain": unc_predicted_as_other,
        "coincidence_hits": coincidence_hits,
        "total_main_correct_uncertain": total_main_correct_unc,
        "coincidence_fraction_of_uncertain_accuracy": float(coincidence_hits / max(total_main_correct_unc, 1)),
    }

    # Empty string rates per condition across all examples
    empty_rates = {c: per_condition[c]["empty_string_rate"] for c in CONDITIONS}
    uncertain_decomp["empty_string_rates_all_conditions"] = empty_rates

    logger.info(f"Gold-Uncertain ({n_gold_unc}): pred=Uncertain={unc_predicted_as_uncertain}, "
                f"pred=empty={unc_predicted_as_empty}, pred=wrong={unc_predicted_as_other}")

    # ── 5. Cross-world agreement AUC ─────────────────────────────────────────
    logger.info("Computing cross-world agreement AUC")
    from sklearn.metrics import roc_auc_score
    from scipy.stats import kendalltau

    world_scores_list: list[float] = []
    binary_correct: list[int] = []
    for ex in examples:
        try:
            ws = json.loads(ex["metadata_world_scores"])
            max_ws = float(max(ws))
        except Exception:
            max_ws = 0.5
        pred = ex.get("predict_main_method", "")
        gold = ex["metadata_gold_label"]
        world_scores_list.append(max_ws)
        binary_correct.append(int(is_correct(pred, gold)))

    ws_arr = np.array(world_scores_list)
    bc_arr = np.array(binary_correct)

    # AUC requires both classes present
    if len(set(binary_correct)) < 2:
        auc_roc = float("nan")
        logger.warning("Only one class in binary_correct — AUC undefined")
    else:
        auc_roc = float(roc_auc_score(bc_arr, ws_arr))

    tau, tau_pval = kendalltau(ws_arr, bc_arr)

    # Summary stats per group
    correct_ws = ws_arr[bc_arr == 1]
    wrong_ws = ws_arr[bc_arr == 0]

    cross_world_auc = {
        "auc_roc": auc_roc,
        "kendall_tau": float(tau),
        "kendall_tau_pval": float(tau_pval),
        "n_correct": int(bc_arr.sum()),
        "n_incorrect": int((bc_arr == 0).sum()),
        "mean_max_ws_correct": float(correct_ws.mean()) if len(correct_ws) > 0 else float("nan"),
        "mean_max_ws_incorrect": float(wrong_ws.mean()) if len(wrong_ws) > 0 else float("nan"),
        "std_max_ws_correct": float(correct_ws.std()) if len(correct_ws) > 0 else float("nan"),
        "std_max_ws_incorrect": float(wrong_ws.std()) if len(wrong_ws) > 0 else float("nan"),
    }
    logger.info(f"Agreement AUC={auc_roc:.4f} tau={tau:.4f} (p={tau_pval:.4f})")

    # ── 6. Inter-model agreement rate ─────────────────────────────────────────
    logger.info("Computing inter-model agreement rate")
    hetero_preds = [ex.get("predict_main_method", "") for ex in examples]
    homo_preds = [ex.get("predict_ablation_same_oracle", "") for ex in examples]

    # Agreement on prediction (same label string, including empty)
    agree = sum(1 for h, s in zip(hetero_preds, homo_preds) if h == s)
    agree_rate = float(agree / n_total)

    hetero_correct = np.array([int(is_correct(p, g)) for p, g in zip(hetero_preds, golds_all)])
    homo_correct = np.array([int(is_correct(p, g)) for p, g in zip(homo_preds, golds_all)])

    both_correct = int((hetero_correct & homo_correct).sum())
    both_wrong = int(((1 - hetero_correct) & (1 - homo_correct)).sum())
    only_hetero = int((hetero_correct & (1 - homo_correct)).sum())
    only_homo = int(((1 - hetero_correct) & homo_correct).sum())

    # Conditional accuracy: hetero correct given homo wrong
    homo_wrong_idx = np.where(homo_correct == 0)[0]
    cond_hetero_given_homo_wrong = float(hetero_correct[homo_wrong_idx].mean()) if len(homo_wrong_idx) > 0 else float("nan")

    inter_model = {
        "inter_model_agreement_rate": agree_rate,
        "n_agree": agree,
        "both_correct": both_correct,
        "both_wrong": both_wrong,
        "only_hetero_correct": only_hetero,
        "only_homo_correct": only_homo,
        "conditional_accuracy_hetero_given_homo_wrong": cond_hetero_given_homo_wrong,
        "mcnemar_key": "predict_main_method__vs__predict_ablation_same_oracle",
    }
    logger.info(f"Inter-model agreement={agree_rate:.4f} cond_acc_hetero|homo_wrong={cond_hetero_given_homo_wrong:.4f}")

    # ── Figures ───────────────────────────────────────────────────────────────
    logger.info("Generating figures")
    (WORKSPACE / "figures").mkdir(exist_ok=True)

    results_for_plots = {"per_condition": per_condition}
    plot_ci_forest(results_for_plots)
    plot_agreement_score_violin(examples)
    plot_mcnemar_heatmap(mcnemar_results, CONDITIONS)

    # ── Assemble eval_out.json ────────────────────────────────────────────────
    logger.info("Assembling eval_out.json")

    # metrics_agg: flat numeric metrics required by schema
    metrics_agg: dict[str, float] = {}
    for cond in CONDITIONS:
        sn = SHORT_NAMES[cond].replace("-", "_").replace("=", "_").replace(" ", "_")
        metrics_agg[f"acc_{sn}"] = per_condition[cond]["accuracy"]
        metrics_agg[f"ci_lo_{sn}"] = per_condition[cond]["ci_95_lo"]
        metrics_agg[f"ci_hi_{sn}"] = per_condition[cond]["ci_95_hi"]

    # primary McNemar metrics
    for cA, cB, label in primary:
        key = f"{cA}__vs__{cB}"
        r = mcnemar_results[key]
        slug = label.replace(" ", "_").replace("-", "_")
        metrics_agg[f"mcnemar_chi2_{slug}"] = r["chi2_stat"]
        metrics_agg[f"mcnemar_p_adj_{slug}"] = r["p_value_adjusted"]
        metrics_agg[f"mcnemar_h_{slug}"] = r["cohen_h"]

    metrics_agg["normalized_sc_accuracy"] = normalized_sc["normalized_sc_accuracy"]
    metrics_agg["normalized_sc_delta"] = normalized_sc["delta_vs_original_sc"]
    metrics_agg["normalized_sc_fraction_changed"] = normalized_sc["fraction_changed"]
    metrics_agg["uncertain_coincidence_hits"] = float(uncertain_decomp["coincidence_hits"])
    metrics_agg["uncertain_n_gold"] = float(uncertain_decomp["n_gold_uncertain"])
    metrics_agg["uncertain_coincidence_fraction"] = uncertain_decomp["coincidence_fraction_of_uncertain_accuracy"]
    metrics_agg["cross_world_auc_roc"] = cross_world_auc["auc_roc"]
    metrics_agg["cross_world_kendall_tau"] = cross_world_auc["kendall_tau"]
    metrics_agg["inter_model_agreement_rate"] = inter_model["inter_model_agreement_rate"]
    metrics_agg["conditional_acc_hetero_given_homo_wrong"] = inter_model["conditional_accuracy_hetero_given_homo_wrong"]

    # Remove any NaN/inf (JSON can't handle them)
    def clean_float(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    def clean_dict(d):
        if isinstance(d, dict):
            return {k: clean_dict(v) for k, v in d.items()}
        if isinstance(d, list):
            return [clean_dict(v) for v in d]
        if isinstance(d, float):
            return clean_float(d)
        return d

    metrics_agg_clean = {k: clean_float(v) for k, v in metrics_agg.items() if clean_float(v) is not None}
    # Ensure numeric (remove None from required numeric schema)
    metrics_agg_final = {k: v for k, v in metrics_agg_clean.items() if isinstance(v, (int, float))}

    # Per-example eval fields
    eval_examples = []
    for i, ex in enumerate(examples):
        item = {
            "input": ex["input"],
            "output": ex["output"],
        }
        # copy predict_ fields
        for k in ex:
            if k.startswith("predict_") or k.startswith("metadata_"):
                item[k] = ex[k]
        # per-example eval metrics
        item["eval_main_correct"] = float(is_correct(ex.get("predict_main_method", ""), ex["metadata_gold_label"]))
        item["eval_top1_correct"] = float(is_correct(ex.get("predict_top1_baseline", ""), ex["metadata_gold_label"]))
        item["eval_sc_correct"] = float(is_correct(ex.get("predict_self_consistency", ""), ex["metadata_gold_label"]))
        item["eval_direct_judge_correct"] = float(is_correct(ex.get("predict_direct_judge", ""), ex["metadata_gold_label"]))
        item["eval_same_oracle_correct"] = float(is_correct(ex.get("predict_ablation_same_oracle", ""), ex["metadata_gold_label"]))
        item["eval_rand_worlds_correct"] = float(is_correct(ex.get("predict_ablation_random_worlds", ""), ex["metadata_gold_label"]))
        item["eval_m4_correct"] = float(is_correct(ex.get("predict_ablation_m4", ""), ex["metadata_gold_label"]))
        try:
            ws = json.loads(ex.get("metadata_world_scores", "[]"))
            item["eval_max_world_score"] = float(max(ws)) if ws else 0.5
        except Exception:
            item["eval_max_world_score"] = 0.5
        eval_examples.append(item)

    eval_out = {
        "metadata": {
            "evaluation_name": "HOWP_FOLIO_statistical_reanalysis",
            "n_examples": n_total,
            "conditions": CONDITIONS,
            "per_condition_accuracy": {c: per_condition[c]["accuracy"] for c in CONDITIONS},
            "per_condition_ci": {c: [per_condition[c]["ci_95_lo"], per_condition[c]["ci_95_hi"]] for c in CONDITIONS},
            "per_label_main": per_label_main,
            "mcnemar_all_pairs": clean_dict(mcnemar_results),
            "normalized_self_consistency": clean_dict(normalized_sc),
            "uncertain_decomposition": clean_dict(uncertain_decomp),
            "cross_world_auc": clean_dict(cross_world_auc),
            "inter_model_agreement": clean_dict(inter_model),
        },
        "metrics_agg": metrics_agg_final,
        "datasets": [
            {
                "dataset": "yale-nlp/folio",
                "examples": eval_examples,
            }
        ],
    }

    out_path = WORKSPACE / "eval_out.json"
    out_path.write_text(json.dumps(eval_out, indent=2))
    logger.info(f"Saved eval_out.json ({out_path.stat().st_size / 1e6:.1f} MB)")

    logger.info("=== SUMMARY ===")
    for cond in CONDITIONS:
        r = per_condition[cond]
        logger.info(f"  {SHORT_NAMES[cond]:18s}: {r['accuracy']:.4f} [{r['ci_95_lo']:.4f},{r['ci_95_hi']:.4f}]")
    logger.info(f"  AUC-ROC (world score): {auc_roc:.4f}")
    logger.info(f"  Inter-model agreement: {agree_rate:.4f}")
    logger.info("Done.")


if __name__ == "__main__":
    main()
