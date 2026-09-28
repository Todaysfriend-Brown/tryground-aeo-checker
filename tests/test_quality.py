"""AEO 체커 자동 검증 (API 호출 없음).

실행:  python -m unittest discover -s tests -v

주간 실행(GitHub Actions) 전에 자동으로 돌아서, 브랜드 감지·에러/잘림 판별·집계·재시도 로직이 깨졌거나
저장된 history.json이 어긋나 있으면 API 비용을 쓰기 전에 실행을 멈춘다.
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aeo_checker as ac  # noqa: E402

GOOD = "1. 위워크 - 위치: 홍대 | URL: https://wework.com\n" + "상세 설명입니다. " * 30
BRAND = "1. **트라이그라운드 낙성대점** - 위치: 낙성대 | URL: https://tryground.co.kr\n" + "상세 설명입니다. " * 30
TRUNCATED = "영등포 지역에서 소호사무실을 찾으시는군요! 웹 검색을 통해 현재 운영 중인"   # 실제 Gemini 잘림 응답과 같은 형태(41자)


class ApiError(Exception):
    """OpenAI/Anthropic 스타일(.status_code) 또는 google-genai 스타일(.code) 예외 흉내"""
    def __init__(self, status, text=""):
        super().__init__(text or f"error {status}")
        self.status_code = status
        self.code = status


def entry_for(date, engine, results, settings=None):
    e = {"date": date, "engine": engine, **ac.stats_fields(ac.calc_stats(results)), "detail": results}
    if settings is not None:
        e["settings"] = settings
    return e


def all_results(raw):
    return {p["id"]: ac.parse_result(raw) for p in ac.PROMPTS}


class BrandDetection(unittest.TestCase):
    def test_full_name_and_english_case_insensitive(self):
        self.assertTrue(ac.has_brand("1. 트라이그라운드 낙성대점"))
        self.assertTrue(ac.has_brand("TRYGROUND 홍대점"))
        self.assertTrue(ac.has_brand("visit tryground.co.kr"))

    def test_short_alias_as_own_word(self):
        self.assertTrue(ac.has_brand("트그 홍대점을 추천합니다"))
        self.assertTrue(ac.has_brand("트그는 합정역 근처입니다"))   # 조사가 붙어도 인정

    def test_short_alias_inside_other_word_is_not_a_hit(self):
        self.assertFalse(ac.has_brand("1. 퍼스트그룹 공유오피스"))
        self.assertFalse(ac.has_brand("웨스트그룹 강남점"))

    def test_no_brand(self):
        self.assertFalse(ac.has_brand(GOOD))

    def test_citation_type(self):
        self.assertEqual(ac.parse_result(BRAND)["citationType"], "홈페이지 링크")
        text_only = "1. 트라이그라운드 - 위치: 낙성대\n" + "상세 설명입니다. " * 30
        self.assertEqual(ac.parse_result(text_only)["citationType"], "텍스트만 언급")
        self.assertEqual(ac.parse_result(GOOD)["citationType"], "미언급")


class ErrorClassification(unittest.TestCase):
    def test_real_world_error_texts_are_invalid(self):
        samples = [
            "Error code: 404 - {'error': {'message': 'The model `gpt-4o-mini-search-preview` has been deprecated'}}",
            "Error code: 400 - {'type': 'error', 'error': {'message': 'Your credit balance is too low'}}",
            "404 NOT_FOUND. {'error': {'message': 'models/gemini-3-flash is not found'}}",
            "429 RESOURCE_EXHAUSTED. {'error': {'message': 'You exceeded your current quota'}}",
        ]
        for raw in samples:
            self.assertTrue(ac.is_error_result({"raw": raw, "mentioned": False}), raw[:40])

    def test_truncated_and_empty_are_invalid(self):
        self.assertTrue(ac.is_error_result({"raw": TRUNCATED}))
        self.assertTrue(ac.is_error_result({"raw": ""}))
        self.assertTrue(ac.is_error_result({}))

    def test_normal_answers_are_valid(self):
        self.assertFalse(ac.is_error_result({"raw": GOOD}))
        self.assertFalse(ac.is_error_result({"raw": BRAND}))

    def test_explicit_error_flag(self):
        self.assertTrue(ac.is_error_result({"raw": GOOD, "error": True}))


class Stats(unittest.TestCase):
    def test_errors_are_excluded_from_denominator(self):
        results = all_results(GOOD)
        results["p1"] = ac.parse_result(BRAND)                        # 언급 1건
        for pid in ("p2", "p3", "p4", "p5"):
            results[pid] = {"mentioned": False, "raw": "429 RESOURCE_EXHAUSTED. quota", "error": True}
        s = ac.calc_stats(results)
        self.assertEqual((s["total"], s["errors"], s["mentioned"]), (6, 4, 1))
        self.assertAlmostEqual(s["mention_rate"], 1 / 6 * 100)

    def test_too_few_valid_responses_excludes_the_whole_run(self):
        results = {p["id"]: {"mentioned": False, "raw": "Error code: 400 - x", "error": True} for p in ac.PROMPTS}
        results["p1"] = ac.parse_result(BRAND)     # 10개 중 1개만 유효 → 1/1=100% 같은 왜곡 방지
        s = ac.calc_stats(results)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["errors"], 9)

    def test_all_valid(self):
        s = ac.calc_stats(all_results(GOOD))
        self.assertEqual((s["total"], s["errors"], s["mention_rate"]), (10, 0, 0))


class RetryAndPacing(unittest.TestCase):
    def run_engine(self, fn, engine="Claude", prompts=2):
        calls = {"n": 0}

        def wrapped(question):
            calls["n"] += 1
            return fn(calls["n"])

        sleeps = []
        with mock.patch.object(ac.time, "sleep", side_effect=lambda s: sleeps.append(round(s))), \
                mock.patch.dict(ac.CALL_FN, {engine: wrapped}), \
                mock.patch.object(ac, "PROMPTS", ac.PROMPTS[:prompts]), \
                contextlib.redirect_stdout(io.StringIO()):
            results = ac.run_engine(engine)
        return results, calls["n"], sleeps

    def test_429_waits_for_retry_delay_then_succeeds(self):
        class Quota(ApiError):
            def __str__(self):
                return "429 RESOURCE_EXHAUSTED. {'retryDelay': '47s'}"

        def fn(n):
            if n == 1:
                raise Quota(429)
            return GOOD

        results, calls, sleeps = self.run_engine(fn)
        self.assertEqual(sleeps, [49])                      # 47초 + 여유 2초
        self.assertFalse(results["p1"].get("error"))

    def test_non_transient_errors_are_not_retried(self):
        for status in (400, 404):
            def fn(n, status=status):
                raise ApiError(status)
            results, calls, sleeps = self.run_engine(fn)
            self.assertEqual((sleeps, calls), ([], 2), status)
            self.assertTrue(all(r["error"] for r in results.values()))

    def test_persistent_server_error_retries_three_times_then_gives_up(self):
        def fn(n):
            raise ApiError(503)
        results, calls, sleeps = self.run_engine(fn, prompts=1)
        self.assertEqual(sleeps, [10, 30, 60])
        self.assertEqual(calls, 4)
        self.assertTrue(results["p1"]["error"])

    def test_truncated_answer_is_retried_once(self):
        results, calls, _ = self.run_engine(lambda n: TRUNCATED if n == 1 else GOOD, prompts=1)
        self.assertEqual(calls, 2)
        self.assertFalse(results["p1"].get("error"))

    def test_still_truncated_after_retry_is_marked_invalid_and_raw_is_kept(self):
        results, calls, _ = self.run_engine(lambda n: TRUNCATED, prompts=1)
        self.assertEqual(calls, 2)
        self.assertTrue(results["p1"]["error"])
        self.assertEqual(results["p1"]["raw"], TRUNCATED)

    def test_gemini_calls_are_spaced_out(self):
        _, _, sleeps = self.run_engine(lambda n: GOOD, engine="Gemini", prompts=3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(12 <= s <= 13 for s in sleeps), sleeps)


class MeasurementConditions(unittest.TestCase):
    A = {"model": "m1", "max_output_tokens": 1000}
    B = {"model": "m2", "max_output_tokens": 600}

    def test_change_is_detected_per_engine(self):
        h = [
            {"date": "2026-09-05", "engine": "Claude", "settings": self.A},
            {"date": "2026-09-05", "engine": "Gemini", "settings": self.A},
            {"date": "2026-09-15", "engine": "Claude", "settings": self.B},
            {"date": "2026-09-15", "engine": "Gemini", "settings": self.A},
        ]
        ch = ac.build_setting_changes(h)
        self.assertEqual(list(ch), [("2026-09-15", "Claude")])
        self.assertIn("model: m1 → m2", ch[("2026-09-15", "Claude")])

    def test_entries_without_settings_are_skipped_not_flagged(self):
        h = [
            {"date": "2026-09-05", "engine": "Claude"},
            {"date": "2026-09-15", "engine": "Claude", "settings": self.B},
        ]
        self.assertEqual(ac.build_setting_changes(h), {})

    def test_current_settings_cover_every_engine(self):
        self.assertEqual(set(ac.CALL_SETTINGS), set(ac.ENGINES))


class AddEntry(unittest.TestCase):
    def make(self, valid):
        results = all_results(GOOD)
        for pid in list(results)[valid:]:
            results[pid] = {"mentioned": False, "raw": "Error code: 400 - x", "error": True}
        return entry_for("2026-09-21", "Gemini", results)

    def test_new_entry_is_added(self):
        h = []
        self.assertEqual(ac.add_entry(h, self.make(10)), "added")
        self.assertEqual(len(h), 1)

    def test_rerun_with_more_valid_responses_replaces(self):
        h = [self.make(6)]
        self.assertEqual(ac.add_entry(h, self.make(10)), "replaced")
        self.assertEqual((len(h), h[0]["total"]), (1, 10))

    def test_worse_rerun_does_not_overwrite_better_data(self):
        h = [self.make(10)]
        self.assertEqual(ac.add_entry(h, self.make(6)), "kept_old")
        self.assertEqual(h[0]["total"], 10)


class ValidateHistory(unittest.TestCase):
    def clean(self):
        return [entry_for("2026-09-21", "Claude", all_results(GOOD))]

    def test_clean_data_has_no_issues(self):
        self.assertEqual(ac.validate_history(self.clean()), [])

    def test_duplicate_date_engine(self):
        h = self.clean() * 2
        self.assertTrue(any("중복" in i for i in ac.validate_history(h)))

    def test_stored_aggregate_differs_from_recomputed(self):
        h = self.clean()
        h[0]["mentioned"] = 5
        self.assertTrue(any("재계산" in i for i in ac.validate_history(h)))

    def test_missing_prompt(self):
        h = self.clean()
        del h[0]["detail"]["p3"]
        self.assertTrue(any("p3" in i for i in ac.validate_history(h)))

    def test_mention_flag_that_disagrees_with_raw_text(self):
        h = self.clean()
        h[0]["detail"]["p1"]["mentioned"] = True    # 원문엔 브랜드가 없음
        self.assertTrue(any("멘션 판정" in i for i in ac.validate_history(h)))

    def test_bad_date_and_engine(self):
        self.assertTrue(ac.validate_history([{"date": "9/21", "engine": "Claude"}]))
        self.assertTrue(ac.validate_history([{"date": "2026-09-21", "engine": "Bing"}]))


class SlackPayload(unittest.TestCase):
    def make_history(self):
        return [
            entry_for("2026-09-21", "Claude", all_results(BRAND), settings={"model": "claude-haiku-4-5"}),
            {**entry_for("2026-09-21", "ChatGPT", {p["id"]: {"mentioned": False, "raw": "Error code: 400 - x", "error": True} for p in ac.PROMPTS}), "settings": {"model": "gpt-4o-mini"}},
        ]

    def test_includes_all_engines_and_rates(self):
        payload = ac.build_slack_payload({"date": "2026-09-21", "mode": "check", "warnings": [], "problems": []}, self.make_history())
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("2026-09-21", text)
        for eng in ac.ENGINES:
            self.assertIn(eng, text)
        self.assertIn("100%", text)          # Claude: 전부 BRAND 응답이라 멘션률 100%
        self.assertIn("전부 오류", text)      # ChatGPT: total=0

    def test_problems_and_warnings_are_rendered(self):
        status = {"date": "2026-09-21", "mode": "check", "warnings": ["측정 조건이 바뀜"], "problems": ["Gemini: 유효 응답 부족"]}
        payload = ac.build_slack_payload(status, [])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("문제", text)
        self.assertIn("Gemini: 유효 응답 부족", text)
        self.assertIn("주의", text)
        self.assertIn("측정 조건이 바뀜", text)
        self.assertIn("문제 있음", payload["text"])   # 문제가 있으면 fallback 텍스트에도 반영

    def test_no_issues_gives_clean_fallback_text(self):
        payload = ac.build_slack_payload({"date": "2026-09-21", "mode": "check", "warnings": [], "problems": []}, [])
        self.assertIn("정상", payload["text"])

    def test_rebuild_only_mode_has_different_title(self):
        payload = ac.build_slack_payload({"date": "2026-09-21", "mode": "rebuild-only", "warnings": [], "problems": []}, [])
        self.assertIn("재빌드", payload["blocks"][0]["text"]["text"])

    def test_links_included_only_with_github_env(self):
        # GitHub Actions 안에서 이 테스트를 돌리면 GITHUB_REPOSITORY 등이 이미 채워져 있으므로,
        # '환경변수가 없는 경우'를 실제로 재현하려면 명시적으로 지워야 한다.
        env_keys = ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL")
        with mock.patch.dict(ac.os.environ, {k: "" for k in env_keys}):
            without = ac.build_slack_payload({"date": "2026-09-21", "mode": "check"}, [])
        self.assertNotIn("context", [b["type"] for b in without["blocks"]])

        with mock.patch.dict(ac.os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123", "GITHUB_SERVER_URL": "https://github.com"}):
            with_env = ac.build_slack_payload({"date": "2026-09-21", "mode": "check"}, [])
        ctx = next(b for b in with_env["blocks"] if b["type"] == "context")
        self.assertIn("htmlpreview.github.io", ctx["elements"][0]["text"])
        self.assertIn("actions/runs/123", ctx["elements"][0]["text"])

    def test_payload_is_json_serializable(self):
        payload = ac.build_slack_payload({"date": "2026-09-21", "mode": "check", "warnings": ["w"], "problems": ["p"]}, self.make_history())
        json.dumps(payload)   # 예외 없이 직렬화되어야 curl로 그대로 보낼 수 있음


if __name__ == "__main__":
    unittest.main()
