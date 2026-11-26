"""Package import boundary of the service ticket system (design.md, "Package boundary"; R20.1 to R20.4).

The check is a static scan. Every ``.py`` file is parsed with ``ast`` and never imported, so the test
cannot be satisfied by an import that happens not to run, and it cannot trigger side effects.

Rules enforced:

1. Nothing in ``app/service_tickets`` imports ``app.services.audit``, any ``app.models`` module other
   than ``app.models.user``, or the names ``Caller``, ``require_roles`` and ``EnrolledUser`` from
   ``app.api.deps``. The package's own ``app.service_tickets.models`` is a different module and is fine.
2. Nothing under ``app`` outside the package imports from it, except the files in ``ALLOWED_IMPORTERS``,
   and those only for the modules listed there.
3. ``customer_token.py`` imports no ``app.api`` module and, from ``app``, only ``app.core.config``.
4. ``app/service_tickets/__init__.py`` is empty.
5. Aliases (``import x as y``), relative imports (resolved to absolute names first) and dynamic imports
   with a string literal (``importlib.import_module``, ``__import__``) count as imports.

The ``test_scanner_*`` tests feed the scanner small synthetic sources so that a broken scanner cannot
pass the real scan vacuously.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_ROOT / "app"
PACKAGE_DIR = APP_DIR / "service_tickets"
PACKAGE = "app.service_tickets"

AUDIT_MODULE = "app.services.audit"
MODELS_PACKAGE = "app.models"
ALLOWED_MODEL_MODULE = "app.models.user"
DEPS_MODULE = "app.api.deps"
FORBIDDEN_DEPS_NAMES = ("Caller", "require_roles", "EnrolledUser")
FORBIDDEN_DEPS_TARGETS = frozenset(f"{DEPS_MODULE}.{name}" for name in FORBIDDEN_DEPS_NAMES)

CUSTOMER_TOKEN_MODULE = f"{PACKAGE}.customer_token"
CUSTOMER_TOKEN_ALLOWED_APP_IMPORT = "app.core.config"

# File (relative to the backend directory, POSIX form) -> package modules it may import from.
# An entry allows the module itself and anything below it. `api.body_limit` is not written yet; it joins
# the main.py entry in a later task and is listed here so that task does not have to edit this test.
ALLOWED_IMPORTERS: dict[str, tuple[str, ...]] = {
    "app/main.py": (
        f"{PACKAGE}.mount",
        f"{PACKAGE}.api.features",
        f"{PACKAGE}.api.preflight_guard",
        f"{PACKAGE}.api.body_limit",
        f"{PACKAGE}.config",
    ),
    "app/api/deps.py": (CUSTOMER_TOKEN_MODULE,),
    "app/api/routers/auth.py": (CUSTOMER_TOKEN_MODULE,),
    "app/models/__init__.py": (f"{PACKAGE}.models",),
}

_DYNAMIC_IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})


# --- scanner -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """One imported absolute dotted name, with where and how it was imported."""

    line: int
    target: str
    how: str  # "import", "from-import" or "dynamic"


@dataclass
class Scan:
    refs: list[Ref] = field(default_factory=list)
    # Dynamic imports whose target is not a string literal (or is a relative literal): cannot be checked.
    unresolved: list[tuple[int, str]] = field(default_factory=list)
    # Attribute accesses that resolve to a forbidden `app.api.deps` name through an alias or full path.
    forbidden_attributes: list[tuple[int, str]] = field(default_factory=list)


def under(target: str, prefix: str) -> bool:
    """True when `target` is `prefix` or a dotted name below it (`app.models2` is not under `app.models`)."""
    return target == prefix or target.startswith(prefix + ".")


def module_name_for(relative_path: str) -> tuple[str, bool]:
    """Dotted module name and is-package flag for a POSIX path relative to the backend directory."""
    parts = list(Path(relative_path).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _resolve_from(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    """Absolute module name of a `from ... import` statement, resolving relative levels."""
    if node.level == 0:
        return node.module or ""
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    drop = node.level - 1
    if drop >= len(package_parts):
        raise ValueError(f"relative import beyond the top level in {module}: line {node.lineno}")
    base = package_parts[: len(package_parts) - drop]
    if node.module:
        base = [*base, node.module]
    return ".".join(base)


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _literal_strings(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.List | ast.Tuple) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
    ):
        return [item.value for item in node.elts if isinstance(item, ast.Constant)]
    return None


def _dynamic_import(node: ast.Call, scan: Scan) -> None:
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name not in _DYNAMIC_IMPORT_FUNCTIONS:
        return
    first: ast.AST | None = node.args[0] if node.args else None
    if first is None:
        first = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)) or first.value.startswith("."):
        message = f"{name}() with a target that is not an absolute string literal"
        scan.unresolved.append((node.lineno, message))
        return
    module = first.value
    fromlist_node: ast.AST | None = node.args[3] if name == "__import__" and len(node.args) > 3 else None
    if fromlist_node is None:
        fromlist_node = next((kw.value for kw in node.keywords if kw.arg == "fromlist"), None)
    fromlist = _literal_strings(fromlist_node) if fromlist_node is not None else None
    if fromlist:
        for item in fromlist:
            scan.refs.append(Ref(node.lineno, f"{module}.{item}", "dynamic"))
    else:
        scan.refs.append(Ref(node.lineno, module, "dynamic"))


def scan_source(source: str, module: str, *, is_package: bool = False) -> Scan:
    """Collect every import of `source` as absolute dotted names. `module` is the module being scanned."""
    tree = ast.parse(source)
    scan = Scan()
    aliases: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                scan.refs.append(Ref(node.lineno, item.name, "import"))
                if item.asname:
                    aliases[item.asname] = item.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, module, is_package)
            for item in node.names:
                target = f"{base}.{item.name}" if base else item.name
                scan.refs.append(Ref(node.lineno, target, "from-import"))
                aliases[item.asname or item.name] = target
        elif isinstance(node, ast.Call):
            _dynamic_import(node, scan)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted is None:
                continue
            head, _, rest = dotted.partition(".")
            resolved = f"{aliases[head]}.{rest}" if head in aliases and rest else dotted
            if resolved in FORBIDDEN_DEPS_TARGETS:
                scan.forbidden_attributes.append((node.lineno, resolved))
    return scan


# --- the rules -----------------------------------------------------------------------------------


def package_file_violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Rules 1 and 5 for one file inside the package."""
    scan = scan_source(source, module, is_package=is_package)
    problems: list[str] = []
    for ref in scan.refs:
        if under(ref.target, AUDIT_MODULE):
            problems.append(f"line {ref.line}: imports {ref.target} ({ref.how}); audit is off limits")
        elif under(ref.target, MODELS_PACKAGE) and not under(ref.target, ALLOWED_MODEL_MODULE):
            problems.append(
                f"line {ref.line}: imports {ref.target} ({ref.how}); only {ALLOWED_MODEL_MODULE} is allowed"
            )
        elif under(ref.target, DEPS_MODULE) and (
            ref.target in FORBIDDEN_DEPS_TARGETS or ref.target == f"{DEPS_MODULE}.*"
        ):
            problems.append(f"line {ref.line}: imports {ref.target} ({ref.how}); forbidden app.api.deps name")
    problems.extend(
        f"line {line}: uses {target} through an attribute; forbidden app.api.deps name"
        for line, target in scan.forbidden_attributes
    )
    problems.extend(f"line {line}: {text}" for line, text in scan.unresolved)
    return problems


