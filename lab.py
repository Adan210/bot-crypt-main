"""Command line for phase 1: download candles, check gaps, run honest backtests.

    py lab.py download --years 3          # BTC, ETH, SOL into market.db
    py lab.py gaps                        # missing candles per coin
    py lab.py backtest --symbol ETHUSDT   # train/test + walk-forward vs buy and hold
"""
import argparse
import json
import sys
import time
from pathlib import Path

from backtest import format_report, holdout, walk_forward
import portfolio_bt
from signals import Params
from data import DEFAULT_SYMBOLS, INTERVAL_MS, UNIVERSE, Store, download
from engine import Config

DB = Path(__file__).parent / 'market.db'
DAY_CANDLES = 24


def cmd_download(args, store):
    end = int(time.time() * 1000)
    start = end - int(args.years * 365.25 * 24 * INTERVAL_MS)
    for symbol in args.symbols:
        print(f'{symbol}: descargando...', flush=True)
        result = download(store, symbol, start, end)
        first, last, count = store.bounds(symbol) or (None, None, 0)
        print(f"  {result['requests']} peticiones, {result['inserted']} velas nuevas, {count} en total")
    return 0


def cmd_gaps(args, store):
    for symbol in args.symbols:
        bounds = store.bounds(symbol)
        if not bounds:
            print(f'{symbol}: sin datos')
            continue
        gaps = store.gaps(symbol)
        missing = sum(g['missing'] for g in gaps)
        print(f'{symbol}: {bounds[2]} velas, {len(gaps)} huecos ({missing} velas faltantes)')
        for g in gaps[:10]:
            print(f"  faltan {g['missing']} tras {time.strftime('%Y-%m-%d %H:%M', time.gmtime(g['after'] / 1000))} UTC")
    return 0


def cmd_backtest(args, store):
    rows, excluded = store.longest_run(args.symbol)
    if not rows:
        print(f'{args.symbol}: sin datos. Ejecuta primero: py lab.py download', file=sys.stderr)
        return 1
    if excluded:
        print(f'Aviso: {excluded} velas quedan fuera por huecos; se usa el tramo continuo más largo ({len(rows)} velas).')
    btc = None
    if args.symbol != 'BTCUSDT':
        btc = {c['time']: c for c in store.rows('BTCUSDT')} or None
    base = Config(capital=100, exposure=args.exposure, fee=args.fee, slippage=args.slippage)
    train, test = args.train_days * DAY_CANDLES, args.test_days * DAY_CANDLES
    results = [holdout(rows, base, btc_by_time=btc), walk_forward(rows, base, train, test, btc_by_time=btc)]
    for result in results:
        print(format_report(args.symbol, result), end='\n\n')
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(f'Informe guardado en {args.json}')
    return 0


def cmd_bot(args, store):
    symbols = list(DEFAULT_SYMBOLS)
    if any(store.gaps(s) for s in symbols) or not all(store.bounds(s) for s in symbols):
        print('Faltan datos o hay huecos. Ejecuta: py lab.py download y py lab.py gaps', file=sys.stderr)
        return 1
    rows = portfolio_bt.align({s: store.rows(s) for s in symbols})
    params, n = Params(aggression=args.aggression), len(rows['BTCUSDT'])
    split, size = int(n * 0.7), args.window_days * DAY_CANDLES
    parts = dict(dev=('DESARROLLO (primer 70%: aquí se puede mirar y ajustar)', None, split),
                 final=('PRUEBA FINAL (último 30%: mírala UNA vez, sin ajustar después)', split, n))
    for key in (['dev', 'final'] if args.part == 'all' else [args.part]):
        title, start, end = parts[key]
        items = portfolio_bt.windows(rows, params, size, start, end)
        if items:
            print(portfolio_bt.report(title, items, params), end='\n\n')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=str(DB), help='archivo SQLite (por defecto market.db)')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('download', 'gaps'):
        p = sub.add_parser(name)
        p.add_argument('--symbols', nargs='+', default=list(DEFAULT_SYMBOLS), choices=UNIVERSE)
        if name == 'download':
            p.add_argument('--years', type=float, default=3)
    p = sub.add_parser('backtest')
    p.add_argument('--symbol', default='BTCUSDT', choices=UNIVERSE)
    p.add_argument('--exposure', type=float, default=0.25)
    p.add_argument('--fee', type=float, default=0.001)
    p.add_argument('--slippage', type=float, default=0.0005)
    p.add_argument('--train-days', type=int, default=180)
    p.add_argument('--test-days', type=int, default=60)
    p.add_argument('--json', help='guardar el informe en este archivo (usa nombre *-informe.json)')
    p = sub.add_parser('bot', help='backtest del bot completo (señales + motor de riesgo) contra Bitcoin')
    p.add_argument('--part', choices=('dev', 'final', 'all'), default='dev')
    p.add_argument('--window-days', type=int, default=90)
    p.add_argument('--aggression', type=float, default=Params().aggression)
    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        return dict(download=cmd_download, gaps=cmd_gaps, backtest=cmd_backtest, bot=cmd_bot)[args.command](args, store)
    except ValueError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
