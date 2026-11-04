"""No direct SettingsState mutation outside Dispatcher/Reducer.

Closes the documented follow-up blind spot of the store-redux dogma
("true Store mutations via ``store.settings.*`` still need a separate dogma").

Covers ``docs/dev/STORE.md`` invariants 1-2::

    Action ──► Dispatcher.dispatch ──► RootReducer.reduce (replace) ──► Store ──► emit_state_change

Any direct assignment through ``store.settings`` (``store.settings.theme =``,
``self.store.settings.x =``, ``window.store.settings.x =``, or a
``settings``/``s`` alias bound to ``<store>.settings``) bypasses the
Dispatcher lock, action history, subscribers and ``emit_state_change`` — the
same race/staleness class as direct viewport mutation, for the settings scope.

This AST dogma scans ``src/`` and flags:

(a) ``Assign`` / ``AnnAssign`` / ``AugAssign`` whose LHS chain passes through
    ``settings`` rooted at a Store object (whole-object ``store.settings =``
    included), and
(b) ``setattr(target, name, value)`` call shapes with the same
    settings-chain property on ``target``.

Reuse (not fork-duplicated): ``_get_chain`` / ``_collect_transients`` /
``EXEMPT`` are imported from the sibling ``test_no_direct_store_mutation``
(which deliberately excludes ``settings``/``workspace``/``runtime_cache``
from its ``INTERMEDIATE`` set to avoid ``self.workspace`` false positives).
This module adds only settings-specific layers: Store-rooted
``settings``-chain matching, ``settings``-alias tracking, and
store-factory transient extension.

Store-unique-root discipline (same as the sibling): the chain segment before
``settings`` must name the Store (``store``/``_store``/``Store``), or the
root must be a Store-derived transient / a ``settings`` alias bound to such
a chain. Generic ``self.settings = ...`` (dialogs, ``QSettings`` holders),
``WorkerStoreSnapshot.__init__``, and bare ``settings`` function params
(helpers taking a detached ``SettingsState``) do NOT flag.

Exemptions:

- Store/Reducer implementation files — the sibling's discovered ``EXEMPT``
  set (core store/settings/reducer/dispatcher impl + tab reducer files via
  rglob, not hardcoded lists).
- Transient ``Store()`` builders — the sibling's data-flow set, extended to
  store-factory helpers (module functions that construct ``Store()``, e.g.
  snapshot/export ``_get_reusable_*_store``) whose call-site results are
  therefore transient working copies.
- ``SettingsManager.load_all_settings`` is deliberately NOT exempt: it
  assigns ``s.<field> = self._get_setting(...)`` directly instead of going
  through dispatch/replace (``save_all_settings`` only reads, so it is
  clean). Those bootstrap writes are pinned in ``ALLOWLIST`` below, not
  silently exempted.

Ratchet: ``ALLOWLIST`` pins every pre-existing hit as ``file:line``. A new
bypass fails; a stale entry (site already migrated to dispatch) fails too,
forcing cleanup of the entry.
"""

from __future__ import annotations

import ast

import pytest

from ._framework import SRC, iter_py, parse, rel
from .test_no_direct_store_mutation import (
    EXEMPT,
    _collect_transients,
    _get_chain,
)

pytestmark = pytest.mark.slow

#: Names that identify the Store at the root of an attribute chain.
STORE_NAMES = frozenset({"store", "_store", "Store"})

#: Call-roots that are test doubles, never real Store mutation.
_MOCK_ROOTS = frozenset({"monkeypatch", "mock", "mocker", "patch"})

