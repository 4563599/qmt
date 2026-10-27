from datetime import datetime, timedelta, timezone
from copy import deepcopy
import json
import subprocess
import types
import unittest

from test_bridge import ROOT
from test_runner import runner_module


NEW_FIELDS = {'limit_up_gap_yin_lte1_30d', 'prev_limit_up_date_30d', 'limit_up_gap_yin_count_30d'}


class LimitGapTests(unittest.TestCase):
    def setUp(self):
        self.module = runner_module().load_strategy(ROOT.parent / '盘中.py')

    def series(self, prior=(24,), yin=(), size=35, stock='000001.SZ', current_limit=True):
        dates = []
        day = datetime(2026, 8, 1)
        while len(dates) < size:
            if day.weekday() < 5:
                dates.append(day.strftime('%Y%m%d'))
            day += timedelta(days=1)
        opens, closes = [], []
        price = 10.0
        for index in range(size):
            opened = price * 1.01 if index in yin else price
            closed = (self.module.calc_limit_up_price(price, stock)
                      if index in prior or index == size - 1 and current_limit else price)
            opens.append(opened)
            closes.append(closed)
            price = closed
        return {'opens': opens, 'closes': closes, 'dates': dates, 'pre_close': closes[-2],
                'highs': [max(o, c) for o, c in zip(opens, closes)],
                'lows': [min(o, c) for o, c in zip(opens, closes)],
                'volumes': [1000] * size, 'amounts': [10000] * size}

    def test_separated_limit_ups_allow_zero_or_one_yin_but_not_two(self):
        for yin, expected in (((), 1), ((27,), 1), ((27, 30), 0)):
            series = self.series(yin=yin)
            result = self.module.calculate_limit_up_gap_flags('000001.SZ', series)
            self.assertEqual(result['limit_up_gap_yin_lte1_30d'], expected)
            self.assertEqual(result['limit_up_gap_yin_count_30d'], len(yin))
            self.assertEqual(result['prev_limit_up_date_30d'], series['dates'][24])

    def test_current_day_must_be_limit_up_and_previous_limit_must_exist(self):
        for series in (self.series(current_limit=False), self.series(prior=())):
            result = self.module.calculate_limit_up_gap_flags('000001.SZ', series)
            self.assertEqual(result['limit_up_gap_yin_lte1_30d'], 0)
            self.assertIsNone(result['limit_up_gap_yin_count_30d'])

    def test_lookback_30_days_includes_reference_day(self):
        inside = self.series(prior=(5,))
        outside = self.series(prior=(4,))
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', inside)['limit_up_gap_yin_lte1_30d'], 1)
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', outside)['limit_up_gap_yin_lte1_30d'], 0)

    def test_nearest_prior_limit_is_recorded_and_consecutive_boards_are_not_required(self):
        series = self.series(prior=(12, 24))
        result = self.module.calculate_limit_up_gap_flags('000001.SZ', series)
        self.assertEqual(result['prev_limit_up_date_30d'], series['dates'][24])
        self.assertEqual(result['limit_up_gap_yin_lte1_30d'], 1)
        consecutive = self.series(prior=(33,))
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', consecutive)['limit_up_gap_yin_count_30d'], 0)

    def test_twenty_and_thirty_percent_boards_use_existing_stock_rules(self):
        for stock in ('300001.SZ', '688001.SH', '920001.BJ'):
            series = self.series(stock=stock, yin=(27,))
            self.assertEqual(self.module.calculate_limit_up_gap_flags(stock, series)['limit_up_gap_yin_lte1_30d'], 1)

    def test_invalid_intermediate_candle_is_not_reported_as_no_yin(self):
        series = self.series()
        series['opens'][28] = 0
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', series)['limit_up_gap_yin_lte1_30d'], 0)

    def test_one_cent_below_the_limit_is_not_a_limit_up_for_the_new_marker(self):
        current = self.series()
        current['closes'][-1] -= .01
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', current)['limit_up_gap_yin_lte1_30d'], 0)
        prior = self.series(prior=())
        prior['closes'][24] = 10.99
        self.assertEqual(self.module.calculate_limit_up_gap_flags('000001.SZ', prior)['limit_up_gap_yin_lte1_30d'], 0)

    def xiangyang_series(self):
        payload = json.loads((ROOT / 'tests' / 'fixtures' / 'xiangyang_20261022.json').read_text(encoding='utf-8'))
        rows = [dict(zip(payload['columns'], row)) for row in payload['rows']]
        series = {'dates': [row['trade_date'] for row in rows], 'pre_close': rows[-1]['pre_close']}
        for field, column in (('opens', 'open'), ('highs', 'high'), ('lows', 'low'), ('closes', 'close')):
            series[field] = [row[column] for row in rows]
        return series

    def test_xiangyang_enters_on_eighth_route_without_matching_the_original_seven(self):
        series = self.xiangyang_series()
        original = deepcopy(series)
        result = self.module.build_pz1_result('000678.SZ', 'sample', series, '20261022', 'fixed')
        self.assertIsNotNone(result)
        self.assertEqual(result['match_reasons'], ['limit_up_gap_yin_lte1_30d'])
        self.assertEqual(result['yin_count_20'], 8)
        self.assertEqual(result['high_gain_10d_count'], 3)
        self.assertEqual(result['prev_limit_up_date_30d'], '20261016')
        self.assertEqual(result['limit_up_gap_yin_count_30d'], 1)
        self.assertEqual(series, original)

    def test_candidate_keeps_xiangyang_even_without_the_reference_candle(self):
        series = self.xiangyang_series()
        history = {key: value[:-1] if isinstance(value, list) else value for key, value in series.items()}
        original = deepcopy(history)
        self.assertTrue(self.module.is_pz1_history_candidate(history, '20261022', '000678.SZ'))
        self.assertEqual(history, original)
        self.module.g_history_data = {'000678.SZ': history}
        self.module.LOG_ENABLED = False
        self.assertIn('000678.SZ', self.module.ensure_pz1_candidate_cache('20261022'))

    def test_eighth_route_does_not_admit_two_yin_or_an_unsealed_reference(self):
        two_yin = self.xiangyang_series()
        two_yin['opens'][-3] = 11.3
        self.assertFalse(self.module.is_pz1_history_candidate(two_yin, '20261022', '000678.SZ'))
        self.assertIsNone(self.module.build_pz1_result('000678.SZ', 'sample', two_yin, '20261022', 'fixed'))
        not_limit = self.xiangyang_series()
        not_limit['closes'][-1] -= .01
        self.assertIsNone(self.module.build_pz1_result('000678.SZ', 'sample', not_limit, '20261022', 'fixed'))

    def test_existing_matches_keep_old_values_except_the_new_pz1_match_reason(self):
        source = (ROOT / 'tests' / 'fixtures' / 'panzhong_before_limit_gap.py').read_bytes()
        baseline = types.ModuleType('panzhong_before_limit_gap')
        baseline.__file__ = str(ROOT.parent / '盘中.py')
        exec(compile(source, baseline.__file__, 'exec'), baseline.__dict__)
        baseline.now_text = self.module.now_text = lambda now=None: '2026-10-27 10:00:00'
        for series in (self.series(), self.series(yin=(27, 30)), self.series(prior=(), current_limit=False)):
            metrics = self.module.calculate_window_metrics(series['opens'], series['highs'], series['lows'], series['closes'])
            args = ('000001.SZ', 'sample', series, series['dates'][-1], '2026-10-27 10:00:00')
            for name in ('build_pz1_result', 'build_qy1_result', 'build_qy2_result'):
                before = getattr(baseline, name)(*args, metrics=metrics)
                after = getattr(self.module, name)(*args, metrics=metrics)
                self.assertEqual(before is None, after is None)
                if after is not None:
                    self.assertTrue(NEW_FIELDS <= set(after), name)
                    if name == 'build_pz1_result' and after['limit_up_gap_yin_lte1_30d']:
                        before = dict(before)
                        before['match_reasons'] = before['match_reasons'] + ['limit_up_gap_yin_lte1_30d']
                    self.assertEqual(before, {key: value for key, value in after.items() if key not in NEW_FIELDS}, name)
            before, _, _ = baseline.evaluate_candidate(*args[:3], {}, *args[3:], metrics=metrics)
            after, _, _ = self.module.evaluate_candidate(*args[:3], {}, *args[3:], metrics=metrics)
            self.assertEqual(before is None, after is None)
            if after is not None:
                self.assertTrue(NEW_FIELDS <= set(after))
                self.assertEqual(before, {key: value for key, value in after.items() if key not in NEW_FIELDS})