def outside_file_violations(source: str, relative_path: str) -> list[str]:
    """Rule 2 for one file outside the package. `relative_path` is POSIX, relative to the backend dir."""
    module, is_package = module_name_for(relative_path)
    allowed = ALLOWED_IMPORTERS.get(relative_path, ())
    problems: list[str] = []
    for ref in scan_source(source, module, is_package=is_package).refs:
        if under(ref.target, PACKAGE) and not any(under(ref.target, prefix) for prefix in allowed):
            problems.append(f"line {ref.line}: imports {ref.target} ({ref.how}); not on the allowlist")
    return problems


def customer_token_violations(source: str) -> list[str]:
    """Rule 3: no `app.api` module, and from `app` only `app.core.config`."""
    module, is_package = module_name_for("app/service_tickets/customer_token.py")
    problems: list[str] = []
    for ref in scan_source(source, module, is_package=is_package).refs:
        if under(ref.target, "app.api"):
            problems.append(f"line {ref.line}: imports {ref.target}; customer_token must not import app.api")
        elif under(ref.target, "app") and not under(ref.target, CUSTOMER_TOKEN_ALLOWED_APP_IMPORT):
            problems.append(
                f"line {ref.line}: imports {ref.target}; only {CUSTOMER_TOKEN_ALLOWED_APP_IMPORT} is allowed"
            )
    return problems


