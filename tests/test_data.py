"""저장된 실제 데이터(aeo_results/history.json) 검증 — 코드 로직이 아니라 '데이터 자체'가 멀쩡한지 본다.

실행:  python -m unittest discover -s tests -p "test_data.py" -v

주간 실행의 사전 테스트(test_quality.py)와 분리했다. 데이터 문제가 있다고 새 측정 자체를 막아 버리면
결측이 더 커지므로, 주간 실행에서는 aeo_checker.py가 같은 검사를 경고(::warning)로만 알리고,
이 테스트는 코드·데이터를 푸시할 때(test.yml) 돌린다.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aeo_checker as ac  # noqa: E402

HISTORY_PATH = ROOT / "aeo_results" / "history.json"


@unittest.skipUnless(HISTORY_PATH.exists(), "history.json 없음")
class CommittedData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))

    def test_history_is_internally_consistent(self):
        # 저장된 집계 = 응답 원문 재계산 결과 / 중복 없음 / 프롬프트 누락 없음 / 멘션 판정 = 현재 브랜드 감지 규칙
        self.assertEqual(ac.validate_history(self.history), [])

    def test_every_run_records_its_measurement_settings(self):
        # 조건 기록이 없으면 '측정 조건이 바뀌었는지'를 나중에 알 수 없다
        missing = [f'{e["date"]} {e["engine"]}' for e in self.history if not e.get("settings")]
        self.assertEqual(missing, [])

    def test_pages_render_without_error(self):
        self.assertIn("<html", ac.generate_dashboard(self.history))
        self.assertIn("<html", ac.generate_matrix_page(self.history))
        by_date = {}
        for e in self.history:
            by_date.setdefault(e["date"], []).append(e)
        for d, entries in by_date.items():
            self.assertIn("<html", ac.generate_report_page(d, entries))


if __name__ == "__main__":
    unittest.main()
