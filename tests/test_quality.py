"""AEO 체커 자동 검증 (API 호출 없음).

실행:  python -m unittest discover -s tests -v

주간 실행(GitHub Actions) 전에 자동으로 돌아서, 브랜드 감지·에러/잘림 판별·집계·재시도 로직이 깨졌거나
저장된 history.json이 어긋나 있으면 API 비용을 쓰기 전에 실행을 멈춘다.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
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


OPENAI_BILLING = "Error code: 429 - {'error': {'message': 'You have no credits remaining. Add credits to continue using the API', 'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}}"
ANTHROPIC_BILLING = "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Your credit balance is too low to access the Anthropic API.'}}"
GEMINI_QUOTA = "429 RESOURCE_EXHAUSTED. {'error': {'message': 'You exceeded your current quota', 'status': 'RESOURCE_EXHAUSTED'}}"


class BillingError(unittest.TestCase):
    def test_real_billing_messages_are_detected(self):
        self.assertTrue(ac.is_billing_error(Exception(OPENAI_BILLING)))
        self.assertTrue(ac.is_billing_error(Exception(ANTHROPIC_BILLING)))

    def test_billing_is_not_transient_even_though_http_429(self):
        e = ApiError(429, OPENAI_BILLING)
        self.assertTrue(ac.is_billing_error(e))
        self.assertFalse(ac.is_transient_error(e))

    def test_gemini_rate_quota_is_still_retried(self):
        e = ApiError(429, GEMINI_QUOTA)
        self.assertFalse(ac.is_billing_error(e))
        self.assertTrue(ac.is_transient_error(e))

    def run_engine(self, fn, engine="ChatGPT", prompts=10):
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

    def test_billing_error_is_not_retried(self):
        def fn(n):
            raise ApiError(429, OPENAI_BILLING)
        results, calls, sleeps = self.run_engine(fn, prompts=1)
        self.assertEqual((calls, sleeps), (1, []))
        self.assertTrue(results["p1"]["billing"])

    def test_remaining_prompts_are_skipped_after_billing_error(self):
        def fn(n):
            if n == 2:
                raise ApiError(429, OPENAI_BILLING)
            return GOOD
        results, calls, _ = self.run_engine(fn)
        self.assertEqual(calls, 2)                       # 2번째에서 멈추고 나머지 8개는 호출하지 않음
        self.assertTrue(all(results[p["id"]].get("billing") for p in ac.PROMPTS[1:]))
        self.assertFalse(results["p1"].get("billing"))

    def test_main_reports_billing_as_a_problem_with_link(self):
        tmp = tempfile.mkdtemp()
        cwd = Path.cwd()
        try:
            os.chdir(tmp)
            def boom(q):
                raise ApiError(429, OPENAI_BILLING)
            with mock.patch.dict(ac.API_KEYS, {"openai": "x", "anthropic": "", "gemini": ""}), \
                    mock.patch.dict(ac.CALL_FN, {"ChatGPT": boom}), \
                    mock.patch.object(ac.time, "sleep", lambda s: None), \
                    mock.patch.object(ac.sys, "argv", ["aeo_checker.py", "--project=tryground"]), \
                    contextlib.redirect_stdout(io.StringIO()) as buf:
                with self.assertRaises(SystemExit) as cm:
                    ac.main()
            self.assertEqual(cm.exception.code, 1)
            status = json.loads(Path("last_run_status.json").read_text(encoding="utf-8"))
            problems = status["projects"]["tryground"]["problems"]
            self.assertTrue(any("잔액 부족" in p and "platform.openai.com" in p for p in problems), problems)
            self.assertIn("::error", buf.getvalue())
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)


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

    def proj(self, label="트라이그라운드", out="aeo_results", history=None, warnings=None, problems=None):
        return {"label": label, "output_dir": out, "history": history or [], "warnings": warnings or [], "problems": problems or []}

    def test_includes_all_engines_and_rates(self):
        payload = ac.build_slack_payload("2026-09-21", "check", [self.proj(history=self.make_history())])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("2026-09-21", text)
        for eng in ac.ENGINES:
            self.assertIn(eng, text)
        self.assertIn("100%", text)          # Claude: 전부 BRAND 응답이라 멘션률 100%
        self.assertIn("전부 오류", text)      # ChatGPT: total=0

    def test_problems_and_warnings_are_rendered(self):
        payload = ac.build_slack_payload("2026-09-21", "check", [self.proj(warnings=["측정 조건이 바뀜"], problems=["Gemini: 유효 응답 부족"])])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("문제", text)
        self.assertIn("Gemini: 유효 응답 부족", text)
        self.assertIn("주의", text)
        self.assertIn("측정 조건이 바뀜", text)
        self.assertIn("문제 있음", payload["text"])   # 문제가 있으면 fallback 텍스트에도 반영

    def test_no_issues_gives_clean_fallback_text(self):
        payload = ac.build_slack_payload("2026-09-21", "check", [self.proj()])
        self.assertIn("정상", payload["text"])

    def test_rebuild_only_mode_has_different_title(self):
        payload = ac.build_slack_payload("2026-09-21", "rebuild-only", [self.proj()])
        self.assertIn("재빌드", payload["blocks"][0]["text"]["text"])

    def test_one_message_covers_every_project_and_worst_status_wins(self):
        payload = ac.build_slack_payload("2026-09-21", "check", [
            self.proj(label="트라이그라운드"),
            self.proj(label="스페이스업", out="spaceup_results", problems=["ChatGPT 잔액 부족"]),
        ])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(sum(1 for b in payload["blocks"] if b["type"] == "header"), 1)
        self.assertIn("트라이그라운드", text)
        self.assertIn("스페이스업", text)
        self.assertIn("divider", [b["type"] for b in payload["blocks"]])
        self.assertIn("문제 있음", payload["text"])   # 한 프로젝트만 문제여도 전체 요약은 '문제 있음'

    def test_links_included_only_with_github_env(self):
        # GitHub Actions 안에서 이 테스트를 돌리면 GITHUB_REPOSITORY 등이 이미 채워져 있으므로,
        # '환경변수가 없는 경우'를 실제로 재현하려면 명시적으로 지워야 한다.
        env_keys = ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL")
        projects = [self.proj(), self.proj(label="스페이스업", out="spaceup_results")]
        with mock.patch.dict(ac.os.environ, {k: "" for k in env_keys}):
            without = ac.build_slack_payload("2026-09-21", "check", projects)
        self.assertNotIn("context", [b["type"] for b in without["blocks"]])

        with mock.patch.dict(ac.os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "123", "GITHUB_SERVER_URL": "https://github.com"}):
            with_env = ac.build_slack_payload("2026-09-21", "check", projects)
        ctx = next(b for b in with_env["blocks"] if b["type"] == "context")["elements"][0]["text"]
        self.assertIn("htmlpreview.github.io", ctx)
        self.assertIn("aeo_results/index.html", ctx)        # 프로젝트마다 자기 대시보드 링크
        self.assertIn("spaceup_results/index.html", ctx)
        self.assertIn("actions/runs/123", ctx)

    def test_payload_is_json_serializable(self):
        payload = ac.build_slack_payload("2026-09-21", "check", [self.proj(history=self.make_history(), warnings=["w"], problems=["p"])])
        json.dumps(payload)   # 예외 없이 직렬화되어야 curl로 그대로 보낼 수 있음


class MultiProject(unittest.TestCase):
    def tearDown(self):
        ac.load_project("tryground")

    def test_registry_has_both_projects_with_unique_prompts(self):
        self.assertEqual(set(ac.PROJECTS), {"tryground", "spaceup"})
        self.assertEqual(len(ac.PROJECTS["tryground"]["prompts"]), 10)
        self.assertEqual(len(ac.PROJECTS["spaceup"]["prompts"]), 9)
        for key, cfg in ac.PROJECTS.items():
            ids = [p["id"] for p in cfg["prompts"]]
            self.assertEqual(len(set(ids)), len(ids), key)
            self.assertTrue(all(p["area"] in cfg["areas"] for p in cfg["prompts"]), key)
        out_dirs = [cfg["output_dir"] for cfg in ac.PROJECTS.values()]
        self.assertEqual(len(set(out_dirs)), len(out_dirs))   # 프로젝트끼리 결과 폴더가 겹치면 history가 섞인다

    def test_spaceup_prompts_match_requested_questions(self):
        qs = [p["q"] for p in ac.PROJECTS["spaceup"]["prompts"]]
        self.assertEqual(qs[0], "공간대여 예약 관리 프로그램 추천해줘")
        self.assertEqual(qs[-1], "연습실 창업 준비 중인데 운영 자동화 어떻게 해야 돼?")
        # 브랜드를 직접 묻는 질문은 노출도를 부풀리므로 넣지 않는다
        ac.load_project("spaceup")
        self.assertEqual([q for q in qs if ac.has_brand(q)], [])
        self.assertNotIn("sp9", [p["id"] for p in ac.PROJECTS["spaceup"]["prompts"]])

    def test_import_default_is_tryground(self):
        self.assertEqual(ac.PROJECT_KEY, "tryground")
        self.assertEqual(ac.OUTPUT_DIR, "aeo_results")

    def test_load_project_switches_brand_detection_and_link_rules(self):
        ac.load_project("spaceup")
        self.assertTrue(ac.has_brand("1. 스페이스업 - 특징: 통합 예약 캘린더"))
        self.assertTrue(ac.has_brand("이전 이름은 공간비서입니다"))
        self.assertFalse(ac.has_brand("1. 트라이그라운드 낙성대점"))            # 다른 프로젝트 브랜드는 안 잡힘
        self.assertFalse(ac.has_brand("1. 스페이스클라우드 - 공간 예약 플랫폼"))  # '스페이스' 들어간 경쟁사 오탐 방지
        linked = "1. 스페이스업 | URL: https://www.space-up.co.kr/\n" + "설명 " * 60
        self.assertEqual(ac.parse_result(linked)["citationType"], "홈페이지 링크")
        ac.load_project("tryground")
        self.assertTrue(ac.has_brand("1. 트라이그라운드 낙성대점"))
        self.assertEqual(ac.parse_result(BRAND)["citationType"], "홈페이지 링크")

    def run_main(self, argv):
        tmp = tempfile.mkdtemp()
        cwd = Path.cwd()
        try:
            os.chdir(tmp)
            seen = []

            def fake(q):
                seen.append((ac.PROJECT_KEY, q))
                brand = "스페이스업" if ac.PROJECT_KEY == "spaceup" else "트라이그라운드"
                return f"1. {brand} - 특징: 예시 | URL: https://example.com\n" + "설명 " * 60

            with mock.patch.dict(ac.API_KEYS, {"openai": "", "anthropic": "x", "gemini": ""}), \
                    mock.patch.dict(ac.CALL_FN, {"Claude": fake}), \
                    mock.patch.object(ac.time, "sleep", lambda s: None), \
                    mock.patch.object(ac.sys, "argv", ["aeo_checker.py", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()):
                ac.main()
            dirs = {d: Path(d, "history.json").exists() for d in ("aeo_results", "spaceup_results")}
            status = json.loads(Path("last_run_status.json").read_text(encoding="utf-8"))
            hist = {d: json.loads(Path(d, "history.json").read_text(encoding="utf-8")) for d, ok in dirs.items() if ok}
            return seen, dirs, status, hist
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_run_checks_both_projects_into_separate_folders(self):
        seen, dirs, status, hist = self.run_main([])
        self.assertEqual(dirs, {"aeo_results": True, "spaceup_results": True})
        self.assertEqual(set(status["projects"]), {"tryground", "spaceup"})
        # 각 프로젝트는 자기 질문만 보냈다
        sp_qs = {p["q"] for p in ac.SPACEUP_PROMPTS}
        self.assertTrue(all((key == "spaceup") == (q in sp_qs) for key, q in seen))
        self.assertEqual(len(seen), len(ac.TRYGROUND_PROMPTS) + len(ac.SPACEUP_PROMPTS))
        # 각 history에는 자기 프롬프트 id만 있고, 자기 브랜드로 멘션이 잡혔다
        sp = hist["spaceup_results"][0]
        self.assertEqual(set(sp["detail"]), {p["id"] for p in ac.SPACEUP_PROMPTS})
        self.assertEqual(sp["mention_rate"], 100.0)
        self.assertEqual(set(hist["aeo_results"][0]["detail"]), {p["id"] for p in ac.TRYGROUND_PROMPTS})
        self.assertEqual(ac.PROJECT_KEY, "tryground")   # main()이 끝나면 원래 프로젝트로 복원

    def test_project_flag_runs_only_that_project(self):
        seen, dirs, status, _ = self.run_main(["--project=spaceup"])
        self.assertEqual(dirs, {"aeo_results": False, "spaceup_results": True})
        self.assertEqual(set(status["projects"]), {"spaceup"})
        self.assertTrue(all(key == "spaceup" for key, _ in seen))

    def test_unknown_project_is_rejected(self):
        with mock.patch.object(ac.sys, "argv", ["aeo_checker.py", "--project=bizspot"]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ac.main()
        self.assertEqual(cm.exception.code, 2)


class KstDate(unittest.TestCase):
    """주간 실행은 월요일 새벽(KST) = UTC 일요일 밤에 돈다. 기록 날짜는 월요일이어야 한다."""

    def test_sunday_night_utc_is_monday_in_kst(self):
        from datetime import datetime as real_dt, timezone

        class FakeDatetime(real_dt):
            @classmethod
            def now(cls, tz=None):
                utc = real_dt(2026, 11, 8, 20, 37, tzinfo=timezone.utc)  # 일요일 20:37 UTC
                return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)

        with mock.patch.object(ac, "datetime", FakeDatetime):
            self.assertEqual(ac.today_kst(), "2026-11-09")  # 월요일

    def test_weekly_cron_lands_before_9am_kst(self):
        wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "aeo-check.yml").read_text(encoding="utf-8")
        import re
        minute, hour, _, _, dow = re.search(r"cron: '([^']+)'", wf).group(1).split()
        kst_hour = (int(hour) + 9) % 24
        kst_dow = (int(dow) + (int(hour) + 9) // 24) % 7
        self.assertEqual(kst_dow, 1, "KST 기준 월요일에 시작해야 함")
        self.assertLessEqual(kst_hour, 6, "지연(3~4시간)을 감안해 KST 06시 이전에 시작해야 9시 전에 결과가 옴")


if __name__ == "__main__":
    unittest.main()
