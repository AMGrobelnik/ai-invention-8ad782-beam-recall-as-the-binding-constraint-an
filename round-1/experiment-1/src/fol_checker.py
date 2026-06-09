"""Deterministic recursive FOL evaluator over finite named worlds."""
from __future__ import annotations
import re
from typing import Any


World = dict  # {'domain': list[str], 'atoms': dict[str, bool]}


def _strip_outer_parens(s: str) -> str:
    s = s.strip()
    if not s.startswith("("):
        return s
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                if i == len(s) - 1:
                    return s[1:-1].strip()
                return s
    return s


def _find_binary_op(s: str, op: str) -> int:
    """Find the rightmost (lowest-precedence) occurrence of op at depth 0."""
    depth = 0
    op_len = len(op)
    # scan left to right, return last match at depth 0
    pos = -1
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and s[i : i + op_len] == op:
            # Make sure it's not part of a longer token (e.g. '->' vs '-')
            after = s[i + op_len] if i + op_len < len(s) else " "
            if op == "->" or after in (" ", "(", "\t") or not after.isalpha():
                pos = i
        i += 1
    return pos


def _apply_binding(s: str, bindings: dict[str, str]) -> str:
    """Substitute variable bindings into an atom string."""
    for var, val in bindings.items():
        s = re.sub(r"\b" + re.escape(var) + r"\b", val, s)
    return s


def _normalize_fol(s: str) -> str:
    return (s.replace("∀", "forall ").replace("∃", "exists ").replace("¬", "not ")
             .replace("→", "->").replace("⊃", "->").replace("∧", " & ").replace("∨", " | "))


def eval_formula(formula_str: str, world: World, bindings: dict[str, str] | None = None) -> bool | None:
    """Evaluate an FOL formula in a world. Returns True/False or None on parse error."""
    if bindings is None:
        bindings = {}
    try:
        return _eval(_normalize_fol(formula_str.strip()), world, bindings)
    except Exception:
        return None


def _eval(s: str, world: World, bindings: dict[str, str]) -> bool | None:
    s = _strip_outer_parens(s)
    if not s:
        return None

    # Quantifiers
    m = re.match(r"^(forall|exists)\s+([A-Za-z_]\w*)\s*[.,]?\s*(.+)$", s, re.DOTALL)
    if m:
        qtype, var, body = m.group(1), m.group(2), m.group(3).strip()
        domain = world.get("domain", [])
        if not domain:
            return True if qtype == "forall" else False
        results = []
        for entity in domain:
            new_bindings = {**bindings, var: entity}
            r = _eval(body, world, new_bindings)
            if r is None:
                return None
            results.append(r)
        if qtype == "forall":
            return all(results)
        else:
            return any(results)

    # Negation
    for neg_kw in ("not ", "~ ", "- ", "~", "-not "):
        if s.startswith(neg_kw):
            inner = s[len(neg_kw):].strip()
            r = _eval(inner, world, bindings)
            return None if r is None else (not r)
    if s.startswith("not(") or s.startswith("~("):
        prefix = "not(" if s.startswith("not(") else "~("
        inner = s[len(prefix)-1:]  # keep the paren
        r = _eval(inner, world, bindings)
        return None if r is None else (not r)

    # Binary: -> | & (precedence: -> lowest, then |, then &)
    for op in ("->", "|", "&"):
        pos = _find_binary_op(s, op)
        if pos != -1:
            left = s[:pos].strip()
            right = s[pos + len(op):].strip()
            if not left or not right:
                continue
            l = _eval(left, world, bindings)
            r = _eval(right, world, bindings)
            if l is None or r is None:
                return None
            if op == "->":
                return (not l) or r
            elif op == "|":
                return l or r
            else:  # &
                return l and r

    # Equality
    eq_m = re.match(r"^([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)$", s)
    if eq_m:
        a = bindings.get(eq_m.group(1), eq_m.group(1))
        b = bindings.get(eq_m.group(2), eq_m.group(2))
        return a == b

    # Atom: Predicate(args...)
    atom_m = re.match(r"^([A-Za-z_]\w*)\(([^)]*)\)$", s)
    if atom_m:
        pred = atom_m.group(1)
        args_raw = [a.strip() for a in atom_m.group(2).split(",")]
        args = [bindings.get(a, a) for a in args_raw]
        key = f"{pred}({', '.join(args)})"
        # Try exact, then case-insensitive, then lowercase
        atoms = world.get("atoms", {})
        if key in atoms:
            return bool(atoms[key])
        key_lower = key.lower()
        for k, v in atoms.items():
            if k.lower() == key_lower:
                return bool(v)
        # Also try without spaces
        key_nospace = f"{pred}({','.join(args)})"
        for k, v in atoms.items():
            if k.replace(" ", "") == key_nospace.replace(" ", ""):
                return bool(v)
        return False  # closed-world assumption

    # Bare predicate (arity 0)
    bare_m = re.match(r"^([A-Za-z_]\w*)$", s)
    if bare_m:
        key = bare_m.group(1)
        resolved = bindings.get(key, key)
        atoms = world.get("atoms", {})
        return bool(atoms.get(resolved, False))

    return None


def parse_world_from_llm(text: str) -> World | None:
    """Extract world dict from LLM response."""
    import json

    # Try JSON block extraction
    for pattern in [
        r"```json\s*(\{.*?\})\s*```",
        r"```\s*(\{.*?\})\s*```",
        r"(\{[^{}]*\"domain\"[^{}]*\"atoms\"[^{}]*\})",
        r"(\{[^{}]*\"atoms\"[^{}]*\"domain\"[^{}]*\})",
    ]:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                if _valid_world(obj):
                    return _normalize_world(obj)
            except json.JSONDecodeError:
                pass

    # Try full text as JSON
    try:
        obj = json.loads(text.strip())
        if _valid_world(obj):
            return _normalize_world(obj)
    except json.JSONDecodeError:
        pass

    # Regex fallback
    domain_m = re.search(r'"?domain"?\s*:\s*\[([^\]]+)\]', text, re.IGNORECASE)
    atoms_m = re.search(r'"?atoms"?\s*:\s*\{(.+?)\}', text, re.IGNORECASE | re.DOTALL)
    if domain_m and atoms_m:
        try:
            domain = [d.strip().strip('"').strip("'") for d in domain_m.group(1).split(",")]
            atoms_text = atoms_m.group(1)
            atoms = {}
            for pair in re.finditer(r'"([^"]+)"\s*:\s*(true|false|True|False)', atoms_text):
                atoms[pair.group(1)] = pair.group(2).lower() == "true"
            if domain and atoms:
                return {"domain": domain, "atoms": atoms}
        except Exception:
            pass

    return None


def _valid_world(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and "domain" in obj
        and "atoms" in obj
        and isinstance(obj["domain"], list)
        and isinstance(obj["atoms"], dict)
    )


def _normalize_world(obj: dict) -> World:
    atoms = {}
    for k, v in obj["atoms"].items():
        if isinstance(v, bool):
            atoms[k] = v
        elif isinstance(v, str):
            atoms[k] = v.lower() in ("true", "yes", "1")
        else:
            atoms[k] = bool(v)
    return {"domain": [str(d) for d in obj["domain"]], "atoms": atoms}
