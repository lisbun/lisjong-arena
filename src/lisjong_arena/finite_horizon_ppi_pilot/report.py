"""Human-readable projection of the Issue #279 machine-readable result."""

from __future__ import annotations

from pathlib import Path

from lisjong_arena._artifact_io import write_new_artifact_file

from .artifact import validate_result_artifact


def _fmt_interval(value: object) -> str:
    if type(value) is not list or len(value) != 2:
        return "n/a"
    return f"[{float(value[0]):+.6f}, {float(value[1]):+.6f}]"


def render_report(document: object) -> str:
    result = validate_result_artifact(document)
    primary = result["primary_max_budget_analysis"]
    if type(primary) is not dict:
        raise TypeError("primary analysis is invalid")
    reference = primary["reference_only"]
    ppi = primary["ppi"]
    ppi_plus = primary["ppi_plus"]
    diagnostics = primary["diagnostics"]
    efficiency = primary["efficiency"]
    sample_size = primary["sample_size"]
    runtime = result["runtime_compute_metrics"]
    if any(
        type(value) is not dict
        for value in (
            reference,
            ppi,
            ppi_plus,
            diagnostics,
            efficiency,
            sample_size,
            runtime,
        )
    ):
        raise TypeError("result projection is invalid")

    lines = [
        "# Arena Issue #279 — Prediction-powered inference feasibility pilot",
        "",
        f"Protocol fingerprint: {result['protocol']['protocol_identity']}",
        f"Result fingerprint: {result['result_identity']}",
        "",
        "## Locked estimand",
        "",
        (
            "Equal-hanchan-weighted mean H3 local structural-completion-probability "
            "advantage of hand-value-aware over the source two-step action, under "
            "the locked two-step x4 / 4p-red-half source distribution."
        ),
        "",
        "## Maximum labeled budget",
        "",
        f"- labeled hanchans: {sample_size['reference_labeled']}",
        f"- cheap-only hanchans: {sample_size['cheap_only']}",
        f"- reference-only: {float(reference['estimate']):+.6f} {_fmt_interval(reference['ci'])}",
        f"- basic PPI: {float(ppi['estimate']):+.6f} {_fmt_interval(ppi['ci'])}",
        f"- PPI++: {float(ppi_plus['estimate']):+.6f} {_fmt_interval(ppi_plus['ci'])}",
        f"- PPI++ lambda: {float(ppi_plus['lambda']):.6f}",
        f"- cheap-only mean: {float(primary['cheap_only_mean']):+.6f}",
        f"- reference - cheap mean: {float(diagnostics['mean_reference_minus_cheap']):+.6f}",
        (
            "- reference/cheap correlation: "
            + (
                "n/a"
                if diagnostics["correlation_reference_cheap"] is None
                else f"{float(diagnostics['correlation_reference_cheap']):.6f}"
            )
        ),
        (
            "- reference-only CI width / PPI++ CI width: "
            + (
                "n/a"
                if efficiency["reference_only_ci_width_over_ppi_plus"] is None
                else f"{float(efficiency['reference_only_ci_width_over_ppi_plus']):.6f}"
            )
        ),
        "",
        "## Budget curve",
        "",
        "| L hanchans | reference width | PPI width | PPI++ width | lambda |",
        "| ---: | ---: | ---: | ---: | ---: |",
    ]
    budget_curve = result["budget_curve"]
    if type(budget_curve) is not list:
        raise TypeError("budget curve is invalid")
    for row in budget_curve:
        if type(row) is not dict:
            raise TypeError("budget row is invalid")
        ref = row["reference_only"]
        basic = row["ppi"]
        plus = row["ppi_plus"]
        if type(ref) is not dict or type(basic) is not dict or type(plus) is not dict:
            raise TypeError("budget method result is invalid")
        lines.append(
            f"| {row['labeled_budget']} | {float(ref['ci_width']):.6f} | "
            f"{float(basic['ci_width']):.6f} | {float(plus['ci_width']):.6f} | "
            f"{float(plus['lambda']):.6f} |"
        )

    lines += [
        "",
        "## Compute",
        "",
        f"- measured worker-seconds: {float(runtime['total_measured_worker_seconds']):.3f}",
        f"- cheap evaluator seconds: {float(runtime['cheap_evaluation_seconds']):.3f}",
        f"- high-fidelity reference seconds: {float(runtime['reference_evaluation_seconds']):.3f}",
        "",
        "## Interpretation boundaries",
        "",
        "- This is a diagnostic local counterfactual estimand, not candidate-vs-reference match strength.",
        "- The H3 high-fidelity reference measurement is not ground truth.",
        "- Statistical efficiency does not establish evaluator correctness beyond the locked target.",
        "- These results are not formal promotion or holdout evidence.",
        "",
        (
            "Reusable PPI infrastructure is a separate engineering decision; no "
            "efficiency threshold was used as an Issue-completion gate."
        ),
        "",
    ]
    return "\n".join(lines)


def save_report(document: object, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_new_artifact_file(destination, render_report(document))
