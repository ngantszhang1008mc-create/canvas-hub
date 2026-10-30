"""A small, safe formula language shared with the browser (``static/formula.js``).

Both sides use the same hand-written parser and the same rules, so a page can
recompute a component without a round trip, and the assistant reads the same
numbers here. ``tests/fixtures/formula_vectors.json`` is run against both.

Grammar (lowest precedence first)::

    or      and ("or" and)*
    and     not ("and" not)*
    not     "not" not | cmp
    cmp     add (("<" | "<=" | ">" | ">=" | "==" | "!=") add)?
    add     mul (("+" | "-") mul)*
    mul     unary (("*" | "/" | "%") unary)*
    unary   ("-" | "+") unary | pow
    pow     atom (("^" | "**") unary)?
    atom    number | true | false | name | name "(" args ")" | "(" or ")"

Functions: min, max, round(x[, digits]), floor, ceil, abs, sqrt,
clamp(x, lo, hi), if(cond, a, b) (only the chosen branch is evaluated).
Rounding is half away from zero and ``%`` takes the sign of the divisor on
both sides. Division by zero, a negative square root and a non-finite value at
any step are errors. There is no attribute access, no strings and nothing but the
names the caller passes in.
"""

from __future__ import annotations

import math
import re

from .util import InvalidInput

MAX_LEN = 500
MAX_NODES = 200
FUNCS = {"min": (1, 50), "max": (1, 50), "round": (1, 2), "floor": (1, 1), "ceil": (1, 1), "abs": (1, 1),
         "sqrt": (1, 1), "clamp": (3, 3), "if": (3, 3)}
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|<=|>=|==|!=|[-+*/%^()<>,]))")
_CMP = ("<", "<=", ">", ">=", "==", "!=")


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise InvalidInput(f"formula: unexpected character at {pos + 1}: {text[pos:pos + 10]!r}")
        num, name, op = m.groups()
        out.append(("num", num) if num else ("name", name) if name else ("op", op))
        pos = m.end()
    return out


