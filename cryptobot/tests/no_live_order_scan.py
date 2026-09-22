"""AST scanner proving there is no live-order / authenticated code path.

Shared by ``cryptobot/tests/test_no_live_orders.py`` and
``cryptobot/scripts/verify_all.py`` so both report exactly the same evidence.

Rules (deliberately precise, to avoid false positives from the deny-lists that
this very guard needs):

1. **Identifiers** -- any ``Name``, attribute, function/keyword argument or dict
   key whose *exact* lower-cased name is an order-submission / private-API name
   (``create_order``, ``place_order``, ``fetch_balance``, ``apiKey`` ...).
2. **Root modules** -- any call/attribute rooted at ``hmac``, ``sapi``,
   ``futures_``, ``ccxt.binance`` private surface, ``requests.post/put/...``.
3. **String constants** -- exact matches of credential / signed-endpoint paths
   (``/sapi``, ``/api/v3/order``, ``X-MBX-APIKEY`` ...), excluding docstrings and
   elements of a deny-list constant (``_FORBIDDEN*``, ``ALLOWED_*``,
   ``SENSITIVE_KEYS``, ``_CREDENTIAL*``).

Result: the scan flags real code and documentation prose is ignored, while the
deny-list definitions in ``safety.py``/``config.py`` are explicitly recognised.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple

#: Package root (``cryptobot/``).
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

#: Directories excluded from the scan: tests must name what they forbid, and the
#: verify script holds this pattern list.
EXCLUDED_PARTS = ("tests", "scripts", "__pycache__")

#: Exact identifier names that would imply order submission / private API use.
FORBIDDEN_IDENTIFIERS: Set[str] = {
    "create_order", "createorder", "create_market_order", "createmarketorder",
    "create_limit_order", "createlimitorder", "create_orders", "createorders",
    "place_order", "placeorder", "submit_order", "submitorder", "new_order", "neworder",
    "cancel_order", "cancelorder", "cancel_all_orders", "cancelallorders",
    "fetch_balance", "fetchbalance", "fetch_open_orders", "fetchopenorders",
    "fetch_my_trades", "fetchmytrades", "fetch_positions", "fetchpositions",
    "private_get", "privateget", "private_post", "privatepost", "private_put", "privateput",
    "set_leverage", "setleverage", "set_margin_mode", "setmarginmode",
    "api_key", "apikey", "secret_key", "secretkey", "api_secret", "apisecret",
    "recvwindow", "signature", "signed_request", "signedrequest",
}

#: Root names of calls that must never appear (auth / signing / derivatives).
FORBIDDEN_ROOTS: Set[str] = {"hmac", "sapi", "ccxt_private"}

#: String constants (or substrings) that identify signed/trading endpoints or keys.
FORBIDDEN_STRINGS: Tuple[str, ...] = (
    "/sapi", "/api/v3/order", "/api/v3/account", "/order", "/withdraw", "/deposit",
    "/allorders", "/openorders", "/batchorders", "/userdata", "x-mbx-apikey",
    "x-mbx-signature", "apikey", "recvwindows",
)

#: Assignment targets that are *allowed* to contain forbidden strings/names
#: because they are deny-lists / documentation tables.
DENYLIST_TARGET_PATTERNS: Tuple[str, ...] = (
    "_FORBIDDEN", "ALLOWED_", "_ALLOWED", "SENSITIVE_KEYS", "_CREDENTIAL",
    "FORBIDDEN_",
)

#: HTTP verbs that imply a write request.
WRITE_VERBS = {"post", "put", "patch", "delete"}


@dataclass
class ScanResult:
    scanned_files: List[str] = field(default_factory=list)
    findings: List[Dict[str, object]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def render(self, limit: int = 40) -> str:
        lines = ["scanned {} runtime modules under {}".format(len(self.scanned_files), PACKAGE_ROOT.name)]
        if self.ok:
            lines.append("no live-order / signed-request code path found (0 findings)")
        else:
            lines.append("{} finding(s):".format(len(self.findings)))
            for finding in self.findings[:limit]:
                lines.append("  {file}:{line} [{rule}] {snippet}".format(**finding))
        return "\n".join(lines)


def runtime_files(exclude_parts: Sequence[str] = EXCLUDED_PARTS) -> List[Tuple[Path, str]]:
    """All ``.py`` files under the package except tests/scripts/__pycache__."""
    out: List[Tuple[Path, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT)
        if any(part in exclude_parts for part in relative.parts):
            continue
        out.append((path, str(relative).replace("\\", "/")))
    return out


def _target_names(node: ast.AST) -> Set[str]:
    """Names being assigned to (``a = ...``, ``a: T = ...``, ``a, b = ...``)."""
    names: Set[str] = set()
    if isinstance(node, ast.Assign):
        for target in node.targets:
            names |= _target_names(target)
    elif isinstance(node, ast.AnnAssign):
        names |= _target_names(node.target)
    elif isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names |= _target_names(element)
    return names


def _is_denylist_assignment(targets: Set[str]) -> bool:
    return any(any(pattern in name for pattern in DENYLIST_TARGET_PATTERNS) for name in targets)


def _docstring_lines(tree: ast.AST) -> Set[int]:
    """Line numbers occupied by docstrings (documentation, not code)."""
    lines: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                start = body[0].lineno
                end = getattr(body[0], "end_lineno", start) or start
                lines.update(range(start, end + 1))
    return lines


def scan_file(path: Path, relative: str) -> List[Dict[str, object]]:
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - would fail elsewhere too
        return [{"file": relative, "line": exc.lineno or 0, "rule": "syntax-error", "snippet": str(exc)}]

    source_lines = text.splitlines()
    docstrings = _docstring_lines(tree)
    deny_nodes: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            if _is_denylist_assignment(_target_names(node)):
                value = node.value
                if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                    deny_nodes.update(id(element) for element in value.elts)
                deny_nodes.add(id(value))

    def snippet(lineno: int) -> str:
        raw = source_lines[lineno - 1].strip() if 0 < lineno <= len(source_lines) else ""
        return raw[:110]

    findings: List[Dict[str, object]] = []

    def flag(lineno: int, rule: str, detail: str) -> None:
        findings.append({"file": relative, "line": lineno, "rule": rule, "snippet": "{} :: {}".format(snippet(lineno), detail)})

    for node in ast.walk(tree):
        # --- identifiers ----------------------------------------------------
        if isinstance(node, ast.Name) and node.id.lower() in FORBIDDEN_IDENTIFIERS:
            flag(node.lineno, "identifier", node.id)
        elif isinstance(node, ast.Attribute) and node.attr.lower() in FORBIDDEN_IDENTIFIERS:
            flag(node.lineno, "attribute", node.attr)
        elif isinstance(node, ast.keyword) and node.arg and node.arg.lower() in FORBIDDEN_IDENTIFIERS:
            flag(node.lineno, "keyword-arg", node.arg)
        elif isinstance(node, ast.arg) and node.arg.lower() in FORBIDDEN_IDENTIFIERS:
            flag(node.lineno, "param", node.arg)
        elif isinstance(node, ast.FunctionDef) and node.name.lower() in FORBIDDEN_IDENTIFIERS:
            flag(node.lineno, "function-def", node.name)

        # --- dict keys ------------------------------------------------------
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) \
                        and key.value.lower() in FORBIDDEN_IDENTIFIERS:
                    flag(key.lineno, "dict-key", key.value)

        # --- roots / write verbs -------------------------------------------
        if isinstance(node, ast.Attribute):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            root_name = root.id.lower() if isinstance(root, ast.Name) else ""
            if root_name in FORBIDDEN_ROOTS or "futures" in root_name:
                flag(node.lineno, "forbidden-root", "{}.{}".format(root_name, node.attr))
            if node.attr.lower() in WRITE_VERBS and root_name in {"requests", "session", "http", "client", "self"}:
                if root_name != "self" or "http" in node.attr.lower():
                    flag(node.lineno, "http-write", "{}.{}".format(root_name, node.attr))

        # --- credential strings --------------------------------------------
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in deny_nodes or node.lineno in docstrings:
                continue
            value = node.value
            lowered = value.lower().strip()
            for token in FORBIDDEN_STRINGS:
                if lowered == token or (token.startswith("/") and token in lowered) \
                        or lowered.startswith(token):
                    flag(node.lineno, "credential-string", repr(value[:60]))
                    break

    # Comment-stripped lexical check for write helpers (belt and braces).
    for lineno, raw in enumerate(source_lines, start=1):
        code = raw.split("#", 1)[0]
        for verb in ("requests.post", "requests.put", "requests.delete", "session.post", "session.put"):
            if verb in code:
                flag(lineno, "http-write-lexical", verb)
    return findings


def scan(exclude_parts: Sequence[str] = EXCLUDED_PARTS) -> ScanResult:
    result = ScanResult()
    for path, relative in runtime_files(exclude_parts):
        result.scanned_files.append(relative)
        result.findings.extend(scan_file(path, relative))
    return result


def main(argv: Iterable[str] = ()) -> int:  # pragma: no cover - CLI convenience
    result = scan()
    print(result.render())
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
