# SportsQuant — proyecciones personales (fútbol · baloncesto · béisbol)

App Android ligera (Python + Flet) para un POCO X3 NFC (arm64-v8a). Proyecta **un partido a la vez**, solo al
pulsar **Proyectar**, con Monte Carlo (1.000–50.000 simulaciones, ajustable en la barra superior).

## Reglas implementadas
- Cartelera: solo partidos de **hoy** (fecha local) no terminados. `SCHEDULED` → botón Proyectar; `LIVE` → marcador y
  minuto/cuarto/inning; `FINISHED` desaparece. Un LIVE que cruza la medianoche sigue visible (`include_live_carryover`).
- **Cero ejecución masiva**: refrescar nunca proyecta. El cálculo corre en un hilo aparte, de uno en uno.
- **`LINEUP_REQUIRED`**: sin alineaciones oficiales válidas el botón queda deshabilitado con el motivo
  (11 titulares y 1 portero; quinteto + reporte de lesiones; lineup 1-9 + abridor confirmado; titular `OUT` bloquea).
- **t < T**: no se proyecta a partir del inicio; la compuerta se evalúa al tocar y otra vez justo antes de simular;
  el motor descarta cualquier dato con fecha ≥ t.
- 4 capas: **L4** base (IPF ajustado por rival + xG/tiros · pace/ORtg/DRtg/eFG% · wRC+/FIP/xFIP/bullpen) →
  **L3** forma (decaimiento exponencial) y fatiga → **L2** sede, clima, park factor y escenarios similares →
  **L1** alineación (on/off, minutos, platoon).
- Ranking: EV = P·O − 1; se descarta EV ≤ 0; orden por crecimiento logarítmico esperado con **Kelly fraccionado**
  (1/4 por defecto, tope 5 % de banca). Cuotas: proveedor (ESPN) y/o **manuales** (✎), las manuales mandan.

## Uso
```
pip install -e ".[dev]"
pytest -q                                      # 158+ pruebas, sin red
python -m sportsquant.tools.check_sources      # diagnóstico de fuentes EN VIVO (requiere red)
python -m sportsquant.tools.check_sources --sport baseball --project
flet run src/main.py                           # probar la app en el escritorio
```
APK: sube el repo a GitHub → Actions → **Build APK** → descarga el artefacto `sportsquant-apk`
(`flet build apk --arch arm64-v8a`; corre las pruebas antes de compilar).

## Fuentes de datos (sin claves)
ESPN (fútbol y NBA), MLB StatsAPI (béisbol), Open-Meteo (clima). Todo se cachea en SQLite; los resultados
históricos y los resúmenes de partidos finalizados se piden una sola vez.

## Límites conocidos (léelos)
- **No hay backtest ni calibración histórica.** Las probabilidades son salida de un modelo; contra el mercado el EV suele
  estar sobreestimado. Por eso el Kelly es fraccionado. Úsalo como herramienta de análisis, no como garantía.
- Aproximaciones declaradas: xG = proxy por tiros; wRC+ = proxy por OPS (sin parque); park factors aproximados
  (`data/park_factors.py`, editables, se contraen 50 % hacia 1.0); xFIP usa outs aéreos + HR como proxy de bolas elevadas.
- Las APIs públicas de ESPN no son oficiales y pueden cambiar: ante un campo ausente el sistema **falla seguro**
  (alineación pendiente / capa con cobertura baja) en lugar de adivinar. Si algo no aparece, corre `check_sources`.
- NBA: ESPN puede publicar los titulares solo muy cerca del salto inicial; hasta entonces seguirá `LINEUP_REQUIRED`.
- Parámetros del modelo en `projection/params.py`, `projection/sims/*.py` y constantes al inicio de cada capa.
