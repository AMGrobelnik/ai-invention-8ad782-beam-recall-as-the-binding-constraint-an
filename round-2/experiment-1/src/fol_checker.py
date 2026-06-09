"""Z3-based FOL model checker and entailment verifier.

Handles two distinct use cases:
1. check_formula_in_world(): CWA check (HOWP scoring against explicit worlds)
2. z3_entailment(): Open-world FOL entailment for downstream accuracy
"""

import re
from itertools import product as itertools_product
from typing import Any
from loguru import logger
from z3 import (
    DeclareSort, Function, BoolSort, Const, Solver,
    ForAll, Exists, And, Or, Not, Implies,
    unsat, unknown, sat, BoolVal
)


# ─── Tokenizer ───────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r'<->|->|all|exists|[A-Za-z][A-Za-z0-9_]*|[(){},.\-&|]|\s+'
)

def tokenize(s: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(s) if not t.isspace()]


# ─── Recursive Descent Parser ─────────────────────────────────────────────────

class _Parser:
    """Parse NLTK logic module syntax → Z3 AST, within a given world context."""

    def __init__(
        self,
        tokens: list[str],
        domain,
        entities: dict[str, Any],
        predicates: dict[str, Any],
        var_scope: dict[str, Any] | None = None,
    ):
        self.tokens = tokens
        self.pos = 0
        self.domain = domain
        self.entities = entities          # {name: Z3 Const}
        self.predicates = predicates      # {name: Z3 Function}
        self.var_scope = dict(var_scope or {})  # {name: Z3 Const (variable)}

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def consume(self, expected: str | None = None) -> str:
        tok = self.tokens[self.pos]
        if expected is not None and tok != expected:
            raise ValueError(f"Expected {expected!r} got {tok!r} at pos {self.pos}, tokens={self.tokens}")
        self.pos += 1
        return tok

    def parse(self):
        result = self.parse_biconditional()
        if self.pos != len(self.tokens):
            raise ValueError(f"Trailing tokens from pos {self.pos}: {self.tokens[self.pos:]}")
        return result

    def parse_biconditional(self):
        left = self.parse_implication()
        while self.peek() == '<->':
            self.consume('<->')
            right = self.parse_implication()
            left = And(Implies(left, right), Implies(right, left))
        return left

    def parse_implication(self):
        left = self.parse_disjunction()
        if self.peek() == '->':
            self.consume('->')
            right = self.parse_implication()  # right-associative
            return Implies(left, right)
        return left

    def parse_disjunction(self):
        left = self.parse_conjunction()
        while self.peek() == '|':
            self.consume('|')
            right = self.parse_conjunction()
            left = Or(left, right)
        return left

    def parse_conjunction(self):
        left = self.parse_negation()
        while self.peek() == '&':
            self.consume('&')
            right = self.parse_negation()
            left = And(left, right)
        return left

    def parse_negation(self):
        if self.peek() == '-':
            self.consume('-')
            # Check if this is part of '->' (shouldn't happen after tokenizer, but be safe)
            operand = self.parse_negation()
            return Not(operand)
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok is None:
            raise ValueError("Unexpected end of formula")

        if tok == '(':
            self.consume('(')
            result = self.parse_biconditional()
            self.consume(')')
            return result

        if tok == 'all':
            return self.parse_quantifier(universal=True)

        if tok == 'exists':
            return self.parse_quantifier(universal=False)

        # Must be an atom: Name(...) or just Name (nullary pred / constant truth)
        return self.parse_atom()

    def parse_quantifier(self, universal: bool):
        self.consume('all' if universal else 'exists')
        var_name = self.consume()
        if var_name in ('all', 'exists', '-', '&', '|', '->', '<->', '(', ')', ',', '.'):
            raise ValueError(f"Invalid variable name: {var_name!r}")
        self.consume('.')

        # Create a fresh Z3 variable for this quantifier
        var_const = Const(f"_v_{var_name}", self.domain)
        old = self.var_scope.get(var_name)
        self.var_scope[var_name] = var_const

        body = self.parse_biconditional()

        if old is None:
            del self.var_scope[var_name]
        else:
            self.var_scope[var_name] = old

        if universal:
            return ForAll([var_const], body)
        else:
            return Exists([var_const], body)

    def parse_atom(self):
        name = self.consume()
        if self.peek() != '(':
            # Nullary: could be a propositional variable – treat as 0-ary pred
            # Or a named constant used as a boolean (unlikely but handle gracefully)
            if name in self.var_scope:
                # A variable alone is unusual but could be a predicate applied to nothing
                return BoolVal(True)  # fallback
            # Unknown atom – warn and return True
            logger.warning(f"Unknown atom {name!r} with no args, treating as True")
            return BoolVal(True)

        self.consume('(')
        args = []
        while self.peek() != ')':
            args.append(self._resolve_term(self.consume()))
            if self.peek() == ',':
                self.consume(',')
        self.consume(')')

        if name not in self.predicates:
            # Predicate not in world – add a constant-false predicate
            logger.debug(f"Predicate {name!r} not in world, treating atom as False")
            return BoolVal(False)

        pred = self.predicates[name]
        expected_arity = pred.arity()  # Z3 FuncDeclRef.arity() = number of domain args
        if len(args) != expected_arity:
            logger.warning(
                f"Arity mismatch: {name} expects {expected_arity}, got {len(args)}"
            )
            return BoolVal(False)

        return pred(*args)

    def _resolve_term(self, name: str):
        if name in self.var_scope:
            return self.var_scope[name]
        if name in self.entities:
            return self.entities[name]
        # Unknown entity – create a fresh constant and add it
        c = Const(name, self.domain)
        self.entities[name] = c
        logger.debug(f"Added unknown entity {name!r} to world")
        return c


