"""Locations of Tau's packaged self-documentation and examples.

Tau 随包自带文档和示例的位置。
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DATA_ROOT = _PACKAGE_ROOT / "data"


def tau_readme_path() -> Path:
    """Return the installed overview document for Tau-aware tasks.

    返回供 Tau 感知任务使用的已安装概览文档。
    """
    return _DATA_ROOT / "docs" / "README.md"


def tau_docs_path() -> Path:
    """Return the installed Tau self-documentation directory.

    返回已安装的 Tau 自身文档目录。
    """
    return _DATA_ROOT / "docs"


def tau_examples_path() -> Path:
    """Return the installed Tau example directory.

    返回已安装的 Tau 示例目录。
    """
    return _DATA_ROOT / "examples"
