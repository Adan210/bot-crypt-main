# Crypto Lab · v0.1

Laboratorio educativo de criptomonedas con panel web local. No promete ganancias,
no envía órdenes, no admite claves privadas y no requiere API de IA.
Desarrollado para aprender y colaborar mediante GitHub.

## Iniciar en Windows

Necesitas Python 3.11 o superior. Descomprime el proyecto, abre una terminal dentro
de `crypto-lab` y ejecuta:

```powershell
py server.py
```

Abre http://127.0.0.1:8000 en el navegador. Para salir: Ctrl+C en la terminal.
En macOS/Linux: `python3 server.py`. No hay paquetes adicionales que instalar.
El servicio escucha solo en tu computadora. No lo expongas a internet: no tiene
autenticación ni configuración de producción. Si el puerto está ocupado, cierra
el otro proceso o cambia el puerto al final de `server.py`.

## Qué puedes hacer

- Ejecutar backtests y variar capital ficticio, medias, exposición y costos.
- Usar 500 velas sintéticas reproducibles, claramente marcadas DEMO.
- Solicitar hasta 1000 velas recientes de una hora de BTCUSDT, ETHUSDT o SOLUSDT.
  Se excluyen velas todavía abiertas. La disponibilidad depende de Binance y
  tu conexión/región. Un error no se convierte silenciosamente en datos ficticios.
- Comparar capital neto estimado con comprar y mantener.
- Reproducir el histórico vela por vela y exportar datos, ajustes y resultados JSON.

**No incluye todavía paper trading en vivo**, almacenamiento de una cuenta
simulada entre sesiones, IA ni ejecución automática continua. La reproducción
histórica no demuestra comportamiento futuro. Volver a ejecutar empieza de cero.

## Estrategia y supuestos

Se busca estar comprado cuando la media simple rápida supera la lenta y el RSI
simple de 14 cambios es menor de 70; se sale cuando deja de cumplirse. No es RSI
Wilder. Se usa solo información disponible al cierre; las operaciones se ejecutan
en la apertura siguiente con deslizamiento adverso y comisión por cada lado.
No se hace optimización automática de parámetros.

La exposición limita el presupuesto al entrar, no lo rebalancea continuamente.
La caída máxima se mide sobre el máximo histórico del capital neto. Si se supera
al cierre, se liquida en la siguiente apertura y se bloquean nuevas entradas.
No es una garantía de pérdida máxima: los saltos de precio pueden superarla.
Si sucede en la última vela, la liquidación queda pendiente por falta de otra vela.

El capital mostrado descuenta costos hipotéticos de liquidación de posiciones
abiertas. Las comisiones reportadas corresponden solo a operaciones ejecutadas.
La referencia invierte el 100% tras el mismo calentamiento de indicadores, con
los mismos costos. Tiene más exposición que el bot predeterminado (25%): no es
una comparación ajustada por riesgo. Las cantidades son USDT, no soles ni USD
garantizados; no se simula el riesgo de la moneda estable.

Se omiten profundidad de mercado, latencia, ejecuciones parciales, mínimos,
redondeos del exchange, impuestos, financiación y costos de infraestructura.
Una muestra de hasta 1000 horas es corta. No uses estos resultados para concluir
que la estrategia es rentable ni para decidir cuánto dinero real arriesgar.

Fuente técnica del adaptador:
[Binance · Market data](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints),
GET `/api/v3/klines`. No hay llamadas a endpoints de órdenes.

## Pruebas

```powershell
py -m unittest -v
```

Prueban contabilidad, parámetros inválidos, ausencia de datos futuros,
ejecución en vela siguiente, reproducibilidad y parada por caída.

## Subir a GitHub con tu amigo

1. Crea un repositorio vacío en GitHub (privado si prefieren).
2. Desde esta carpeta, con Git instalado:

```sh
git init
git add .
git commit -m "Initial crypto simulation lab"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/TU_REPOSITORIO.git
git push -u origin main
```

3. Invita a tu amigo desde la configuración de colaboradores del repositorio.
4. Usen una rama por cambio y revisen pull requests antes de unirlos a main.

No subas contraseñas, claves de exchanges, archivos `.env` ni datos privados.
Este paquete no ha sido publicado en GitHub por el asistente.

## Estructura y siguientes tareas

- `engine.py`: señales, contabilidad y riesgo.
- `server.py`: servidor local y datos públicos.
- `index.html`: interfaz sin frameworks ni recursos externos.
- `test_engine.py`: pruebas; GitHub Actions las ejecuta en cada push/PR.

Próximos pasos: datos históricos más largos con validación de huecos, pruebas
fuera de muestra, paper trading persistente con SQLite y deduplicación de velas,
y después un módulo de IA opcional separado de los controles de riesgo.
La IA no está implementada: esta versión tiene costo de API de IA igual a cero.