#: Pre-existing direct settings mutations, pinned as ``rel-path:lineno``.
#: Migrate a site to ``dispatch(Set*Action)`` and drop its entry (a stale
#: entry fails the test).
ALLOWLIST: frozenset[str] = frozenset({
    "src/plugins/settings/manager.py:448",
    "src/plugins/settings/manager.py:449",
    "src/plugins/settings/manager.py:450",
    "src/plugins/settings/manager.py:451",
    "src/plugins/settings/manager.py:452",
    "src/plugins/settings/manager.py:453",
    "src/plugins/settings/manager.py:454",
    "src/plugins/settings/manager.py:455",
    "src/plugins/settings/manager.py:458",
    "src/plugins/settings/manager.py:461",
    "src/plugins/settings/manager.py:464",
    "src/plugins/settings/manager.py:467",
    "src/plugins/settings/manager.py:468",
    "src/plugins/settings/manager.py:501",
    "src/plugins/settings/manager.py:502",
    "src/plugins/settings/manager.py:503",
    "src/plugins/settings/manager.py:504",
    "src/plugins/settings/manager.py:505",
    "src/plugins/settings/manager.py:507",
    "src/plugins/settings/manager.py:510",
    "src/plugins/settings/manager.py:511",
    "src/plugins/settings/manager.py:512",
    "src/plugins/settings/manager.py:513",
    "src/plugins/settings/manager.py:514",
    "src/plugins/settings/manager.py:517",
    "src/plugins/settings/manager.py:520",
    "src/plugins/settings/manager.py:521",
    "src/plugins/settings/manager.py:524",
    "src/plugins/settings/manager.py:525",
    "src/plugins/settings/manager.py:528",
    "src/plugins/settings/manager.py:531",
    "src/plugins/settings/manager.py:532",
    "src/plugins/settings/manager.py:535",
    "src/plugins/settings/manager.py:538",
    "src/plugins/settings/manager.py:541",
    "src/plugins/settings/manager.py:544",
    "src/plugins/settings/manager.py:547",
    "src/plugins/settings/manager.py:550",
    "src/tabs/image_compare/plugins/video_editor/presenter_parts/output_paths.py:108",
    "src/tabs/image_compare/services/image_export/state.py:115",
    "src/tabs/image_compare/services/image_export/state.py:116",
    "src/tabs/image_compare/services/image_export/state.py:120",
    "src/tabs/image_compare/services/image_export/state.py:124",
    "src/tabs/image_compare/services/image_export/state.py:127",
    "src/tabs/image_compare/services/image_export/state.py:128",
    "src/tabs/image_compare/services/image_export/state.py:129",
    "src/tabs/image_compare/services/image_export/state.py:133",
    "src/tabs/image_compare/services/image_export/state.py:134",
    "src/tabs/image_compare/services/image_export/state.py:136",
    "src/tabs/image_compare/services/image_export/state.py:137",
    "src/tabs/image_compare/services/image_export/state.py:142",
    "src/tabs/multi_compare/use_cases/export.py:65",
    "src/tabs/multi_compare/use_cases/export.py:67",
    "src/tabs/multi_compare/use_cases/export.py:68",
    "src/tabs/multi_compare/use_cases/export.py:69",
    "src/tabs/multi_compare/use_cases/export.py:71",
    "src/tabs/multi_compare/use_cases/export.py:73",
    "src/tabs/multi_compare/use_cases/export.py:75",
    "src/tabs/multi_compare/use_cases/export.py:77",
    "src/tabs/multi_compare/use_cases/export.py:79",
    "src/tabs/multi_compare/use_cases/export.py:81",
    "src/tabs/multi_compare/use_cases/export.py:85",
    "src/tabs/multi_compare/use_cases/export.py:93",
    "src/ui/canvas_infra/rhi/rhi_fallback_notice.py:72",
})
_MIGRATION_HINT = (
    "Direct Store.settings mutation outside Dispatcher/Reducer — "
    "use store.get_dispatcher().dispatch(Set*Action, scope=\"settings\") "
    "instead"
)


def _is_store_rooted_settings_chain(chain: list[str] | None) -> bool:
    """True when ``chain`` passes through ``settings`` behind a Store root.

    E.g. ``[store, settings, theme]``, ``[self, store, settings, x]``,
    ``[window, store, settings, ui_mode]`` match; ``[self, settings]``,
    ``[settings, x]`` (bare param) and ``[self, settings_panel]`` do not.
    """
    if not chain or "settings" not in chain:
        return False
    prefix = chain[: chain.index("settings")]
    return any(part in STORE_NAMES for part in prefix)


def _store_factory_names(tree: ast.Module) -> set[str]:
    """Module functions that construct a ``Store()`` (transient builders).

    Covers snapshot/export helpers such as ``_get_reusable_snapshot_store``
    whose call-site results (``store = _get_reusable_snapshot_store()``)
    are working copies, not the live Store.
    """
    makers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "Store":
                makers.add(node.name)
                break
    return makers


def _value_is_store_settings(value: ast.AST, aliases: set[str]) -> bool:
    """True when ``value`` evaluates to the live ``<store>.settings``."""
    if isinstance(value, ast.Attribute):
        return _is_store_rooted_settings_chain(_get_chain(value))
    if isinstance(value, ast.Name):
        return value.id in aliases
    if isinstance(value, ast.Call):
        # getattr(<store-chain>, "settings", ...) alias form.
        func = value.func
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(value.args) >= 2
        ):
            base = value.args[0]
            key = value.args[1]
            if (
                isinstance(key, ast.Constant)
                and key.value == "settings"
                and isinstance(base, ast.Attribute)
            ):
                chain = _get_chain(base)
                if chain and any(part in STORE_NAMES for part in chain):
                    return True
    return False


