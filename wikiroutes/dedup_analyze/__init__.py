"""Analysis of route overlaps (dedup_analyze).

Split from the former monolithic `dedup_analyze.py` into preparation
(`prepare`), pair/decision frames (`pairs`), spatial coverage
(`coverage`), point sampling (`sampling`) and the orchestration entry
point (`workflow`).
"""
from .workflow import dedup_analyze

__all__ = ["dedup_analyze"]