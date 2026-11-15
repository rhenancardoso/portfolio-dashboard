"""Retirement projections: the fixed baseline plan and what-if scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import RetirementForecast, RetirementPlan

CENT = Decimal("0.01")

# The baseline assumptions are the "truth"; changing them means a new baseline.
BASELINE_NAME = "Baseline"
BASELINE_START_YEAR = 2023
BASELINE_END_YEAR = 2050
BASELINE_OPENING_VALUE = Decimal("52163.45")
BASELINE_MONTHLY_INVESTMENT = Decimal("1500")
BASELINE_ANNUAL_RETURN_RATE = Decimal("0.09")  # including dividends
BASELINE_ANNUAL_DIVIDEND_YIELD = Decimal("0.03")


@dataclass(frozen=True)
class ProjectedYear:
    year: int
    predicted_value: Decimal
    forecast_dividend: Decimal


def project_retirement(
    *,
    opening_value: Decimal,
    monthly_investment: Decimal,
    annual_return_rate: Decimal,
    annual_dividend_yield: Decimal,
    start_year: int,
    end_year: int,
) -> list[ProjectedYear]:
    """Project the 31 Dec portfolio value and dividend for each year.

    Compounds monthly at the rate that gives ``annual_return_rate`` over a year, with
    the monthly investment added at the end of each month. Full precision is carried
    between months; only the results are rounded to cents.
    """
    monthly_rate = (1 + annual_return_rate) ** (Decimal(1) / 12) - 1
    value = opening_value
    years = []
    for year in range(start_year, end_year + 1):
        for _ in range(12):
            value = value * (1 + monthly_rate) + monthly_investment
        years.append(
            ProjectedYear(
                year=year,
                predicted_value=value.quantize(CENT, ROUND_HALF_UP),
                forecast_dividend=(value * annual_dividend_yield).quantize(CENT, ROUND_HALF_UP),
            )
        )
    return years


def create_retirement_plan(
    session: Session,
    *,
    name: str,
    opening_value: Decimal,
    monthly_investment: Decimal,
    annual_return_rate: Decimal,
    annual_dividend_yield: Decimal,
    start_year: int,
    end_year: int,
    is_baseline: bool = False,
) -> RetirementPlan:
    """Store a plan and its yearly projection. The caller commits."""
    plan = RetirementPlan(
        name=name,
        is_baseline=is_baseline,
        start_year=start_year,
        end_year=end_year,
        opening_value=opening_value,
        monthly_investment=monthly_investment,
        annual_return_rate=annual_return_rate,
        annual_dividend_yield=annual_dividend_yield,
    )
    plan.forecasts = [
        RetirementForecast(
            year=item.year,
            predicted_value=item.predicted_value,
            forecast_dividend=item.forecast_dividend,
        )
        for item in project_retirement(
            opening_value=opening_value,
            monthly_investment=monthly_investment,
            annual_return_rate=annual_return_rate,
            annual_dividend_yield=annual_dividend_yield,
            start_year=start_year,
            end_year=end_year,
        )
    ]
    session.add(plan)
    session.flush()
    return plan


def ensure_baseline_retirement_plan(session: Session) -> RetirementPlan:
    """Create the baseline plan if it does not exist yet. Never modifies an existing one."""
    baseline = session.scalars(
        select(RetirementPlan).where(RetirementPlan.is_baseline.is_(True))
    ).one_or_none()
    if baseline is not None:
        return baseline
    return create_retirement_plan(
        session,
        name=BASELINE_NAME,
        opening_value=BASELINE_OPENING_VALUE,
        monthly_investment=BASELINE_MONTHLY_INVESTMENT,
        annual_return_rate=BASELINE_ANNUAL_RETURN_RATE,
        annual_dividend_yield=BASELINE_ANNUAL_DIVIDEND_YIELD,
        start_year=BASELINE_START_YEAR,
        end_year=BASELINE_END_YEAR,
        is_baseline=True,
    )