def _collect_settings_aliases(tree: ast.Module) -> set[str]:
    """Names bound to ``<store>.settings`` (incl. tuple destructuring)."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            pairs: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
                if (
                    isinstance(target, ast.Tuple)
                    and isinstance(value, ast.Tuple)
                    and len(target.elts) == len(value.elts)
                ):
                    pairs = list(zip(target.elts, value.elts))
                else:
                    pairs = [(target, value)]
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
            ):
                pairs = [(node.target, node.value)]
            for target, value in pairs:
                if (
                    isinstance(target, ast.Name)
                    and target.id not in aliases
                    and _value_is_store_settings(value, aliases)
                ):
                    aliases.add(target.id)
                    changed = True
    return aliases


def _collect_extended_transients(tree: ast.Module, path=None) -> set[str]:
    """Sibling transients + store-factory call results + one more hop.

    Starts from the imported sibling set (``Store()``-derived data flow)
    and additionally treats ``x = <store-factory>(...)`` as transient,
    then propagates one fixpoint hop (``y = <expr containing transient>``),
    mirroring the sibling's transitive rule.
    """
    transients: set[str] = set(_collect_transients(tree, path))
    makers = _store_factory_names(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            func = node.value.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in makers:
                transients.add(node.targets[0].id)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                lhs = node.targets[0].id
                if lhs in transients:
                    continue
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Name) and sub.id in transients:
                        transients.add(lhs)
                        changed = True
                        break
                    if isinstance(sub, ast.Attribute):
                        chain = _get_chain(sub)
                        if chain and chain[0] in transients:
                            transients.add(lhs)
                            changed = True
                            break
                if changed:
                    break
    return transients


def _target_is_settings_write(
    chain: list[str] | None, aliases: set[str], transients: set[str]
) -> bool:
    """True for a live-Store settings write; False for transient/foreign."""
    if not chain:
        return False
    if chain[0] in transients:
        return False
    if chain[0] in aliases:
        # Alias root: any dotted/suffixed write mutates the live state
        # (``settings.x =``, ``s.theme =``). A bare rebind (``s = ...``)
        # never reaches here — only Attribute/Subscript LHS targets do.
        return True
    return _is_store_rooted_settings_chain(chain)


def _settings_offenders() -> list[tuple[str, int, str]]:
    offenders: list[tuple[str, int, str]] = []
    for path in iter_py(SRC):
        rel_path = rel(path)
        if rel_path in EXEMPT:
            continue
        tree = parse(path)
        if tree is None:
            continue
        transients = _collect_extended_transients(tree, path)
        aliases = _collect_settings_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    if node.target is None:  # type: ignore[attr-defined]
                        continue
                    targets = [node.target]  # type: ignore[attr-defined]
                for target in targets:
                    elts: list[ast.AST] = []
                    if isinstance(target, ast.Attribute):
                        elts = [target]
                    elif isinstance(target, ast.Tuple):
                        elts = target.elts
                    elif isinstance(target, ast.Subscript):
                        elts = [target]
                    else:
                        continue
                    for elt in elts:
                        chain: list[str] | None = None
                        display = ""
                        if isinstance(elt, ast.Attribute):
                            chain = _get_chain(elt)
                            if chain is None:
                                continue
                            display = ".".join(chain)
                        elif isinstance(elt, ast.Subscript):
                            val = elt.value
                            if isinstance(val, ast.Attribute):
                                chain = _get_chain(val)
                            elif isinstance(val, ast.Name):
                                chain = [val.id]
                            else:
                                continue
                            if chain is None:
                                continue
                            try:
                                display = ast.unparse(elt).strip()  # type: ignore[attr-defined]
                            except Exception:
                                display = ".".join(chain) + "[...]"
                        else:
                            continue
                        if _target_is_settings_write(chain, aliases, transients):
                            offenders.append((rel_path, node.lineno, display))
            elif isinstance(node, ast.Call):
                func = node.func
                is_setattr = (
                    (isinstance(func, ast.Name) and func.id == "setattr")
                    or (isinstance(func, ast.Attribute) and func.attr == "setattr")
                )
                if not is_setattr or not node.args:
                    continue
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in _MOCK_ROOTS
                ):
                    continue
                target = node.args[0]
                chain = None
                if isinstance(target, ast.Attribute):
                    chain = _get_chain(target)
                elif isinstance(target, ast.Name):
                    chain = [target.id]
                else:
                    continue
                if not _target_is_settings_write(chain, aliases, transients):
                    continue
                name_arg = node.args[1] if len(node.args) > 1 else None
                attr_name = (
                    repr(name_arg.value)
                    if isinstance(name_arg, ast.Constant)
                    else "?"
                )
                offenders.append(
                    (rel_path, node.lineno, f"setattr({'.'.join(chain)}, {attr_name})")  # type: ignore[arg-type]
                )
    return sorted(offenders)


def test_no_direct_settings_mutation():
    offenders = _settings_offenders()
    offender_keys = {f"{p}:{ln}" for p, ln, _ in offenders}
    allowlist = set(ALLOWLIST)
    stale = sorted(allowlist - offender_keys)
    assert not stale, (
        "Stale ALLOWLIST entries in test_no_direct_settings_mutation — "
        "the site was already migrated to dispatch; drop these entries "
        f"({len(stale)}):\n  " + "\n  ".join(stale)
    )
    fresh = [(p, ln, c) for p, ln, c in offenders if f"{p}:{ln}" not in allowlist]
    assert not fresh, (
        f"{_MIGRATION_HINT} ({len(fresh)} hits):\n  "
        + "\n  ".join(f"{p}:{ln} — {chain} = ..." for p, ln, chain in fresh)
    )
