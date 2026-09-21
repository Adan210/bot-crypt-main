# Crypto Lab

Laboratorio educativo de trading de criptomonedas **solo en simulación**. Descarga datos
reales, prueba estrategias sin mirar el futuro, opera con capital ficticio bajo un motor de
riesgo con reglas bloqueadas y compara todo contra comprar y mantener Bitcoin.

**No promete ganancias. No envía órdenes reales. No maneja claves de exchange.**
Requiere Python 3.11+ y no instala paquetes (solo biblioteca estándar).

## Estado honesto (léelo primero)

La estrategia actual **no ha demostrado ventaja** sobre comprar y mantener Bitcoin.
Con datos reales de 3 años (BTC, ETH, SOL, velas de 1h, comisión 0.1% + deslizamiento 0.05%):

| Prueba | Bot | Bitcoin, misma exposición | Operaciones |
|---|---|---|---|
| Desarrollo (primer 70%, 8 ventanas de 90 días) | +0.45% | +4.85% | 109 |
| **Prueba final** (último 30%, vista una sola vez) | **−4.52%** | −0.64% | 20 (no concluyente) |

En la prueba final Bitcoin al 100% cayó −44%; el bot perdió poco porque casi no estuvo
expuesto (~1%), no porque la señal sea buena. Historia: la primera estrategia (cruce de
medias) perdía −27% a −38% por comisiones; la de "comprar sobreventa en mercado lateral"
perdió −0.22R por operación en desarrollo y quedó **apagada**. Lo único con R positivo en
desarrollo (rupturas con volumen en tendencia alcista) fue negativo fuera de muestra (R promedio −0.46 en solo 20 operaciones: no concluyente).
Los criterios para dinero real (abajo) **no se cumplen**.

## Inicio rápido

```powershell
py -m unittest -v                     # todas las pruebas (deben terminar en OK)
py lab.py download --years 3          # BTC, ETH, SOL a market.db (ignorada por Git)
py lab.py bot --part dev              # backtest del bot completo contra Bitcoin
py bot.py init                        # cuenta de paper trading con 1000 USDT ficticios
py bot.py run                         # mantenerlo corriendo (Ctrl+C para detener)
py server.py                          # panel en http://127.0.0.1:8000
```

El panel escucha solo en tu computadora (127.0.0.1). No lo expongas a internet: no tiene
autenticación. Las acciones que cambian estado (POST) rechazan orígenes externos.

## Reglas bloqueadas (nadie las cambia, ni la IA)

Definidas en `rules.py` como dataclass congelada; `test_rules.py` fija cada valor y busca
en el código cualquier intento de modificarlas.

- Riesgo máximo por operación: **1%** del capital (la agresividad ajustable solo puede bajarlo; tope bloqueado en 1.0).
- **Stop loss obligatorio**: sin stop válido no se abre la posición.
- Pérdida diaria máx. **3%** y semanal **6%**: el bot deja de abrir operaciones hasta que **tú** lo reactives (`py bot.py reactivate` o el botón).
- Máx. **3 posiciones** abiertas, cada una ≤ 34% del capital, y no se abre una moneda con correlación > 0.85 con una ya abierta.
- Un gap de precio puede superar el stop (el stop se ejecuta al precio del gap): el 1% es lo planificado, no una garantía.

## Fases

### Fase 1 · Datos y backtest (`data.py`, `backtest.py`, `lab.py`)
Descarga paginada de Binance (1000 velas/petición, reintentos), descarta la vela abierta,
valida velas y **huecos** (el backtest usa el tramo continuo más largo), guarda en SQLite sin
duplicados. `UNIVERSE` trae 16 pares; empieza con BTC, ETH, SOL. Backtest con comisión y
deslizamiento, elección de parámetros solo con datos de entrenamiento, separación 70/30 y
walk-forward. Métricas: retorno, caída máxima, ganancia promedio vs pérdida promedio,
aciertos, operaciones y comparación contra comprar y mantener con la misma exposición.
Menos de 30 operaciones = NO CONCLUYENTE.

### Fase 2 · Análisis y señales (`indicators.py`, `signals.py`, `extras.py`)
Indicadores: medias, RSI, ATR, volatilidad, volumen, eficiencia de Kaufman, Bollinger.
Detector de régimen (tendencia alcista/bajista, lateral, transición) y una estrategia por
régimen; solo opera con señal clara (rupturas con volumen). Datos extra usados **solo como
veto**: funding de Binance (derivados), Fear & Greed (sentimiento), titulares de noticias
(RSS, palabras de alarma) y un calendario económico que **tú** mantienes en `events.json`
(ver `events.example.json`; bloquea entradas ±2h alrededor de eventos de alto impacto).
Estos filtros no están respaldados por backtest (no hay historia limpia): son
salvaguardas, y se guardan con cada operación para estudiarlos después.

### Fase 3 · Riesgo y ejecución (`rules.py`, `risk.py`, `trader.py`, `portfolio_bt.py`)
Tamaño por operación según ATR y distancia al stop (los costos entran en el riesgo), stop
loss, trailing stop, toma parcial (50% en 2R y el resto pasa a break-even), modo solo
cierre, **botón de emergencia** (cierra todo y detiene), agresividad ajustable con tope.
El mismo motor sirve para backtest y paper trading. Short: no implementado (requiere datos
y pruebas sólidas primero).

