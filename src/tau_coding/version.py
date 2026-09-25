"""Package version helpers.

包版本辅助函数。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_DISTRIBUTION_NAME = "tau-ai"
_UNKNOWN_VERSION = "0+unknown"


def current_version() -> str:
    """Return Tau's installed package version from package metadata.

    从包元数据返回已安装的 Tau 版本。
    """
    try:
        return version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION
