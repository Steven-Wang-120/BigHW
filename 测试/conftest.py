"""Synthetic inputs and temporary databases only."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "数据/fixtures/controlled_cases.json").read_text(encoding="utf-8"))


@pytest.fixture
def controlled_cases():
    return FIXTURE


@pytest.fixture
def make_row():
    from 主程序.domain.models import RawRow, SourceRowRef

    def build(values=None, *, row_number=2, cell_types=None, excel_epoch="1900", has_formula=False):
        fields = {**FIXTURE["defaults"], **(values or {})}
        kwargs = {"has_formula": has_formula}
        if cell_types is not None:
            kwargs["cell_types"] = cell_types
        if excel_epoch != "1900":
            kwargs["excel_epoch"] = excel_epoch
        return RawRow(SourceRowRef("a" * 64, "Synthetic", row_number), fields, **kwargs)

    return build
