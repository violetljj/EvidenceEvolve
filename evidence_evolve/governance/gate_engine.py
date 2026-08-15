from __future__ import annotations

from evidence_evolve.models import (
    ArchiveClass,
    ConstraintCheck,
    EvaluationInput,
    GateDecision,
    GateVerdict,
    MechanicsStatus,
    ResearchContract,
    ScientificOutcome,
)


class GateEngine:
    """Pure deterministic gate evaluation. No aggregate score is accepted."""

    def __init__(self, contract: ResearchContract):
        self.contract = contract

    def _constraints(self, evaluation: EvaluationInput) -> dict[str, ConstraintCheck]:
        checks: dict[str, ConstraintCheck] = {}
        for metric, constraint in self.contract.metrics.hard_constraints.items():
            value = evaluation.metrics.get(metric)
            if value is None:
                checks[metric] = ConstraintCheck(
                    metric=metric,
                    value=None,
                    min=constraint.min,
                    max=constraint.max,
                    passed=False,
                    reason="MISSING_HARD_CONSTRAINT_METRIC",
                )
                continue
            passed = True
            if constraint.min is not None and value < constraint.min:
                passed = False
            if constraint.max is not None and value > constraint.max:
                passed = False
            checks[metric] = ConstraintCheck(
                metric=metric,
                value=value,
                min=constraint.min,
                max=constraint.max,
                passed=passed,
                reason="PASS" if passed else "HARD_CONSTRAINT_VIOLATION",
            )
        return checks

    def evaluate(self, evaluation: EvaluationInput) -> GateVerdict:
        constraints = self._constraints(evaluation)
        required_controls = set(self.contract.required_controls)
        controls_complete = all(
            evaluation.controls.get(control) is True for control in required_controls
        )
        protocol_valid = not evaluation.protocol_violations and not evaluation.data_leakage

        if not protocol_valid:
            reasons = list(evaluation.protocol_violations)
            if evaluation.data_leakage:
                reasons.append("DATA_LEAKAGE")
            return GateVerdict(
                decision=GateDecision.INVALID_PROTOCOL_TAMPERING,
                archive_class=ArchiveClass.INVALID,
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reasons=sorted(set(reasons)),
                constraint_checks=constraints,
                controls_complete=controls_complete,
                protocol_valid=False,
            )

        if evaluation.mechanics_status is MechanicsStatus.FAIL:
            return GateVerdict(
                decision=GateDecision.REPAIR_IMPLEMENTATION,
                archive_class=ArchiveClass.INVALID,
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reasons=["MECHANICS_FAILED"],
                constraint_checks=constraints,
                controls_complete=controls_complete,
                protocol_valid=True,
            )

        if (
            evaluation.stage.value != "P0_PROTOCOL_LOCK"
            and evaluation.mechanics_status is MechanicsStatus.NOT_RUN
        ):
            return GateVerdict(
                decision=GateDecision.REPAIR_IMPLEMENTATION,
                archive_class=ArchiveClass.INVALID,
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reasons=["MECHANICS_NOT_RUN"],
                constraint_checks=constraints,
                controls_complete=controls_complete,
                protocol_valid=True,
            )

        if not controls_complete:
            missing = sorted(
                control
                for control in required_controls
                if evaluation.controls.get(control) is not True
            )
            return GateVerdict(
                decision=GateDecision.REPAIR_IMPLEMENTATION,
                archive_class=ArchiveClass.INVALID,
                scientific_outcome=ScientificOutcome.INVALID_MECHANICS_OR_ADAPTER,
                reasons=[f"REQUIRED_CONTROLS_INCOMPLETE:{','.join(missing)}"],
                constraint_checks=constraints,
                controls_complete=False,
                protocol_valid=True,
            )

        if not evaluation.data_eligible or (
            evaluation.scientific_outcome is ScientificOutcome.NOT_EVALUABLE_DATA
        ):
            reasons = evaluation.data_ineligibility_reasons or ["ELIGIBLE_DATA_MISSING"]
            return GateVerdict(
                decision=GateDecision.PAUSE_NOT_EVALUABLE,
                archive_class=ArchiveClass.PAUSED_NOT_EVALUABLE,
                scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
                reasons=sorted(set(reasons)),
                constraint_checks=constraints,
                controls_complete=True,
                protocol_valid=True,
            )

        missing_metrics = [
            metric for metric, check in constraints.items() if check.value is None
        ]
        if missing_metrics:
            return GateVerdict(
                decision=GateDecision.PAUSE_NOT_EVALUABLE,
                archive_class=ArchiveClass.PAUSED_NOT_EVALUABLE,
                scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
                reasons=[f"MISSING_HARD_METRICS:{','.join(sorted(missing_metrics))}"],
                constraint_checks=constraints,
                controls_complete=True,
                protocol_valid=True,
            )

        failed_constraints = sorted(
            metric for metric, check in constraints.items() if not check.passed
        )
        if failed_constraints:
            return GateVerdict(
                decision=GateDecision.KILL,
                archive_class=ArchiveClass.VALID_NEGATIVE,
                scientific_outcome=ScientificOutcome.VALID_NEGATIVE,
                reasons=[f"HARD_CONSTRAINTS_FAILED:{','.join(failed_constraints)}"],
                constraint_checks=constraints,
                controls_complete=True,
                protocol_valid=True,
            )

        if evaluation.scientific_outcome is ScientificOutcome.POSITIVE_HEADROOM:
            return GateVerdict(
                decision=GateDecision.ADMIT,
                archive_class=ArchiveClass.ELITE,
                scientific_outcome=ScientificOutcome.POSITIVE_HEADROOM,
                reasons=["POSITIVE_OUTCOME_AND_ALL_HARD_GATES_PASS"],
                constraint_checks=constraints,
                controls_complete=True,
                protocol_valid=True,
            )

        if evaluation.scientific_outcome is ScientificOutcome.VALID_NEGATIVE:
            return GateVerdict(
                decision=GateDecision.KILL,
                archive_class=ArchiveClass.VALID_NEGATIVE,
                scientific_outcome=ScientificOutcome.VALID_NEGATIVE,
                reasons=["VALID_NEGATIVE_REPORTED_BY_FROZEN_EVALUATOR"],
                constraint_checks=constraints,
                controls_complete=True,
                protocol_valid=True,
            )

        return GateVerdict(
            decision=GateDecision.PAUSE_NOT_EVALUABLE,
            archive_class=ArchiveClass.PAUSED_NOT_EVALUABLE,
            scientific_outcome=ScientificOutcome.NOT_EVALUABLE_DATA,
            reasons=["SCIENTIFIC_OUTCOME_MISSING"],
            constraint_checks=constraints,
            controls_complete=True,
            protocol_valid=True,
        )
