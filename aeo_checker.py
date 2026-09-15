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
    response = client.responses.create(
        model="gpt-4o-mini",   # 검색 1회당 8,000토큰 고정 청구 - 입력단가 낮은 모델일수록 저렴 (4.1-mini 대비 약 1/2.7)
        instructions=SYSTEM_PROMPT + "\n\n비용 절감을 위해 웹검색은 최대 2회까지만 사용해줘.",
        tools=[{"type": "web_search"}],
        input=question,
        max_output_tokens=600,   # 업체 리스트 용도로 충분, 출력 폭주 방지
    )
    return response.output_text or ""


def call_gemini(question: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=API_KEYS["gemini"])
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[grounding_tool],
        max_output_tokens=600,   # 업체 리스트 용도로 충분, 출력 비용 상한
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=question,
        config=config,
    )
    return response.text or ""


def call_claude(question: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEYS["anthropic"])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",   # Sonnet보다 훨씬 저렴 - 업체명/URL 추출 용도로 충분
        max_tokens=600,   # 업체 리스트 용도로 충분, 출력 비용 상한
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],  # 검색 횟수 상한 - 비용 폭주 방지
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


LINK_CITATION_TYPES = {"홈페이지 링크", "블로그 링크", "contractup 링크"}


def calc_stats(results: dict) -> dict:
    total = len(results)
    mentioned = sum(1 for r in results.values() if r["mentioned"])
    cited = sum(1 for r in results.values() if r.get("citationType") in LINK_CITATION_TYPES)
    mention_rate = (mentioned / total * 100) if total else 0
    citation_rate = (cited / total * 100) if total else 0

    area_stats = {}
    for area in AREAS:
        aps = [p for p in PROMPTS if p["area"] == area]
        am = sum(1 for p in aps if results.get(p["id"], {}).get("mentioned", False))
        ac = sum(1 for p in aps if results.get(p["id"], {}).get("citationType") in LINK_CITATION_TYPES)
        area_stats[area] = {
            "total": len(aps),
            "mentioned": am,
            "cited": ac,
            "rate": (am / len(aps) * 100) if aps else 0,          # 하위호환: 멘션률
            "mention_rate": (am / len(aps) * 100) if aps else 0,
            "citation_rate": (ac / len(aps) * 100) if aps else 0,
        }

    return {
        "total": total,
        "mentioned": mentioned,
        "cited": cited,
        "rate": mention_rate,            # 하위호환: 기존 코드가 참조하던 'rate'는 멘션률
        "mention_rate": mention_rate,
        "citation_rate": citation_rate,
        "areas": area_stats,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 통합 대시보드 HTML 생성 (전체 히스토리 기반, 매번 덮어씀)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_dashboard(history: list) -> str:
    OVERALL_COLOR = "#1e293b"

    if not history:
        current_month = None
    else:
        current_month = sorted(e["date"] for e in history)[-1][:7]

    def engine_month_avg(eng, month):
        ents = [e for e in history if e["engine"] == eng and month and e["date"][:7] == month]
        if not ents:
            return None
        m = sum(e.get("mention_rate", e["rate"]) for e in ents) / len(ents)
        c = sum(e.get("citation_rate", 0) for e in ents) / len(ents)
        return {"mention_rate": m, "citation_rate": c, "count": len(ents)}

    engine_month_stats = {eng: engine_month_avg(eng, current_month) for eng in ENGINES}
    valid_month_stats = [v for v in engine_month_stats.values() if v]
    overall_month = None
    if valid_month_stats:
        overall_month = {
            "mention_rate": sum(v["mention_rate"] for v in valid_month_stats) / len(valid_month_stats),
            "citation_rate": sum(v["citation_rate"] for v in valid_month_stats) / len(valid_month_stats),
            "count": sum(v["count"] for v in valid_month_stats),
        }

    def render_card(label, color, stats):
        if not stats:
            return f"""
            <div style="background:#f8fafc;border-radius:12px;padding:16px;border:1px solid #e2e8f0;text-align:center">
              <div style="font-size:13px;font-weight:700;color:{color};margin-bottom:6px">{label}</div>
              <div style="font-size:32px;font-weight:800;color:#d1d5db">–</div>
              <div style="font-size:11px;color:#94a3b8;margin-top:2px">이번 달 데이터 없음</div>
            </div>"""
        m_rate = stats["mention_rate"]
        c_rate = stats["citation_rate"]
        m_color = "#059669" if m_rate >= 30 else "#dc2626"
        c_color = "#059669" if c_rate >= 30 else "#dc2626"
        return f"""
        <div style="background:#f8fafc;border-radius:12px;padding:16px;border:1px solid #e2e8f0;text-align:center">
          <div style="font-size:13px;font-weight:700;color:{color};margin-bottom:8px">{label}</div>
          <div style="display:flex;justify-content:center;gap:14px">
            <div>
              <div style="font-size:24px;font-weight:800;color:{m_color}">{m_rate:.0f}%</div>
              <div style="font-size:10px;color:#94a3b8">멘션률</div>
            </div>
            <div style="width:1px;background:#e2e8f0"></div>
            <div>
              <div style="font-size:24px;font-weight:800;color:{c_color}">{c_rate:.0f}%</div>
              <div style="font-size:10px;color:#94a3b8">인용률</div>
            </div>
          </div>
          <div style="font-size:11px;color:#94a3b8;margin-top:8px">{current_month} 평균 ({stats['count']}회 실행)</div>
        </div>"""

    summary_cards = render_card("전체 (3개 평균)", OVERALL_COLOR, overall_month)
    for eng in ENGINES:
        summary_cards += render_card(eng, ENGINE_COLORS[eng], engine_month_stats[eng])

    # ── 실행 기록: 날짜별로 엔진 묶고, 날짜마다 "전체"(3개 평균) 행 + 엔진별 행 ──
    by_date = {}
    for e in history:
        by_date.setdefault(e["date"], {})[e["engine"]] = e

    rows = ""
    for d in sorted(by_date.keys(), reverse=True)[:60]:
        engines_here = by_date[d]
        present = [eng for eng in ENGINES if eng in engines_here]
        m_vals = [engines_here[eng].get("mention_rate", engines_here[eng]["rate"]) for eng in present]
        c_vals = [engines_here[eng].get("citation_rate", 0) for eng in present]
        if m_vals:
            avg_m = sum(m_vals) / len(m_vals)
            avg_c = sum(c_vals) / len(c_vals)
            am_color = "#059669" if avg_m >= 30 else "#dc2626"
            ac_color = "#059669" if avg_c >= 30 else "#dc2626"
            area_avg = {}
            for a in AREAS:
                a_vals = [engines_here[eng].get("areas", {}).get(a, 0) for eng in present]
                area_avg[a] = sum(a_vals) / len(a_vals) if a_vals else 0
            area_str = " · ".join([f'{a} {area_avg[a]:.0f}%' for a in AREAS])
            rows += f"""
            <tr data-engine="전체">
              <td style="padding:10px 12px;font-weight:600">
                <a href="reports/{d}.html" style="color:#2563eb;text-decoration:none">{d} →</a>
              </td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{OVERALL_COLOR};background:#1e293b15;padding:3px 10px;border-radius:8px">전체</span></td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{am_color}">{avg_m:.0f}%</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{ac_color}">{avg_c:.0f}%</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b;font-size:12px">{len(present)}개 엔진 평균</td>
              <td style="padding:10px 12px;color:#94a3b8;font-size:11px">{area_str}</td>
            </tr>"""
        for eng in present:
            entry = engines_here[eng]
            color = ENGINE_COLORS.get(eng, "#64748b")
            m_rate = entry.get("mention_rate", entry["rate"])
            c_rate = entry.get("citation_rate", 0)
            m_color = "#059669" if m_rate >= 30 else "#dc2626"
            c_color = "#059669" if c_rate >= 30 else "#dc2626"
            areas = entry.get("areas", {})
            area_str = " · ".join([f'{a} {areas.get(a, 0):.0f}%' for a in AREAS])
            rows += f"""
            <tr data-engine="{eng}">
              <td style="padding:10px 12px;font-weight:600">
                <a href="reports/{d}.html" style="color:#2563eb;text-decoration:none">{d} →</a>
              </td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{eng}</span></td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{m_color}">{m_rate:.0f}%</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{c_color}">{c_rate:.0f}%</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b;font-size:12px">{entry['mentioned']}/{entry['total']}</td>
              <td style="padding:10px 12px;color:#94a3b8;font-size:11px">{area_str}</td>
            </tr>"""

    # ── 월별 요약: 월마다 "전체"(3개 평균) 행 + 엔진별 행 ──
    months_data = {}
    for entry in history:
        mk = entry["date"][:7]
        months_data.setdefault(mk, {}).setdefault(entry["engine"], []).append(entry)

    m_rows = ""
    for mk in sorted(months_data.keys(), reverse=True):
        eng_avgs = {}
        for eng in ENGINES:
            ents = months_data[mk].get(eng, [])
            if ents:
                eng_avgs[eng] = {
                    "m": sum(e.get("mention_rate", e["rate"]) for e in ents) / len(ents),
                    "c": sum(e.get("citation_rate", 0) for e in ents) / len(ents),
                    "n": len(ents),
                }
        if eng_avgs:
            overall_m = sum(v["m"] for v in eng_avgs.values()) / len(eng_avgs)
            overall_c = sum(v["c"] for v in eng_avgs.values()) / len(eng_avgs)
            om_color = "#059669" if overall_m >= 30 else "#dc2626"
            oc_color = "#059669" if overall_c >= 30 else "#dc2626"
            total_n = sum(v["n"] for v in eng_avgs.values())
            m_rows += f"""
            <tr data-engine="전체">
              <td style="padding:10px 12px;font-weight:600;color:#334155">{mk}</td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{OVERALL_COLOR};background:#1e293b15;padding:3px 10px;border-radius:8px">전체</span></td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{om_color}">{overall_m:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{oc_color}">{overall_c:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b">{total_n}회</td>
            </tr>"""
        for eng in ENGINES:
            if eng not in eng_avgs:
                continue
            v = eng_avgs[eng]
            color = ENGINE_COLORS.get(eng, "#64748b")
            m_color = "#059669" if v["m"] >= 30 else "#dc2626"
            c_color = "#059669" if v["c"] >= 30 else "#dc2626"
            m_rows += f"""
            <tr data-engine="{eng}">
              <td style="padding:10px 12px;font-weight:600;color:#334155">{mk}</td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{eng}</span></td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{m_color}">{v['m']:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{c_color}">{v['c']:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b">{v['n']}회</td>
            </tr>"""

    last_updated = history[-1]["date"] if history else "–"

    rows_html = rows if rows else '<tr><td colspan="6" style="padding:20px;text-align:center;color:#94a3b8">아직 기록이 없습니다</td></tr>'
    m_rows_html = m_rows if m_rows else '<tr><td colspan="5" style="padding:20px;text-align:center;color:#94a3b8">데이터 없음</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>트라이그라운드 AEO 인용률 대시보드</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9; padding: 20px; }}
  .container {{ max-width: 960px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 22px; font-weight: 800; color: #1e293b; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #1e293b; margin: 28px 0 12px; }}
  .subtitle {{ font-size: 13px; color: #94a3b8; margin-bottom: 8px; }}
  .nav-link {{ display: inline-block; font-size: 13px; color: #2563eb; text-decoration: none; margin-bottom: 20px; font-weight: 600; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 10px 12px; color: #64748b; font-weight: 600; border-bottom: 2px solid #e2e8f0; background: #f8fafc; }}
  td {{ border-bottom: 1px solid #f1f5f9; }}
  tr:hover td {{ background: #fafbfc; }}
  .grid4 {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }}
  .table-wrap {{ overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 12px; }}
  .filter-tabs {{ display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }}
  .filter-btn {{ padding: 6px 14px; border-radius: 20px; border: 1px solid #e2e8f0; background: #fff; color: #64748b; font-size: 12px; font-weight: 600; cursor: pointer; }}
  .filter-btn.active {{ background: #1e293b; color: #fff; border-color: #1e293b; }}
  @media (max-width: 700px) {{ .grid4 {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>
<div class="container">
  <h1>트라이그라운드 AEO 인용률 대시보드</h1>
  <div class="subtitle">마지막 업데이트: {last_updated} | 매주 월요일 자동 실행 (GitHub Actions)</div>
  <a class="nav-link" href="matrix.html">📋 프롬프트별 전체 추이표 보기 →</a>

  <div class="grid4">{summary_cards}</div>

  <h2>실행 기록 (최근 순)</h2>
  <div class="filter-tabs" id="history-filter">
    <button class="filter-btn active" data-engine="전체">전체</button>
    <button class="filter-btn" data-engine="Claude">클로드</button>
    <button class="filter-btn" data-engine="ChatGPT">챗지피티</button>
    <button class="filter-btn" data-engine="Gemini">제미나이</button>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>날짜</th><th style="text-align:center">엔진</th><th style="text-align:center">멘션률</th><th style="text-align:center">인용률</th><th style="text-align:center">인용/전체</th><th>지점별</th></tr></thead>
      <tbody id="history-tbody">{rows_html}</tbody>
    </table>
  </div>

  <h2>월별 요약</h2>
  <div class="filter-tabs" id="monthly-filter">
    <button class="filter-btn active" data-engine="전체">전체</button>
    <button class="filter-btn" data-engine="Claude">클로드</button>
    <button class="filter-btn" data-engine="Gemini">제미나이</button>
    <button class="filter-btn" data-engine="ChatGPT">챗지피티</button>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>월</th><th style="text-align:center">엔진</th><th style="text-align:center">평균 멘션률</th><th style="text-align:center">평균 인용률</th><th style="text-align:center">실행 횟수</th></tr></thead>
      <tbody id="monthly-tbody">{m_rows_html}</tbody>
    </table>
  </div>

  <div style="margin-top:24px;padding:12px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
    <p style="font-size:11px;color:#92400e;line-height:1.5">⚠ AI 엔진 간 인용 소스 겹침은 약 25%입니다. 같은 질문도 매번 결과가 달라질 수 있으므로 개별 실행보다 추세를 보세요.</p>
  </div>
</div>
<script>
function setupFilter(filterId, tbodyId) {{
  var container = document.getElementById(filterId);
  if (!container) return;
  var buttons = container.querySelectorAll('.filter-btn');
  buttons.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      buttons.forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      var eng = btn.getAttribute('data-engine');
      var rows = document.querySelectorAll('#' + tbodyId + ' tr[data-engine]');
      rows.forEach(function(tr) {{
        var match = (eng === '전체') || (tr.getAttribute('data-engine') === eng);
        tr.style.display = match ? '' : 'none';
      }});
    }});
  }});
}}
setupFilter('history-filter', 'history-tbody');
setupFilter('monthly-filter', 'monthly-tbody');
</script>
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
            ctype = r.get("citationType", "미언급")
            is_linked = ctype in LINK_CITATION_TYPES
            if is_linked:
                badge, bg, fg = "O", "#dcfce7", "#166534"
            elif m:
                badge, bg, fg = "△", "#fef3c7", "#92400e"
            else:
                badge, bg, fg = "X", "#fee2e2", "#991b1b"
            rank_str = f' ({r["rank"]}위)' if r.get("rank") else ""
            comps = ", ".join(r.get("competitors", [])[:3]) or "-"
            ac = AREA_COLORS.get(p["area"], "#64748b")

            rows += f"""
            <tr>
              <td style="padding:10px 12px"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{ac};margin-right:6px"></span>{p['short']}</td>
              <td style="padding:10px 12px;text-align:center"><span style="display:inline-block;width:26px;height:26px;line-height:26px;border-radius:50%;background:{bg};color:{fg};font-weight:700;font-size:12px">{badge}</span></td>
              <td style="padding:10px 12px;text-align:center;font-size:12px">{ctype}{rank_str}</td>
              <td style="padding:10px 12px;font-size:11px;color:#64748b">{comps}</td>
            </tr>"""

        area_boxes = ""
        for area in AREAS:
            am_rate = entry.get("areas_mention", entry.get("areas", {})).get(area, 0)
            ac_rate = entry.get("areas_citation", {}).get(area, 0)
            aps = [p for p in PROMPTS if p["area"] == area]
            am = sum(1 for p in aps if detail.get(p["id"], {}).get("mentioned", False))
            area_boxes += f'''<div style="text-align:center;padding:10px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0">
              <div style="font-size:16px;font-weight:800;color:{AREA_COLORS[area]}">{am_rate:.0f}% <span style="font-size:11px;color:#94a3b8;font-weight:400">/ {ac_rate:.0f}%</span></div>
              <div style="font-size:11px;color:#64748b">{area} ({am}/{len(aps)})</div></div>'''

        m_rate = entry.get("mention_rate", s["rate"])
        c_rate = entry.get("citation_rate", 0)
        m_color = "#059669" if m_rate >= 30 else "#dc2626"
        c_color = "#059669" if c_rate >= 30 else "#dc2626"
        cards_html += f"""
        <div style="margin-bottom:32px">
          <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px;flex-wrap:wrap">
            <span style="font-size:18px;font-weight:800;color:{color}">{engine}</span>
            <div>
              <span style="font-size:24px;font-weight:800;color:{m_color}">{m_rate:.0f}%</span>
              <span style="font-size:11px;color:#94a3b8">멘션률</span>
            </div>
            <div>
              <span style="font-size:24px;font-weight:800;color:{c_color}">{c_rate:.0f}%</span>
              <span style="font-size:11px;color:#94a3b8">인용률</span>
            </div>
            <span style="font-size:13px;color:#94a3b8">({s['mentioned']}/{s['total']})</span>
          </div>
          <div style="font-size:10px;color:#94a3b8;margin:-6px 0 12px">지점별 박스: 멘션률 / 인용률</div>
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 매트릭스 페이지 (프롬프트 × 날짜 × 엔진 전체 추이표)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def generate_matrix_page(history: list, max_dates: int = 12) -> str:
    """세로: 프롬프트, 가로: 날짜(그룹) × 엔진(하위열). 한 눈에 O/X 추이를 보는 표."""

    # 날짜별로 엔진 결과 묶기: {date: {engine: entry}}
    by_date = {}
    for entry in history:
        by_date.setdefault(entry["date"], {})[entry["engine"]] = entry

    # 최신 날짜부터 max_dates개만
    dates = sorted(by_date.keys(), reverse=True)[:max_dates]

    if not dates:
        table_html = '<p style="padding:20px;text-align:center;color:#94a3b8">아직 데이터가 없습니다</p>'
    else:
        # 헤더 행 1: 날짜 (엔진 3개를 colspan으로 묶음)
        header_dates = "".join(
            f'<th colspan="{len(ENGINES)}" style="text-align:center;padding:8px 6px;border-bottom:1px solid #e2e8f0;border-left:2px solid #e2e8f0">{d}</th>'
            for d in dates
        )
        # 헤더 행 2: 엔진명
        header_engines = ""
        for d in dates:
            for eng in ENGINES:
                header_engines += f'<th style="text-align:center;padding:6px 8px;font-size:11px;color:{ENGINE_COLORS[eng]};border-bottom:2px solid #e2e8f0;font-weight:700">{eng}</th>'

        # 데이터 행 1: 날짜별·엔진별 멘션률 %
        mention_row = ""
        citation_row = ""
        for d in dates:
            for eng in ENGINES:
                e = by_date.get(d, {}).get(eng)
                if e:
                    m_rate = e.get("mention_rate", e["rate"])
                    c_rate = e.get("citation_rate", 0)
                    mc = "#059669" if m_rate >= 30 else "#dc2626"
                    cc = "#059669" if c_rate >= 30 else "#dc2626"
                    mention_row += f'<td style="text-align:center;padding:6px 8px;font-weight:800;font-size:13px;color:{mc};background:#f8fafc">{m_rate:.0f}%</td>'
                    citation_row += f'<td style="text-align:center;padding:6px 8px;font-weight:800;font-size:13px;color:{cc};background:#fdfdfd">{c_rate:.0f}%</td>'
                else:
                    mention_row += '<td style="text-align:center;padding:6px 8px;color:#d1d5db;background:#f8fafc">–</td>'
                    citation_row += '<td style="text-align:center;padding:6px 8px;color:#d1d5db;background:#fdfdfd">–</td>'

        # 프롬프트별 O/X 행 — 링크 인용(진한 초록) vs 텍스트만 언급(연한 주황) vs 미언급(빨강) 구분
        prompt_rows = ""
        for p in PROMPTS:
            cells = ""
            for d in dates:
                for eng in ENGINES:
                    e = by_date.get(d, {}).get(eng)
                    r = e.get("detail", {}).get(p["id"]) if e else None
                    if r is None:
                        cells += '<td style="text-align:center;padding:6px 8px;color:#d1d5db">–</td>'
                    else:
                        m = r.get("mentioned", False)
                        ctype = r.get("citationType", "")
                        is_linked = ctype in LINK_CITATION_TYPES
                        if is_linked:
                            color, label = "#059669", "O"      # 링크 인용 - 진한 초록
                        elif m:
                            color, label = "#d97706", "△"      # 텍스트만 언급 - 주황 세모
                        else:
                            color, label = "#dc2626", "X"       # 미언급 - 빨강
                        rank_val = r.get("rank")
                        rank_suffix = f" ({rank_val}위)" if rank_val else ""
                        title = f'{ctype}{rank_suffix}' if m else "미언급"
                        cells += f'<td title="{title}" style="text-align:center;padding:6px 8px;font-weight:700;color:{color}">{label}</td>'
            ac = AREA_COLORS.get(p["area"], "#64748b")
            prompt_rows += f"""
            <tr>
              <td style="padding:8px 12px;font-size:12px;font-weight:600;white-space:nowrap;position:sticky;left:0;background:#fff;border-right:2px solid #e2e8f0">
                <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{ac};margin-right:6px"></span>{p['short']}
              </td>{cells}
            </tr>"""

        table_html = f"""
        <table style="border-collapse:collapse;font-size:12px;min-width:100%">
          <thead>
            <tr><th style="position:sticky;left:0;background:#f8fafc;border-right:2px solid #e2e8f0"></th>{header_dates}</tr>
            <tr><th style="text-align:left;padding:8px 12px;position:sticky;left:0;background:#f8fafc;border-right:2px solid #e2e8f0;border-bottom:2px solid #e2e8f0">프롬프트</th>{header_engines}</tr>
          </thead>
          <tbody>
            <tr>
              <td style="padding:8px 12px;font-weight:700;font-size:12px;position:sticky;left:0;background:#fff;border-right:2px solid #e2e8f0">멘션률<br><span style="font-weight:400;font-size:10px;color:#94a3b8">(이름 언급)</span></td>
              {mention_row}
            </tr>
            <tr>
              <td style="padding:8px 12px;font-weight:700;font-size:12px;position:sticky;left:0;background:#fff;border-right:2px solid #e2e8f0;border-bottom:2px solid #e2e8f0">인용률<br><span style="font-weight:400;font-size:10px;color:#94a3b8">(링크 포함)</span></td>
              {citation_row}
            </tr>
            {prompt_rows}
          </tbody>
        </table>
        <div style="margin-top:10px;font-size:11px;color:#64748b">
          <span style="color:#059669;font-weight:700">O</span> = 링크까지 인용됨 &nbsp;·&nbsp;
          <span style="color:#d97706;font-weight:700">△</span> = 이름만 언급(링크 없음) &nbsp;·&nbsp;
          <span style="color:#dc2626;font-weight:700">X</span> = 미언급
        </div>"""

    # ── 변화 추이 차트 데이터 (전체 히스토리 기준, 오름차순) ──
    all_dates_sorted = sorted(by_date.keys())
    trend_all = []
    trend_by_prompt = {p["id"]: [] for p in PROMPTS}
    for d in all_dates_sorted:
        engines_here = by_date[d]
        m_vals = [e.get("mention_rate", e["rate"]) for e in engines_here.values()]
        trend_all.append(round(sum(m_vals) / len(m_vals), 1) if m_vals else None)
        for p in PROMPTS:
            hits = []
            for e in engines_here.values():
                r = e.get("detail", {}).get(p["id"])
                if r is not None:
                    hits.append(1 if r.get("mentioned") else 0)
            trend_by_prompt[p["id"]].append(round(sum(hits) / len(hits) * 100, 1) if hits else None)

    trend_datasets = {"__all__": trend_all}
    trend_datasets.update(trend_by_prompt)
    trend_labels_json = json.dumps(all_dates_sorted, ensure_ascii=False)
    trend_datasets_json = json.dumps(trend_datasets, ensure_ascii=False)

    prompt_buttons_html = '<button class="filter-btn active" data-key="__all__">전체</button>'
    for p in PROMPTS:
        prompt_buttons_html += f'<button class="filter-btn" data-key="{p["id"]}">{p["short"]}</button>'

    trend_section = f"""
  <h2>변화 추이 (멘션률 기준)</h2>
  <div class="subtitle">아래 버튼을 선택하면 해당 항목의 추이만 그래프에 표시됩니다</div>
  <div class="filter-tabs" id="trend-filter">{prompt_buttons_html}</div>
  <div style="border:1px solid #e2e8f0;border-radius:12px;padding:16px">
    <canvas id="trendChart" height="90"></canvas>
  </div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
  <script>
    var trendLabels = {trend_labels_json};
    var trendDatasets = {trend_datasets_json};
    var trendCtx = document.getElementById('trendChart').getContext('2d');
    var trendChart = new Chart(trendCtx, {{
      type: 'line',
      data: {{
        labels: trendLabels,
        datasets: [{{
          label: '전체',
          data: trendDatasets['__all__'],
          borderColor: '#1e293b',
          backgroundColor: 'rgba(30,41,59,0.08)',
          tension: 0.3,
          spanGaps: true,
          fill: true,
        }}]
      }},
      options: {{
        responsive: true,
        scales: {{ y: {{ min: 0, max: 100, ticks: {{ callback: function(v) {{ return v + '%'; }} }} }} }},
        plugins: {{ legend: {{ display: false }} }}
      }}
    }});
    document.querySelectorAll('#trend-filter .filter-btn').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        document.querySelectorAll('#trend-filter .filter-btn').forEach(function(b) {{ b.classList.remove('active'); }});
        btn.classList.add('active');
        var key = btn.getAttribute('data-key');
        trendChart.data.datasets[0].data = trendDatasets[key];
        trendChart.data.datasets[0].label = btn.textContent;
        trendChart.update();
      }});
    }});
  </script>"""

    trend_filter_css = """
  .filter-tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .filter-btn { padding: 6px 14px; border-radius: 20px; border: 1px solid #e2e8f0; background: #fff; color: #64748b; font-size: 12px; font-weight: 600; cursor: pointer; }
  .filter-btn.active { background: #1e293b; color: #fff; border-color: #1e293b; }"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>프롬프트별 전체 추이표 - 트라이그라운드 AEO</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9; padding: 20px; }}
  .container {{ max-width: 1100px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  h1 {{ font-size: 20px; font-weight: 800; color: #1e293b; margin-bottom: 4px; }}
  h2 {{ font-size: 16px; font-weight: 700; color: #1e293b; margin: 28px 0 8px; }}
  .subtitle {{ font-size: 13px; color: #94a3b8; margin-bottom: 16px; }}
  .back-link {{ display: inline-block; margin-bottom: 16px; font-size: 13px; color: #2563eb; text-decoration: none; }}
  .table-wrap {{ overflow-x: auto; border: 1px solid #e2e8f0; border-radius: 12px; }}
  td, th {{ border-bottom: 1px solid #f1f5f9; }}
  tr:hover td {{ background: #fafbfc !important; }}{trend_filter_css}
</style>
</head>
<body>
<div class="container">
  <a class="back-link" href="index.html">← 대시보드로 돌아가기</a>
  <h1>프롬프트별 전체 추이표</h1>
  <div class="subtitle">최근 {len(dates) if dates else 0}회 실행 기준 | O=링크 인용 · △=이름만 언급 · X=미언급 | 셀에 마우스를 올리면 상세 정보가 보입니다</div>
  <div class="table-wrap">{table_html}</div>
  {trend_section}
  <div style="margin-top:20px;padding:12px;background:#fffbeb;border-radius:8px;border:1px solid #fde68a">
    <p style="font-size:11px;color:#92400e;line-height:1.5">⚠ 날짜별 상세(경쟁사, 인용 유형 등)는 대시보드의 실행 기록에서 날짜를 클릭하면 볼 수 있습니다.</p>
  </div>
</div>
</body>
</html>"""


def main():
    import sys
    rebuild_only = "--rebuild-only" in sys.argv

    date = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*50}\n  트라이그라운드 AEO 인용률 체커\n  {date}\n{'='*50}\n")

    output_dir = Path("aeo_results")
    output_dir.mkdir(exist_ok=True)
    history_path = output_dir / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []

    if rebuild_only:
        print("🔧 재빌드 전용 모드 — API를 호출하지 않고 기존 history.json으로만 페이지를 다시 생성합니다. (비용 0원)\n")
        if not history:
            print("⚠ history.json에 데이터가 없습니다. 먼저 한 번은 정상 실행이 필요합니다.")
    else:
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

        for engine in engines_to_run:
            print(f"\n▶ {engine} 체크 시작...")
            results = run_engine(engine)
            s = calc_stats(results)
            print(f"  → 멘션률: {s['mention_rate']:.1f}% ({s['mentioned']}/{s['total']}) | 인용률: {s['citation_rate']:.1f}% ({s['cited']}/{s['total']})")

            history.append({
                "date": date,
                "engine": engine,
                "rate": round(s["mention_rate"], 1),           # 하위호환
                "mention_rate": round(s["mention_rate"], 1),
                "citation_rate": round(s["citation_rate"], 1),
                "mentioned": s["mentioned"],
                "cited": s["cited"],
                "total": s["total"],
                "areas": {a: round(v["rate"], 1) for a, v in s["areas"].items()},               # 하위호환: 멘션률
                "areas_mention": {a: round(v["mention_rate"], 1) for a, v in s["areas"].items()},
                "areas_citation": {a: round(v["citation_rate"], 1) for a, v in s["areas"].items()},
                "detail": results,
            })

        # 저장 (재빌드 전용 모드에서는 새 데이터가 없으니 저장 생략)
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 히스토리 저장: {history_path}")

    # ── 아래는 rebuild_only 여부와 무관하게 항상 실행 (페이지 재생성) ──
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

    matrix_path = output_dir / "matrix.html"
    matrix_path.write_text(generate_matrix_page(history), encoding="utf-8")
    print(f"✅ 프롬프트별 전체 추이표 저장: {matrix_path}")

    print(f"\n{'='*50}\n  완료! aeo_results/index.html 을 브라우저로 여세요.\n{'='*50}\n")


if __name__ == "__main__":
    main()
