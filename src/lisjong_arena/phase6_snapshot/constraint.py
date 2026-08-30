"""Differentiable physical allocation constraint for Phase 6."""

from dataclasses import dataclass

MAX_ITERATIONS = 64
RESIDUAL_TOLERANCE = 1e-6


class ConstraintConvergenceError(RuntimeError):
    """The locked balancing procedure did not meet its residual contract."""


@dataclass(frozen=True, slots=True)
class ConstrainedAllocation:
    allocation: object
    maximum_residual: float
    iterations: int


def constrain_allocation(
    logits,
    row_marginals,
    column_marginals,
    *,
    max_iterations: int = MAX_ITERATIONS,
    residual_tolerance: float = RESIDUAL_TOLERANCE,
) -> ConstrainedAllocation:
    """Balance positive latent scores to exact row/column marginals in log space."""
    import torch

    if type(max_iterations) is not int or max_iterations <= 0:
        raise ValueError("max_iterations must be a positive int")
    if not isinstance(residual_tolerance, (int, float)) or residual_tolerance < 0:
        raise ValueError("residual_tolerance must be non-negative")
    if logits.ndim < 2 or logits.shape[-2:] != (4, 34):
        raise ValueError("logits must end in shape (4, 34)")
    if row_marginals.shape != logits.shape[:-2] + (4,):
        raise ValueError("row marginals must match logits batch axes and four rows")
    if column_marginals.shape != logits.shape[:-2] + (34,):
        raise ValueError("column marginals must match logits batch axes and 34 columns")
    work = logits.to(dtype=torch.float64)
    rows = row_marginals.to(device=logits.device, dtype=torch.float64)
    columns = column_marginals.to(device=logits.device, dtype=torch.float64)
    if not bool(torch.isfinite(work).all()):
        raise ValueError("logits must be finite")
    if not bool(torch.isfinite(rows).all()) or not bool(torch.isfinite(columns).all()):
        raise ValueError("marginals must be finite")
    if bool((rows < 0).any()) or bool((columns < 0).any()):
        raise ValueError("marginals must be non-negative")
    row_total = rows.sum(dim=-1)
    column_total = columns.sum(dim=-1)
    if not bool(torch.isclose(row_total, column_total, atol=1e-9, rtol=0).all()):
        raise ValueError("row and column marginal total mass must match")

    active = (rows > 0).unsqueeze(-1) & (columns > 0).unsqueeze(-2)
    log_values = torch.where(active, work, torch.full_like(work, -torch.inf))
    maximum_residual = float("inf")
    for iteration in range(1, max_iterations + 1):
        row_sums = torch.logsumexp(log_values, dim=-1)
        row_targets = torch.where(rows > 0, torch.log(rows), torch.zeros_like(rows))
        row_delta = torch.where(
            rows > 0, row_targets - row_sums, torch.zeros_like(rows)
        )
        log_values = torch.where(
            active, log_values + row_delta.unsqueeze(-1), log_values
        )

        column_sums = torch.logsumexp(log_values, dim=-2)
        column_targets = torch.where(
            columns > 0, torch.log(columns), torch.zeros_like(columns)
        )
        column_delta = torch.where(
            columns > 0,
            column_targets - column_sums,
            torch.zeros_like(columns),
        )
        log_values = torch.where(
            active, log_values + column_delta.unsqueeze(-2), log_values
        )
        allocation = torch.where(active, torch.exp(log_values), torch.zeros_like(work))
        row_residual = (allocation.sum(dim=-1) - rows).abs().amax()
        column_residual = (allocation.sum(dim=-2) - columns).abs().amax()
        maximum_residual = float(torch.maximum(row_residual, column_residual).detach())
        if maximum_residual <= residual_tolerance:
            return ConstrainedAllocation(allocation, maximum_residual, iteration)
    raise ConstraintConvergenceError(
        "allocation did not converge within "
        f"{max_iterations} iterations; maximum residual={maximum_residual:.12g}"
    )


__all__ = [
    "MAX_ITERATIONS",
    "RESIDUAL_TOLERANCE",
    "ConstrainedAllocation",
    "ConstraintConvergenceError",
    "constrain_allocation",
]
