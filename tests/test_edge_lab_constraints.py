"""Behavioral tests for the auditable event-constraint tracer bullet."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json

import pytest

from src.edge_lab.constraints import (
    BookSnapshotEvidence,
    BundleLeg,
    Constraint,
    ConstraintGraph,
    InventoryTransform,
    PredicateNode,
    enumerate_bundle_opportunities,
)
from src.edge_lab.models import BookLevel, FeeSchedule, OrderBook


def predicate(
    node_id: str,
    *,
    event_id: str = "event-1",
    market_id: str | None = None,
    yes_token_id: str | None = None,
    no_token_id: str | None = None,
    role: str = "named",
    rules: str = "Exactly one listed outcome resolves Yes.",
    resolution_source: str = "https://example.test/rules",
    evidence: tuple[str, ...] = ("gamma:event-1", "rules:snapshot-1"),
    payoff_domain: tuple[Decimal, ...] = (Decimal("0"), Decimal("1")),
) -> PredicateNode:
    return PredicateNode(
        node_id=node_id,
        event_id=event_id,
        market_id=market_id or f"market-{node_id}",
        condition_id=f"condition-{node_id}",
        yes_token_id=yes_token_id or f"yes-{node_id}",
        no_token_id=no_token_id or f"no-{node_id}",
        question=f"Will {node_id} occur?",
        rules=rules,
        resolution_source=resolution_source,
        role=role,
        evidence=evidence,
        payoff_domain=payoff_domain,
    )


def order_book(
    token_id: str,
    *,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    min_order_size: str = "5",
    tick_size: str = "0.01",
    condition_id: str | None = None,
    timestamp_ms: int | None = 1_000,
    neg_risk: bool = False,
) -> OrderBook:
    node_suffix = token_id.split("-", 1)[1] if "-" in token_id else token_id
    return OrderBook(
        token_id=token_id,
        bids=tuple(
            BookLevel(price=Decimal(price), size=Decimal(size))
            for price, size in bids
        ),
        asks=tuple(
            BookLevel(price=Decimal(price), size=Decimal(size))
            for price, size in asks
        ),
        min_order_size=Decimal(min_order_size),
        tick_size=Decimal(tick_size),
        condition_id=condition_id or f"condition-{node_suffix}",
        timestamp_ms=timestamp_ms,
        neg_risk=neg_risk,
    )


def verified_neg_risk_mutex(*node_ids: str) -> Constraint:
    return Constraint(
        constraint_id="adapter-at-most-one",
        kind="mutex",
        node_ids=tuple(node_ids),
        verified=True,
        evidence=("NegRisk adapter mechanism",),
    )


def neg_risk_provenance(*, fee_bips: int = 0) -> dict[str, object]:
    return {
        "adapter_address": "0xadapter",
        "adapter_block_number": 123456,
        "adapter_fee_bips": fee_bips,
        "collateral_decimals": 6,
        "chain_index_evidence": ("polygon:block-123456",),
    }


def snapshot_policy(
    *,
    observed_at_ms: int = 1_050,
    max_book_age_ms: int = 1_000,
    max_book_skew_ms: int = 100,
    max_book_rtt_ms: int = 100,
) -> dict[str, int]:
    return {
        "observed_at_ms": observed_at_ms,
        "max_book_age_ms": max_book_age_ms,
        "max_book_skew_ms": max_book_skew_ms,
        "max_book_rtt_ms": max_book_rtt_ms,
    }


def snapshot_evidence(
    *token_ids: str,
    received_at_ms: int = 1_025,
    rtt_ms: int = 25,
) -> dict[str, BookSnapshotEvidence]:
    return {
        token_id: BookSnapshotEvidence(
            book_hash=sha256(
                f"book:{token_id}".encode("utf-8")
            ).hexdigest(),
            received_at_ms=received_at_ms,
            rtt_ms=rtt_ms,
        )
        for token_id in token_ids
    }


def snapshot_inputs(
    *token_ids: str,
    observed_at_ms: int = 1_050,
    max_book_age_ms: int = 1_000,
    max_book_skew_ms: int = 100,
    max_book_rtt_ms: int = 100,
) -> dict[str, object]:
    return {
        **snapshot_policy(
            observed_at_ms=observed_at_ms,
            max_book_age_ms=max_book_age_ms,
            max_book_skew_ms=max_book_skew_ms,
            max_book_rtt_ms=max_book_rtt_ms,
        ),
        "book_evidence": snapshot_evidence(
            *token_ids,
            received_at_ms=observed_at_ms - 25,
            rtt_ms=25,
        ),
    }


def test_exactly_one_constraint_enumerates_only_verified_payoff_states() -> None:
    nodes = (predicate("a"), predicate("b"), predicate("c"))
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(
            Constraint(
                constraint_id="only-one",
                kind="exactly-one",
                node_ids=("a", "b", "c"),
                verified=True,
                evidence=("event rules paragraph 2",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ()
    assert result.states == (
        {"a": 0, "b": 0, "c": 1},
        {"a": 0, "b": 1, "c": 0},
        {"a": 1, "b": 0, "c": 0},
    )
    assert len(graph.graph_hash) == 64
    assert nodes[0].rules_hash != nodes[0].resolution_source_hash


def test_unverified_semantic_edge_blocks_state_enumeration() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                constraint_id="title-only-guess",
                kind="implication",
                node_ids=("a", "b"),
                verified=False,
                evidence=("market titles only",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("semantic_unverified",)


def test_augmented_neg_risk_graph_is_fail_closed() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(),
        augmented_neg_risk=True,
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("augmented_neg_risk_blocked",)


@pytest.mark.parametrize(
    ("role", "failure_code"),
    (
        ("other", "other_mutable"),
        ("placeholder", "placeholder"),
        ("ambiguous", "semantic_ambiguous"),
        ("unexpected-role", "node_role_unknown"),
    ),
)
def test_other_and_placeholder_nodes_are_fail_closed(
    role: str, failure_code: str
) -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a", role=role),),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == (failure_code,)


def test_rule_or_resolution_snapshot_change_invalidates_expected_graph_hash() -> None:
    original = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )
    changed = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a", rules="The amended rule text resolves this market."),),
    )

    result = changed.enumerate_payoff_states(expected_graph_hash=original.graph_hash)

    assert changed.graph_hash != original.graph_hash
    assert result.states == ()
    assert result.failure_codes == ("graph_hash_changed",)


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("complement", ((0, 1), (1, 0))),
        ("implication", ((0, 0), (0, 1), (1, 1))),
        ("equivalence", ((0, 0), (1, 1))),
        ("mutex", ((0, 0), (0, 1), (1, 0))),
        ("exhaustive", ((0, 1), (1, 0), (1, 1))),
        ("exactly-one", ((0, 1), (1, 0))),
    ),
)
def test_each_verified_relation_has_auditable_binary_states(
    kind: str, expected: tuple[tuple[int, int], ...]
) -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                constraint_id=kind,
                kind=kind,
                node_ids=("a", "b"),
                verified=True,
                evidence=("verified rules clause",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert tuple((state["a"], state["b"]) for state in result.states) == expected


def test_missing_rule_source_or_evidence_blocks_the_graph() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(
            predicate(
                "a",
                rules="",
                resolution_source="",
                evidence=(),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == (
        "rules_missing",
        "resolution_source_missing",
        "node_evidence_missing",
    )


def test_blank_semantic_evidence_is_missing_not_verified() -> None:
    node_without_evidence = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a", evidence=("   ",)),),
    )
    constraint_without_evidence = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "blank-evidence",
                "mutex",
                ("a", "b"),
                verified=True,
                evidence=("\t",),
            ),
        ),
    )

    node_result = node_without_evidence.enumerate_payoff_states()
    constraint_result = constraint_without_evidence.enumerate_payoff_states()

    assert node_result.failure_codes == ("node_evidence_missing",)
    assert constraint_result.failure_codes == (
        "constraint_evidence_missing",
    )


def test_non_binary_resolution_domain_is_explicitly_blocked() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(
            predicate(
                "conditional",
                payoff_domain=(
                    Decimal("0"),
                    Decimal("0.5"),
                    Decimal("1"),
                ),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ("non_binary_payoff_domain",)


@pytest.mark.parametrize(
    ("graph", "failure_code"),
    (
        (
            ConstraintGraph(
                event_id="event-1",
                nodes=(predicate("a"), predicate("a")),
            ),
            "node_id_duplicate",
        ),
        (
            ConstraintGraph(
                event_id="event-2",
                nodes=(predicate("a", event_id="event-1"),),
            ),
            "node_event_mismatch",
        ),
        (
            ConstraintGraph(
                event_id="event-1",
                nodes=(
                    predicate("a", yes_token_id="shared-token"),
                    predicate("b", yes_token_id="shared-token"),
                ),
            ),
            "token_id_duplicate",
        ),
        (
            ConstraintGraph(event_id="event-1", nodes=()),
            "empty_graph",
        ),
    ),
)
def test_graph_identity_is_fail_closed(
    graph: ConstraintGraph, failure_code: str
) -> None:
    result = graph.enumerate_payoff_states()

    assert result.failure_codes == (failure_code,)


def test_verified_constraint_without_its_own_evidence_is_rejected() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                constraint_id="unsupported-assertion",
                kind="mutex",
                node_ids=("a", "b"),
                verified=True,
                evidence=(),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("constraint_evidence_missing",)


def test_neg_risk_graph_requires_explicit_chain_index_mapping() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        neg_risk=True,
        onchain_question_count=2,
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("index_mapping_missing",)


def test_neg_risk_graph_requires_verified_at_most_one_constraint() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map={"market-a": 0, "market-b": 1},
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("neg_risk_mutex_missing",)


def test_neg_risk_graph_requires_adapter_block_fee_and_mapping_evidence() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map={"market-a": 0, "market-b": 1},
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ("adapter_provenance_missing",)


def test_neg_risk_index_mapping_is_independent_of_gamma_market_order() -> None:
    gamma_order = (predicate("c"), predicate("a"), predicate("b"))
    chain_map = {
        "market-a": 1,
        "market-b": 2,
        "market-c": 0,
    }
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=gamma_order,
        constraints=(verified_neg_risk_mutex("a", "b", "c"),),
        neg_risk=True,
        onchain_question_count=3,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )
    same_graph_different_gamma_order = ConstraintGraph(
        event_id="event-1",
        nodes=tuple(sorted(gamma_order, key=lambda node: node.market_id)),
        constraints=(verified_neg_risk_mutex("a", "b", "c"),),
        neg_risk=True,
        onchain_question_count=3,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ()
    assert graph.chain_index_for("market-c") == 0
    assert graph.chain_index_for("market-a") == 1
    assert graph.chain_index_for("market-b") == 2
    assert graph.graph_hash == same_graph_different_gamma_order.graph_hash


def test_neg_risk_mapping_rejects_missing_markets_and_duplicate_indices() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b"), predicate("c")),
        neg_risk=True,
        onchain_question_count=3,
        chain_index_map={"market-a": 0, "market-b": 0},
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == (
        "question_count_mismatch",
        "index_mapping_mismatch",
    )


def test_single_question_neg_risk_has_no_convertible_positions() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
        constraints=(
            Constraint(
                "duplicate-mutex",
                "mutex",
                ("a", "a"),
                verified=True,
                evidence=("malformed fixture",),
            ),
        ),
        neg_risk=True,
        onchain_question_count=1,
        chain_index_map={"market-a": 0},
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ("no_convertible_positions",)


@pytest.mark.parametrize(
    ("overrides", "failure_code"),
    (
        ({"onchain_question_count": 2.0}, "question_count_invalid"),
        (
            {"chain_index_map": {"market-a": False, "market-b": True}},
            "index_mapping_mismatch",
        ),
        ({"adapter_block_number": 1.5}, "adapter_provenance_invalid"),
        (
            {"adapter_block_number": float("nan")},
            "adapter_provenance_invalid",
        ),
        ({"adapter_fee_bips": 1.5}, "adapter_provenance_invalid"),
        ({"collateral_decimals": 6.5}, "adapter_provenance_invalid"),
    ),
)
def test_neg_risk_numeric_provenance_requires_exact_integers(
    overrides: dict[str, object],
    failure_code: str,
) -> None:
    graph_kwargs: dict[str, object] = {
        "event_id": "event-1",
        "nodes": (predicate("a"), predicate("b")),
        "constraints": (verified_neg_risk_mutex("a", "b"),),
        "neg_risk": True,
        "onchain_question_count": 2,
        "chain_index_map": {"market-a": 0, "market-b": 1},
        **neg_risk_provenance(),
        **overrides,
    }
    graph = ConstraintGraph(**graph_kwargs)  # type: ignore[arg-type]

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == (failure_code,)


def test_neg_risk_transform_uses_explicit_chain_indices_and_exact_decimal_fee() -> None:
    nodes = (predicate("c"), predicate("a"), predicate("b"))
    chain_map = {"market-a": 1, "market-b": 2, "market-c": 0}
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(verified_neg_risk_mutex("a", "b", "c"),),
        neg_risk=True,
        onchain_question_count=3,
        chain_index_map=chain_map,
        **neg_risk_provenance(fee_bips=100),
    )
    transform = InventoryTransform.neg_risk_convert(
        transform_id="convert-a-c",
        quantity=Decimal("5"),
        selected_market_ids=("market-a", "market-c"),
        fee_bips=100,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )

    result = transform.evaluate(
        graph,
        inventory={"no-a": Decimal("5"), "no-c": Decimal("5")},
    )

    assert result.failure_codes == ()
    assert result.index_set == 3
    assert result.amount_out == Decimal("4.95")
    assert result.collateral_delta == Decimal("4.95")
    assert result.token_deltas == {
        "no-a": Decimal("-5"),
        "no-c": Decimal("-5"),
        "yes-b": Decimal("4.95"),
    }


def test_inventory_transform_is_invalidated_by_graph_snapshot_change() -> None:
    chain_map = {"market-a": 0, "market-b": 1}
    original = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )
    transform = InventoryTransform.neg_risk_convert(
        transform_id="convert-a",
        quantity=Decimal("5"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=original.graph_hash,
    )
    changed = ConstraintGraph(
        event_id="event-1",
        nodes=(
            predicate("a", rules="Amended event rules."),
            predicate("b"),
        ),
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )

    result = transform.evaluate(
        changed,
        inventory={"no-a": Decimal("5")},
    )

    assert result.failure_codes == ("graph_hash_changed",)


def test_neg_risk_transform_requires_neg_risk_graph_and_input_inventory() -> None:
    nodes = (predicate("a"), predicate("b"))
    chain_map = {"market-a": 0, "market-b": 1}
    non_neg_risk = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        chain_index_map=chain_map,
    )
    valid_neg_risk = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )
    transform = InventoryTransform.neg_risk_convert(
        transform_id="convert-a",
        quantity=Decimal("5"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=valid_neg_risk.graph_hash,
    )
    wrong_graph_transform = InventoryTransform.neg_risk_convert(
        transform_id="convert-a-wrong-graph",
        quantity=Decimal("5"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=non_neg_risk.graph_hash,
    )

    wrong_graph = wrong_graph_transform.evaluate(
        non_neg_risk,
        inventory={"no-a": Decimal("5")},
    )
    missing_inventory = transform.evaluate(
        valid_neg_risk,
        inventory={"no-a": Decimal("4")},
    )

    assert wrong_graph.failure_codes == ("neg_risk_required",)
    assert missing_inventory.failure_codes == ("transform_input_missing",)


def test_neg_risk_fee_uses_six_decimal_base_unit_floor_and_pinned_fee() -> None:
    nodes = (predicate("a"), predicate("b"))
    chain_map = {"market-a": 0, "market-b": 1}
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(fee_bips=1),
    )
    exact_base_unit = InventoryTransform.neg_risk_convert(
        transform_id="one-base-unit",
        quantity=Decimal("0.000001"),
        selected_market_ids=("market-a",),
        fee_bips=1,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )
    stale_fee = InventoryTransform.neg_risk_convert(
        transform_id="stale-fee",
        quantity=Decimal("1"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )
    excess_precision = InventoryTransform.neg_risk_convert(
        transform_id="excess-precision",
        quantity=Decimal("0.0000001"),
        selected_market_ids=("market-a",),
        fee_bips=1,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )

    exact = exact_base_unit.evaluate(
        graph,
        inventory={"no-a": Decimal("0.000001")},
    )
    stale = stale_fee.evaluate(graph, inventory={"no-a": Decimal("1")})
    too_precise = excess_precision.evaluate(
        graph,
        inventory={"no-a": Decimal("0.0000001")},
    )

    assert exact.amount_out == Decimal("0.000001")
    assert stale.failure_codes == ("adapter_fee_mismatch",)
    assert too_precise.failure_codes == ("transform_precision_invalid",)


def test_transform_direct_construction_cannot_bypass_graph_or_decimal_pin() -> None:
    nodes = (predicate("a"), predicate("b"))
    chain_map = {"market-a": 0, "market-b": 1}
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )
    unpinned = InventoryTransform(
        transform_id="direct-unpinned",
        kind="neg_risk_convert",
        quantity=Decimal("1"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=None,
    )
    wrong_decimals = InventoryTransform.neg_risk_convert(
        transform_id="wrong-decimals",
        quantity=Decimal("1"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
        collateral_decimals=18,
    )

    unpinned_result = unpinned.evaluate(
        graph,
        inventory={"no-a": Decimal("1")},
    )
    wrong_decimals_result = wrong_decimals.evaluate(
        graph,
        inventory={"no-a": Decimal("1")},
    )

    assert unpinned_result.failure_codes == ("graph_hash_unpinned",)
    assert wrong_decimals_result.failure_codes == (
        "collateral_decimals_mismatch",
    )


def test_transform_rejects_nonfinite_quantity_and_inventory() -> None:
    nodes = (predicate("a"), predicate("b"))
    chain_map = {"market-a": 0, "market-b": 1}
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=nodes,
        constraints=(verified_neg_risk_mutex("a", "b"),),
        neg_risk=True,
        onchain_question_count=2,
        chain_index_map=chain_map,
        **neg_risk_provenance(),
    )
    invalid_quantity = InventoryTransform.neg_risk_convert(
        transform_id="nan-quantity",
        quantity=Decimal("NaN"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )
    valid_transform = InventoryTransform.neg_risk_convert(
        transform_id="invalid-inventory",
        quantity=Decimal("1"),
        selected_market_ids=("market-a",),
        fee_bips=0,
        chain_index_map=chain_map,
        expected_graph_hash=graph.graph_hash,
    )

    invalid_quantity_result = invalid_quantity.evaluate(
        graph,
        inventory={"no-a": Decimal("1")},
    )
    invalid_inventory_result = valid_transform.evaluate(
        graph,
        inventory={"no-a": Decimal("NaN")},
    )

    assert invalid_quantity_result.failure_codes == (
        "transform_terms_invalid",
    )
    assert invalid_inventory_result.failure_codes == (
        "transform_input_missing",
    )


def test_bundle_enumeration_walks_every_depth_breakpoint_and_fees_each_level() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                constraint_id="one-winner",
                kind="exactly-one",
                node_ids=("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book(
            "yes-a",
            asks=(("0.45", "5"), ("0.50", "5")),
        ),
        "yes-b": order_book("yes-b", asks=(("0.50", "10"),)),
    }

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="buy-exhaustive-yes",
        legs=(
            BundleLeg("a", "YES", "BUY"),
            BundleLeg("b", "YES", "BUY"),
        ),
        books=books,
        fee_schedule=FeeSchedule(
            rate=Decimal("0.04"),
            exponent=Decimal("1"),
        ),
        fee_source="clob-market-info:fixture",
        gas=Decimal("0.01"),
        latency_buffer=Decimal("0.02"),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert tuple(item.quantity for item in opportunities) == (
        Decimal("5"),
        Decimal("10"),
    )
    assert opportunities[0].taker_fees == Decimal("0.09950")
    assert opportunities[0].diagnostic_edge == Decimal("0.12050")
    assert opportunities[0].net_edge == Decimal("0.12048")
    assert opportunities[1].taker_fees == Decimal("0.19950")
    assert opportunities[1].diagnostic_edge == Decimal("0.02050")
    assert opportunities[1].net_edge == Decimal("0.02047")
    assert all(item.failure_code == "accepted" for item in opportunities)
    assert opportunities[1].leg_fills[0].levels == (
        (Decimal("0.45"), Decimal("5")),
        (Decimal("0.50"), Decimal("5")),
    )
    assert opportunities[0].to_record()["event_id"] == "event-1"
    assert opportunities[0].to_record()["constraint_ids"] == ["one-winner"]
    assert (
        opportunities[0].to_record()["fee_source"]
        == "clob-market-info:fixture"
    )
    assert (
        opportunities[0].to_record()["audit_inputs"]["fee_schedules"]["yes-a"][
            "rate"
        ]
        == "0.04"
    )


@pytest.mark.parametrize(
    (
        "units",
        "depth",
        "min_order_size",
        "expected_quantity",
        "expected_edge",
    ),
    (
        ("3", "10", "5", "3.333333", "1.99999980"),
        ("1.5", "5", "1", "3.333332", "0.9999996"),
    ),
)
def test_automatic_breakpoints_round_to_executable_leg_quantity(
    units: str,
    depth: str,
    min_order_size: str,
    expected_quantity: str,
    expected_edge: str,
) -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="rounded-breakpoints",
        legs=(
            BundleLeg("a", "YES", "BUY", units=Decimal(units)),
            BundleLeg("b", "YES", "BUY", units=Decimal(units)),
        ),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.40", depth),),
                min_order_size=min_order_size,
            ),
            "yes-b": order_book(
                "yes-b",
                asks=(("0.40", depth),),
                min_order_size=min_order_size,
            ),
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    by_quantity = {item.quantity: item for item in opportunities}
    expected = Decimal(expected_quantity)
    assert expected in by_quantity
    assert by_quantity[expected].accepted
    assert by_quantity[expected].net_edge == Decimal(expected_edge)


def test_bundle_books_must_match_the_graph_neg_risk_mode() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="mixed-market-mode",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "5"),),
                neg_risk=True,
            ),
            "yes-b": order_book("yes-b", asks=(("0.45", "5"),)),
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.failure_code == "book_neg_risk_mismatch"
    assert opportunity.net_edge is None


def test_automatic_depth_breakpoints_use_the_same_price_order_as_execution() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book(
            "yes-a",
            asks=(("0.95", "100"), ("0.10", "7")),
        ),
        "yes-b": order_book("yes-b", asks=(("0.10", "107"),)),
    }

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="unsorted-depth",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        gas=Decimal("4.5"),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    by_quantity = {item.quantity: item for item in opportunities}
    assert Decimal("7") in by_quantity
    assert by_quantity[Decimal("7")].accepted
    assert by_quantity[Decimal("7")].net_edge == Decimal("1.10")


def test_contradictory_verified_constraints_block_the_graph() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "same",
                "equivalence",
                ("a", "b"),
                verified=True,
                evidence=("rule clause 1",),
            ),
            Constraint(
                "opposite",
                "complement",
                ("a", "b"),
                verified=True,
                evidence=("rule clause 2",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("semantic_contradiction",)


def test_large_unstructured_graph_fails_closed_before_exponential_enumeration() -> None:
    graph = ConstraintGraph(
        event_id="large-event",
        nodes=tuple(
            predicate(f"n-{index}", event_id="large-event")
            for index in range(17)
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == ("state_space_too_large",)


def test_large_verified_neg_risk_mutex_uses_linear_at_most_one_states() -> None:
    node_ids = tuple(f"n-{index}" for index in range(17))
    graph = ConstraintGraph(
        event_id="large-neg-risk",
        nodes=tuple(
            predicate(node_id, event_id="large-neg-risk")
            for node_id in node_ids
        ),
        constraints=(verified_neg_risk_mutex(*node_ids),),
        neg_risk=True,
        onchain_question_count=len(node_ids),
        chain_index_map={
            f"market-{node_id}": index
            for index, node_id in enumerate(node_ids)
        },
        **neg_risk_provenance(),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ()
    assert len(result.states) == 18
    assert all(sum(state.values()) <= 1 for state in result.states)


def test_fed_five_outcome_fixture_records_the_observed_negative_bundle() -> None:
    node_ids = ("cut-50", "cut-25", "hold", "hike-25", "hike-50")
    nodes = tuple(
        predicate(node_id, event_id="fed-july-fixture")
        for node_id in node_ids
    )
    graph = ConstraintGraph(
        event_id="fed-july-fixture",
        nodes=nodes,
        constraints=(
            Constraint(
                constraint_id="fed-exactly-one",
                kind="exactly-one",
                node_ids=node_ids,
                verified=True,
                evidence=("Fed decision event rules fixture",),
            ),
        ),
    )
    ask_prices = ("0.120", "0.140", "0.460", "0.150", "0.144")
    books = {
        node.yes_token_id: order_book(
            node.yes_token_id,
            asks=((price, "5"),),
            tick_size="0.001",
        )
        for node, price in zip(nodes, ask_prices, strict=True)
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="fed-buy-all-yes",
        legs=tuple(BundleLeg(node_id, "YES", "BUY") for node_id in node_ids),
        books=books,
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="clob-fee-fixture:confirmed-zero",
        quantities=(Decimal("5"),),
        gas=Decimal("0.01"),
        latency_buffer=Decimal("0.02"),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs(*(node.yes_token_id for node in nodes)),
    )

    assert opportunity.gross_cash_flow == Decimal("-5.070")
    assert opportunity.worst_case_payoff == Decimal("5")
    assert opportunity.net_edge == Decimal("-0.100")
    assert not opportunity.accepted
    assert opportunity.failure_code == "edge_nonpositive"
    assert opportunity.to_record()["net_edge"] == "-0.100"


def test_semantically_blocked_candidates_are_retained_with_failure_codes() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                constraint_id="title-inference",
                kind="implication",
                node_ids=("a", "b"),
                verified=False,
                evidence=("titles only",),
            ),
        ),
    )
    books = {
        "yes-a": order_book("yes-a", asks=(("0.40", "10"),)),
        "yes-b": order_book("yes-b", asks=(("0.40", "10"),)),
    }

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="blocked-title-inference",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"), Decimal("10")),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert len(opportunities) == 2
    assert tuple(item.quantity for item in opportunities) == (
        Decimal("5"),
        Decimal("10"),
    )
    assert all(item.failure_code == "semantic_unverified" for item in opportunities)
    assert all(not item.accepted for item in opportunities)
    assert all(item.net_edge is None for item in opportunities)


def test_sell_bundle_enforces_minimum_size_and_preheld_inventory() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book("yes-a", bids=(("0.55", "10"),)),
        "yes-b": order_book("yes-b", bids=(("0.55", "10"),)),
    }
    kwargs = {
        "graph": graph,
        "family": "sell-all-yes",
        "legs": (BundleLeg("a", "YES", "SELL"), BundleLeg("b", "YES", "SELL")),
        "books": books,
        "fee_schedule": FeeSchedule(rate=Decimal("0")),
        "fee_source": "fixture",
        "expected_graph_hash": graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    }

    rejected = enumerate_bundle_opportunities(
        **kwargs,
        quantities=(Decimal("4"), Decimal("5")),
    )
    (inventory_backed,) = enumerate_bundle_opportunities(
        **kwargs,
        quantities=(Decimal("5"),),
        inventory={"yes-a": Decimal("5"), "yes-b": Decimal("5")},
    )
    inventory_breakpoints = enumerate_bundle_opportunities(
        **kwargs,
        inventory={"yes-a": Decimal("7"), "yes-b": Decimal("7")},
    )

    assert tuple(item.failure_code for item in rejected) == (
        "below_min_size",
        "naked_sell",
    )
    assert inventory_backed.accepted
    assert inventory_backed.failure_code == "accepted"
    assert inventory_backed.net_edge == Decimal("0.50")
    assert rejected[1].opportunity_id != inventory_backed.opportunity_id
    assert inventory_backed.to_record()["audit_inputs"]["inventory"] == {
        "yes-a": "5",
        "yes-b": "5",
    }
    assert tuple(item.quantity for item in inventory_breakpoints) == (
        Decimal("5"),
        Decimal("7"),
        Decimal("10"),
    )
    assert tuple(item.failure_code for item in inventory_breakpoints) == (
        "accepted",
        "accepted",
        "naked_sell",
    )


@pytest.mark.parametrize(
    ("constraint", "failure_code"),
    (
        (
            Constraint(
                "unknown-node",
                "implication",
                ("a", "missing"),
                verified=True,
                evidence=("rules",),
            ),
            "constraint_node_unknown",
        ),
        (
            Constraint(
                "unknown-kind",
                "correlated",
                ("a", "b"),
                verified=True,
                evidence=("rules",),
            ),
            "constraint_kind_unsupported",
        ),
        (
            Constraint(
                "bad-arity",
                "implication",
                ("a", "b", "c"),
                verified=True,
                evidence=("rules",),
            ),
            "constraint_arity_invalid",
        ),
    ),
)
def test_malformed_constraints_fail_closed_without_state_solver_exceptions(
    constraint: Constraint, failure_code: str
) -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b"), predicate("c")),
        constraints=(constraint,),
    )

    result = graph.enumerate_payoff_states()

    assert result.states == ()
    assert result.failure_codes == (failure_code,)


def test_constraint_cannot_repeat_the_same_predicate() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "duplicate-node",
                "mutex",
                ("a", "a"),
                verified=True,
                evidence=("rules",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ("constraint_node_duplicate",)


def test_constraint_ids_are_unique_audit_keys() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "same-id",
                "mutex",
                ("a", "b"),
                verified=True,
                evidence=("rule one",),
            ),
            Constraint(
                "same-id",
                "exhaustive",
                ("a", "b"),
                verified=True,
                evidence=("rule two",),
            ),
        ),
    )

    result = graph.enumerate_payoff_states()

    assert result.failure_codes == ("constraint_id_duplicate",)


def test_implication_bundle_uses_no_a_plus_yes_b_worst_case_payoff() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "a-implies-b",
                "implication",
                ("a", "b"),
                verified=True,
                evidence=("verified implication rule",),
            ),
        ),
    )
    books = {
        "no-a": order_book("no-a", asks=(("0.45", "5"),)),
        "yes-b": order_book("yes-b", asks=(("0.45", "5"),)),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="implication-cover",
        legs=(BundleLeg("a", "NO", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("no-a", "yes-b"),
    )

    assert opportunity.worst_case_payoff == Decimal("5")
    assert opportunity.gross_cash_flow == Decimal("-4.50")
    assert opportunity.net_edge == Decimal("0.50")
    assert opportunity.accepted


def test_unknown_fee_and_incomplete_depth_are_both_preserved_on_rejection() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book("yes-a", asks=(("0.40", "4"),)),
        "yes-b": order_book("yes-b", asks=(("0.50", "5"),)),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="incomplete",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=None,
        fee_source=None,
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.failure_code == "fee_unknown"
    assert opportunity.failure_codes == ("fee_unknown", "depth_insufficient")
    assert opportunity.leg_fills[0].filled_size == Decimal("4")
    assert opportunity.net_edge is None


def test_bundle_supports_token_specific_fee_schedules() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book("yes-a", asks=(("0.45", "5"),)),
        "yes-b": order_book("yes-b", asks=(("0.45", "5"),)),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="per-token-fees",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule={
            "yes-a": FeeSchedule(rate=Decimal("0")),
            "yes-b": FeeSchedule(
                rate=Decimal("0.04"),
                exponent=Decimal("1"),
            ),
        },
        fee_source={
            "yes-a": "clob:a",
            "yes-b": "clob:b",
        },
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.taker_fees == Decimal("0.04950")
    assert opportunity.diagnostic_edge == Decimal("0.45050")
    assert opportunity.net_edge == Decimal("0.45049")
    assert (
        opportunity.to_record()["audit_inputs"]["fee_schedules"]["yes-b"][
            "rate"
        ]
        == "0.04"
    )


def test_invalid_fee_terms_fail_closed_instead_of_becoming_zero_fee() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book("yes-a", asks=(("0.45", "5"),)),
        "yes-b": order_book("yes-b", asks=(("0.45", "5"),)),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="invalid-fee",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(
            rate=Decimal("-0.04"),
            exponent=Decimal("1"),
        ),
        fee_source="invalid-fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.failure_code == "fee_terms_invalid"
    assert opportunity.net_edge is None


def test_subquantum_fee_uses_documented_zero_but_rounding_buffer_blocks_dust() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book(
            "yes-a",
            asks=(("0.49", "0.000600"),),
            min_order_size="0.000001",
        ),
        "yes-b": order_book(
            "yes-b",
            asks=(("0.49", "0.000600"),),
            min_order_size="0.000001",
        ),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="subquantum-rounding",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(
            rate=Decimal("0.04"),
            exponent=Decimal("1"),
        ),
        fee_source="clob:fixture",
        quantities=(Decimal("0.000600"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.taker_fees == Decimal("0")
    assert opportunity.diagnostic_edge == Decimal("0.00001200")
    assert opportunity.rounding_uncertainty == Decimal("0.00002")
    assert opportunity.failure_code == "edge_below_rounding_uncertainty"
    assert opportunity.net_edge is None


def test_unenumerated_fee_jump_cannot_be_the_only_accepted_edge() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )
    books = {
        "yes-a": order_book(
            "yes-a",
            asks=(("0.49", "1.253002"),),
            min_order_size="0.000001",
        ),
        "yes-b": order_book(
            "yes-b",
            asks=(("0.49", "1.253002"),),
            min_order_size="0.000001",
        ),
    }

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="fee-jump-coverage",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books=books,
        fee_schedule=FeeSchedule(
            rate=Decimal("0.04"),
            exponent=Decimal("1"),
        ),
        fee_source="clob:fixture",
        quantities=(Decimal("1.253001"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a", "yes-b"),
    )

    assert opportunity.diagnostic_edge == Decimal("0.00002002")
    assert opportunity.rounding_uncertainty == Decimal("0.00002")
    assert opportunity.failure_code == "edge_below_rounding_uncertainty"
    assert opportunity.net_edge is None


def test_opportunity_id_changes_when_executable_depth_changes() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
        constraints=(),
    )
    kwargs = {
        "graph": graph,
        "family": "single-leg-audit-id",
        "legs": (BundleLeg("a", "YES", "BUY"),),
        "fee_schedule": FeeSchedule(rate=Decimal("0")),
        "fee_source": "fixture",
        "quantities": (Decimal("5"),),
        "expected_graph_hash": graph.graph_hash,
        **snapshot_inputs("yes-a"),
    }

    (first,) = enumerate_bundle_opportunities(
        **kwargs,
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
    )
    (second,) = enumerate_bundle_opportunities(
        **kwargs,
        books={"yes-a": order_book("yes-a", asks=(("0.46", "5"),))},
    )

    assert first.opportunity_id != second.opportunity_id


def test_opportunity_id_normalizes_equivalent_decimal_representations() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )
    kwargs = {
        "graph": graph,
        "family": "canonical-decimal-id",
        "legs": (BundleLeg("a", "YES", "BUY"),),
        "books": {"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        "fee_schedule": FeeSchedule(rate=Decimal("0")),
        "fee_source": "fixture",
        "expected_graph_hash": graph.graph_hash,
        **snapshot_inputs("yes-a"),
    }

    (integer_form,) = enumerate_bundle_opportunities(
        **kwargs,
        quantities=(Decimal("5"),),
    )
    (scaled_form,) = enumerate_bundle_opportunities(
        **kwargs,
        quantities=(Decimal("5.0"),),
    )

    assert integer_form.opportunity_id == scaled_form.opportunity_id


def test_fee_source_is_snapshotted_and_record_remains_json_safe() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )
    mutable_source = {"yes-a": "clob:snapshot-1"}

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="immutable-fee-source",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule={"yes-a": FeeSchedule(rate=Decimal("0"))},
        fee_source=mutable_source,
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )
    mutable_source["yes-a"] = "tampered-after-enumeration"

    record = opportunity.to_record()

    assert record["fee_source"] == {"yes-a": "clob:snapshot-1"}
    json.dumps(record, allow_nan=False)


def test_opportunity_snapshot_hash_change_is_retained_as_rejection() -> None:
    original = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )
    changed = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a", resolution_source="https://amended.test/rules"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=changed,
        family="stale-graph",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=original.graph_hash,
        **snapshot_inputs("yes-a"),
    )
    (current_snapshot,) = enumerate_bundle_opportunities(
        graph=changed,
        family="stale-graph",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=changed.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "graph_hash_changed"
    assert not opportunity.accepted
    assert opportunity.opportunity_id != current_snapshot.opportunity_id
    assert (
        opportunity.to_record()["audit_inputs"]["expected_graph_hash"]
        == original.graph_hash
    )


def test_bundle_rejects_book_from_a_different_condition() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="wrong-condition",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "5"),),
                condition_id="condition-b",
            )
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "book_condition_mismatch"
    assert not opportunity.accepted


def test_cross_market_books_must_be_fresh_and_synchronized() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"), predicate("b")),
        constraints=(
            Constraint(
                "one-winner",
                "exactly-one",
                ("a", "b"),
                verified=True,
                evidence=("event rules",),
            ),
        ),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="unsynchronized-books",
        legs=(BundleLeg("a", "YES", "BUY"), BundleLeg("b", "YES", "BUY")),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "5"),),
                timestamp_ms=1_000,
            ),
            "yes-b": order_book(
                "yes-b",
                asks=(("0.45", "5"),),
                timestamp_ms=1_300,
            ),
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        book_evidence={
            **snapshot_evidence(
                "yes-a",
                received_at_ms=1_350,
                rtt_ms=25,
            ),
            **snapshot_evidence(
                "yes-b",
                received_at_ms=1_360,
                rtt_ms=25,
            ),
        },
        **snapshot_policy(
            observed_at_ms=1_400,
            max_book_age_ms=1_000,
            max_book_skew_ms=100,
        ),
    )

    assert opportunity.failure_code == "stale_or_unsynced_books"
    assert opportunity.net_edge is None


@pytest.mark.parametrize(
    "evidence",
    (
        {},
        {
            "yes-a": BookSnapshotEvidence(
                book_hash="not-a-sha256",
                received_at_ms=1_025,
                rtt_ms=25,
            )
        },
        snapshot_evidence("yes-a", received_at_ms=1_025, rtt_ms=101),
        snapshot_evidence("yes-a", received_at_ms=999, rtt_ms=25),
    ),
)
def test_book_requires_local_receipt_and_bounded_rtt(
    evidence: dict[str, BookSnapshotEvidence],
) -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="untrusted-snapshot",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "5"),),
                timestamp_ms=1_000,
            )
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        book_evidence=evidence,
        **snapshot_policy(observed_at_ms=1_050, max_book_rtt_ms=100),
    )

    assert opportunity.failure_code == "stale_or_unsynced_books"
    assert opportunity.net_edge is None


def test_nonfinite_snapshot_policy_is_rejected_and_serialized_as_valid_json() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="invalid-snapshot-policy",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "5"),),
                timestamp_ms=0,
            )
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        observed_at_ms=float("nan"),  # type: ignore[arg-type]
        max_book_age_ms=1_000,
        max_book_skew_ms=100,
        max_book_rtt_ms=100,
        book_evidence=snapshot_evidence(
            "yes-a",
            received_at_ms=0,
            rtt_ms=25,
        ),
    )

    assert opportunity.failure_code == "snapshot_policy_invalid"
    assert opportunity.net_edge is None
    assert ":NaN" not in opportunity.audit_inputs_json


def test_bundle_cannot_consume_the_same_token_depth_twice() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="duplicate-token",
        legs=(
            BundleLeg("a", "YES", "BUY"),
            BundleLeg("a", "YES", "BUY"),
        ),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "duplicate_bundle_token"
    assert sum(fill.filled_size for fill in opportunity.leg_fills) == Decimal("5")


def test_nonfinite_inventory_fails_closed_during_automatic_enumeration() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="invalid-inventory",
        legs=(BundleLeg("a", "YES", "SELL"),),
        books={"yes-a": order_book("yes-a", bids=(("0.55", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        inventory={"yes-a": Decimal("NaN")},
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "inventory_invalid"
    assert opportunity.net_edge is None


def test_nonpositive_depth_size_is_rejected_not_silently_skipped() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="invalid-depth",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "-1"), ("0.50", "5")),
            )
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("5"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "book_level_invalid"
    assert opportunity.net_edge is None


def test_order_share_quantity_must_fit_six_decimal_base_units() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    (opportunity,) = enumerate_bundle_opportunities(
        graph=graph,
        family="invalid-share-precision",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={
            "yes-a": order_book(
                "yes-a",
                asks=(("0.45", "0.0000001"),),
                min_order_size="0.0000001",
            )
        },
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("0.0000001"),),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert opportunity.failure_code == "quantity_precision_invalid"
    assert opportunity.net_edge is None


def test_explicit_nonfinite_quantity_does_not_break_candidate_sorting() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="nonfinite-candidate",
        legs=(BundleLeg("a", "YES", "BUY"),),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        quantities=(Decimal("NaN"), Decimal("5")),
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert len(opportunities) == 2
    assert any(
        item.failure_code == "quantity_invalid"
        and item.quantity.is_nan()
        for item in opportunities
    )


def test_tiny_leg_units_do_not_crash_automatic_breakpoint_generation() -> None:
    graph = ConstraintGraph(
        event_id="event-1",
        nodes=(predicate("a"),),
    )

    opportunities = enumerate_bundle_opportunities(
        graph=graph,
        family="tiny-units",
        legs=(
            BundleLeg(
                "a",
                "YES",
                "BUY",
                units=Decimal("1e-30"),
            ),
        ),
        books={"yes-a": order_book("yes-a", asks=(("0.45", "5"),))},
        fee_schedule=FeeSchedule(rate=Decimal("0")),
        fee_source="fixture",
        expected_graph_hash=graph.graph_hash,
        **snapshot_inputs("yes-a"),
    )

    assert tuple(item.quantity for item in opportunities) == (
        Decimal("5e30"),
    )
