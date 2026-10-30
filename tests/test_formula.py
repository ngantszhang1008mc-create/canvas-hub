"""The formula language: Python and the browser module agree on every shared vector."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from canvas_hub import formula
from canvas_hub.util import InvalidInput

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "formula_vectors.json").read_text())["cases"]
JS = Path(__file__).parents[1] / "canvas_hub" / "web" / "static" / "formula.js"


def _python(case):
    try:
        return {"out": formula.evaluate(case["f"], case.get("vars"))}
    except InvalidInput as e:
        return {"error": str(e)}


def _check(case, got):
    if "error" in case:
        assert "error" in got and case["error"] in got["error"], (case, got)
    else:
        assert "out" in got and abs(got["out"] - case["out"]) < 1e-9, (case, got)


@pytest.mark.parametrize("case", VECTORS, ids=[c["f"] or "empty" for c in VECTORS])
def test_python_vectors(case):
    _check(case, _python(case))


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_browser_module_matches_python(tmp_path):
    mod = tmp_path / "formula.mjs"
    mod.write_text(JS.read_text())
    script = ("import { evaluate } from './formula.mjs';\n"
              "const cases = JSON.parse(process.argv[2]);\n"
              "console.log(JSON.stringify(cases.map((c) => { try { return { out: evaluate(c.f, c.vars || {}) }; }"
              " catch (e) { return { error: e.message }; } })));\n")
    (tmp_path / "run.mjs").write_text(script)
    res = subprocess.run(["node", "run.mjs", json.dumps(VECTORS)], cwd=tmp_path, capture_output=True, text=True, check=True)
    for case, got in zip(VECTORS, json.loads(res.stdout), strict=True):
        _check(case, got)
        py = _python(case)
        assert ("error" in py) == ("error" in got), (case, py, got)
        if "out" in py:
            assert py["out"] == got["out"], (case, py, got)       # bit-for-bit the same number


def test_names_and_limits():
    assert formula.names(formula.parse("if(hours >= min_value, max * hours / full_at, 0)")) == {"hours", "min_value", "max",
                                                                                               "full_at"}
    with pytest.raises(InvalidInput):
        formula.parse("1+" * 300 + "1")            # too long
    with pytest.raises(InvalidInput, match="too long"):
        formula.parse("1+" * 120 + "1")            # too many nodes
