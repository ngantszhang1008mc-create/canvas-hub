// A small, safe formula language: the browser twin of canvas_hub/formula.py.
// Same grammar, same rules, same errors; tests/fixtures/formula_vectors.json
// runs against both. No eval and no Function constructor (the page CSP forbids them anyway).

export const MAX_LEN = 500;
const MAX_NODES = 200;
const MAX_DEPTH = 32;
export const FUNCS = { min: [1, 50], max: [1, 50], round: [1, 2], floor: [1, 1], ceil: [1, 1], abs: [1, 1],
  sqrt: [1, 1], clamp: [3, 3], if: [3, 3] };
const TOKEN = /\s*(?:(\d+(?:\.\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|<=|>=|==|!=|[-+*/%^()<>,]))/y;
const CMP = ["<", "<=", ">", ">=", "==", "!="];

export class FormulaError extends Error {}

function tokens(text) {
  const out = [];
  text = text.replace(/\s+$/, "");
  let pos = 0, depth = 0;
  while (pos < text.length) {
    TOKEN.lastIndex = pos;
    const m = TOKEN.exec(text);
    if (!m || TOKEN.lastIndex === pos) throw new FormulaError(`formula: unexpected character at ${pos + 1}: '${text.slice(pos, pos + 10)}'`);
    if (m[3] === "(" && ++depth > MAX_DEPTH) throw new FormulaError(`formula nesting is too deep (at most ${MAX_DEPTH} levels)`);
    if (m[3] === ")") depth--;
    out.push(m[1] ? ["num", m[1]] : m[2] ? ["name", m[2]] : ["op", m[3]]);
    pos = TOKEN.lastIndex;
  }
  return out;
}

export function parse(text) {
  if (typeof text !== "string" || !text.trim() || text.length > MAX_LEN) throw new FormulaError(`formula must be text of 1 to ${MAX_LEN} characters`);
  const toks = tokens(text);
  let i = 0, count = 0;
  const peek = (...vals) => i < toks.length && toks[i][0] !== "num" && vals.includes(toks[i][1]);
  const take = () => toks[i++];
  const node = (...n) => { if (++count > MAX_NODES) throw new FormulaError("formula is too long"); return n; };
  const pOr = () => { let n = pAnd(); while (peek("or")) { take(); n = node("or", n, pAnd()); } return n; };
  const pAnd = () => { let n = pNot(); while (peek("and")) { take(); n = node("and", n, pNot()); } return n; };
  const pNot = () => { if (peek("not")) { take(); return node("not", pNot()); } return pCmp(); };
  const pCmp = () => {
    let n = pAdd();
    if (peek(...CMP)) {
      const op = take()[1];
      n = node("cmp", op, n, pAdd());
      if (peek(...CMP)) throw new FormulaError("formula: join comparisons with 'and'");
    }
    return n;
  };
  const pAdd = () => { let n = pMul(); while (peek("+", "-")) { const op = take()[1]; n = node("bin", op, n, pMul()); } return n; };
  const pMul = () => { let n = pUnary(); while (peek("*", "/", "%")) { const op = take()[1]; n = node("bin", op, n, pUnary()); } return n; };
  const pUnary = () => {
    if (peek("-", "+")) { const op = take()[1]; return op === "-" ? node("neg", pUnary()) : pUnary(); }
    return pPow();
  };
  const pPow = () => { let n = pAtom(); if (peek("^", "**")) { take(); n = node("bin", "^", n, pUnary()); } return n; };
  const pAtom = () => {
    if (i >= toks.length) throw new FormulaError("formula ends too early");
    const [kind, val] = take();
    if (kind === "num") return node("num", parseFloat(val));
    if (kind === "name") {
      if (val === "true" || val === "false") return node("num", val === "true" ? 1 : 0);
      if (["and", "or", "not"].includes(val)) throw new FormulaError(`formula: unexpected '${val}'`);
      if (peek("(")) {
        take();
        if (!Object.hasOwn(FUNCS, val)) throw new FormulaError(`formula: unknown function '${val}'`);
        const args = [];
        if (!peek(")")) { args.push(pOr()); while (peek(",")) { take(); args.push(pOr()); } }
        if (!peek(")")) throw new FormulaError("formula: missing ')'");
        take();
        const [lo, hi] = FUNCS[val];
        if (args.length < lo || args.length > hi) throw new FormulaError(`formula: ${val} takes ${lo}${lo === hi ? "" : ` to ${hi}`} argument(s)`);
        return node("call", val, args);
      }
      return node("var", val);
    }
    if (val === "(") {
      const n = pOr();
      if (!peek(")")) throw new FormulaError("formula: missing ')'");
      take();
      return n;
    }
    throw new FormulaError(`formula: unexpected '${val}'`);
  };
  const tree = pOr();
  if (i !== toks.length) throw new FormulaError(`formula: unexpected '${toks[i][1]}'`);
  return tree;
}

export function names(tree) {
  const out = new Set();
  const walk = (n) => {
    if (n[0] === "var") out.add(n[1]);
    else if (n[0] === "call") n[2].forEach(walk);
    else if (n[0] !== "num") n.slice(1).forEach((x) => { if (Array.isArray(x)) walk(x); });
  };
  walk(tree);
  return out;
}

function round(x, digits = 0) {
  if (digits !== Math.trunc(digits) || digits < 0 || digits > 10) throw new FormulaError("formula: round digits must be a whole number from 0 to 10");
  const f = 10 ** digits;
  return Math.sign(x) * Math.floor(Math.abs(x) * f + 0.5) / f;
}

export function evaluate(formula, variables = {}) {
  const tree = typeof formula === "string" ? parse(formula) : formula;
  const env = new Map();
  for (const [k, v0] of Object.entries(variables || {})) {
    const v = typeof v0 === "boolean" ? (v0 ? 1 : 0) : v0;
    if (typeof v !== "number" || !Number.isFinite(v)) throw new FormulaError(`formula: variable ${k} must be a number`);
    env.set(k, v);
  }
  const ev = (n) => {
    const r = step(n);
    if (!Number.isFinite(r)) throw new FormulaError("formula: the result is not a finite number");   // at every step, as in Python
    return r;
  };
  const step = (n) => {
    const t = n[0];
    if (t === "num") return n[1];
    if (t === "var") { if (!env.has(n[1])) throw new FormulaError(`formula: unknown name '${n[1]}'`); return env.get(n[1]); }
    if (t === "neg") return -ev(n[1]);
    if (t === "not") return ev(n[1]) ? 0 : 1;
    if (t === "and") return ev(n[1]) ? ev(n[2]) : 0;
    if (t === "or") { const l = ev(n[1]); return l ? l : ev(n[2]); }
    if (t === "cmp") {
      const a = ev(n[2]), b = ev(n[3]);
      return ({ "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a === b, "!=": a !== b })[n[1]] ? 1 : 0;
    }
    if (t === "bin") {
      const op = n[1], a = ev(n[2]), b = ev(n[3]);
      if (op === "+") return a + b;
      if (op === "-") return a - b;
      if (op === "*") return a * b;
      if ((op === "/" || op === "%") && b === 0) throw new FormulaError("formula: division by zero");
      if (op === "/") return a / b;
      if (op === "%") return a - b * Math.floor(a / b);
      if (Math.abs(b) > 100) throw new FormulaError("formula: exponent too large");
      if (a < 0 && b !== Math.trunc(b)) throw new FormulaError("formula: fractional power of a negative number");
      if (a === 0 && b < 0) throw new FormulaError("formula: division by zero");
      return a ** b;
    }
    const [name, args] = [n[1], n[2]];
    if (name === "if") return ev(args[0]) ? ev(args[1]) : ev(args[2]);
    const v = args.map(ev);
    if (name === "min") return Math.min(...v);
    if (name === "max") return Math.max(...v);
    if (name === "round") return round(...v);
    if (name === "floor") return Math.floor(v[0]);
    if (name === "ceil") return Math.ceil(v[0]);
    if (name === "abs") return Math.abs(v[0]);
    if (name === "sqrt") { if (v[0] < 0) throw new FormulaError("formula: square root of a negative number"); return Math.sqrt(v[0]); }
    return Math.min(Math.max(v[0], v[1]), v[2]);       // clamp
  };
  return ev(tree);
}
