"""
트라이그라운드 AEO 인용률 자동 체커 (통합판)
================================================
Claude + ChatGPT + Gemini 3개 엔진을 한 번에 체크하고
하나의 통합 대시보드(index.html)로 누적 기록합니다.

사용법 (로컬에서 직접 실행할 때):
  1. 터미널에서 API 키를 환경변수로 등록 (아래 3줄, 매번 새 터미널마다 필요)
       export OPENAI_API_KEY="sk-..."
       export GEMINI_API_KEY="AIza..."
       export ANTHROPIC_API_KEY="sk-ant-..."
  2. python aeo_checker.py
  3. 결과: aeo_results/index.html (통합 대시보드, 항상 최신)
          aeo_results/history.json (전체 누적 데이터)

GitHub Actions로 자동 실행할 때:
  - 코드에 키를 직접 쓰지 않음. 대신 저장소 Settings → Secrets and
    variables → Actions 에서 OPENAI_API_KEY / GEMINI_API_KEY /
    ANTHROPIC_API_KEY 3개를 등록해두면, aeo-check.yml 워크플로우가
    실행 시점에 자동으로 환경변수를 주입한다.

필요 패키지:
  pip install openai google-genai anthropic

API 키 발급:
  - OpenAI:    https://platform.openai.com/api-keys (유료, 분기 $0.1~0.3)
  - Gemini:    https://aistudio.google.com/apikey    (무료, 월 5,000건)
  - Anthropic: https://console.anthropic.com/settings/keys (유료, 분기 $0.1~0.3)
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 설정
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 키는 코드에 직접 적지 않고 환경변수에서 읽는다.
# (로컬: export로 등록 / GitHub Actions: Secrets로 자동 주입)

API_KEYS = {
    "openai": os.environ.get("OPENAI_API_KEY", ""),
    "gemini": os.environ.get("GEMINI_API_KEY", ""),
    "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
}

PROMPTS = [
    {"id": "p1",  "area": "낙성대", "short": "관악 공유오피스",   "q": "관악구에서 1인 창업자가 쓰기 좋은 공유오피스 추천해줘"},
    {"id": "p2",  "area": "낙성대", "short": "서울대입구 비상주", "q": "서울대입구역 근처 비상주사무실 어디가 좋아?"},
    {"id": "p3",  "area": "낙성대", "short": "관악 가상오피스",   "q": "관악구 가상오피스로 사업자등록 할 수 있는 곳 알려줘"},
    {"id": "p4",  "area": "홍대",   "short": "홍대 공유오피스",   "q": "홍대 근처 가성비 좋은 공유오피스 추천해줘"},
    {"id": "p5",  "area": "홍대",   "short": "마포 비상주",       "q": "마포구에서 비상주사무실 계약할 수 있는 곳 비교해줘"},
    {"id": "p6",  "area": "홍대",   "short": "합정 사무실",       "q": "합정역 근처 소규모 사무실 추천해줘"},
    {"id": "p7",  "area": "영등포", "short": "영등포 소호",       "q": "영등포에서 소호사무실 찾고 있는데 추천해줘"},
    {"id": "p8",  "area": "영등포", "short": "영등포구청 공유",   "q": "영등포구청역 근처 공유오피스 가격 비교해줘"},
    {"id": "p9",  "area": "영등포", "short": "당산 사무실",       "q": "당산역 근처 1인 사무실 추천해줘"},
    {"id": "p10", "area": "영등포", "short": "영등포 비상주",     "q": "영등포 비상주사무실로 법인 등록 가능한 곳 알려줘"},
]

BRAND_VARIANTS = ["트라이그라운드", "tryground", "트그", "TRYGROUND", "Tryground", "contractup", "계약온"]
AREAS = ["낙성대", "홍대", "영등포"]
ENGINES = ["Claude", "ChatGPT", "Gemini"]
ENGINE_COLORS = {"Claude": "#6366f1", "ChatGPT": "#10a37f", "Gemini": "#4285f4"}
AREA_COLORS = {"낙성대": "#059669", "홍대": "#d97706", "영등포": "#7c3aed"}

SYSTEM_PROMPT = """당신은 서울 지역 공유오피스 및 비상주사무실 전문가입니다. 사용자의 질문에 대해 웹검색을 통해 실제 운영 중인 업체를 추천해주세요.