# ─── World Checking (CWA) ─────────────────────────────────────────────────────

def check_formula_in_world(
    fol_formula_str: str,
    world: dict,
    timeout_ms: int = 5000,
) -> bool | None:
    """
    Check whether fol_formula_str holds in world under CWA.

    world = {
        'entities': ['alice', 'bob'],
        'atoms': {'Student': [['alice']], 'Smart': [['alice'], ['bob']]},
    }

    Returns True/False, or None on parse/solver error.
    """
    try:
        Domain = DeclareSort('Domain')
        entity_names = [e.lower() for e in world.get('entities', [])]
        entities = {name: Const(name, Domain) for name in entity_names}

        # Also handle CamelCase entity names (LLM may generate them capitalized)
        for e in world.get('entities', []):
            if e not in entities:
                entities[e] = Const(e, Domain)

        atoms = world.get('atoms', {})

        # Infer predicate arities from atoms
        predicates = {}
        for pred_name, instances in atoms.items():
            if instances:
                arity = len(instances[0])
            else:
                arity = 1  # default unary
            sorts = [Domain] * arity + [BoolSort()]
            predicates[pred_name] = Function(pred_name, *sorts)

        # Collect all predicate names from the formula too (using a quick scan)
        formula_preds = _extract_predicates(fol_formula_str)
        for pred_name, arity in formula_preds.items():
            if pred_name not in predicates and arity > 0:
                sorts = [Domain] * arity + [BoolSort()]
                predicates[pred_name] = Function(pred_name, *sorts)

        s = Solver()
        s.set('timeout', timeout_ms)

        # Finite domain constraint
        if entities:
            x = Const('_domain_x', Domain)
            s.add(ForAll([x], Or(*[x == e for e in entities.values()])))

        # Assert positive ground atoms
        for pred_name, instances in atoms.items():
            if pred_name not in predicates:
                continue
            pred = predicates[pred_name]
            for args in instances:
                resolved = [entities.get(a.lower(), entities.get(a)) for a in args]
                if any(r is None for r in resolved):
                    continue
                s.add(pred(*resolved))

        # CWA: negate all unlisted ground atoms (for predicates defined in the world)
        for pred_name, instances in atoms.items():
            if pred_name not in predicates:
                continue
            pred = predicates[pred_name]
            arity = pred.arity()
            listed = set(tuple(a.lower() for a in inst) for inst in instances)
            for combo in itertools_product(entity_names, repeat=arity):
                if combo not in listed:
                    args_z3 = [entities[e] for e in combo]
                    s.add(Not(pred(*args_z3)))

        # Parse formula
        tokens = tokenize(fol_formula_str)
        parser = _Parser(tokens, Domain, entities, predicates)
        formula = parser.parse()

        # Check: Not(formula) unsat → formula holds
        s.push()
        s.add(Not(formula))
        result = s.check()
        s.pop()

        if result == unsat:
            return True
        elif result == sat:
            return False
        else:
            # unknown (timeout) – fallback: try direct satisfiability
            logger.debug(f"Z3 returned unknown for formula, trying direct check")
            s.push()
            s.add(formula)
            r2 = s.check()
            s.pop()
            if r2 == unsat:
                return False  # formula is unsatisfiable → False in world
            return None  # can't determine

    except Exception as e:
        logger.debug(f"check_formula_in_world error: {e}")
        return None


