import unittest
from engine import Config, demo, simulate, signal


class EngineTests(unittest.TestCase):
    def test_reproducible(self):
        self.assertEqual(simulate(demo(),Config()),simulate(demo(),Config()))

    def test_initial_capital(self):
        r=simulate(demo(),Config())
        self.assertEqual(r['curve'][0]['equity'],100)
        self.assertGreaterEqual(r['cash'],0)
        self.assertGreaterEqual(r['units'],0)

    def test_no_future_leak(self):
        rows=demo()
        full=simulate(rows,Config())
        short=simulate(rows[:150],Config())
        self.assertEqual(full['curve'][:150],short['curve'])
        self.assertEqual([t for t in full['trades'] if t['time']<=rows[149]['time']],short['trades'])

    def test_flat(self):
        rows=[dict(time=i+1,open=100,close=100) for i in range(100)]
        r=simulate(rows,Config())
        self.assertEqual(r['trades'],[])
        self.assertEqual(r['return_pct'],0)

    def test_invalid(self):
        for cfg in [Config(capital=-1),Config(fee=float('nan')),Config(fast=50,slow=30),Config(exposure=2)]:
            with self.assertRaises(ValueError):simulate(demo(),cfg)
        rows=demo();rows[10]['close']=float('nan')
        with self.assertRaises(ValueError):simulate(rows,Config())

    def test_fee_accounting(self):
        cfg=Config();r=simulate(demo(),cfg)
        cash=cfg.capital;units=0
        for t in r['trades']:
            direction=1 if t['side']=='COMPRA' else -1
            units+=direction*t['quantity']
            cash-=direction*t['quantity']*t['price']+t['fee']
        self.assertAlmostEqual(cash,r['cash'])
        self.assertAlmostEqual(units,r['units'])
        self.assertAlmostEqual(sum(t['fee'] for t in r['trades']),r['fees'])

    def test_entry_signal_previous_close(self):
        rows=demo();cfg=Config();r=simulate(rows,cfg)
        first=next(t for t in r['trades'] if t['side']=='COMPRA')
        index=next(i for i,x in enumerate(rows) if x['time']==first['time'])
        self.assertTrue(signal([x['close'] for x in rows[:index]],cfg))
        self.assertAlmostEqual(first['price'],rows[index]['open']*(1+cfg.slippage))

    def test_halt(self):
        rows=demo();base=simulate(rows,Config())
        trade=next(t for t in base['trades'] if t['side']=='COMPRA')
        i=next(i for i,r in enumerate(rows) if r['time']==trade['time'])
        rows[i]['close']=rows[i]['open']*.1
        r=simulate(rows,Config())
        self.assertTrue(r['halted'])
        self.assertEqual(r['units'],0)
        self.assertEqual(r['trades'][-1]['reason'],'Límite de caída')
        self.assertEqual(r['trades'][-1]['time'],rows[i+1]['time'])


if __name__=='__main__':unittest.main()
