"""Import-discipline guard + adapter conformance for the surface-agnostic core (#50).

The curation core (`bot/curation/`) must never import a chat-platform SDK - that is
what keeps it surface-agnostic. Both surface adapters (Discord, the Matrix skeleton)
must structurally satisfy the `Surface` port, so a handler can run against either.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from bot.curation.surface import NullSurface, Surface
from bot.discord_ingest.discord_notifier import DiscordSurface
from bot.matrix_ingest.surface import MatrixSurface

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "bot"
_CURATION = _SRC / "curation"
_FORBIDDEN_SDKS = {"discord", "nio", "matrix"}  # chat-platform client libraries


def _imported_roots(path: pathlib.Path) -> set[str]:
    """Top-level module names this file imports absolutely (relative imports skipped)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_curation_core_imports_no_surface_sdk():
    offenders = {
        path.name: bad
        for path in sorted(_CURATION.rglob("*.py"))
        if (bad := _imported_roots(path) & _FORBIDDEN_SDKS)
    }
    assert not offenders, f"bot/curation must not import a surface SDK: {offenders}"


def test_curation_package_holds_the_expected_ports():
    names = {p.stem for p in _CURATION.glob("*.py")}
    assert {"components", "surface", "events", "outcomes", "types"} <= names


def test_discord_surface_conforms_to_port():
    # A raw messageable isn't needed for the structural check.
    assert isinstance(DiscordSurface(channel=object()), Surface)


def test_matrix_surface_skeleton_conforms_to_port():
    assert isinstance(MatrixSurface(), Surface)


def test_null_surface_conforms_to_port():
    assert isinstance(NullSurface(), Surface)


async def test_matrix_surface_methods_raise_until_wired():
    # The skeleton is deliberately unimplemented; each port method fails loudly until a
    # homeserver is wired (proving the shape without pretending it works).
    s = MatrixSurface()
    with pytest.raises(NotImplementedError):
        await s.send("hi")
    with pytest.raises(NotImplementedError):
        await s.edit(1)
    with pytest.raises(NotImplementedError):
        await s.disable_components(1, "x")
    with pytest.raises(NotImplementedError):
        await s.edit_or_none(1, "x")
    with pytest.raises(NotImplementedError):
        await s.message_exists(1)
    with pytest.raises(NotImplementedError):
        await s.archive()
    with pytest.raises(NotImplementedError):
        s.archive_after_delay()
    with pytest.raises(NotImplementedError):
        await s.unarchive()
    with pytest.raises(NotImplementedError):
        await s.clear_trigger(1, 2, "x")
