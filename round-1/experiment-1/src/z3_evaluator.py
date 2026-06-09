"""Z3-based FOLIO downstream inference evaluator."""
from __future__ import annotations
import re
import signal
from typing import Optional

try:
    import z3
    Z3_AVAILABLE = True
except ImportError:
    Z3_AVAILABLE = False


def _normalize_fol(s: str) -> str:
    """Normalize Unicode FOL to ASCII."""
    return (s
        .replace("∀", "forall ")
        .replace("∃", "exists ")
        .replace("¬", "not ")
        .replace("→", "->")
        .replace("⊃", "->")
        .replace("∧", " & ")
        .replace("∨", " | ")
        .replace("⊤", "True")
        .replace("⊥", "False")
        .replace("∀", "forall ")
        .replace("∃", "exists ")
        .replace("¬", "not ")
        .replace("→", "->")
        .replace("∧", " & ")
        .replace("∨", " | ")
    )


def folio_inference(premises_fol: list[str], conclusion_fol: str, timeout_secs: int = 10) -> Optional[str]:
    """
    Returns 'Entailment', 'Contradiction', or 'Uncertain'.
    Returns None on timeout/parse error.
    """
    if not Z3_AVAILABLE:
        return None

    try:
        premises_fol = [_normalize_fol(p) for p in premises_fol]
        conclusion_fol = _normalize_fol(conclusion_fol)
        sorts, decls = _infer_sorts_decls(premises_fol + [conclusion_fol])

        def _try_solver(formulas: list[str]) -> Optional[bool]:
            s = z3.Solver()
            s.set("timeout", timeout_secs * 1000)
            for f in formulas:
                z3f = _fol_to_z3(f, sorts, decls)
                if z3f is None:
                    return None
                s.add(z3f)
            result = s.check()
            if result == z3.sat:
                return True
            elif result == z3.unsat:
                return False
            return None  # unknown/timeout

        # Check entailment: premises + not(conclusion) is UNSAT
        neg_conc = _negate_formula(conclusion_fol)
        r1 = _try_solver(premises_fol + [neg_conc])
        if r1 is False:
            return "Entailment"

        # Check contradiction: premises + conclusion is UNSAT
        r2 = _try_solver(premises_fol + [conclusion_fol])
        if r2 is False:
            return "Contradiction"

        if r1 is True and r2 is True:
            return "Uncertain"

        return None  # solver returned unknown

    except Exception:
        return None


def _negate_formula(f: str) -> str:
    f = f.strip()
    if f.startswith("not ") or f.startswith("~"):
        return f[4:].strip() if f.startswith("not ") else f[1:].strip()
    return f"not ({f})"


def _infer_sorts_decls(formulas: list[str]) -> tuple[dict, dict]:
    """Infer Z3 sorts and function declarations from FOL formulas."""
    Entity = z3.DeclareSort("Entity")
    sorts = {"Entity": Entity}
    decls = {}

    for f in formulas:
        # Find predicates: Name(arg1, arg2, ...)
        for m in re.finditer(r"\b([A-Z][A-Za-z_]*)\(([^)]*)\)", f):
            pred = m.group(1)
            args = [a.strip() for a in m.group(2).split(",") if a.strip()]
            arity = len(args)
            if pred not in decls:
                decls[pred] = z3.Function(pred, *([Entity] * arity), z3.BoolSort())

    return sorts, decls


def _fol_to_z3(formula: str, sorts: dict, decls: dict) -> Optional[z3.ExprRef]:
    """Convert FOL string to Z3 expression. Returns None on failure."""
    try:
        Entity = sorts["Entity"]
        return _parse_z3(formula.strip(), sorts, decls, {})
    except Exception:
        return None


def _parse_z3(s: str, sorts: dict, decls: dict, env: dict) -> Optional[z3.ExprRef]:
    """Recursive Z3 parser."""
    s = s.strip()
    # Strip outer parens
    s = _strip_outer_parens(s)
    if not s:
        return None

    Entity = sorts["Entity"]

    # Quantifiers
    m = re.match(r"^(forall|exists)\s+([A-Za-z_]\w*)\s*[.,]?\s*(.+)$", s, re.DOTALL)
    if m:
        qtype, var, body = m.group(1), m.group(2), m.group(3).strip()
        v = z3.Const(var, Entity)
        new_env = {**env, var: v}
        body_z3 = _parse_z3(body, sorts, decls, new_env)
        if body_z3 is None:
            return None
        if qtype == "forall":
            return z3.ForAll([v], body_z3)
        else:
            return z3.Exists([v], body_z3)

    # Negation
    for neg_kw in ("not ", "~ ", "~"):
        if s.startswith(neg_kw):
            inner = s[len(neg_kw):].strip()
            r = _parse_z3(inner, sorts, decls, env)
            return None if r is None else z3.Not(r)

    # Binary ops (lowest precedence first)
    for op, z3op in [("->", None), ("|", None), ("&", None)]:
        pos = _find_op(s, op)
        if pos != -1:
            left = _parse_z3(s[:pos].strip(), sorts, decls, env)
            right = _parse_z3(s[pos + len(op):].strip(), sorts, decls, env)
            if left is None or right is None:
                return None
            if op == "->":
                return z3.Implies(left, right)
            elif op == "|":
                return z3.Or(left, right)
            else:
                return z3.And(left, right)

    # Equality
    eq_m = re.match(r"^([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)$", s)
    if eq_m:
        a = env.get(eq_m.group(1), z3.Const(eq_m.group(1), Entity))
        b = env.get(eq_m.group(2), z3.Const(eq_m.group(2), Entity))
        return a == b

    # Atom
    atom_m = re.match(r"^([A-Za-z_]\w*)\(([^)]*)\)$", s)
    if atom_m:
        pred = atom_m.group(1)
        args_raw = [a.strip() for a in atom_m.group(2).split(",")]
        if pred not in decls:
            arity = len(args_raw)
            decls[pred] = z3.Function(pred, *([Entity] * arity), z3.BoolSort())
        args_z3 = []
        for a in args_raw:
            if a in env:
                args_z3.append(env[a])
            else:
                args_z3.append(z3.Const(a, Entity))
        try:
            return decls[pred](*args_z3)
        except Exception:
            return None

    # Boolean constant
    if s.lower() == "true":
        return z3.BoolVal(True)
    if s.lower() == "false":
        return z3.BoolVal(False)

    # Bare name — treat as 0-arity predicate or boolean var
    bare_m = re.match(r"^([A-Za-z_]\w*)$", s)
    if bare_m:
        name = bare_m.group(1)
        if name in env:
            return env[name]
        return z3.Bool(name)

    return None


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    if i == len(s) - 1:
                        s = s[1:-1].strip()
                        break
                    else:
                        return s
        else:
            break
    return s


def _find_op(s: str, op: str) -> int:
    depth = 0
    pos = -1
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and s[i: i + len(op)] == op:
            pos = i
        i += 1
    return pos
