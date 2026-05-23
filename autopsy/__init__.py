"""autopsy - your agent died. here's why.

Public API. Users only need::

    from autopsy import lens

    @lens.trace
    async def my_agent(query):
        ...
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autopsy.core.decorator import LensDecorator
from autopsy.core.events import DiagnosisResult, TraceBundle

__version__ = "0.1.0"


@dataclass
class LensConfig:
    gmi_api_key: Optional[str] = None
    google_ai_api_key: Optional[str] = None
    session_dir: Optional[str] = None
    port: int = 7823
    auto_diagnose: bool = False
    model: Optional[str] = None


# Default singleton. Reads keys from env at call time.
lens = LensDecorator()

__all__ = ["lens", "LensConfig", "TraceBundle", "DiagnosisResult", "__version__"]