def _extract_predicates(formula_str: str) -> dict[str, int]:
    """Quick scan to find predicates and their arities from token stream."""
    tokens = tokenize(formula_str)
    result = {}
    for i, tok in enumerate(tokens):
        if i + 1 < len(tokens) and tokens[i + 1] == '(':
            # tok is a predicate name (capitalized or not)
            if tok not in ('all', 'exists') and tok[0].isupper():
                # Count args
                depth = 0
                arity = 1
                for j in range(i + 1, len(tokens)):
                    if tokens[j] == '(':
                        depth += 1
                    elif tokens[j] == ')':
                        depth -= 1
                        if depth == 0:
                            break
                    elif tokens[j] == ',' and depth == 1:
                        arity += 1
                if i + 2 < len(tokens) and tokens[i + 2] == ')':
                    arity = 0  # zero-arity (nullary pred? rare)
                result[tok] = arity
    return result


# ─── Open-World Entailment (for downstream accuracy) ──────────────────────────

def z3_entailment(
    premises_nltk: list[str],
    conclusion_nltk: str,
    timeout_ms: int = 8000,
) -> str:
    """
    Check FOL entailment: premises ⊨ conclusion?

    Returns: 'entailment', 'contradiction', or 'neutral'.
    All formulas in NLTK syntax with explicit named individuals.
    Uses open-world assumption (no finite domain) for correct FOL semantics.
    """
    try:
        Domain = DeclareSort('Domain')
        all_formulas = premises_nltk + [conclusion_nltk]
        entity_names, pred_specs = _collect_names(all_formulas)

        entities = {name: Const(name, Domain) for name in entity_names}
        predicates = {}
        for pred_name, arity in pred_specs.items():
            if arity > 0:
                sorts = [Domain] * arity + [BoolSort()]
                predicates[pred_name] = Function(pred_name, *sorts)

        def parse_formula(s: str):
            tokens = tokenize(s)
            parser = _Parser(tokens, Domain, dict(entities), dict(predicates))
            return parser.parse()

        s = Solver()
        s.set('timeout', timeout_ms)

        # Add premises
        prem_formulas = []
        for p in premises_nltk:
            try:
                prem_formulas.append(parse_formula(p))
            except Exception as e:
                logger.debug(f"Failed to parse premise {p!r}: {e}")

        for pf in prem_formulas:
            s.add(pf)

        # Parse conclusion
        try:
            conc = parse_formula(conclusion_nltk)
        except Exception as e:
            logger.debug(f"Failed to parse conclusion {conclusion_nltk!r}: {e}")
            return 'neutral'

        # Check entailment: Not(conclusion) unsat?
        s.push()
        s.add(Not(conc))
        r = s.check()
        s.pop()

        if r == unsat:
            return 'entailment'

        # Check contradiction: conclusion itself unsat?
        s.push()
        s.add(conc)
        r2 = s.check()
        s.pop()

        if r2 == unsat:
            return 'contradiction'

        return 'neutral'

    except Exception as e:
        logger.debug(f"z3_entailment error: {e}")
        return 'neutral'


def _collect_names(formulas: list[str]) -> tuple[set[str], dict[str, int]]:
    """Collect entity names (capitalized non-predicate tokens) and predicate arities."""
    entities = set()
    predicates: dict[str, int] = {}

    for formula in formulas:
        tokens = tokenize(formula)
        for i, tok in enumerate(tokens):
            if not tok[0].isalpha():
                continue
            if tok in ('all', 'exists'):
                continue

            is_pred = (i + 1 < len(tokens) and tokens[i + 1] == '(')
            # Is it a quantifier variable? (appears after 'all'/'exists')
            is_qvar = (i > 0 and tokens[i - 1] in ('all', 'exists'))

            if is_pred and tok[0].isupper():
                # Count arity
                if tok not in predicates:
                    arity = _count_arity(tokens, i)
                    predicates[tok] = arity
            elif not is_pred and not is_qvar and tok[0].isupper():
                entities.add(tok)
            # lowercase non-qvar in arg position: a quantifier-bound variable (skip)

    return entities, predicates


def _count_arity(tokens: list[str], pred_pos: int) -> int:
    """Count argument count of predicate at pred_pos."""
    # tokens[pred_pos] = pred name, tokens[pred_pos+1] = '('
    depth = 0
    arity = 0
    for j in range(pred_pos + 1, len(tokens)):
        if tokens[j] == '(':
            depth += 1
            if depth == 1:
                arity = 1  # at least one arg unless immediately closed
        elif tokens[j] == ')':
            depth -= 1
            if depth == 0:
                break
        elif tokens[j] == ',' and depth == 1:
            arity += 1
    # Special case: P() → arity 0
    if pred_pos + 2 < len(tokens) and tokens[pred_pos + 2] == ')':
        arity = 0
    return arity
