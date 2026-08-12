"""Evidence-tiered Polymarket liquidity-reward accounting.

This module deliberately keeps three ledgers separate:

``RewardTheoretical``
    A public-book formula bound.  Public L2 does not identify makers, so its
    lower bound is zero and it can never recognize PnL.
``RewardScoring``
    An authenticated observation about one known order.  The caller supplies
    an evidence reference; this read-only module never authenticates itself.
``RewardPayout``
    A dated, asset-specific payment observation.  Only a payment with explicit
    confirmation evidence is recognized as PnL.

All financial arithmetic uses :class:`~decimal.Decimal`; JSON-ready output
serializes Decimal values as strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional


ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
SINGLE_SIDE_DISCOUNT = Decimal("3")
MINIMUM_ASSUMED_PAYOUT = Decimal("1")
REWARD_HAIRCUTS = (
    Decimal("1"),
    Decimal("0.5"),
    Decimal("0.3"),
    Decimal("0"),
)


def _parse_decimal(value: Any) -> Optional[Decimal]:
    """Convert one untrusted API scalar at the module boundary.

    JSON decoders can produce binary floats for fields such as ``ma=5.5``.
    Converting through ``str`` immediately prevents those values from entering
    any arithmetic as floats.
    """

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _require_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True)
class RewardMarketConfig:
    """The compact CLOB V2 reward configuration for one condition."""

    condition_id: str
    minimum_size: Optional[Decimal]
    maximum_spread_cents: Optional[Decimal]
    enabled: bool
    minimum_order_age_seconds: Optional[Decimal]

    @classmethod
    def from_compact_market(
        cls,
        raw: Mapping[str, Any],
    ) -> "RewardMarketConfig":
        reward = raw.get("r")
        if not isinstance(reward, Mapping):
            reward = {}

        minimum_age = _parse_decimal(reward.get("moas"))
        if minimum_age is None:
            minimum_age = _parse_decimal(raw.get("oas"))
        # ``smoa`` is an explicit platform instruction to skip the minimum
        # order-age gate.  It is therefore known zero, not an unknown age.
        if reward.get("smoa") is True:
            minimum_age = ZERO

        return cls(
            condition_id=str(
                raw.get("c") or raw.get("condition_id") or ""
            ),
            minimum_size=_parse_decimal(reward.get("mi")),
            maximum_spread_cents=_parse_decimal(reward.get("ma")),
            enabled=reward.get("e") is True,
            minimum_order_age_seconds=minimum_age,
        )

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.condition_id:
            reasons.append("missing_condition_id")
        if not self.enabled:
            reasons.append("rewards_disabled")
        if self.minimum_size is None:
            reasons.append("missing_minimum_size")
        elif self.minimum_size <= ZERO:
            reasons.append("invalid_minimum_size")
        if self.maximum_spread_cents is None:
            reasons.append("missing_maximum_spread")
        elif self.maximum_spread_cents <= ZERO:
            reasons.append("invalid_maximum_spread")
        if self.minimum_order_age_seconds is None:
            reasons.append("missing_minimum_order_age")
        elif self.minimum_order_age_seconds < ZERO:
            reasons.append("invalid_minimum_order_age")
        return tuple(reasons)

    @property
    def eligibility_configured(self) -> bool:
        return not self.rejection_reasons

    def to_dict(self) -> dict[str, object]:
        return {
            "condition_id": self.condition_id,
            "minimum_size": (
                str(self.minimum_size)
                if self.minimum_size is not None
                else None
            ),
            "maximum_spread_cents": (
                str(self.maximum_spread_cents)
                if self.maximum_spread_cents is not None
                else None
            ),
            "enabled": self.enabled,
            "minimum_order_age_seconds": (
                str(self.minimum_order_age_seconds)
                if self.minimum_order_age_seconds is not None
                else None
            ),
            "eligibility_configured": self.eligibility_configured,
            "rejection_reasons": list(self.rejection_reasons),
        }


@dataclass(frozen=True)
class RewardLevel:
    """One visible aggregate price level with independently established age."""

    price: Decimal
    size: Decimal
    age_seconds: Optional[Decimal]
    evidence_ref: Optional[str] = None

    def __post_init__(self) -> None:
        _require_decimal("price", self.price)
        _require_decimal("size", self.size)
        if not ZERO <= self.price <= ONE:
            raise ValueError("price must be between 0 and 1")
        if self.size <= ZERO:
            raise ValueError("size must be positive")
        if self.age_seconds is not None:
            _require_decimal("age_seconds", self.age_seconds)
            if self.age_seconds < ZERO:
                raise ValueError("age_seconds cannot be negative")


@dataclass(frozen=True)
class PublicRewardBook:
    """Current and complementary aggregate books for one binary outcome."""

    current_bids: tuple[RewardLevel, ...] = ()
    current_asks: tuple[RewardLevel, ...] = ()
    complement_bids: tuple[RewardLevel, ...] = ()
    complement_asks: tuple[RewardLevel, ...] = ()


def _single_order_score(
    *,
    maximum_spread_cents: Decimal,
    distance_cents: Decimal,
    size: Decimal,
) -> Decimal:
    """Official quadratic order score ``((v-s)/v)^2 * b``."""

    if (
        maximum_spread_cents <= ZERO
        or size <= ZERO
        or distance_cents < ZERO
        or distance_cents > maximum_spread_cents
    ):
        return ZERO
    closeness = (
        maximum_spread_cents - distance_cents
    ) / maximum_spread_cents
    return closeness * closeness * size


def _score_levels(
    *,
    role: str,
    levels: Iterable[RewardLevel],
    midpoint: Decimal,
    config: RewardMarketConfig,
) -> tuple[Decimal, tuple[str, ...]]:
    assert config.minimum_size is not None
    assert config.maximum_spread_cents is not None
    assert config.minimum_order_age_seconds is not None

    total = ZERO
    rejections: list[str] = []
    for level in levels:
        if level.size < config.minimum_size:
            rejections.append(f"{role}:below_minimum_size")
            continue
        if level.age_seconds is None:
            rejections.append(f"{role}:unknown_order_age")
            continue
        if level.age_seconds < config.minimum_order_age_seconds:
            rejections.append(f"{role}:below_minimum_order_age")
            continue

        if role == "current_bids":
            distance = midpoint - level.price
        elif role == "current_asks":
            distance = level.price - midpoint
        elif role == "complement_asks":
            # A complement ask at p is the current-side bid equivalent 1-p.
            distance = midpoint - (ONE - level.price)
        elif role == "complement_bids":
            # A complement bid at p is the current-side ask equivalent 1-p.
            distance = (ONE - level.price) - midpoint
        else:  # pragma: no cover - private call sites enumerate all roles
            raise ValueError(f"unsupported reward-book role: {role}")

        if distance < ZERO:
            rejections.append(f"{role}:crosses_reward_midpoint")
            continue
        distance_cents = distance / CENT
        if distance_cents > config.maximum_spread_cents:
            rejections.append(f"{role}:outside_maximum_spread")
            continue
        total += _single_order_score(
            maximum_spread_cents=config.maximum_spread_cents,
            distance_cents=distance_cents,
            size=level.size,
        )
    return total, tuple(rejections)


def official_combined_score(
    q_one: Decimal,
    q_two: Decimal,
    midpoint: Decimal,
) -> Decimal:
    """Combine official Q sides, including the extreme-midpoint rule."""

    _require_decimal("q_one", q_one)
    _require_decimal("q_two", q_two)
    _require_decimal("midpoint", midpoint)
    if q_one < ZERO or q_two < ZERO:
        raise ValueError("Q scores cannot be negative")
    if not ZERO <= midpoint <= ONE:
        raise ValueError("midpoint must be between 0 and 1")

    paired = min(q_one, q_two)
    if Decimal("0.10") <= midpoint <= Decimal("0.90"):
        discounted_single_side = max(
            q_one / SINGLE_SIDE_DISCOUNT,
            q_two / SINGLE_SIDE_DISCOUNT,
        )
        return max(paired, discounted_single_side)
    return paired


@dataclass(frozen=True)
class RewardTheoretical:
    """A public aggregate formula bound, never recognized income."""

    condition_id: str
    midpoint: Decimal
    q_one_lower: Decimal
    q_one_upper: Decimal
    q_two_lower: Decimal
    q_two_upper: Decimal
    score_lower: Decimal
    score_upper: Decimal
    reward_lower: Decimal
    reward_upper: Decimal
    components: tuple[tuple[str, Decimal], ...]
    rejection_reasons: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def recognized_pnl(self) -> Decimal:
        """Theory can inform scenarios but can never become realized PnL."""

        return ZERO

    def component_score(self, name: str) -> Decimal:
        for component_name, score in self.components:
            if component_name == name:
                return score
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {
            "ledger": "theoretical",
            "condition_id": self.condition_id,
            "midpoint": str(self.midpoint),
            "q_one_lower": str(self.q_one_lower),
            "q_one_upper": str(self.q_one_upper),
            "q_two_lower": str(self.q_two_lower),
            "q_two_upper": str(self.q_two_upper),
            "score_lower": str(self.score_lower),
            "score_upper": str(self.score_upper),
            "reward_lower": str(self.reward_lower),
            "reward_upper": str(self.reward_upper),
            "recognized_pnl": str(self.recognized_pnl),
            "components": {
                name: str(value) for name, value in self.components
            },
            "rejection_reasons": list(self.rejection_reasons),
            "notes": list(self.notes),
        }


def score_public_reward_book(
    *,
    config: RewardMarketConfig,
    midpoint: Decimal,
    book: PublicRewardBook,
    daily_reward_pool: Decimal = ZERO,
) -> RewardTheoretical:
    """Return conservative maker-score/reward bounds from a public book.

    ``Q_one`` is current bids plus complementary asks.  ``Q_two`` is current
    asks plus complementary bids.  Public aggregate levels have neither maker
    identities nor exact per-order decomposition, so zero is the only justified
    lower bound.  The upper bound treats every otherwise eligible aggregate
    level as if it could belong to one maker.
    """

    _require_decimal("midpoint", midpoint)
    _require_decimal("daily_reward_pool", daily_reward_pool)
    if not ZERO <= midpoint <= ONE:
        raise ValueError("midpoint must be between 0 and 1")
    if daily_reward_pool < ZERO:
        raise ValueError("daily_reward_pool cannot be negative")

    components = {
        "current_bids": ZERO,
        "current_asks": ZERO,
        "complement_bids": ZERO,
        "complement_asks": ZERO,
    }
    config_rejections = tuple(
        f"config:{reason}" for reason in config.rejection_reasons
    )
    rejections: list[str] = list(config_rejections)

    if config.eligibility_configured:
        for role, levels in (
            ("current_bids", book.current_bids),
            ("current_asks", book.current_asks),
            ("complement_bids", book.complement_bids),
            ("complement_asks", book.complement_asks),
        ):
            score, rejected = _score_levels(
                role=role,
                levels=levels,
                midpoint=midpoint,
                config=config,
            )
            components[role] = score
            rejections.extend(rejected)

    q_one_upper = (
        components["current_bids"] + components["complement_asks"]
    )
    q_two_upper = (
        components["current_asks"] + components["complement_bids"]
    )
    score_upper = official_combined_score(
        q_one_upper,
        q_two_upper,
        midpoint,
    )
    reward_upper = daily_reward_pool if score_upper > ZERO else ZERO
    notes = ["public_book_has_no_maker_identity"]
    if not Decimal("0.10") <= midpoint <= Decimal("0.90"):
        notes.append("extreme_midpoint_requires_two_sides")
    if reward_upper > ZERO:
        notes.append("reward_upper_is_full_pool_not_expected_share")

    return RewardTheoretical(
        condition_id=config.condition_id,
        midpoint=midpoint,
        q_one_lower=ZERO,
        q_one_upper=q_one_upper,
        q_two_lower=ZERO,
        q_two_upper=q_two_upper,
        score_lower=ZERO,
        score_upper=score_upper,
        reward_lower=ZERO,
        reward_upper=reward_upper,
        components=tuple(components.items()),
        rejection_reasons=tuple(dict.fromkeys(rejections)),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class RewardScoring:
    """Authenticated scoring evidence supplied by a caller.

    Constructing this record does not perform authentication and does not make
    any network request.
    """

    order_id: str
    auth_evidence_ref: str
    maker_evidence_ref: str
    condition_id: str
    asset_id: str
    epoch_id: str
    observed_at: str
    scoring: bool
    reward_percentage: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for name in (
            "order_id",
            "auth_evidence_ref",
            "maker_evidence_ref",
            "condition_id",
            "asset_id",
            "epoch_id",
            "observed_at",
        ):
            _require_text(name, getattr(self, name))
        if not isinstance(self.scoring, bool):
            raise TypeError("scoring must be bool")
        if self.reward_percentage is not None:
            _require_decimal("reward_percentage", self.reward_percentage)
            if self.reward_percentage < ZERO:
                raise ValueError("reward_percentage cannot be negative")

    @property
    def recognized_pnl(self) -> Decimal:
        return ZERO

    def to_dict(self) -> dict[str, object]:
        return {
            "ledger": "scoring",
            "order_id": self.order_id,
            "auth_evidence_ref": self.auth_evidence_ref,
            "maker_evidence_ref": self.maker_evidence_ref,
            "condition_id": self.condition_id,
            "asset_id": self.asset_id,
            "epoch_id": self.epoch_id,
            "observed_at": self.observed_at,
            "scoring": self.scoring,
            "reward_percentage": (
                str(self.reward_percentage)
                if self.reward_percentage is not None
                else None
            ),
            "recognized_pnl": str(self.recognized_pnl),
        }


@dataclass(frozen=True)
class RewardPayout:
    """Dated payout evidence in one reward asset."""

    payout_date: str
    epoch_id: str
    condition_id: str
    asset_id: str
    maker_evidence_ref: str
    amount: Decimal
    payment_confirmed: bool
    payment_evidence_ref: Optional[str] = None

    def __post_init__(self) -> None:
        for name in (
            "payout_date",
            "epoch_id",
            "condition_id",
            "asset_id",
            "maker_evidence_ref",
        ):
            _require_text(name, getattr(self, name))
        try:
            date.fromisoformat(self.payout_date)
        except ValueError as exc:
            raise ValueError("payout_date must be ISO YYYY-MM-DD") from exc
        _require_decimal("amount", self.amount)
        if self.amount < ZERO:
            raise ValueError("amount cannot be negative")
        if not isinstance(self.payment_confirmed, bool):
            raise TypeError("payment_confirmed must be bool")
        if self.payment_confirmed:
            if not self.payment_evidence_ref:
                raise ValueError(
                    "payment_evidence_ref is required for confirmed payment"
                )

    @property
    def recognized_pnl(self) -> Decimal:
        return self.amount if self.payment_confirmed else ZERO

    @property
    def payout_status(self) -> str:
        if self.payment_confirmed:
            return "confirmed"
        if ZERO < self.amount < MINIMUM_ASSUMED_PAYOUT:
            return "below_minimum_unconfirmed"
        return "unconfirmed"

    def to_dict(self) -> dict[str, object]:
        return {
            "ledger": "payout",
            "payout_date": self.payout_date,
            "epoch_id": self.epoch_id,
            "condition_id": self.condition_id,
            "asset_id": self.asset_id,
            "maker_evidence_ref": self.maker_evidence_ref,
            "amount": str(self.amount),
            "payment_confirmed": self.payment_confirmed,
            "payment_evidence_ref": self.payment_evidence_ref,
            "payout_status": self.payout_status,
            "recognized_pnl": str(self.recognized_pnl),
        }


@dataclass(frozen=True)
class RewardHaircutScenario:
    multiplier: Decimal
    non_reward_profit: Decimal
    theoretical_reward: Decimal
    confirmed_reward: Decimal
    net_profit: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "multiplier": str(self.multiplier),
            "non_reward_profit": str(self.non_reward_profit),
            "theoretical_reward": str(self.theoretical_reward),
            "confirmed_reward": str(self.confirmed_reward),
            "net_profit": str(self.net_profit),
        }


def apply_reward_haircuts(
    *,
    non_reward_profit: Decimal,
    theoretical_reward: Decimal | RewardTheoretical,
    condition_id: str,
    reward_asset_id: str,
    maker_evidence_ref: str,
    theoretical_epoch_ids: Iterable[str] = (),
    confirmed_payouts: Iterable[RewardPayout] = (),
) -> tuple[RewardHaircutScenario, ...]:
    """Apply the fixed 100/50/30/0% stress grid to unconfirmed theory.

    Confirmed payout evidence is included at 100% in every row and is never
    haircut.  This keeps scenario analysis separate from recognized income.
    """

    _require_decimal("non_reward_profit", non_reward_profit)
    _require_text("condition_id", condition_id)
    _require_text("reward_asset_id", reward_asset_id)
    _require_text("maker_evidence_ref", maker_evidence_ref)
    if isinstance(theoretical_reward, RewardTheoretical):
        if theoretical_reward.condition_id != condition_id:
            raise ValueError(
                "theoretical reward does not match condition scope"
            )
        theoretical_amount = theoretical_reward.reward_upper
    else:
        theoretical_amount = _require_decimal(
            "theoretical_reward",
            theoretical_reward,
        )
    if theoretical_amount < ZERO:
        raise ValueError("theoretical_reward cannot be negative")
    theory_epochs = tuple(theoretical_epoch_ids)
    if any(not isinstance(epoch, str) or not epoch.strip() for epoch in theory_epochs):
        raise ValueError("theoretical epoch ids must be non-empty strings")
    theory_epoch_set = set(theory_epochs)
    if len(theory_epoch_set) != len(theory_epochs):
        raise ValueError("theoretical epoch ids must be unique")
    if theoretical_amount > ZERO and not theory_epoch_set:
        raise ValueError(
            "positive theoretical reward requires explicit epoch ids"
        )

    confirmed_by_evidence: dict[str, RewardPayout] = {}
    for payout in confirmed_payouts:
        if payout.condition_id != condition_id:
            raise ValueError("confirmed payout violates condition scope")
        if payout.asset_id != reward_asset_id:
            raise ValueError("confirmed payout violates reward asset scope")
        if payout.maker_evidence_ref != maker_evidence_ref:
            raise ValueError("confirmed payout violates maker scope")
        if not payout.payment_confirmed:
            continue
        assert payout.payment_evidence_ref is not None
        previous = confirmed_by_evidence.get(payout.payment_evidence_ref)
        if previous is not None and previous != payout:
            raise ValueError("conflicting duplicate payment evidence")
        confirmed_by_evidence[payout.payment_evidence_ref] = payout
    confirmed_epochs = {
        payout.epoch_id for payout in confirmed_by_evidence.values()
    }
    overlap = theory_epoch_set & confirmed_epochs
    if overlap:
        raise ValueError(
            "theoretical reward epoch overlaps a confirmed payout"
        )
    confirmed_reward = sum(
        (
            payout.recognized_pnl
            for payout in confirmed_by_evidence.values()
        ),
        ZERO,
    )
    return tuple(
        RewardHaircutScenario(
            multiplier=multiplier,
            non_reward_profit=non_reward_profit,
            theoretical_reward=theoretical_amount * multiplier,
            confirmed_reward=confirmed_reward,
            net_profit=(
                non_reward_profit
                + confirmed_reward
                + theoretical_amount * multiplier
            ),
        )
        for multiplier in REWARD_HAIRCUTS
    )


@dataclass(frozen=True)
class RewardStressClassification:
    classification: str
    reward_zero_positive: bool
    net_profit_by_multiplier: tuple[tuple[Decimal, Decimal], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "reward_zero_positive": self.reward_zero_positive,
            "net_profit_by_multiplier": {
                str(multiplier): str(profit)
                for multiplier, profit in self.net_profit_by_multiplier
            },
        }


def classify_reward_stress(
    scenarios: Iterable[RewardHaircutScenario],
) -> RewardStressClassification:
    """Label reward dependence without upgrading an evidence classification."""

    scenario_rows = tuple(scenarios)
    by_multiplier = {
        scenario.multiplier: scenario.net_profit
        for scenario in scenario_rows
    }
    if tuple(by_multiplier) != REWARD_HAIRCUTS:
        raise ValueError("scenarios must use the fixed 1, 0.5, 0.3, 0 grid")

    non_reward_values = {
        scenario.non_reward_profit for scenario in scenario_rows
    }
    if len(non_reward_values) != 1:
        raise ValueError("scenarios must share one non-reward profit")
    non_reward_profit = next(iter(non_reward_values))
    reward_zero_positive = non_reward_profit > ZERO
    if by_multiplier[ONE] <= ZERO:
        classification = "rejected"
    elif reward_zero_positive:
        classification = "reward_independent"
    elif (
        by_multiplier[Decimal("0.5")] <= ZERO
        and by_multiplier[Decimal("0.3")] <= ZERO
    ):
        classification = "fragile"
    else:
        classification = "reward_dependent"
    return RewardStressClassification(
        classification=classification,
        reward_zero_positive=reward_zero_positive,
        net_profit_by_multiplier=tuple(by_multiplier.items()),
    )


@dataclass(frozen=True)
class RewardValidationCeiling:
    """The maximum classification allowed by reward-specific evidence only."""

    classification_ceiling: str
    scoring_epochs: int
    payout_epochs: int
    paired_epochs: int
    missing_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "classification_ceiling": self.classification_ceiling,
            "scoring_epochs": self.scoring_epochs,
            "payout_epochs": self.payout_epochs,
            "paired_epochs": self.paired_epochs,
            "missing_evidence": list(self.missing_evidence),
        }


def reward_validation_ceiling(
    scoring_records: Iterable[RewardScoring],
    payout_records: Iterable[RewardPayout],
    *,
    condition_id: str,
    payout_asset_id: str,
    maker_evidence_ref: str,
    minimum_epochs: int = 2,
) -> RewardValidationCeiling:
    """Enforce the multi-epoch evidence ceiling for reward strategies.

    Returning ``validated_profitable`` means only that this reward-specific
    ceiling no longer blocks that classification; all normal out-of-sample,
    fill, concentration, confidence-interval, and PnL gates still apply.
    """

    if minimum_epochs < 2:
        raise ValueError("minimum_epochs must be at least 2")
    _require_text("condition_id", condition_id)
    _require_text("payout_asset_id", payout_asset_id)
    _require_text("maker_evidence_ref", maker_evidence_ref)
    scoring_rows = tuple(scoring_records)
    payout_rows = tuple(payout_records)
    if any(record.condition_id != condition_id for record in scoring_rows):
        raise ValueError("scoring record violates condition scope")
    if any(
        record.maker_evidence_ref != maker_evidence_ref
        for record in scoring_rows
    ):
        raise ValueError("scoring record violates maker scope")
    if any(record.condition_id != condition_id for record in payout_rows):
        raise ValueError("payout record violates condition scope")
    if any(record.asset_id != payout_asset_id for record in payout_rows):
        raise ValueError("payout record violates reward asset scope")
    if any(
        record.maker_evidence_ref != maker_evidence_ref
        for record in payout_rows
    ):
        raise ValueError("payout record violates maker scope")
    scoring_epochs = {
        record.epoch_id for record in scoring_rows if record.scoring
    }
    payout_epochs = {
        record.epoch_id
        for record in payout_rows
        if record.payment_confirmed and record.recognized_pnl > ZERO
    }
    paired_epochs = scoring_epochs & payout_epochs
    missing: list[str] = []
    if len(scoring_epochs) < minimum_epochs:
        missing.append("multiple_scoring_epochs")
    if len(payout_epochs) < minimum_epochs:
        missing.append("multiple_confirmed_payout_epochs")
    if len(paired_epochs) < minimum_epochs:
        missing.append("multiple_paired_reward_epochs")
    return RewardValidationCeiling(
        classification_ceiling=(
            "promising_not_validated"
            if missing
            else "validated_profitable"
        ),
        scoring_epochs=len(scoring_epochs),
        payout_epochs=len(payout_epochs),
        paired_epochs=len(paired_epochs),
        missing_evidence=tuple(missing),
    )


__all__ = [
    "MINIMUM_ASSUMED_PAYOUT",
    "REWARD_HAIRCUTS",
    "PublicRewardBook",
    "RewardHaircutScenario",
    "RewardLevel",
    "RewardMarketConfig",
    "RewardPayout",
    "RewardScoring",
    "RewardStressClassification",
    "RewardTheoretical",
    "RewardValidationCeiling",
    "apply_reward_haircuts",
    "classify_reward_stress",
    "official_combined_score",
    "reward_validation_ceiling",
    "score_public_reward_book",
]
