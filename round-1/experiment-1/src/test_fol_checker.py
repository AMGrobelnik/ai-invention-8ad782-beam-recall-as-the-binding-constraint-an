#!/usr/bin/env python3
"""Smoke tests for FOL checker and Z3 evaluator."""
import sys
sys.path.insert(0, ".")

from fol_checker import eval_formula, parse_world_from_llm
from z3_evaluator import folio_inference

# FOL checker tests
tests = [
    ("forall X. (Student(X) -> Smart(X))",
     {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Smart(alice)": True, "Student(bob)": True, "Smart(bob)": True}},
     True),
    ("forall X. (Student(X) -> Smart(X))",
     {"domain": ["alice"], "atoms": {"Student(alice)": True, "Smart(alice)": False}},
     False),
    ("exists X. Cat(X)",
     {"domain": ["fluffy"], "atoms": {"Cat(fluffy)": True}},
     True),
    ("not exists X. Dog(X)",
     {"domain": ["rex"], "atoms": {"Dog(rex)": True}},
     False),
    ("forall X. (Bird(X) -> CanFly(X))",
     {"domain": ["tweety"], "atoms": {"Bird(tweety)": True, "CanFly(tweety)": True}},
     True),
    ("forall X. (Bird(X) -> CanFly(X))",
     {"domain": ["tweety", "sam"], "atoms": {"Bird(tweety)": True, "CanFly(tweety)": True, "Bird(sam)": True, "CanFly(sam)": False}},
     False),
    ("Student(alice) & not Student(bob)",
     {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Student(bob)": False}},
     True),
    ("exists X. (Student(X) & Smart(X))",
     {"domain": ["alice", "bob"], "atoms": {"Student(alice)": True, "Smart(alice)": False, "Student(bob)": False, "Smart(bob)": True}},
     False),
    ("exists X. (Student(X) & Smart(X))",
     {"domain": ["alice"], "atoms": {"Student(alice)": True, "Smart(alice)": True}},
     True),
]

print("=== FOL Checker Tests ===")
passed = 0
for formula, world, expected in tests:
    result = eval_formula(formula, world)
    status = "PASS" if result == expected else "FAIL"
    if result != expected:
        print(f"  {status}: {formula[:60]} -> got {result}, expected {expected}")
    else:
        passed += 1
print(f"FOL checker: {passed}/{len(tests)} passed")

# World parsing tests
print("\n=== World Parsing Tests ===")
wt1 = '{"domain": ["alice", "bob"], "atoms": {"Student(alice)": true, "Student(bob)": false}}'
w = parse_world_from_llm(wt1)
assert w is not None and "alice" in w["domain"], f"Failed: {w}"
print("  JSON parsing: PASS")

wt2 = "```json\n{\"domain\": [\"a\", \"b\"], \"atoms\": {\"P(a)\": true}}\n```"
w = parse_world_from_llm(wt2)
assert w is not None, f"Failed: {w}"
print("  JSON block parsing: PASS")

# Z3 tests
print("\n=== Z3 Inference Tests ===")
r1 = folio_inference(
    ["forall X. (Cat(X) -> Mammal(X))", "Cat(whiskers)"],
    "Mammal(whiskers)"
)
print(f"  Entailment test: {r1} (expected Entailment) -> {'PASS' if r1 == 'Entailment' else 'WARN'}")

r2 = folio_inference(
    ["forall X. (Cat(X) -> not Dog(X))", "Cat(rex)"],
    "Dog(rex)"
)
print(f"  Contradiction test: {r2} (expected Contradiction) -> {'PASS' if r2 == 'Contradiction' else 'WARN'}")

r3 = folio_inference(
    ["Student(alice)"],
    "Happy(alice)"
)
print(f"  Uncertain test: {r3} (expected Uncertain) -> {'PASS' if r3 == 'Uncertain' else 'WARN'}")

print("\nAll smoke tests complete.")
