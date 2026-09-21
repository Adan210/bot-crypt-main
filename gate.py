"""Criteria that must ALL hold before real money is even considered (phase 6).

This module only REPORTS. It cannot enable anything: the project contains no code that sends real orders
or handles exchange keys. Going live would be a new, separately reviewed change made by a human.
"""
MIN_DAYS = 90
MIN_TRADES = 200   # "more than 200"
NOTE = ('Aunque todo esté en verde, esto NO activa dinero real: este proyecto no contiene código para enviar órdenes ni '
        'manejar claves de exchange. Pasar a dinero real sería un cambio nuevo, revisado por ti. Si algún día lo haces: '
        'empieza con un monto que puedas perder por completo, usa una API key SIN permiso de retiro y con lista de IPs, '
        'y mantén los límites de pérdida bloqueados.')


def evaluate(s):
    """`s` is Paper.summary(). Returns dict(ok, checks=[{name, ok, value}], note)."""
    if not s.get('initialized'):
        return dict(ok=False, checks=[], note=NOTE)
    bot_ret, bot_dd = s['return_pct'], s['drawdown_pct']
    ref = s['btc_same_exposure']
    checks = [
        dict(name='Al menos 3 meses de paper trading', ok=s['days'] >= MIN_DAYS, value=f"{s['days']:.0f} de {MIN_DAYS} días"),
        dict(name='Más de 200 operaciones cerradas', ok=s['trade_count'] > MIN_TRADES, value=f"{s['trade_count']} de {MIN_TRADES}"),
        dict(name='Supera a comprar y mantener Bitcoin (misma exposición)', ok=bot_ret > ref['return_pct'],
             value=f"bot {bot_ret:+.2f}% vs BTC {ref['return_pct']:+.2f}%"),
        dict(name='Con menos caída máxima que esa referencia', ok=bot_dd < ref['max_drawdown_pct'],
             value=f"bot {bot_dd:.2f}% vs BTC {ref['max_drawdown_pct']:.2f}%"),
        dict(name='Sin detenciones por pérdida en el periodo', ok=not s['halted'], value=s['halted'] or 'ninguna'),
    ]
    return dict(ok=all(c['ok'] for c in checks), checks=checks, note=NOTE)


def format_gate(result):
    if not result['checks']:
        return 'Sin cuenta simulada todavía.\n' + result['note']
    lines = [f"{'OK ' if c['ok'] else 'NO '} {c['name']}: {c['value']}" for c in result['checks']]
    head = 'CRITERIOS CUMPLIDOS' if result['ok'] else 'CRITERIOS NO CUMPLIDOS: NO se considera dinero real'
    return '\n'.join([head] + lines + [result['note']])
