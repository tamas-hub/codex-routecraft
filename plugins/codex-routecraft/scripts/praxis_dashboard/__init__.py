"""Source-neutral, local-only Praxis Dashboard projections."""

from .projection import DASHBOARD_API_VERSION, build_snapshot
from .query import PraxisDashboardQuery

__all__ = ("DASHBOARD_API_VERSION", "PraxisDashboardQuery", "build_snapshot")