def parse(text: str) -> tuple:
    """Parse a formula into a small tuple tree; raises InvalidInput."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_LEN:
        raise InvalidInput(f"formula must be text of 1 to {MAX_LEN} characters")
    toks = _tokens(text)
    i = 0
    count = 0

    def peek(*vals):
        return i < len(toks) and toks[i][1] in vals and toks[i][0] != "num"

    def take():
        nonlocal i
        t = toks[i]
        i += 1
        return t

    def node(*n):
        nonlocal count
        count += 1
        if count > MAX_NODES:
            raise InvalidInput("formula is too long")
        return n

    def p_or():
        n = p_and()
        while peek("or"):
            take()
            n = node("or", n, p_and())
        return n

    def p_and():
        n = p_not()
        while peek("and"):
            take()
            n = node("and", n, p_not())
        return n

    def p_not():
        if peek("not"):
            take()
            return node("not", p_not())
        return p_cmp()

    def p_cmp():
        n = p_add()
        if peek(*_CMP):
            op = take()[1]
            n = node("cmp", op, n, p_add())
            if peek(*_CMP):
                raise InvalidInput("formula: join comparisons with 'and'")
        return n

    def p_add():
        n = p_mul()
        while peek("+", "-"):
            n = node("bin", take()[1], n, p_mul())
        return n

    def p_mul():
        n = p_unary()
        while peek("*", "/", "%"):
            n = node("bin", take()[1], n, p_unary())
        return n

    def p_unary():
        if peek("-", "+"):
            op = take()[1]
            return node("neg", p_unary()) if op == "-" else p_unary()
        return p_pow()

    def p_pow():
        n = p_atom()
        if peek("^", "**"):
            take()
            n = node("bin", "^", n, p_unary())
        return n

    def p_atom():
        if i >= len(toks):
            raise InvalidInput("formula ends too early")
        kind, val = take()
        if kind == "num":
            return node("num", float(val))
        if kind == "name":
            if val in ("true", "false"):
                return node("num", 1.0 if val == "true" else 0.0)
            if val in ("and", "or", "not"):
                raise InvalidInput(f"formula: unexpected '{val}'")
            if peek("("):
                take()
                if val not in FUNCS:
                    raise InvalidInput(f"formula: unknown function {val!r}")
                args = []
                if not peek(")"):
                    args.append(p_or())
                    while peek(","):
                        take()
                        args.append(p_or())
                if not peek(")"):
                    raise InvalidInput("formula: missing ')'")
                take()
                lo, hi = FUNCS[val]
                if not lo <= len(args) <= hi:
                    raise InvalidInput(f"formula: {val} takes {lo}{'' if lo == hi else f' to {hi}'} argument(s)")
                return node("call", val, tuple(args))
            return node("var", val)
        if val == "(":
            n = p_or()
            if not peek(")"):
                raise InvalidInput("formula: missing ')'")
            take()
            return n
        raise InvalidInput(f"formula: unexpected '{val}'")

    tree = p_or()
    if i != len(toks):
        raise InvalidInput(f"formula: unexpected '{toks[i][1]}'")
    return tree


def names(tree: tuple) -> set[str]:
    """Variable names a parsed formula uses."""
    if tree[0] == "var":
        return {tree[1]}
    if tree[0] == "num":
        return set()
    if tree[0] == "call":
        return set().union(*(names(a) for a in tree[2]))
    return set().union(*(names(x) for x in tree[1:] if isinstance(x, tuple)))


def _round(x: float, digits: float = 0) -> float:
    if digits != int(digits) or not 0 <= digits <= 10:
        raise InvalidInput("formula: round digits must be a whole number from 0 to 10")
    f = 10 ** int(digits)
    return math.copysign(math.floor(abs(x) * f + 0.5), x) / f


def evaluate(formula: str | tuple, variables: dict | None = None) -> float:
    tree = parse(formula) if isinstance(formula, str) else formula
    env = {}
    for k, v in (variables or {}).items():
        if isinstance(v, bool):
            v = 1.0 if v else 0.0
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise InvalidInput(f"formula: variable {k} must be a number")
        env[k] = float(v)

    def ev(n) -> float:
        try:
            r = step(n)
        except OverflowError:
            r = math.inf
        if not math.isfinite(r):        # checked at every step, so both sides agree on edge cases
            raise InvalidInput("formula: the result is not a finite number")
        return r

    def step(n) -> float:
        t = n[0]
        if t == "num":
            return n[1]
        if t == "var":
            if n[1] not in env:
                raise InvalidInput(f"formula: unknown name {n[1]!r}")
            return env[n[1]]
        if t == "neg":
            return -ev(n[1])
        if t == "not":
            return 0.0 if ev(n[1]) else 1.0
        if t == "and":
            return ev(n[2]) if ev(n[1]) else 0.0
        if t == "or":
            left = ev(n[1])
            return left if left else ev(n[2])
        if t == "cmp":
            a, b = ev(n[2]), ev(n[3])
            r = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[n[1]]
            return 1.0 if r else 0.0
        if t == "bin":
            op, a, b = n[1], ev(n[2]), ev(n[3])
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op in ("/", "%") and b == 0:
                raise InvalidInput("formula: division by zero")
            if op == "/":
                return a / b
            if op == "%":
                return a - b * math.floor(a / b)
            if abs(b) > 100:
                raise InvalidInput("formula: exponent too large")
            if a < 0 and b != int(b):
                raise InvalidInput("formula: fractional power of a negative number")
            if a == 0 and b < 0:
                raise InvalidInput("formula: division by zero")
            return a ** b
        name, args = n[1], n[2]
        if name == "if":
            return ev(args[1]) if ev(args[0]) else ev(args[2])
        vals = [ev(a) for a in args]
        if name == "min":
            return min(vals)
        if name == "max":
            return max(vals)
        if name == "round":
            return _round(*vals)
        if name == "floor":
            return float(math.floor(vals[0]))
        if name == "ceil":
            return float(math.ceil(vals[0]))
        if name == "abs":
            return abs(vals[0])
        if name == "sqrt":
            if vals[0] < 0:
                raise InvalidInput("formula: square root of a negative number")
            return math.sqrt(vals[0])
        return min(max(vals[0], vals[1]), vals[2])          # clamp

    return ev(tree)
