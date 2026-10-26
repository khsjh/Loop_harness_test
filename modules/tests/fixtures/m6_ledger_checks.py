"""Protected independent tests and a bounded, CPU-active randomized stress trial."""
import argparse
import json
import random
import sys
import time
import unittest

from modules.ledger import summarize


class LedgerTests(unittest.TestCase):
    def test_empty_and_single_pass(self):
        self.assertEqual(summarize(iter(["", " \t\n"])), [])
        self.assertEqual(summarize(iter(['{"id":"1","account":"a","amount":-5}'])),
                         [{"account": "a", "count": 1, "total": -5, "min": -5, "max": -5}])

    def test_iterable_is_consumed_only_once(self):
        class OnePass:
            consumed = False

            def __iter__(self):
                if self.consumed:
                    raise AssertionError("input iterated more than once")
                self.consumed = True
                yield '{"id":"1","account":"a","amount":2}'
                yield '{"id":"2","account":"a","amount":3}'

        self.assertEqual(summarize(OnePass()), [
            {"account": "a", "count": 2, "total": 5, "min": 2, "max": 3}])

    def check_decimal_boundary(self, digit_limit, exponent):
        # Build JSON as text: json.dumps(10**5000) itself hits Python's limit.
        # Arithmetic constructs the independent expected value without int(text).
        original = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(digit_limit)
            decimal = "1" + "0" * exponent
            value = 10 ** exponent
            positive = '{"id":"p","account":"a","amount":' + decimal + '}'
            negative = '{"id":"n","account":"a","amount":-' + decimal + '}'
            expected = [{"account": "a", "count": 2, "total": 0,
                         "min": -value, "max": value}]
            # Avoid a failure formatter trying to render a giant integer.
            self.assertTrue(summarize(iter([positive, negative, positive])) == expected,
                            "large signed amounts or duplicate handling differ")
            with self.assertRaises(ValueError):
                summarize(iter([positive, positive.replace(decimal, decimal[:-1] + "1")]))
            self.assertEqual(sys.get_int_max_str_digits(), digit_limit,
                             "summarize must not alter the interpreter's digit limit")
        finally:
            sys.set_int_max_str_digits(original)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"), "Python has no digit-limit API")
    def test_more_than_4300_decimal_digits(self):
        self.check_decimal_boundary(4300, 5000)

    @unittest.skipUnless(hasattr(sys, "set_int_max_str_digits"), "Python has no digit-limit API")
    def test_minimum_configurable_decimal_limit(self):
        self.check_decimal_boundary(sys.int_info.str_digits_check_threshold, 1200)

    def test_order_duplicates_and_large_integers(self):
        records = [{"id": "z", "account": "β", "amount": 10**30},
                   {"id": "a", "account": "a", "amount": -10},
                   {"id": "b", "account": "a", "amount": 2}]
        self.assertEqual(summarize(map(json.dumps, records + records)), [
            {"account": "a", "count": 2, "total": -8, "min": -10, "max": 2},
            {"account": "β", "count": 1, "total": 10**30, "min": 10**30, "max": 10**30}])

    def test_invalid_records_and_conflicting_ids(self):
        invalid = ["{", "null", "[]", "false", "3", '{}',
            '{"id":"a","account":"b","amount":true}',
            '{"id":"a","account":"b","amount":1.5}',
            '{"id":"a","account":"b","amount":NaN}',
            '{"id":"a","account":"b","amount":1,"extra":2}',
            '{"id":"a","account":"b","amount":1,"amount":2}',
            '{"id":" ","account":"b","amount":1}',
            '{"id":"a","account":null,"amount":1}']
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                summarize(iter([value]))
        with self.assertRaises(ValueError):
            summarize(iter(['{"id":"a","account":"b","amount":1}',
                            '{"id":"a","account":"b","amount":2}']))

    def test_preserve_nonempty_account_spelling(self):
        self.assertEqual(summarize(iter(['{"id":"a","account":" a ","amount":0}'])),
                         [{"account": " a ", "count": 1, "total": 0, "min": 0, "max": 0}])


def randomized_trial(seconds):
    rng = random.Random(20261002)
    start = time.monotonic()
    batches = records_checked = 0
    while time.monotonic() - start < seconds:
        records, buckets = [], {}
        for i in range(rng.randrange(1, 400)):
            account = rng.choice(["a", "b", "한글", " a ", "β"])
            amount = rng.randrange(-10**20, 10**20)
            row = {"id": str(i), "account": account, "amount": amount}
            records.append(row)
            buckets.setdefault(account, []).append(amount)
            if rng.random() < .2:
                records.append(dict(row))
        rng.shuffle(records)
        expected = [{"account": key, "count": len(values), "total": sum(values),
                     "min": min(values), "max": max(values)} for key, values in sorted(buckets.items())]
        actual = summarize(json.dumps(row, ensure_ascii=False) for row in records)
        if actual != expected:
            raise AssertionError(f"randomized mismatch in batch {batches}")
        # Also exercise duplicate conflict detection on each generated batch.
        altered = dict(records[0], amount=records[0]["amount"] + 1)
        try:
            summarize(map(json.dumps, [records[0], altered]))
        except ValueError:
            pass
        else:
            raise AssertionError("conflicting duplicate accepted")
        batches += 1
        records_checked += len(records)
        if batches % 10000 == 0:
            print(json.dumps({"batches": batches, "records": records_checked,
                              "elapsed_seconds": time.monotonic() - start}), flush=True)
    print(json.dumps({"batches": batches, "records": records_checked,
                      "elapsed_seconds": time.monotonic() - start, "randomized_checks": "passed"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stress-seconds", type=int, default=0, choices=range(0, 7201))
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LedgerTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    randomized_trial(args.stress_seconds)
