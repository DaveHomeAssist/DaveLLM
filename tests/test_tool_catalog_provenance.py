"""Built-in handler placement, pinned without environment-dependent fingerprints.

A definition fingerprint hashes each handler's compiled code, which embeds the
checkout path, the Python version, the handler source, and its line numbers.
Only the last two are portable, so the catalog fixture pins each built-in
handler's first and last line and a digest of its source. Adding a line above
a handler in app.py, or editing one, fails here with a clear message instead of
silently changing every qualified fingerprint.
"""

import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest


BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "davellm" / "tool_catalog.json").read_text()
)


def handler_code(registry):
    code = {}
    for name in registry.public_catalog():
        lines, first = inspect.getsourcelines(registry.get(name).handler)
        code[name] = {
            "first_line": first,
            "last_line": first + len(lines) - 1,
            "source_sha256": hashlib.sha256(
                "".join(lines).replace("\r\n", "\n").encode("utf-8")
            ).hexdigest(),
        }
    return code


@pytest.fixture
def router(router_factory, tmp_path):
    loaded, _, _ = router_factory(tools=True, tool_roots=[str(tmp_path)], shell=True)
    return loaded


def test_builtin_handlers_have_not_moved_or_changed(router):
    expected = BASELINE["handler_code"]
    for registry in ("TOOL_REGISTRY", "HARNESS_REGISTRY"):
        actual = handler_code(getattr(router, registry))
        assert actual.keys() == expected.keys()
        moved = {tool: {"expected": expected[tool], "actual": actual[tool]}
                 for tool in expected if actual[tool] != expected[tool]}
        assert not moved, (
            "A built-in tool handler moved or changed, so its definition fingerprint and the "
            "qualified catalog changed. Keep new app.py code below the handlers; if the change "
            f"is intended, regenerate handler_code in the catalog fixture. {registry}: {moved}"
        )


def test_extended_code_sits_below_every_builtin_handler(router):
    last_handler_line = max(item["last_line"] for item in BASELINE["handler_code"].values())
    source = Path(router.__file__).read_text(encoding="utf-8").splitlines()

    def first_line(pattern):
        return next(number for number, text in enumerate(source, 1) if re.match(pattern, text))

    extended = {
        "EXTENDED_TOOLS_ENABLED": first_line(r"EXTENDED_TOOLS_ENABLED = "),
        "davellm_files import": first_line(r"from davellm_files import "),
        "resolve_extended_tool_path": inspect.getsourcelines(router.resolve_extended_tool_path)[1],
        "extended_tool_definitions": inspect.getsourcelines(router.extended_tool_definitions)[1],
    }
    above = {name: line for name, line in extended.items() if line <= last_handler_line}
    assert not above, (
        f"Extended-tool code must stay below the last built-in handler (line {last_handler_line}): {above}"
    )