def init_violations(raw: bytes) -> list[str]:
    """Rule 4: empty, or whitespace only."""
    return [] if not raw.strip() else [f"{len(raw)} bytes with content; the package __init__ must stay empty"]


# --- file discovery ------------------------------------------------------------------------------


def _py_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(BACKEND_ROOT).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")  # some existing files, such as main.py, start with a BOM


# --- the real scan -------------------------------------------------------------------------------


def test_package_import_boundary() -> None:
    package_files = _py_files(PACKAGE_DIR)
    outside_files = [path for path in _py_files(APP_DIR) if PACKAGE_DIR not in path.parents]

    # The scan must have looked at real files. The package has 8 files at this task (6 modules and two
    # empty `__init__.py`), and `app` has far more.
    assert len(package_files) >= 8, f"only {len(package_files)} package files found"
    assert len(outside_files) > 50, f"only {len(outside_files)} files found outside the package"
    package_names = {_relative(path) for path in package_files}
    assert {
        "app/service_tickets/__init__.py",
        "app/service_tickets/config.py",
        "app/service_tickets/customer_token.py",
        "app/service_tickets/models.py",
        "app/service_tickets/mount.py",
        "app/service_tickets/api/features.py",
        "app/service_tickets/api/preflight_guard.py",
    } <= package_names

    violations: list[str] = []

    # Rules 1 and 5: inside the package.
    for path in package_files:
        module, is_package = module_name_for(_relative(path))
        violations.extend(
            f"{_relative(path)}: {problem}"
            for problem in package_file_violations(_read(path), module, is_package=is_package)
        )

    # Rule 2: outside the package. Remember which allowed files really import from it.
    importers: set[str] = set()
    for path in outside_files:
        relative = _relative(path)
        source = _read(path)
        module, is_package = module_name_for(relative)
        if any(under(ref.target, PACKAGE) for ref in scan_source(source, module, is_package=is_package).refs):
            importers.add(relative)
        violations.extend(f"{relative}: {problem}" for problem in outside_file_violations(source, relative))

    # Each listed file must really import from the package, so the entries are not dead and the four
    # known importers (main.py, deps.py, auth.py, models/__init__.py) are seen.
    missing = set(ALLOWED_IMPORTERS) - importers
    assert not missing, f"allowed importers not seen: {missing}"

    # Rule 3.
    token_path = PACKAGE_DIR / "customer_token.py"
    violations.extend(
        f"{_relative(token_path)}: {problem}" for problem in customer_token_violations(_read(token_path))
    )

    # Rule 4.
    init_path = PACKAGE_DIR / "__init__.py"
    violations.extend(
        f"{_relative(init_path)}: {problem}" for problem in init_violations(init_path.read_bytes())
    )

    assert not violations, "package boundary broken:\n" + "\n".join(violations)


