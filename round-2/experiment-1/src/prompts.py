"""Prompt templates for the HOWP experiment (LINC-style)."""

FEW_SHOT_EXAMPLES = [
    {
        "premises": ["All students are smart.", "Alice is a student."],
        "conclusion_nl": "Alice is smart.",
        "conclusion_fol": "Smart(Alice)",
        "note": "Literal translation: 'Alice is smart' → Smart(Alice)",
    },
    {
        "premises": ["No cats are dogs.", "Whiskers is a cat."],
        "conclusion_nl": "Whiskers is not a dog.",
        "conclusion_fol": "-Dog(Whiskers)",
        "note": "Literal translation: 'not a dog' → negation prefix -",
    },
    {
        "premises": ["All fast cars are expensive.", "The Ferrari is a fast car."],
        "conclusion_nl": "The Ferrari is NOT expensive.",
        "conclusion_fol": "-Expensive(Ferrari)",
        "note": "Literal translation of the conclusion even if premises say otherwise",
    },
    {
        "premises": ["Some students are happy.", "No teachers are happy."],
        "conclusion_nl": "Alice is happy.",
        "conclusion_fol": "Happy(Alice)",
        "note": "Translate what the sentence SAYS, not what follows from premises",
    },
    {
        "premises": ["All birds can fly.", "Penguins are birds."],
        "conclusion_nl": "Some birds cannot fly.",
        "conclusion_fol": "exists x. (Bird(x) & -CanFly(x))",
        "note": "Existential quantifier for 'some'",
    },
]


def build_fol_generation_prompt(
    conclusion_nl: str,
    premises_nl: list[str],
) -> str:
    """Build LINC-style few-shot FOL generation prompt."""

    few_shot_str = ""
    for ex in FEW_SHOT_EXAMPLES:
        prems = "\n".join(f"{i+1}. {p}" for i, p in enumerate(ex["premises"]))
        few_shot_str += f"""<PREMISES>
{prems}
</PREMISES>
<CONCLUSION>
{ex['conclusion_nl']}
</CONCLUSION>
<EVALUATE>
TEXT: {ex['conclusion_nl']}
FOL: {ex['conclusion_fol']}
</EVALUATE>

"""

    prems_str = "\n".join(f"{i+1}. {p}" for i, p in enumerate(premises_nl))

    return f"""You are a first-order logic translator. Translate the CONCLUSION sentence literally into FOL.

CRITICAL RULES:
1. Output ONLY the FOL formula. No explanation. No natural language. Just the formula.
2. Translate LITERALLY what the conclusion sentence SAYS — do NOT reason about what follows from the premises.
3. If the conclusion says "X is P", write P(X). If it says "X is not P", write -P(X).
4. The premises provide context for vocabulary ONLY — ignore them for truth value.

FOL Syntax (Python NLTK logic module):
- Universal: all x. (P(x) -> Q(x))
- Existential: exists x. (P(x) & Q(x))
- Negation: -P(x)
- Conjunction: P(x) & Q(x)
- Named individuals: Capitalized (Alice, Bob)
- Variables: lowercase (x, y)
- Predicates: Capitalized (Student, Smart)

{few_shot_str}<PREMISES>
{prems_str}
</PREMISES>
<CONCLUSION>
{conclusion_nl}
</CONCLUSION>
<EVALUATE>
TEXT: {conclusion_nl}
FOL:"""


def build_world_generation_prompt(sentence: str, m: int = 8) -> str:
    return f"""Generate {m} diverse minimal world models to test the truth of this sentence:
"{sentence}"

Each world must:
1. List explicit named individuals (3-5, e.g. Alice, Bob, Carol)
2. List ground atoms that hold (predicate-argument pairs)
3. State whether the sentence is TRUE or FALSE in this world
4. Be designed to distinguish different logical interpretations (scope, negation, quantifier)

Return ONLY a valid JSON array of {m} world objects. Each object must have this exact structure:
{{
  "entities": ["Alice", "Bob", "Carol"],
  "atoms": {{"Student": [["Alice"], ["Bob"]], "Smart": [["Bob"]]}},
  "sentence_true": true,
  "world_purpose": "tests universal quantifier scope"
}}

The "atoms" field maps predicate names to lists of argument lists.
For binary predicates: {{"Reads": [["Alice", "Book1"]]}}
For unary predicates: {{"Student": [["Alice"]]}}

Return ONLY the JSON array, no other text."""


def build_random_world_prompt(sentence: str, m: int = 8) -> str:
    """Prompt for generating random (non-diagnostic) worlds."""
    return f"""Generate {m} random minimal world models. For each world, randomly assign truth values to predicates for named individuals.

Sentence to evaluate in each world: "{sentence}"

Return ONLY a valid JSON array of {m} world objects:
{{
  "entities": ["Alice", "Bob", "Carol"],
  "atoms": {{"Student": [["Alice"]], "Smart": [["Bob"], ["Carol"]]}},
  "sentence_true": true,
  "world_purpose": "random world"
}}

Make worlds varied and random. Return ONLY the JSON array."""


def build_oracle_prompt(sentence: str, world: dict) -> str:
    entities_str = ", ".join(str(e) for e in world.get("entities", []))
    atoms_parts = []
    for pred, instances in world.get("atoms", {}).items():
        for inst in instances:
            args_str = ", ".join(str(a) for a in inst)
            atoms_parts.append(f"{pred}({args_str})")
    atoms_str = "; ".join(atoms_parts) if atoms_parts else "(empty world)"

    return f"""Given this world:
Individuals: {entities_str}
True facts: {atoms_str}
(Closed-world assumption: all other facts are false)

Is the following sentence TRUE or FALSE in this world?
"{sentence}"

Answer with exactly one word: TRUE or FALSE."""


def build_direct_judge_prompt(sentence: str, candidates: list[str]) -> str:
    options = "\n".join(f"{i+1}. {c}" for i, c in enumerate(candidates))
    return f"""Which of these FOL formulas best translates the following sentence?
"{sentence}"

Options:
{options}

Answer with just the number (1, 2, 3, etc.) of the best formula."""
