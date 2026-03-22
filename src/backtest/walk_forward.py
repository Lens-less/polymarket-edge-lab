"""
Walk-Forward Backtesting Framework

Implements time-series cross-validation for strategy parameter optimization:
- Train period -> Validation period -> Test period
- Rolling or expanding window support
- Parameter search integration
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple
from decimal import Decimal
from datetime import datetime, timedelta
import pandas as pd

from src.backtest.engine import BacktestEngine
from src.backtest.data import HistoricalData, OrderBookSnapshot


@dataclass
class WalkForwardConfig:
    """Configuration for walk-forward analysis."""
    train_period_days: int = 30      # Training period length
    val_period_days: int = 7         # Validation period length
    test_period_days: int = 7        # Test period length
    step_days: int = 7               # Step forward by this many days
    rolling: bool = True             # True = rolling, False = expanding


@dataclass
class WalkForwardResult:
    """Results from walk-forward analysis."""
    train_periods: List[dict]
    val_periods: List[dict]
    test_periods: List[dict]
    best_params: dict
    oos_sharpe: float
    oos_return: float
    oos_max_drawdown: float


@dataclass
class FoldResult:
    """Result for a single fold."""
    start_date: datetime
    end_date: datetime
    period_type: str  # "train", "val", "test"
    params: dict
    sharpe: float
    return_pct: float
    max_drawdown: float
    num_trades: int


class WalkForwardEngine:
    """
    Walk-forward backtesting engine.

    Splits historical data into rolling train/val/test windows and evaluates
    parameter combinations across each fold to avoid overfitting.
    """

    def __init__(
        self,
        data: HistoricalData,
        config: Optional[WalkForwardConfig] = None,
    ):
        self.data = data
        self.config = config or WalkForwardConfig()
        self._fold_results: List[FoldResult] = []

    def run(
        self,
        param_grid: Dict[str, List],
        metric: str = "sharpe_ratio",
    ) -> WalkForwardResult:
        """
        Run walk-forward analysis.

        Args:
            param_grid: Dict of parameter names to value lists
            metric: Metric to optimize ("sharpe_ratio", "return", etc.)

        Returns:
            WalkForwardResult with all fold results and best params
        """
        # Get date range from data
        snapshots = list(self.data.snapshots)
        if not snapshots:
            raise ValueError("No data in HistoricalData")

        start_ts = snapshots[0].timestamp
        end_ts = snapshots[-1].timestamp

        start_date = datetime.fromtimestamp(start_ts)
        end_date = datetime.fromtimestamp(end_ts)

        # Calculate number of folds
        total_days = (end_date - start_date).days
        fold_period = self.config.train_period_days + self.config.val_period_days + self.config.test_period_days

        if total_days < fold_period:
            raise ValueError(f"Data span ({total_days} days) shorter than fold period ({fold_period} days)")

        num_folds = (total_days - fold_period) // self.config.step_days + 1

        train_periods = []
        val_periods = []
        test_periods = []

        # Run each fold
        for fold in range(num_folds):
            fold_start = start_date + timedelta(days=fold * self.config.step_days)

            train_end = fold_start + timedelta(days=self.config.train_period_days)
            val_end = train_end + timedelta(days=self.config.val_period_days)
            test_end = val_end + timedelta(days=self.config.test_period_days)

            if test_end > end_date:
                break

            # Split data
            train_data = self._slice_data(snapshots, fold_start, train_end)
            val_data = self._slice_data(snapshots, train_end, val_end)
            test_data = self._slice_data(snapshots, val_end, test_end)

            if len(train_data) < 10 or len(val_data) < 10 or len(test_data) < 10:
                continue

            # Find best params on validation
            best_params, best_val_sharpe = self._search_params(
                train_data, val_data, param_grid, metric
            )

            # Evaluate on test with best params
            test_sharpe, test_return, test_dd = self._evaluate(
                test_data, best_params
            )

            # Record results
            train_periods.append({
                "start": fold_start,
                "end": train_end,
                "sharpe": best_val_sharpe,
            })

            val_periods.append({
                "start": train_end,
                "end": val_end,
                "params": best_params,
                "sharpe": best_val_sharpe,
            })

            test_periods.append({
                "start": val_end,
                "end": test_end,
                "params": best_params,
                "sharpe": test_sharpe,
                "return": test_return,
                "max_drawdown": test_dd,
            })

        # Aggregate out-of-sample results
        oos_sharpe = sum(p["sharpe"] for p in test_periods) / len(test_periods) if test_periods else 0
        oos_return = sum(p["return"] for p in test_periods) / len(test_periods) if test_periods else 0
        oos_max_drawdown = max((p["max_drawdown"] for p in test_periods), default=0)

        # Get best params (from last validation)
        best_params = val_periods[-1]["params"] if val_periods else {}

        return WalkForwardResult(
            train_periods=train_periods,
            val_periods=val_periods,
            test_periods=test_periods,
            best_params=best_params,
            oos_sharpe=oos_sharpe,
            oos_return=oos_return,
            oos_max_drawdown=oos_max_drawdown,
        )

    def _slice_data(
        self,
        snapshots: List[OrderBookSnapshot],
        start: datetime,
        end: datetime,
    ) -> HistoricalData:
        """Slice data by date range."""
        start_ts = start.timestamp()
        end_ts = end.timestamp()

        sliced = HistoricalData()
        for snap in snapshots:
            if start_ts <= snap.timestamp <= end_ts:
                sliced.add_snapshot(snap)

        return sliced

    def _search_params(
        self,
        train_data: HistoricalData,
        val_data: HistoricalData,
        param_grid: Dict[str, List],
        metric: str,
    ) -> Tuple[dict, float]:
        """Search for best parameters on training data."""
        best_params = {}
        best_sharpe = float("-inf")

        # Generate parameter combinations
        import itertools
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())

        for values in itertools.product(*param_values):
            params = dict(zip(param_names, values))

            try:
                # Train on train_data, evaluate on val_data
                sharpe, _, _ = self._evaluate(val_data, params)
            except Exception:
                sharpe = float("-inf")

            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_params = params

        return best_params, best_sharpe

    def _evaluate(
        self,
        data: HistoricalData,
        params: dict,
    ) -> Tuple[float, float, float]:
        """Evaluate strategy with given params."""
        # Create engine with params
        engine = BacktestEngine(
            initial_capital=Decimal("1000"),
            fee_config=params.get("fee_config"),
        )

        # Run backtest
        result = engine.run(data)

        return (
            result.sharpe_ratio,
            result.total_return,
            result.max_drawdown,
        )

    def get_fold_results(self) -> List[FoldResult]:
        """Get all fold results."""
        return self._fold_results