# --- the scanner detects each violation ----------------------------------------------------------

PACKAGE_MODULE = "app.service_tickets.config"
NESTED_MODULE = "app.service_tickets.api.features"

PACKAGE_VIOLATIONS = {
    "audit-from": ("from app.services.audit import AuditContext\n", PACKAGE_MODULE),
    "audit-from-package": ("from app.services import audit\n", PACKAGE_MODULE),
    "audit-from-alias": ("from app.services.audit import AuditContext as A\n", PACKAGE_MODULE),
    "audit-import": ("import app.services.audit\n", PACKAGE_MODULE),
    "audit-import-alias": ("import app.services.audit as audit_service\n", PACKAGE_MODULE),
    "audit-relative-one-level-up": ("from ..services.audit import log\n", PACKAGE_MODULE),
    "audit-relative-two-levels-up": ("from ...services.audit import log\n", NESTED_MODULE),
    "audit-relative-from-app": ("from ... import services\nfrom ...services import audit\n", NESTED_MODULE),
    "audit-import-module": (
        'import importlib\nimportlib.import_module("app.services.audit")\n',
        PACKAGE_MODULE,
    ),
    "audit-import-module-bare": (
        'from importlib import import_module\nimport_module("app.services.audit")\n',
        PACKAGE_MODULE,
    ),
    "audit-dunder-import": ('__import__("app.services.audit")\n', PACKAGE_MODULE),
    "audit-dunder-fromlist": ('__import__("app.services", fromlist=["audit"])\n', PACKAGE_MODULE),
    "audit-inside-function": ("def f():\n    from app.services.audit import x\n", PACKAGE_MODULE),
    "audit-type-checking": (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from app.services.audit import x\n",
        PACKAGE_MODULE,
    ),
    "model-from": ("from app.models.booking import Booking\n", PACKAGE_MODULE),
    "model-from-package-name": ("from app.models import Booking\n", PACKAGE_MODULE),
    "model-package": ("import app.models\n", PACKAGE_MODULE),
    "model-star": ("from app.models import *\n", PACKAGE_MODULE),
    "model-relative": ("from ..models.booking import Booking\n", PACKAGE_MODULE),
    "model-dynamic": ('import importlib\nimportlib.import_module("app.models.booking")\n', PACKAGE_MODULE),
    "deps-from-caller": ("from app.api.deps import Caller\n", PACKAGE_MODULE),
    "deps-from-require-roles": ("from app.api.deps import api_error, require_roles\n", PACKAGE_MODULE),
    "deps-from-enrolled-user": ("from app.api.deps import EnrolledUser as E\n", PACKAGE_MODULE),
    "deps-star": ("from app.api.deps import *\n", PACKAGE_MODULE),
    "deps-relative": ("from ...api.deps import Caller\n", NESTED_MODULE),
    "deps-attribute-alias": ("import app.api.deps as d\nx = d.require_roles\n", PACKAGE_MODULE),
    "deps-attribute-from-package": ("from app.api import deps\nx: deps.EnrolledUser\n", PACKAGE_MODULE),
    "deps-attribute-full-path": ("import app.api.deps\nx = app.api.deps.Caller\n", PACKAGE_MODULE),
    "dynamic-non-literal": ("import importlib\nname = 'x'\nimportlib.import_module(name)\n", PACKAGE_MODULE),
    "dynamic-relative-literal": (
        'import importlib\nimportlib.import_module(".x", "app.services")\n',
        PACKAGE_MODULE,
    ),
}


@pytest.mark.parametrize("case", sorted(PACKAGE_VIOLATIONS))
def test_scanner_detects_package_violation(case: str) -> None:
    source, module = PACKAGE_VIOLATIONS[case]
    assert package_file_violations(source, module), f"{case}: not detected"


