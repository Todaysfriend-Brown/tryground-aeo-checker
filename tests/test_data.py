"""저장된 실제 데이터(각 프로젝트의 history.json) 검증 — 코드 로직이 아니라 '데이터 자체'가 멀쩡한지 본다.

실행:  python -m unittest discover -s tests -p "test_data.py" -v

주간 실행의 사전 테스트(test_quality.py)와 분리했다. 데이터 문제가 있다고 새 측정 자체를 막아 버리면
결측이 더 커지므로, 주간 실행에서는 aeo_checker.py가 같은 검사를 경고(::warning)로만 알리고,
이 테스트는 코드·데이터를 푸시할 때(test.yml) 돌린다.

아직 한 번도 실행되지 않아 history.json이 없는 프로젝트(예: 막 추가된 스페이스업)는 건너뛴다.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aeo_checker as ac  # noqa: E402


def committed_histories():
    """(프로젝트 키, history 리스트) — history.json이 있는 프로젝트만"""
    out = []
    for key, cfg in ac.PROJECTS.items():
        path = ROOT / cfg["output_dir"] / "history.json"
        if path.exists():
            out.append((key, json.loads(path.read_text(encoding="utf-8"))))
    return out


class CommittedData(unittest.TestCase):
    def tearDown(self):
        ac.load_project("tryground")

    def test_at_least_one_project_has_data(self):
        self.assertTrue(committed_histories())

    def test_history_is_internally_consistent(self):
        # 저장된 집계 = 응답 원문 재계산 결과 / 중복 없음 / 프롬프트 누락 없음 / 멘션 판정 = 현재 브랜드 감지 규칙
        for key, history in committed_histories():
            with self.subTest(project=key):
                ac.load_project(key)
                self.assertEqual(ac.validate_history(history), [])

    def test_every_run_records_its_measurement_settings(self):
        # 조건 기록이 없으면 '측정 조건이 바뀌었는지'를 나중에 알 수 없다
        for key, history in committed_histories():
            with self.subTest(project=key):
                missing = [f'{e["date"]} {e["engine"]}' for e in history if not e.get("settings")]
                self.assertEqual(missing, [])

    def test_pages_render_without_error(self):
        for key, history in committed_histories():
            with self.subTest(project=key):
                ac.load_project(key)
                self.assertIn("<html", ac.generate_dashboard(history))
                self.assertIn("<html", ac.generate_matrix_page(history))
                by_date = {}
                for e in history:
                    by_date.setdefault(e["date"], []).append(e)
                for d, entries in by_date.items():
                    self.assertIn("<html", ac.generate_report_page(d, entries))


if __name__ == "__main__":
    unittest.main()
