"""Aplicación Flet: barra superior, ranking y cartelera de hoy con botón «Proyectar»."""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

import flet as ft

from ..betting.ranking import BetCandidate, build_candidates, merge_odds, rank_bets, selection_label
from ..core.config import Settings
from ..core.enums import MatchStatus, Sport
from ..projection.results import MAX_SIMULATIONS, MIN_SIMULATIONS
from ..services import Services
from ..settings import save_settings
from .components import SPORT_LABELS, CardHandlers, MatchView, match_card, ranking_tile

log = logging.getLogger(__name__)
KELLY_CHOICES = {"1/10": 0.10, "1/4": 0.25, "1/2": 0.50}


class SportsQuantApp:
    def __init__(self, page: ft.Page, services: Services) -> None:
        self.page, self.svc = page, services
        self.s = services.settings
        self._tz_settings = Settings()
        self._lock = threading.RLock()
        self._refreshing = False
        self._stop = threading.Event()
        self.status = ft.Text("", size=11, color=ft.Colors.GREY_400)
        self.sims_label = ft.Text(self._sims_text(), size=12)
        self.ranking_col = ft.Column(spacing=6)
        self.matches_col = ft.Column(spacing=8)
        self.spinner = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

    # ---------------------------------------------------------------- construcción
    def start(self) -> None:
        p = self.page
        p.title = "SportsQuant"
        p.theme_mode = ft.ThemeMode.DARK
        p.padding = 12
        p.scroll = ft.ScrollMode.AUTO
        sport_dd = ft.Dropdown(
            value=self.s.sport, width=160,
            options=[ft.DropdownOption(key=s.value, text=SPORT_LABELS[s]) for s in Sport],
            on_select=self._on_sport,
        )
        top = ft.Row(alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER, controls=[
            sport_dd,
            ft.Row(spacing=0, controls=[
                self.spinner,
                ft.IconButton(icon=ft.Icons.REFRESH, tooltip="Refrescar", on_click=lambda e: self.refresh()),
                ft.IconButton(icon=ft.Icons.SETTINGS, tooltip="Ajustes", on_click=lambda e: self._open_settings()),
            ]),
        ])
        slider = ft.Slider(
            min=MIN_SIMULATIONS, max=MAX_SIMULATIONS, divisions=(MAX_SIMULATIONS - MIN_SIMULATIONS) // 1000,
            value=self.s.n_simulations, label="{value}", on_change=self._on_sims, on_change_end=self._on_sims_end,
        )
        p.add(ft.SafeArea(content=ft.Column(spacing=8, controls=[
            top,
            ft.Row(controls=[self.sims_label], alignment=ft.MainAxisAlignment.START), slider,
            self.status,
            ft.Divider(height=6),
            ft.Text("Ranking (EV > 0, Kelly fraccionado)", size=16, weight=ft.FontWeight.BOLD), self.ranking_col,
            ft.Divider(height=6),
            ft.Text("Cartelera de hoy", size=16, weight=ft.FontWeight.BOLD), self.matches_col,
        ])))
        self.render()
        self.refresh()
        threading.Thread(target=self._auto_loop, name="auto-refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- handlers
    def _sims_text(self) -> str:
        return f"Simulaciones Monte Carlo: {self.s.n_simulations:,}"

    def _on_sims(self, e) -> None:
        self.s.n_simulations = int(round(float(e.control.value) / 1000) * 1000)
        self.sims_label.value = self._sims_text()
        self.page.update()

    def _on_sims_end(self, e) -> None:
        save_settings(self.svc.storage, self.s)

    def _on_sport(self, e) -> None:
        self.s.sport = e.control.value
        save_settings(self.svc.storage, self.s)
        self.render()
        self.refresh()

    def refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        self.spinner.visible = True
        self.status.value = "Actualizando…"
        self.page.update()
        self.page.run_thread(self._refresh_bg)

    def _refresh_bg(self) -> None:
        try:
            report = self.svc.matches.refresh(Sport(self.s.sport))
            hhmm = report.refreshed_at.astimezone(self._tz_settings.tz()).strftime("%H:%M")
            text = f"{report.visible} partidos hoy · actualizado {hhmm}"
            if report.error:
                text = f"Sin conexión con la fuente ({report.error}). Mostrando datos guardados · {report.visible} partidos"
            elif report.skipped:
                text += f" · {len(report.skipped)} descartados"
            self.status.value = text
        except Exception as exc:  # noqa: BLE001
            log.exception("Fallo al refrescar")
            self.status.value = f"Error al refrescar: {exc}"
        finally:
            with self._lock:
                self._refreshing = False
            self.spinner.visible = False
            self.render()

    def _auto_loop(self) -> None:
        """Mantiene marcadores y alineaciones al día. Nunca ejecuta proyecciones."""
        while not self._stop.wait(self.s.auto_refresh_seconds):
            try:
                now = self.svc.matches.now()
                ms = self.svc.matches.today_matches(Sport(self.s.sport))
                if any(m.status is MatchStatus.LIVE or (m.status is MatchStatus.SCHEDULED and m.kickoff_utc - now <= timedelta(hours=3)) for m in ms):
                    self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("Fallo en el refresco automático")

    def _on_project(self, match_id: str) -> None:
        self.svc.worker.submit(match_id, self.s.n_simulations, listener=lambda mid, st: self.render())
        self.render()

    # ------------------------------------------------------------------ render
    def build_views(self) -> tuple[list[MatchView], list[BetCandidate]]:
        sport, tz = Sport(self.s.sport), self._tz_settings.tz()
        views, all_bets = [], []
        for m in self.svc.matches.today_matches(sport):
            gate = self.svc.matches.gate(m.match_id)
            proj = self.svc.worker.status(m.match_id)
            result = None
            if m.status is MatchStatus.SCHEDULED:  # una proyección prepartido no aplica a un partido en vivo
                result = proj.result or self.svc.storage.load_projection(m.match_id)
            odds = merge_odds(m.odds, self.svc.storage.manual_odds(m.match_id))
            bets = build_candidates(m, result, odds, kelly_multiplier=self.s.kelly_multiplier, stake_cap=self.s.stake_cap) if result else []
            all_bets += bets
            views.append(MatchView(m, gate, proj, result, tuple(odds), tuple(bets), m.kickoff_utc.astimezone(tz).strftime("%H:%M")))
        return views, all_bets

    def render(self) -> None:
        with self._lock:
            views, bets = self.build_views()
            handlers = CardHandlers(self._on_project, self._edit_odds)
            ranking = rank_bets(bets)
            self.ranking_col.controls = (
                [ranking_tile(i + 1, b) for i, b in enumerate(ranking)]
                or [ft.Text("Sin apuestas con EV > 0. Proyecta un partido y ajusta las cuotas.", size=12, color=ft.Colors.GREY_400)]
            )
            self.matches_col.controls = (
                [match_card(v, handlers) for v in views]
                or [ft.Text("No hay partidos de hoy para este deporte.", size=12, color=ft.Colors.GREY_400)]
            )
            self.page.update()

    # ------------------------------------------------------------------ diálogos
    def toast(self, text: str) -> None:
        self.page.show_dialog(ft.SnackBar(content=ft.Text(text)))

    def _edit_odds(self, match_id: str) -> None:
        m = self.svc.matches.get(match_id)
        if m is None:
            return
        sels = ["HOME", "DRAW", "AWAY"] if m.sport is Sport.FOOTBALL else ["HOME", "AWAY"]
        current = {o.selection: o.decimal_odds for o in merge_odds(m.odds, self.svc.storage.manual_odds(match_id))}
        fields = {s: ft.TextField(label=selection_label(m, s), value=f"{current[s]:.2f}" if s in current else "",
                                  keyboard_type=ft.KeyboardType.NUMBER, dense=True) for s in sels}
        error = ft.Text("", color=ft.Colors.RED_300, size=12)

        def save(e) -> None:
            parsed: dict[str, float | None] = {}
            for s, tf in fields.items():
                txt = (tf.value or "").strip().replace(",", ".")
                if not txt:
                    parsed[s] = None
                    continue
                try:
                    val = float(txt)
                except ValueError:
                    val = 0.0
                if not val > 1.0:
                    error.value = f"Cuota inválida en «{tf.label}»: use un decimal mayor que 1.00 (ej. 1.85)."
                    self.page.update()
                    return
                parsed[s] = val
            for s, val in parsed.items():
                self.svc.storage.set_manual_odds(match_id, s, val)
            self.page.pop_dialog()
            self.render()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(f"Cuotas decimales · {m.title}"),
            content=ft.Column(tight=True, controls=[*fields.values(), error,
                              ft.Text("Vacío = usar la cuota del proveedor (si existe).", size=11, color=ft.Colors.GREY_400)]),
            actions=[ft.TextButton(content="Cancelar", on_click=lambda e: self.page.pop_dialog()),
                     ft.Button(content="Guardar", on_click=save)],
        ))

    def _open_settings(self) -> None:
        kelly = ft.Dropdown(
            label="Fracción de Kelly", value=next((k for k, v in KELLY_CHOICES.items() if abs(v - self.s.kelly_multiplier) < 1e-9), "1/4"),
            options=[ft.DropdownOption(key=k, text=k) for k in KELLY_CHOICES],
        )
        cap = ft.TextField(label="Tope por apuesta (% de banca)", value=f"{self.s.stake_cap * 100:g}", keyboard_type=ft.KeyboardType.NUMBER, dense=True)
        leagues = ft.TextField(label="Ligas de fútbol (códigos ESPN, separados por coma)", value=", ".join(self.s.football_leagues),
                               multiline=True, min_lines=2, max_lines=4)
        auto = ft.TextField(label="Refresco automático (segundos)", value=str(self.s.auto_refresh_seconds), keyboard_type=ft.KeyboardType.NUMBER, dense=True)

        def save(e) -> None:
            try:
                self.s.kelly_multiplier = KELLY_CHOICES[kelly.value or "1/4"]
                self.s.stake_cap = float(cap.value.replace(",", ".")) / 100
                self.s.auto_refresh_seconds = int(float(auto.value))
                self.s.football_leagues = [x for x in (leagues.value or "").replace("\n", ",").split(",")]
            except (ValueError, KeyError):
                self.toast("Revise los valores numéricos.")
                return
            save_settings(self.svc.storage, self.s)
            self.svc.apply_leagues(self.s.football_leagues)
            self.page.pop_dialog()
            self.render()
            self.refresh()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Ajustes"),
            content=ft.Column(tight=True, controls=[kelly, cap, auto, leagues]),
            actions=[ft.TextButton(content="Cancelar", on_click=lambda e: self.page.pop_dialog()),
                     ft.Button(content="Guardar", on_click=save)],
        ))
