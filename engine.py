"""Motor de proyección: capa 4 (base) → capas 3, 2, 1 → simulación Monte Carlo."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

import numpy as np

from ..core.enums import Sport
from ..core.stats import MatchContext, TeamData
from ..core.timeutil import to_utc
from .layers.common import InsufficientDataError, LayerOutput, clip
from .layers.l1_lineup import lineup_layer
from .layers.l2_venue_weather import venue_weather_layer
from .layers.l3_form_fatigue import form_fatigue_layer
from .layers.l4_base_stats import base_rates
from .params import PARAMS, SportParams
from .results import LayerReport, ProjectionResult, clamp_simulations
from .sims.baseball import simulate_baseball
from .sims.basketball import simulate_basketball
from .sims.common import SimSummary
from .sims.football import simulate_football

__all__ = ["ProjectionEngine", "ProjectionError", "LeakageError", "InsufficientDataError"]

SIMULATORS: dict[Sport, Callable[..., SimSummary]] = {
    Sport.FOOTBALL: simulate_football,
    Sport.BASKETBALL: simulate_basketball,
    Sport.BASEBALL: simulate_baseball,
}
# Peso de cada capa en la cobertura global (la base manda; lineup solo informa si hay datos).
COVERAGE_WEIGHTS = {"L4 base": 0.4, "L3 forma/fatiga": 0.2, "L2 sede/clima": 0.2, "L1 alineación": 0.2}


class ProjectionError(RuntimeError):
    pass


class LeakageError(ProjectionError):
    """Se intentó proyectar con información de t ≥ T."""


def _strip_future(team: TeamData, cut: datetime) -> TeamData:
    kept = tuple(g for g in team.history if g.played_at < cut)
    return team if len(kept) == len(team.history) else dataclasses.replace(team, history=kept)


class ProjectionEngine:
    def __init__(self, params: Mapping[Sport, SportParams] | None = None) -> None:
        self.params = params or PARAMS

    def project(
        self,
        ctx: MatchContext,
        n_simulations: int,
        *,
        seed: int | None = None,
        now: datetime | None = None,
    ) -> ProjectionResult:
        match = ctx.match
        n = clamp_simulations(n_simulations)
        asof = to_utc(ctx.asof or now or datetime.now(timezone.utc))
        if asof >= match.kickoff_utc:
            raise LeakageError("No se proyecta con información posterior al inicio del partido (se exige t < T).")
        # Regla t < T: se descartan partidos con fecha ≥ t en el historial de ambos equipos.
        ctx = dataclasses.replace(
            ctx, asof=asof, home=_strip_future(ctx.home, asof), away=_strip_future(ctx.away, asof)
        )
        p = self.params[match.sport]

        base, l4 = base_rates(ctx, p)
        outputs: list[LayerOutput] = [l4, form_fatigue_layer(ctx, p), venue_weather_layer(ctx, p), lineup_layer(ctx, p)]
        l3, l2, l1 = outputs[1], outputs[2], outputs[3]
        lo, hi = p.layer_clip
        hm = clip(l3.home_mult * l2.home_mult * l1.home_mult, lo, hi)
        am = clip(l3.away_mult * l2.away_mult * l1.away_mult, lo, hi)
        rate_h, rate_a = base.home_rate * hm, base.away_rate * am

        rng = np.random.default_rng(seed)
        sim = SIMULATORS[match.sport](rate_h, rate_a, n, rng, pace=base.pace)
        coverage = sum(COVERAGE_WEIGHTS[o.name] * o.coverage for o in outputs) / sum(COVERAGE_WEIGHTS.values())
        notes = list(ctx.notes)
        if coverage < 0.5:
            notes.append("Cobertura de datos baja: trate la proyección con cautela.")
        return ProjectionResult(
            match_id=match.match_id, sport=match.sport.value, n_simulations=n,
            created_at=datetime.now(timezone.utc), kickoff_utc=match.kickoff_utc,
            probs=sim.probs, mc_se=sim.se, exp_home=sim.exp_home, exp_away=sim.exp_away,
            base_home=base.home_rate, base_away=base.away_rate, rate_home=rate_h, rate_away=rate_a,
            layers=tuple(LayerReport(o.name, o.home_mult, o.away_mult, o.coverage, o.notes) for o in outputs),
            coverage=coverage, top_scores=sim.top_scores,
            margin_sd=sim.margin_sd, total_sd=sim.total_sd, notes=tuple(notes),
        )
