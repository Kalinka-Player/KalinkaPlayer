"""The years a record can carry, so a placeholder date never becomes one."""

import re
from typing import Any, Optional

_YEAR_RE = re.compile(r"\d{4}")


def plausible_year(year: Optional[int]) -> Optional[int]:
    """``year``, or None where no record could carry it, as the 0 of a
    ``0000`` date tag."""
    return year if year is not None and 1000 <= year <= 2100 else None


def year_from_date(date: Any) -> Optional[int]:
    """The plausible year a date tag names: ``1979``, ``1979-11-30``."""
    m = _YEAR_RE.search(str(date))
    return plausible_year(int(m.group())) if m else None