PACKAGE_ALLOWED = {
    "deps-plain-names": "from app.api.deps import api_error, forbidden, unauthorized\n",
    "deps-module-import": "import app.api.deps\nx = app.api.deps.api_error\n",
    "deps-alias-allowed-attribute": "import app.api.deps as d\nx = d.forbidden\n",
    "user-model": "from app.models.user import User, UserRole\n",
    "user-model-module": "from app.models import user\n",
    "user-model-import": "import app.models.user as user_model\n",
    "own-models-absolute": "from app.service_tickets.models import SvcTicket\n",
    "own-models-relative": "from .models import SvcTicket\nfrom . import models\n",
    "own-services-relative": "from ..services.audit import x\n",  # resolved below for NESTED_MODULE
    "db-base": "from app.db.base import Base\n",
    "other-services": "from app.services.auth import InvalidToken, is_locked\n",
    "similar-prefix": "from app.services.audit_log_viewer import x\nfrom app.models_extra import y\n",
    "dynamic-allowed": 'import importlib\nimportlib.import_module("app.core.redis")\n',
    "comment-and-string": (
        '# from app.services.audit import x\nS = "from app.models.booking import Booking"\n'
    ),
}


@pytest.mark.parametrize("case", sorted(PACKAGE_ALLOWED))
def test_scanner_allows_permitted_imports(case: str) -> None:
    # `..services.audit` inside `app.service_tickets.api` is the package's own `services` subpackage.
    module = NESTED_MODULE if case == "own-services-relative" else PACKAGE_MODULE
    assert package_file_violations(PACKAGE_ALLOWED[case], module) == []


def test_scanner_resolves_relative_imports_from_a_package_init() -> None:
    # In an `__init__.py` the module is the package, so `.` is the package itself.
    source = "from ..services.audit import x\n"
    assert package_file_violations(source, "app.service_tickets", is_package=True)
    assert package_file_violations("from . import models\n", "app.service_tickets", is_package=True) == []


def test_scanner_resolves_relative_imports_to_absolute_names() -> None:
    source = "from ...services.audit import log\nfrom .x import y\nfrom . import z\n"
    scan = scan_source(source, NESTED_MODULE)
    assert [ref.target for ref in scan.refs] == [
        "app.services.audit.log",
        "app.service_tickets.api.x.y",
        "app.service_tickets.api.z",
    ]


def test_scanner_rejects_relative_import_above_the_top_level() -> None:
    with pytest.raises(ValueError):
        scan_source("from ....x import y\n", NESTED_MODULE)


OUTSIDE_VIOLATIONS = {
    "security": ("app/core/security.py", "from app.service_tickets.config import ServiceConfig\n"),
    "unlisted-module-in-main": ("app/main.py", "from app.service_tickets.models import SvcTicket\n"),
    "unlisted-module-in-deps": ("app/api/deps.py", "from app.service_tickets.config import ServiceConfig\n"),
    "unlisted-module-in-auth": (
        "app/api/routers/auth.py",
        "from app.service_tickets.api.features import x\n",
    ),
    "unlisted-module-in-models-init": (
        "app/models/__init__.py",
        "from app.service_tickets import customer_token\n",
    ),
    "plain-import": ("app/core/redis.py", "import app.service_tickets.config\n"),
    "plain-import-alias": ("app/core/redis.py", "import app.service_tickets.config as c\n"),
    "package-itself": ("app/core/redis.py", "import app.service_tickets\n"),
    "star": ("app/core/redis.py", "from app.service_tickets import *\n"),
    "relative": ("app/api/routers/hours.py", "from ...service_tickets.config import ServiceConfig\n"),
    "relative-package-name": ("app/api/routers/hours.py", "from ...service_tickets import config\n"),
    "relative-from-init": ("app/models/__init__.py", "from ..service_tickets import config\n"),
    "dynamic": (
        "app/core/redis.py",
        'import importlib\nimportlib.import_module("app.service_tickets.config")\n',
    ),
    "dunder": ("app/core/redis.py", '__import__("app.service_tickets.config")\n'),
    "inside-function": ("app/core/redis.py", "def f():\n    from app.service_tickets.config import x\n"),
    "sibling-prefix-is-not-allowed": ("app/main.py", "from app.service_tickets.mounted import x\n"),
}


