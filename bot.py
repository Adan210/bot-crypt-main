"""Paper-trading runner: fetch new closed candles, advance the account(s), report, and keep going.

Recovery: all state lives in SQLite and is saved after every batch, and each cycle opens a fresh
connection. If anything fails (network, exchange, a bug) the loop waits with growing delays and tries
again; when the program itself is restarted it resumes from the saved state, open positions included.

    py bot.py init      # start the simulated account (BTC, ETH, SOL) from the latest candles
    py bot.py run       # keep it running (Ctrl+C to stop)
    py bot.py status    # print the current state
    py bot.py emergency # close everything and stop until reactivated
    py bot.py reactivate
    py bot.py review    # weekly review now (AI proposals only if GEMINI_API_KEY is in .env)
    py bot.py versions  # every strategy version and its status
    py bot.py approve N # your OK for a LARGE change that finished paper testing
    py bot.py rollback  # go back to the previous version
    py bot.py gate      # criteria for even considering real money (informational only)
"""
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import advisor as ai
import alerts
import data
import env
import extras
import gate
import portfolio_bt
import report
import review as weekly
from paper import Paper
from signals import WINDOW, Params
from versions import Versions

DB = Path(__file__).parent / 'bot.db'
EVENTS = Path(__file__).parent / 'events.json'   # economic calendar kept by the user (see events.example.json)
H = data.INTERVAL_MS
SYMBOLS = data.DEFAULT_SYMBOLS


def closed_since(symbol, since_ms, fetch=data.fetch_klines, now_ms=None):
    """Closed candles opening after `since_ms`, paginated. The still-forming candle is never included."""
    now = time.time() * 1000 if now_ms is None else now_ms
    rows, cursor = [], since_ms + H
    while cursor <= now:
        raw = fetch(symbol, cursor, now, data.PAGE)
        if not raw:
            break
        last_open = int(raw[-1][0])
        if last_open < cursor:
            raise RuntimeError('La paginación no avanza.')
        rows += [data.candle_from_kline(k) for k in raw if int(k[6]) < now]
        cursor = last_open + H
    return rows


def start(paper, name, params, fetch=data.fetch_klines, capital=1000.0, now_ms=None, version='v0'):
    """Create an account from the latest closed candles (indicator warmup only, no trades in the past)."""
    now = time.time() * 1000 if now_ms is None else now_ms
    since = now - (WINDOW + 20) * H
    paper.init(name, params, {s: closed_since(s, since, fetch, now) for s in SYMBOLS}, capital, version=version)


def cycle(paper, name, fetch=data.fetch_klines, ctx=None, review=None, now_ms=None):
    """One polling round for one account. Returns the engine events."""
    last = paper.last_time(name)
    rows = {s: closed_since(s, last, fetch, now_ms) for s in paper.symbols(name)}
    return paper.process(name, rows, ctx, review)


def run_forever(open_paper, names, fetch=data.fetch_klines, interval=300, sleep=time.sleep, stop=lambda: False,
                on_events=lambda name, events: None, on_error=lambda exc: None, context=lambda now: None,
                after_cycle=lambda paper: None, max_backoff=900, review=None):
    """Poll forever. Errors never end the loop: wait 10s, 20s, 40s... (up to max_backoff) and retry."""
    failures = 0
    while not stop():
        try:
            paper = open_paper()
            try:
                now = time.time() * 1000
                ctx = context(now)
                for name in (names(paper) if callable(names) else names):
                    events = cycle(paper, name, fetch, ctx, review)
                    if events:
                        on_events(name, events)
                after_cycle(paper)
            finally:
                paper.close()
            failures, delay = 0, interval
        except Exception as exc:  # deliberate: the loop must survive anything
            failures += 1
            delay = min(max_backoff, 5 * 2 ** failures)
            on_error(exc)
        sleep(delay)


def describe(events):
    """Human line for an engine event (used for console output and alerts)."""
    kind = events['kind']
    if kind == 'entry':
        return f"COMPRA {events['symbol']} a {events['price']:.2f}, stop {events['stop']:.2f} · {events['reason']}"
    if kind == 'partial':
        return f"Toma parcial {events['symbol']} a {events['price']:.2f}"
    if kind == 'trade':
        t = events['trade']
        return (f"CIERRE {t['symbol']} a {t['exit']:.2f} · resultado {t['pnl']:+.2f} USDT ({t['r']:+.2f}R) · {t['reason_exit']}")
    if kind == 'ai_review':
        verdict = 'aprobó' if events['approve'] else 'RECHAZÓ'
        return f"Analista ({events['source']}) {verdict} {events['symbol']}: {events['reason']}"
    if kind == 'halt':
        return f"BOT DETENIDO: {events['why']}. Requiere reactivación manual."
    return f"{kind}: {events.get('why', '')}".strip()