### Fase 4 · Paper trading en vivo (`paper.py`, `bot.py`)
Cuenta persistente en SQLite con deduplicación de velas, posiciones y órdenes abiertas
recuperables tras reiniciar, cada operación registrada con motivo de entrada/salida,
precio, resultado, R y estado del mercado. `py bot.py run` sondea cada 5 min; ante
cualquier error espera 10 s, 20 s, 40 s… (máx. 15 min) y reintenta solo. Para que arranque con
Windows usa el Programador de tareas (acción: `py bot.py run` en esta carpeta).
Si una moneda deja de publicar velas, la cuenta espera; un hueco puntual se salta.

### Fase 5 · IA opcional (`advisor.py`, `versions.py`, `review.py`, `env.py`)
- El analista IA **solo puede vetar** entradas que las reglas ya generaron (el motor descarta
  cualquier moneda añadida o valor alterado) y el motor de riesgo dimensiona todo.
- **Plan B sin IA**: si falta la clave, se acaba la cuota, hay error de red o respuesta
  inválida, se siguen las reglas y queda registrado por qué. Sin `ANTHROPIC_API_KEY` no hay IA.
- **Control de costo**: tope diario/mensual de llamadas y dólares guardado en SQLite
  (`.env.example` tiene los valores por defecto). No se envía información de cuenta.
- **Revisión semanal**: resume resultados; con menos de 10 operaciones en la semana no saca
  conclusiones. La IA propone como máximo **un** cambio, solo de parámetros de `TUNABLE`.
- **Cambios guardados y reversibles**: propuesta → backtest (≥30 operaciones, mejora ≥0.5 pp,
  sin más caída, más ventanas ganadas que perdidas) → 14 días en una cuenta `candidate`
  de paper trading → aplicado. Uno a la vez, máx. una propuesta por semana.
  Cambios grandes (>25% del rango permitido) necesitan tu aprobación: `py bot.py approve N`.
  `py bot.py versions` los lista y `py bot.py rollback` vuelve atrás.
- No pude probar la API real de Anthropic sin una clave: las pruebas usan un transporte falso.

### Fase 6 · Alertas, panel y dinero real (`alerts.py`, `report.py`, `gate.py`, `server.py`)
Alertas por Telegram (entradas, salidas, detenciones y errores; con `TELEGRAM_BOT_TOKEN` y
`TELEGRAM_CHAT_ID` en `.env`; sin ellos solo se muestra en la terminal; el token se borra de
cualquier texto). Panel local con capital, posiciones, operaciones, caída máxima y
comparación con Bitcoin (misma exposición y 100%). Reporte diario en `reports/AAAA-MM-DD.md`.

**Dinero real**: `py bot.py gate` verifica los criterios: ≥ 3 meses de paper trading,
> 200 operaciones, supera a comprar y mantener Bitcoin (misma exposición) con menos caída
y sin detenciones. **Es solo informativo: el proyecto no contiene código para enviar órdenes
reales ni manejar claves de exchange** (una prueba lo verifica). Activarlo sería un cambio
nuevo, revisado por ti. Si algún día lo haces: monto que puedas perder por completo, API
key **sin permiso de retiro** y con lista de IPs.

## Secretos

Copia `.env.example` a `.env` (ignorado por Git). Nunca subas `.env`, claves, `*.db` ni
`reports/`. No pongas claves de exchange en ningún archivo.

## Backtest rápido del panel (versión original)

El formulario del panel sigue ejecutando el backtest demo original (cruce de medias + RSI
sobre 500 velas sintéticas o 1000 velas de Binance). Es la estrategia de la fase 0, útil para
ver el efecto de comisión y exposición; **no** es el bot actual.

## Supuestos y limitaciones

- Comisión 0.1% y deslizamiento 0.05% por lado (tarifa base de Binance; con descuentos sería menor).
- Solo velas de 1h, spot, largo únicamente, un exchange. Se omiten profundidad de mercado,
  latencia, ejecuciones parciales, mínimos y redondeos del exchange, impuestos y financiación.
- Un stop se ejecuta con datos de vela (mínimo/máximo): si una vela toca stop y objetivo, se asume el stop.
- Los parámetros de señales se fijaron a priori; solo una decisión estructural (apagar la
  estrategia lateral) se tomó mirando el tramo de desarrollo. Cuantas más veces mires los
  mismos datos, menos valen los resultados: la prueba final solo se debe mirar una vez.
- Las cantidades son USDT ficticios; no se simula el riesgo de la moneda estable.
- Las simulaciones no garantizan ganancias ni predicen el futuro.

## Estructura

| Archivo | Para qué |
|---|---|
| `rules.py` | Reglas bloqueadas |
| `risk.py`, `trader.py` | Motor de riesgo y de operaciones (compartido) |
| `signals.py`, `indicators.py`, `extras.py` | Régimen, señales, vetos |
| `data.py`, `backtest.py`, `portfolio_bt.py`, `lab.py` | Datos y backtests |
| `paper.py`, `bot.py`, `report.py`, `gate.py`, `alerts.py` | Paper trading y operación |
| `advisor.py`, `versions.py`, `review.py`, `env.py` | IA opcional y cambios guardados |
| `engine.py`, `server.py`, `index.html` | Backtest original y panel |
| `test_*.py` | Pruebas (GitHub Actions las ejecuta en cada push y PR) |

## Trabajo con GitHub

Una rama por cambio (`git switch -c feature/nombre`), `py -m unittest -v` antes de hacer
commit y pull request para revisión. Si hay conflicto, no borres archivos ni fuerces el push.
