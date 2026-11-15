"""Retirement projection and the protected baseline plan."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.database.db import session_scope
from app.database.models import RetirementForecast, RetirementPlan
from app.database.seed import seed_reference_data
from app.services.forecast_service import (
    create_retirement_plan,
    ensure_baseline_retirement_plan,
    project_retirement,
)


def test_projection_compounds_monthly_with_month_end_deposits():
    years = project_retirement(
        opening_value=Decimal("52163.45"), monthly_investment=Decimal("1500"),
        annual_return_rate=Decimal("0.09"), annual_dividend_yield=Decimal("0.03"),
        start_year=2023, end_year=2050,
    )
    assert len(years) == 28
    assert years[0].year == 2023
    assert years[0].predicted_value == Decimal("75589.11")
    assert years[0].forecast_dividend == Decimal("2267.67")
    assert years[1].predicted_value == Decimal("101123.08")
    assert years[-1].year == 2050
    assert years[-1].predicted_value == Decimal("2698518.35")


def test_projection_without_deposits_grows_by_exactly_the_annual_rate():
    years = project_retirement(
        opening_value=Decimal("1000"), monthly_investment=Decimal("0"),
        annual_return_rate=Decimal("0.09"), annual_dividend_yield=Decimal("0.03"),
        start_year=2023, end_year=2024,
    )
    assert [y.predicted_value for y in years] == [Decimal("1090.00"), Decimal("1188.10")]


def test_baseline_is_created_once_by_seed(session_factory):
    with session_scope(session_factory) as session:
        seed_reference_data(session)
    with session_scope(session_factory) as session:
        seed_reference_data(session)
    with session_scope(session_factory) as session:
        plan = session.scalars(select(RetirementPlan)).one()
        assert plan.is_baseline
        assert plan.opening_value == Decimal("52163.45")
        assert [f.year for f in plan.forecasts] == list(range(2023, 2051))


def test_baseline_cannot_be_changed_or_deleted(session_factory):
    with session_scope(session_factory) as session:
        ensure_baseline_retirement_plan(session)
    for change in (
        lambda s: setattr(s.scalars(select(RetirementPlan)).one(), "monthly_investment", Decimal("2000")),
        lambda s: s.delete(s.scalars(select(RetirementPlan)).one()),
        lambda s: setattr(s.scalars(select(RetirementForecast)).first(), "predicted_value", Decimal("1")),
        lambda s: s.delete(s.scalars(select(RetirementForecast)).first()),
    ):
        with pytest.raises((DatabaseError, IntegrityError)):
            with session_scope(session_factory) as session:
                change(session)
                session.flush()
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(RetirementForecast)) == 28


def test_what_if_scenarios_are_free_to_change_and_there_is_one_baseline(session_factory):
    with session_scope(session_factory) as session:
        ensure_baseline_retirement_plan(session)
        scenario = create_retirement_plan(
            session, name="Higher deposits", opening_value=Decimal("52163.45"),
            monthly_investment=Decimal("2000"), annual_return_rate=Decimal("0.09"),
            annual_dividend_yield=Decimal("0.03"), start_year=2023, end_year=2050,
        )
        scenario.annual_return_rate = Decimal("0.07")
    with session_scope(session_factory) as session:
        scenario = session.scalars(select(RetirementPlan).where(RetirementPlan.name == "Higher deposits")).one()
        assert not scenario.is_baseline and scenario.annual_return_rate == Decimal("0.07")
        session.delete(scenario)
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            create_retirement_plan(
                session, name="Second baseline", opening_value=Decimal("1"),
                monthly_investment=Decimal("1"), annual_return_rate=Decimal("0.09"),
                annual_dividend_yield=Decimal("0.03"), start_year=2023, end_year=2024,
                is_baseline=True,
            )