def build_ai(db_path, values):
    """(review hook, proposer). Without an API key: rules only, no AI anywhere."""
    key = values.get('GEMINI_API_KEY')
    if not key:
        return None, weekly.RuleProposer()
    limiter = ai.UsageLimiter(db_path + '-usage.db', max_calls_day=int(values.get('ADVISOR_MAX_CALLS_DAY', 24)),
                              max_usd_day=float(values.get('ADVISOR_MAX_USD_DAY', 0.5)),
                              max_usd_month=float(values.get('ADVISOR_MAX_USD_MONTH', 10)))
    Gemini = ai.GeminiAdvisor(key, values.get('ADVISOR_MODEL', ai.DEFAULT_MODEL), limiter=limiter)
    return ai.SafeAdvisor(Gemini), weekly.GeminiProposer(Gemini)


def run_review(db_path, proposer, now_ms):
    """Weekly review against the live paper accounts, using the stored history for the backtest gate."""
    paper, versions = Paper(db_path), Versions(db_path)
    try:
        def load_rows():
            store = data.Store(str(Path(__file__).parent / 'market.db'))
            try:
                if not all(store.bounds(s) for s in SYMBOLS):
                    raise ValueError('Falta market.db: ejecuta py lab.py download')
                return portfolio_bt.align({s: store.rows(s) for s in SYMBOLS})
            finally:
                store.close()
        return weekly.weekly_review(paper, versions, proposer, load_rows,
                                    lambda params, label: start(paper, 'candidate', params, version=label), now_ms)
    finally:
        paper.close()
        versions.close()


def print_events(name, events):
    for e in events:
        if e['kind'] != 'skip':
            print(f'[{name}] {describe(e)}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=str(DB))
    parser.add_argument('--account', default='main')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init')
    p.add_argument('--capital', type=float, default=1000.0)
    p.add_argument('--aggression', type=float, default=Params().aggression)
    p = sub.add_parser('run')
    p.add_argument('--interval', type=int, default=300, help='seconds between polls')
    sub.add_parser('status')
    sub.add_parser('emergency')
    sub.add_parser('reactivate')
    sub.add_parser('review')
    sub.add_parser('versions')
    sub.add_parser('rollback')
    sub.add_parser('gate')
    p = sub.add_parser('approve')
    p.add_argument('version', type=int)
    args = parser.parse_args(argv)
    paper = Paper(args.db)
    try:
        if args.command == 'init':
            if args.account in paper.accounts() and input('Ya existe: se borrará su historial. Escribe SI para continuar: ') != 'SI':
                return 1
            start(paper, args.account, Params(aggression=args.aggression), capital=args.capital)
            print(f'Cuenta {args.account} iniciada con {args.capital:.2f} USDT ficticios.')
        elif args.command == 'status':
            summary = paper.summary(args.account)
            summary.pop('curve', None)
            print(json.dumps(summary, indent=2, default=str))
        elif args.command == 'emergency':
            print_events(args.account, paper.emergency(args.account))
        elif args.command == 'reactivate':
            paper.reactivate(args.account)
            print('Bot reactivado.')
        elif args.command == 'gate':
            print(gate.format_gate(gate.evaluate(paper.summary(args.account))))
        elif args.command == 'versions':
            versions = Versions(args.db)
            try:
                versions.baseline(paper.params(args.account))
                for v in versions.all():
                    print(f"v{v['id']} [{v['status']}] {'GRANDE ' if v['large'] else ''}{'aprobada ' if v['approved'] else ''}"
                          f"{v['change'] or 'inicial'} · {v['reason']} · fuente: {v['source']}")
            finally:
                versions.close()
        elif args.command == 'approve':
            versions = Versions(args.db)
            try:
                versions.approve(args.version)
                print(f'Versión {args.version} aprobada. Se aplicará al terminar su prueba en paper trading.')
            finally:
                versions.close()
        elif args.command == 'rollback':
            versions = Versions(args.db)
            try:
                versions.baseline(paper.params(args.account))
                versions.rollback(paper)
                print('Versión anterior restaurada.')
            finally:
                versions.close()
        elif args.command == 'review':
            _, proposer = build_ai(args.db, env.load())
            outcome, _ = run_review(args.db, proposer, paper.last_time(args.account))
            print(outcome)
        else:
            events = extras.load_events(EVENTS)
            paper.close()
            values = env.load()
            reviewer, proposer = build_ai(args.db, values)
            notifier = alerts.Notifier(alerts.from_env(values), describe=describe,
                                       secrets=[values.get('GEMINI_API_KEY')])
            print(f'Bot en marcha ({args.account}). Analista: {"IA (Gemini) con plan B" if reviewer else "solo reglas"}. '
                  'Ctrl+C para detener.', flush=True)

            def after_cycle(p):
                if p.accounts():
                    report.write_daily(p, args.account, notify=notifier.say)
                versions = Versions(args.db)
                try:
                    if p.accounts() and weekly.due(versions, p.last_time(args.account)):
                        print(f'[revisión semanal] {run_review(args.db, proposer, p.last_time(args.account))[0]}', flush=True)
                finally:
                    versions.close()
            try:
                run_forever(lambda: Paper(args.db),
                            lambda p: [n for n in (args.account, 'candidate') if n in p.accounts()],
                            interval=args.interval, on_events=notifier.on_events, review=reviewer, after_cycle=after_cycle,
                            on_error=notifier.on_error,
                            context=lambda now: extras.gather(SYMBOLS, now, events))
            except KeyboardInterrupt:
                print('Detenido.')
    except ValueError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    finally:
        try:
            paper.close()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