@pytest.mark.parametrize("case", sorted(OUTSIDE_VIOLATIONS))
def test_scanner_detects_outside_violation(case: str) -> None:
    relative, source = OUTSIDE_VIOLATIONS[case]
    assert outside_file_violations(source, relative), f"{case}: not detected"


OUTSIDE_ALLOWED = {
    "main": (
        "app/main.py",
        "from app.service_tickets.api.features import build_features_router\n"
        "from app.service_tickets.api.preflight_guard import PreflightGuard\n"
        "from app.service_tickets.config import ServiceConfig\n"
        "from app.service_tickets.mount import mount\n"
        "from app.service_tickets.api.body_limit import BodyLimit\n",
    ),
    "deps": ("app/api/deps.py", "from app.service_tickets import customer_token\n"),
    "deps-from-module": ("app/api/deps.py", "from app.service_tickets.customer_token import mint\n"),
    "deps-relative": ("app/api/deps.py", "from ..service_tickets import customer_token\n"),
    "auth": ("app/api/routers/auth.py", "from app.service_tickets import customer_token\n"),
    "models-init": ("app/models/__init__.py", "from app.service_tickets import models as m\n"),
    "models-init-relative": ("app/models/__init__.py", "from ..service_tickets import models\n"),
    "no-import": ("app/core/security.py", "import hashlib\nfrom app.core.config import get_settings\n"),
    "similar-name": ("app/core/security.py", "from app.service_tickets_legacy import x\n"),
}


@pytest.mark.parametrize("case", sorted(OUTSIDE_ALLOWED))
def test_scanner_allows_allowlisted_outside_imports(case: str) -> None:
    relative, source = OUTSIDE_ALLOWED[case]
    assert outside_file_violations(source, relative) == []


CUSTOMER_TOKEN_VIOLATIONS = {
    "deps": "from app.api.deps import api_error\n",
    "deps-module": "from app.api import deps\n",
    "api-package": "import app.api\n",
    "api-relative": "from ..api.deps import api_error\n",
    "api-dynamic": 'import importlib\nimportlib.import_module("app.api.deps")\n',
    "other-app-module": "from app.core.security import decode_token\n",
    "own-package-module": "from app.service_tickets.config import ServiceConfig\n",
    "own-package-relative": "from .config import ServiceConfig\n",
}


@pytest.mark.parametrize("case", sorted(CUSTOMER_TOKEN_VIOLATIONS))
def test_scanner_detects_customer_token_violation(case: str) -> None:
    assert customer_token_violations(CUSTOMER_TOKEN_VIOLATIONS[case]), f"{case}: not detected"


def test_scanner_allows_customer_token_imports() -> None:
    source = "import hmac\nimport jwt\nfrom app.core import config\nfrom app.core.config import Settings\n"
    assert customer_token_violations(source) == []


@pytest.mark.parametrize("raw", [b"x = 1\n", b'"""doc"""\n', b"# comment\n", b"\xef\xbb\xbf"])
def test_scanner_detects_non_empty_init(raw: bytes) -> None:
    assert raw.strip() and init_violations(raw)


@pytest.mark.parametrize("raw", [b"", b"\n", b"  \r\n\t\n"])
def test_scanner_allows_empty_init(raw: bytes) -> None:
    assert init_violations(raw) == []


def test_module_name_for_paths() -> None:
    assert module_name_for("app/main.py") == ("app.main", False)
    assert module_name_for("app/models/__init__.py") == ("app.models", True)
    assert module_name_for("app/service_tickets/api/features.py") == (NESTED_MODULE, False)
