"""Shared helpers for the research script tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_script(relpath: str) -> ModuleType:
    """Import a script that has no importable name (hyphenated path) as a module.

    The module gets the dotted name mutmut derives from the file path (research/a-b/c-d.py ->
    research.a-b.c-d), so mutmut maps the mutants it generates to the tests that exercise them.
    """
    name = relpath.removesuffix(".py").replace("/", ".")
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def root() -> Path:
    """Repository root (the mutmut sandbox root when running under mutmut)."""
    return ROOT


@pytest.fixture(scope="session")
def build_skill() -> ModuleType:
    return load_script("research/reference-build/skill/build-skill.py")


@pytest.fixture(scope="session")
def guard_cases() -> ModuleType:
    return load_script("research/reference-build/install/guard-test-cases.py")
