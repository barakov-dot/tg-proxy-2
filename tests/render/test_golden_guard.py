from __future__ import annotations

from collections.abc import Callable

import pytest


def test_missing_golden_fails_without_update_flag(
    golden: Callable[[str, bytes | str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UPDATE_GOLDEN", raising=False)
    with pytest.raises(pytest.fail.Exception):
        golden("definitely-missing-golden.txt", "x")
