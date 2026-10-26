"""Protected behavioral checks for the M6 settings merge task (no model calls)."""
import copy
import unittest

from modules.settings_merge import merge_settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.defaults = {"enabled": True, "retries": 3, "label": "default", "tags": ["base"]}

    def test_defaults_and_independent_result(self):
        result = merge_settings(self.defaults, {}, {})
        self.assertEqual(result, self.defaults)
        self.assertIsNot(result, self.defaults)
        self.assertIsNot(result["tags"], self.defaults["tags"])

    def test_file_then_environment_precedence(self):
        result = merge_settings(self.defaults,
            {"enabled": False, "retries": 5, "label": "file", "tags": ["file"]},
            {"APP_RETRIES": "7", "APP_LABEL": "env", "APP_TAGS": '["env", "한글"]'})
        self.assertEqual(result, {"enabled": False, "retries": 7,
                                  "label": "env", "tags": ["env", "한글"]})

    def test_environment_enabled_overrides_opposite_file_value(self):
        for file_value, env_value in ((False, "true"), (True, "false")):
            with self.subTest(file_value=file_value, env_value=env_value):
                result = merge_settings(self.defaults, {"enabled": file_value},
                                        {"APP_ENABLED": env_value})
                self.assertIs(result["enabled"], env_value == "true")

    def test_result_has_required_types(self):
        for env in ({}, {"APP_ENABLED": "false", "APP_RETRIES": "0"},
                    {"APP_ENABLED": "true", "APP_RETRIES": "1"}):
            with self.subTest(env=env):
                result = merge_settings(self.defaults, {}, env)
                self.assertIs(type(result), dict)
                self.assertIs(type(result["enabled"]), bool)
                self.assertIs(type(result["retries"]), int)
                self.assertIsInstance(result["label"], str)
                self.assertIsInstance(result["tags"], list)
                self.assertTrue(all(isinstance(tag, str) for tag in result["tags"]))

    def test_file_false_zero_empty_are_present_values(self):
        empty = {"enabled": False, "retries": 0, "label": "", "tags": []}
        self.assertEqual(merge_settings(self.defaults, empty, {}), empty)

    def test_environment_false_zero_empty_are_present_values(self):
        result = merge_settings(self.defaults, {},
            {"APP_ENABLED": "false", "APP_RETRIES": "0", "APP_LABEL": "", "APP_TAGS": "[]"})
        self.assertEqual(result, {"enabled": False, "retries": 0, "label": "", "tags": []})

    def test_valid_boundaries_spelling_and_unrelated_environment(self):
        result = merge_settings(self.defaults, {"retries": 10},
            {"APP_ENABLED": "true", "APP_LABEL": "  한글  ", "PATH": "/ignored",
             "APP_TAGS": '["", " x ", "x", "x"]'})
        self.assertEqual(result, {"enabled": True, "retries": 10,
                                  "label": "  한글  ", "tags": ["", " x ", "x", "x"]})
        self.assertEqual(merge_settings(self.defaults, {}, {"APP_RETRIES": "10"})["retries"], 10)

    def test_reject_non_dict_inputs(self):
        for index in range(3):
            for invalid in (None, [], "", 0, False):
                args = [self.defaults, {}, {}]
                args[index] = invalid
                with self.subTest(index=index, invalid=invalid), self.assertRaises(ValueError):
                    merge_settings(*args)

    def test_reject_dict_subclasses_at_every_input(self):
        class DictSubclass(dict):
            pass

        for index in range(3):
            args = [self.defaults, {}, {}]
            args[index] = DictSubclass(args[index])
            with self.subTest(index=index), self.assertRaises(ValueError):
                merge_settings(*args)

    def test_reject_non_string_environment_keys(self):
        for key in (None, 1, ("APP_ENABLED",)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                merge_settings(self.defaults, {}, {key: "true"})

    def test_reject_missing_and_unknown_keys(self):
        for key in self.defaults:
            incomplete = dict(self.defaults)
            del incomplete[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                merge_settings(incomplete, {}, {})
        for args in ((dict(self.defaults, extra=1), {}, {}),
                     (self.defaults, {"extra": 1}, {}),
                     (self.defaults, {}, {"APP_UNKNOWN": "1"})):
            with self.subTest(args=args), self.assertRaises(ValueError):
                merge_settings(*args)

    def test_reject_invalid_config_values_even_when_shadowed(self):
        invalids = {"enabled": [0, 1, "false", None],
                    "retries": [True, False, -1, 11, 1.0, "2", None],
                    "label": [None, 0, False, []],
                    "tags": [None, "abc", ("x",), [1], [False], ["a", None]]}
        env = {"APP_ENABLED": "true", "APP_RETRIES": "2", "APP_LABEL": "ok", "APP_TAGS": "[]"}
        for key, values in invalids.items():
            for value in values:
                for layer in ("defaults", "file"):
                    args = [dict(self.defaults), {}, env]
                    args[0 if layer == "defaults" else 1][key] = value
                    with self.subTest(key=key, value=value, layer=layer), self.assertRaises(ValueError):
                        merge_settings(*args)

    def test_reject_invalid_environment_values(self):
        invalids = {"APP_ENABLED": ["TRUE", "False", "1", " false", "", False],
                    "APP_RETRIES": ["-1", "11", "+1", "01", "1.0", " 1", "1\n", "١", "", 1],
                    "APP_LABEL": [None, 1, False],
                    "APP_TAGS": ["", "{", "null", '"x"', "{}", "[1]", "[true]", ["x"]]}
        for key, values in invalids.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    merge_settings(self.defaults, {}, {key: value})

    def test_success_does_not_mutate_inputs_or_alias_lists(self):
        for file in ({}, {"tags": ["file"]}):
            env = {"APP_LABEL": "env"}
            before = copy.deepcopy((self.defaults, file, env))
            result = merge_settings(self.defaults, file, env)
            self.assertEqual((self.defaults, file, env), before)
            result["tags"].append("result-only")
            self.assertEqual((self.defaults, file, env), before)
            original_result = copy.deepcopy(result)
            self.defaults["tags"].append("input-only")
            if "tags" in file:
                file["tags"].append("input-only")
            self.assertEqual(result, original_result)

    def test_failure_does_not_mutate_inputs(self):
        file, env = {"tags": ["file"], "label": "file"}, {"APP_RETRIES": "bad"}
        before = copy.deepcopy((self.defaults, file, env))
        with self.assertRaises(ValueError):
            merge_settings(self.defaults, file, env)
        self.assertEqual((self.defaults, file, env), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