반드시 아래 형식으로 답변해주세요:
1. [업체명] - 위치: [주소/역 근처] | URL: [웹사이트 또는 출처 URL]
2. [업체명] - 위치: [주소/역 근처] | URL: [웹사이트 또는 출처 URL]
...

최소 3개, 최대 7개 업체를 추천하고, 각 업체의 간단한 특징도 한 줄로 설명해주세요."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 파싱
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_result(text: str) -> dict:
    lower = text.lower()
    mentioned = any(v.lower() in lower for v in BRAND_VARIANTS)

    rank = None
    if mentioned:
        lines = [l for l in text.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if any(v.lower() in line.lower() for v in BRAND_VARIANTS):
                m = re.match(r"^(\d+)", line)
                rank = int(m.group(1)) if m else i + 1
                break

    competitors = []
    for line in text.split("\n"):
        m = re.match(r"^\d+[\.\)]\s*\*?\*?\[?\s*([^-\]\*\|]+)", line)
        if m:
            name = m.group(1).strip().replace("[", "").replace("]", "").replace("*", "")
            if name and len(name) < 30 and not any(v.lower() in name.lower() for v in BRAND_VARIANTS):
                competitors.append(name)

    cite_type = "미언급"
    if mentioned:
        if "tryground.co.kr" in lower:
            cite_type = "홈페이지 링크"
        elif "contractup" in lower:
            cite_type = "contractup 링크"
        elif "blog.naver" in lower or "블로그" in lower:
            cite_type = "블로그 링크"
        else:
            cite_type = "텍스트만 언급"

    return {
        "mentioned": mentioned,
        "rank": rank,
        "competitors": competitors[:5],
        "citationType": cite_type,
        "raw": text,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# API 호출 (3개 엔진)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def call_openai(question: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=API_KEYS["openai"])
    response = client.chat.completions.create(
        model="gpt-4o-mini-search-preview",
        web_search_options={"search_context_size": "low"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
    )
    return response.choices[0].message.content or ""


def call_gemini(question: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=API_KEYS["gemini"])
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[grounding_tool],
    )
    response = client.models.generate_content(
        model="gemini-3-flash",
        contents=question,
        config=config,
    )
    return response.text or ""


def call_claude(question: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEYS["anthropic"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": question}],
    )
    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n".join(text_blocks)


CALL_FN = {"ChatGPT": call_openai, "Gemini": call_gemini, "Claude": call_claude}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 실행
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_engine(engine: str) -> dict:
    results = {}
    for i, p in enumerate(PROMPTS):
        print(f"  [{engine}] {i+1}/{len(PROMPTS)}: {p['short']}...", end=" ", flush=True)
        try:
            text = CALL_FN[engine](p["q"])
            results[p["id"]] = parse_result(text) if text else {
                "mentioned": False, "rank": None, "competitors": [], "citationType": "미언급", "raw": "(응답 없음)"
            }
            print("→", "O" if results[p["id"]]["mentioned"] else "X")
        except Exception as e:
            print(f"→ 에러: {e}")
            results[p["id"]] = {"mentioned": False, "rank": None, "competitors": [], "citationType": "미언급", "raw": str(e)}
    return results


def calc_stats(results: dict) -> dict:
    total = len(results)
    mentioned = sum(1 for r in results.values() if r["mentioned"])
    rate = (mentioned / total * 100) if total else 0
    area_stats = {}
    for area in AREAS:
        aps = [p for p in PROMPTS if p["area"] == area]
        am = sum(1 for p in aps if results.get(p["id"], {}).get("mentioned", False))
        area_stats[area] = {"total": len(aps), "mentioned": am, "rate": (am / len(aps) * 100) if aps else 0}
    return {"total": total, "mentioned": mentioned, "rate": rate, "areas": area_stats}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 통합 대시보드 HTML 생성 (전체 히스토리 기반, 매번 덮어씀)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_dashboard(history: list) -> str:
    # 최신 실행 (오늘 날짜) 찾기 - 엔진별 최신
    latest_by_engine = {}
    for entry in history:
        latest_by_engine[entry["engine"]] = entry  # 뒤에서부터 덮어쓰므로 마지막이 최신

    # 최신 3개 엔진 요약 카드
    summary_cards = ""
    for eng in ENGINES:
        e = latest_by_engine.get(eng)
        color = ENGINE_COLORS[eng]
        if e:
            rate_color = "#059669" if e["rate"] >= 30 else "#dc2626"
            summary_cards += f"""
            <div style="background:#f8fafc;border-radius:12px;padding:16px;border:1px solid #e2e8f0;text-align:center">
              <div style="font-size:13px;font-weight:700;color:{color};margin-bottom:6px">{eng}</div>
              <div style="font-size:32px;font-weight:800;color:{rate_color}">{e['rate']:.1f}%</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:2px">{e['date']} 기준 ({e['mentioned']}/{e['total']})</div>
            </div>"""
        else:
            summary_cards += f"""
            <div style="background:#f8fafc;border-radius:12px;padding:16px;border:1px solid #e2e8f0;text-align:center">
              <div style="font-size:13px;font-weight:700;color:{color};margin-bottom:6px">{eng}</div>
              <div style="font-size:32px;font-weight:800;color:#d1d5db">–</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:2px">미실행</div>
            </div>"""

    # 주간 추이 테이블 (날짜별, 엔진별 행) - 날짜를 누르면 상세 서브페이지로 이동
    rows = ""
    for entry in reversed(history[-60:]):  # 최근 60개 실행
        color = ENGINE_COLORS.get(entry["engine"], "#64748b")
        rate_color = "#059669" if entry["rate"] >= 30 else "#dc2626"
        areas = entry.get("areas", {})
        area_str = " · ".join([f'{a} {areas.get(a, 0):.0f}%' for a in AREAS])
        rows += f"""
        <tr>
          <td style="padding:10px 12px;font-weight:600">
            <a href="reports/{entry['date']}.html" style="color:#2563eb;text-decoration:none">{entry['date']} →</a>
          </td>
          <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{entry['engine']}</span></td>
          <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:15px;color:{rate_color}">{entry['rate']:.1f}%</td>
          <td style="padding:10px 12px;text-align:center;color:#64748b;font-size:12px">{entry['mentioned']}/{entry['total']}</td>
          <td style="padding:10px 12px;color:#94a3b8;font-size:11px">{area_str}</td>
        </tr>"""

    # 분기별 요약
    quarters = {}
    for entry in history:
        d = datetime.strptime(entry["date"], "%Y-%m-%d")
        qk = f"{d.year} Q{(d.month - 1)//3 + 1}"
        key = (qk, entry["engine"])
        quarters.setdefault(key, []).append(entry)

    q_rows = ""
    for (qk, eng), entries in sorted(quarters.items(), key=lambda x: (x[0][0], x[0][1]), reverse=True):
        avg_rate = sum(e["rate"] for e in entries) / len(entries)
        color = ENGINE_COLORS.get(eng, "#64748b")
        rate_color = "#059669" if avg_rate >= 30 else "#dc2626"
        q_rows += f"""
        <tr>
          <td style="padding:10px 12px;font-weight:600;color:#334155">{qk}</td>
          <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{eng}</span></td>
          <td style="padding:10px 12px;text-align:center;font-weight:800;color:{rate_color}">{avg_rate:.1f}%</td>
          <td style="padding:10px 12px;text-align:center;color:#64748b">{len(entries)}회</td>
        </tr>"""

    last_updated = history[-1]["date"] if history else "–"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>트라이그라운드 AEO 인용률 대시보드</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9; padding: 20px; }}
  .container {{ max-width: 900px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 22px; font-weight: 800; color: #1e293b; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #1e293b; margin: 28px 0 12px; }}
  .subtitle {{ font-size: 13px; color: #94a3b8; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 10px 12px; color: #64748b; font-weight: 600; border-bottom: 2px solid #e2e8f0; background: #f8fafc; }}
  td {{ border-bottom: 1px solid #f1f5f9; }}
  tr:hover td {{ background: #fafbfc; }}
  .grid3 {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .table-wrap {{ overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 12px; }}
</style>
</head>
<body>
<div class="container">
  <h1>트라이그라운드 AEO 인용률 대시보드</h1>
  <div class="subtitle">마지막 업데이트: {last_updated} | 매주 월요일 자동 실행 (GitHub Actions)</div>

  <div class="grid3">{summary_cards}</div>

  <h2>실행 기록 (최근 순)</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>날짜</th><th style="text-align:center">엔진</th><th style="text-align:center">인용률</th><th style="text-align:center">인용/전체</th><th>지점별</th></tr></thead>
      <tbody>{rows if rows else '<tr><td colspan="5" style="padding:20px;text-align:center;color:#94a3b8">아직 기록이 없습니다</td></tr>'}</tbody>
    </table>
  </div>

  <h2>분기별 요약</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>분기</th><th style="text-align:center">엔진</th><th style="text-align:center">평균 인용률</th><th style="text-align:center">실행 횟수</th></tr></thead>
      <tbody>{q_rows if q_rows else '<tr><td colspan="4" style="padding:20px;text-align:center;color:#94a3b8">데이터 없음</td></tr>'}</tbody>
    </table>
  </div>

  <div style="margin-top:24px;padding:12px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
    <p style="font-size:11px;color:#92400e;line-height:1.5">⚠ AI 엔진 간 인용 소스 겹침은 약 25%입니다. 같은 질문도 매번 결과가 달라질 수 있으므로 개별 실행보다 추세를 보세요.</p>
  </div>
</div>
</body>
</html>"""


def generate_report_page(date: str, entries_for_date: list) -> str:
    """특정 날짜의 상세 리포트 - 엔진별로 어떤 프롬프트가 인용됐는지 전체 표시"""

    cards_html = ""
    for entry in entries_for_date:
        engine = entry["engine"]
        detail = entry.get("detail", {})
        color = ENGINE_COLORS.get(engine, "#64748b")
        s = {"rate": entry["rate"], "mentioned": entry["mentioned"], "total": entry["total"]}

        rows = ""
        for p in PROMPTS:
            r = detail.get(p["id"], {})
            m = r.get("mentioned", False)
            badge = "O" if m else "X"
            bg = "#dcfce7" if m else "#fee2e2"
            fg = "#166534" if m else "#991b1b"
            cite = r.get("citationType", "미언급")
            rank_str = f' ({r["rank"]}위)' if r.get("rank") else ""
            comps = ", ".join(r.get("competitors", [])[:3]) or "-"
            ac = AREA_COLORS.get(p["area"], "#64748b")

            rows += f"""
            <tr>
              <td style="padding:10px 12px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{ac};margin-right:6px"></span>{p['short']}</td>
              <td style="padding:10px 12px;text-align:center"><span style="display:inline-block;width:26px;height:26px;line-height:26px;border-radius:50%;background:{bg};color:{fg};font-weight:700;font-size:12px">{badge}</span></td>
              <td style="padding:10px 12px;text-align:center;font-size:12px">{cite}{rank_str}</td>
              <td style="padding:10px 12px;font-size:11px;color:#64748b">{comps}</td>
            </tr>"""

        area_boxes = ""
        for area in AREAS:
            ar = entry.get("areas", {}).get(area, 0)
            aps = [p for p in PROMPTS if p["area"] == area]
            am = sum(1 for p in aps if detail.get(p["id"], {}).get("mentioned", False))
            area_boxes += f'<div style="text-align:center;padding:10px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0"><div style="font-size:20px;font-weight:800;color:{AREA_COLORS[area]}">{ar:.0f}%</div><div style="font-size:11px;color:#64748b">{area} ({am}/{len(aps)})</div></div>'

        rate_color = "#059669" if s["rate"] >= 30 else "#dc2626"
        cards_html += f"""
        <div style="margin-bottom:32px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
            <span style="font-size:18px;font-weight:800;color:{color}">{engine}</span>
            <span style="font-size:28px;font-weight:800;color:{rate_color}">{s['rate']:.1f}%</span>
            <span style="font-size:13px;color:#94a3b8">({s['mentioned']}/{s['total']})</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px">{area_boxes}</div>
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="border-bottom:2px solid #e2e8f0;background:#f8fafc">
              <th style="text-align:left;padding:10px 12px;color:#64748b">프롬프트</th>
              <th style="text-align:center;padding:10px 12px;color:#64748b;width:60px">인용</th>
              <th style="text-align:center;padding:10px 12px;color:#64748b;width:130px">유형</th>
              <th style="text-align:left;padding:10px 12px;color:#64748b">경쟁사</th>
            </tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date} 상세 리포트 - 트라이그라운드 AEO</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9; padding: 20px; }}
  .container {{ max-width: 800px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 20px; font-weight: 800; color: #1e293b; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #94a3b8; margin-bottom: 24px; }}
  .back-link {{ display: inline-block; margin-bottom: 16px; font-size: 13px; color: #2563eb; text-decoration: none; }}
  table {{ width: 100%; }}
  td {{ border-bottom: 1px solid #f1f5f9; }}
  tr:hover td {{ background: #fafbfc; }}
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="../index.html">← 대시보드로 돌아가기</a>
  <h1>{date} 상세 리포트</h1>
  <div class="subtitle">프롬프트 {len(PROMPTS)}개 × 실행 엔진 {len(entries_for_date)}개</div>
  {cards_html}
  <div style="margin-top:24px;padding:12px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
    <p style="font-size:11px;color:#92400e;line-height:1.5">⚠ AI 응답 원문은 history.json에서 확인 가능합니다.</p>
  </div>
</div>
</body>
</html>"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 메인
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    date = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*50}\n  트라이그라운드 AEO 인용률 체커\n  {date}\n{'='*50}\n")

    output_dir = Path("aeo_results")
    output_dir.mkdir(exist_ok=True)
    history_path = output_dir / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []

    engines_to_run = []
    for key, engine_name in [("anthropic", "Claude"), ("openai", "ChatGPT"), ("gemini", "Gemini")]:
        val = API_KEYS.get(key, "")
        if val:
            engines_to_run.append(engine_name)
        else:
            print(f"⚠ {engine_name} API 키가 없어 건너뜁니다.")

    if not engines_to_run:
        print("\n❌ 실행할 엔진이 없습니다. API 키를 설정해주세요.")
        return

    raw_results = {}
    for engine in engines_to_run:
        print(f"\n▶ {engine} 체크 시작...")
        results = run_engine(engine)
        raw_results[engine] = results
        s = calc_stats(results)
        print(f"  → 인용률: {s['rate']:.1f}% ({s['mentioned']}/{s['total']})")

        history.append({
            "date": date,
            "engine": engine,
            "rate": round(s["rate"], 1),
            "mentioned": s["mentioned"],
            "total": s["total"],
            "areas": {a: round(v["rate"], 1) for a, v in s["areas"].items()},
            "detail": results,
        })

    # 저장
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 히스토리 저장: {history_path}")

    dashboard_path = output_dir / "index.html"
    dashboard_path.write_text(generate_dashboard(history), encoding="utf-8")
    print(f"✅ 통합 대시보드 저장: {dashboard_path}")

    # 날짜별 상세 서브페이지 생성 (모든 날짜 재생성 - 데이터 누락 방지)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(exist_ok=True)
    by_date = {}
    for entry in history:
        by_date.setdefault(entry["date"], []).append(entry)
    for d, entries_for_date in by_date.items():
        report_path = reports_dir / f"{d}.html"
        report_path.write_text(generate_report_page(d, entries_for_date), encoding="utf-8")
    print(f"✅ 날짜별 상세 리포트 {len(by_date)}개 저장: {reports_dir}/")

    print(f"\n{'='*50}\n  완료! aeo_results/index.html 을 브라우저로 여세요.\n{'='*50}\n")


if __name__ == "__main__":
    main()