class TickDateTests(unittest.TestCase):
    def setUp(self):
        self.module = runner_module().load_strategy(ROOT.parent / '盘中.py')

    def test_live_milliseconds_are_not_interpreted_as_year_1793(self):
        for value in (1793030400000, 1793071857000, '1793071857000'):
            self.assertEqual(self.module.tick_trade_date({'time': value}), '20261027')

    def test_seconds_microseconds_and_nanoseconds_use_china_trade_date(self):
        seconds = datetime(2026, 10, 27, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
        for scale in (1, 1000, 1000000, 1000000000):
            self.assertEqual(self.module.tick_trade_date({'time': int(seconds * scale)}), '20261027')

    def test_explicit_calendar_formats_and_fallback_remain_supported(self):
        for value in ('20261027', '20261027113057', '20261027113057000', '2026-10-27 11:30:57'):
            self.assertEqual(self.module.tick_trade_date({'time': value}), '20261027')
        self.assertEqual(self.module.tick_trade_date({'time': 'bad', 'timetag': '20261027 11:30:57'}), '20261027')
        self.assertEqual(self.module.tick_trade_date({}), '')

    def test_ladder_keeps_valid_quote_with_a_millisecond_time_field(self):
        module = self.module
        module.g_ladder_pool = ['000001.SZ']
        module.g_ladder_details_date = '20261027'
        module.g_ladder_details = {'000001.SZ': {'InstrumentName': 'sample', 'PreClose': 10,
                                               'UpStopPrice': 11, 'OpenDate': '20000101'}}
        module.g_ladder_history_date = '20261027'
        module.g_ladder_history = {'000001.SZ': [{'date': '20261026', 'open': 10, 'close': 10, 'pre_close': 10}]}
        tick = {'time': 1793071857000, 'timetag': '20261027 11:30:57', 'lastPrice': 11,
                'lastClose': 10, 'open': 10, 'high': 11, 'low': 10}
        rows, info = module.build_limit_up_ladder(object(), {'000001.SZ': tick}, '20261027', '2026-10-27 11:30:57')
        self.assertEqual(info['missing_quote_count'], 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['consecutive_limit_up_days'], 1)


if __name__ == '__main__':
    unittest.main()
