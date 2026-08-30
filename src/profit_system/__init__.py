"""Public surface for the V0.2 profit-accountable modular monolith."""

from .execution import ExecutionEngine, VenueAdapter
from .opportunities import OpportunityEngine
from .portfolio import PortfolioBook
from .runtime import StrategyRuntime

__version__ = "0.2.0"

__all__ = [
    "ExecutionEngine",
    "OpportunityEngine",
    "PortfolioBook",
    "StrategyRuntime",
    "VenueAdapter",
    "__version__",
]
