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
import sys
import time
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

def has_brand(text: str) -> bool:
    """브랜드명 포함 여부. 2글자 별칭('트그')은 '퍼스트그룹'·'웨스트그룹'처럼 다른 단어 속에 우연히 들어간 경우를
    오탐하지 않도록, 앞 글자가 한글이 아닐 때(단어 시작)만 인정한다. ('트그는' 처럼 뒤에 조사가 붙는 건 허용)"""
    lower = text.lower()
    for v in BRAND_VARIANTS:
        v = v.lower()
        if len(v) <= 2:
            if re.search(rf"(?<![가-힣]){re.escape(v)}", lower):
                return True
        elif v in lower:
            return True
    return False


def parse_result(text: str) -> dict:
    lower = text.lower()
    mentioned = has_brand(text)

    rank = None
    if mentioned:
        lines = [l for l in text.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            if has_brand(line):
                m = re.match(r"^(\d+)", line)
                rank = int(m.group(1)) if m else i + 1
                break

    competitors = []
    for line in text.split("\n"):
        m = re.match(r"^\d+[\.\)]\s*\*?\*?\[?\s*([^-\]\*\|]+)", line)
        if m:
            name = m.group(1).strip().replace("[", "").replace("]", "").replace("*", "")
            if name and len(name) < 30 and not has_brand(name):
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

# 엔진별 호출 조건. 호출 함수와 history.json 기록이 같은 값을 쓰므로, 여기를 바꾸면 '측정 조건 변경'으로 대시보드에 자동 표시된다.
# (조건이 다른 실행끼리는 추세를 그대로 비교하면 안 되기 때문)
CALL_SETTINGS = {
    "ChatGPT": {"model": "gpt-4o-mini", "max_output_tokens": 600, "max_searches": 2},   # 검색 1회당 8,000토큰 고정 청구 - 입력단가 낮은 모델일수록 저렴
    "Gemini": {"model": "gemini-2.5-flash", "max_output_tokens": 600, "thinking_budget": 0},
    "Claude": {"model": "claude-haiku-4-5-20251001", "max_output_tokens": 600, "max_searches": 2},   # Sonnet보다 훨씬 저렴 - 업체명/URL 추출 용도로 충분
}


def call_openai(question: str) -> str:
    from openai import OpenAI
    cfg = CALL_SETTINGS["ChatGPT"]
    client = OpenAI(api_key=API_KEYS["openai"])
    response = client.responses.create(
        model=cfg["model"],
        instructions=SYSTEM_PROMPT + f"\n\n비용 절감을 위해 웹검색은 최대 {cfg['max_searches']}회까지만 사용해줘.",
        tools=[{"type": "web_search"}],
        input=question,
        max_output_tokens=cfg["max_output_tokens"],   # 업체 리스트 용도로 충분, 출력 폭주 방지
    )
    return response.output_text or ""


def call_gemini(question: str) -> str:
    from google import genai
    from google.genai import types
    cfg = CALL_SETTINGS["Gemini"]
    client = genai.Client(api_key=API_KEYS["gemini"])
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[grounding_tool],
        max_output_tokens=cfg["max_output_tokens"],   # 업체 리스트 용도로 충분, 출력 비용 상한
        # 2.5 Flash는 생각(thinking) 토큰도 max_output_tokens에 포함됨 → 9/15 이후 응답이 30~58자에서 잘린 원인으로 추정. 생각을 꺼서 답변에 토큰을 온전히 쓴다 (비용도 줄어듦)
        thinking_config=types.ThinkingConfig(thinking_budget=cfg["thinking_budget"]),
    )
    response = client.models.generate_content(
        model=cfg["model"],
        contents=question,
        config=config,
    )
    return response.text or ""


def call_claude(question: str) -> str:
    import anthropic
    cfg = CALL_SETTINGS["Claude"]
    client = anthropic.Anthropic(api_key=API_KEYS["anthropic"])
    response = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_output_tokens"],   # 업체 리스트 용도로 충분, 출력 비용 상한
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": cfg["max_searches"]}],  # 검색 횟수 상한 - 비용 폭주 방지
        messages=[{"role": "user", "content": question}],
    )
    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n".join(text_blocks)


CALL_FN = {"ChatGPT": call_openai, "Gemini": call_gemini, "Claude": call_claude}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 실행
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ── 데이터 신뢰성 설정 ──
MIN_ANSWER_CHARS = 100              # 이보다 짧은 응답은 비었거나 잘린 것으로 보고 무효 처리 (기존 데이터: 정상 응답 최소 270자 / 잘린 응답 최대 58자)
RETRY_DELAYS = [10, 30, 60]         # 일시적 오류(429/5xx/타임아웃) 재시도 전 대기(초)
MIN_CALL_INTERVAL = {"Gemini": 13}  # 무료 티어 분당 5회 제한(GenerateRequestsPerMinute=5) → 호출 시작 간격(초)


def is_transient_error(e: Exception) -> bool:
    """재시도하면 성공할 수 있는 오류인가 (한도 초과·서버 오류·타임아웃). 모델명 오류(404)·잔액 부족(400)은 재시도해도 소용없음."""
    status = getattr(e, "status_code", None) or getattr(e, "code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    return any(k in type(e).__name__ for k in ("Timeout", "Connection"))


def retry_wait(e: Exception, default: float) -> float:
    """Gemini 429가 알려주는 retryDelay(예: '47s')가 있으면 그만큼, 없으면 기본 대기시간"""
    m = re.search(r"retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", str(e))
    return min(float(m.group(1)) + 2, 90) if m else default


def call_with_retry(engine: str, question: str) -> str:
    """일시적 오류는 대기 후 재시도, 잘린/빈 응답은 1회 재시도. 그래도 안 되면 예외를 던지거나 마지막 응답을 반환."""
    short_retried = False
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            text = CALL_FN[engine](question)
        except Exception as e:
            if attempt == len(RETRY_DELAYS) or not is_transient_error(e):
                raise
            wait = retry_wait(e, RETRY_DELAYS[attempt])
            print(f"[재시도 {attempt + 1}/{len(RETRY_DELAYS)}, {wait:.0f}초 대기]", end=" ", flush=True)
            time.sleep(wait)
            continue
        if len((text or "").strip()) >= MIN_ANSWER_CHARS or short_retried:
            return text
        short_retried = True
        print("[응답이 잘려 재시도]", end=" ", flush=True)
    return text


def run_engine(engine: str) -> dict:
    results = {}
    last_call = None
    for i, p in enumerate(PROMPTS):
        print(f"  [{engine}] {i+1}/{len(PROMPTS)}: {p['short']}...", end=" ", flush=True)
        interval = MIN_CALL_INTERVAL.get(engine, 0)
        if last_call is not None and interval:
            time.sleep(max(0, interval - (time.monotonic() - last_call)))
        last_call = time.monotonic()
        try:
            text = call_with_retry(engine, p["q"])
            if len((text or "").strip()) >= MIN_ANSWER_CHARS:
                results[p["id"]] = parse_result(text)
                print("→", "O" if results[p["id"]]["mentioned"] else "X")
            else:
                print(f"→ 오류: 응답이 비었거나 잘림 ({len((text or '').strip())}자)")
                results[p["id"]] = {"mentioned": False, "rank": None, "competitors": [], "citationType": "미언급", "raw": text or "(응답 없음)", "error": True}
        except Exception as e:
            print(f"→ 에러: {e}")
            results[p["id"]] = {"mentioned": False, "rank": None, "competitors": [], "citationType": "미언급", "raw": str(e), "error": True}
    return results


LINK_CITATION_TYPES = {"홈페이지 링크", "블로그 링크", "contractup 링크"}

# 에러 원문 패턴 — error 플래그가 없던 예전 history.json 기록도 에러로 판별하기 위함
ERROR_RAW_PATTERN = re.compile(r"^(Error code: \d+|\d{3} [A-Z_]+\.)")


def is_error_result(r: dict) -> bool:
    """API 에러이거나 응답이 비었거나 잘렸으면 True. '미언급(X)'이 아니라 무효 응답이므로 인용률 집계에서 제외한다."""
    raw = str(r.get("raw", "")).strip()
    return bool(r.get("error")) or bool(ERROR_RAW_PATTERN.match(raw)) or len(raw) < MIN_ANSWER_CHARS


# 유효 응답이 이 비율 미만이면 표본이 너무 작아(예: 10개 중 1개 응답 = 100%) 그 실행은 통계에서 통째로 제외
MIN_VALID_RATIO = 0.5


def calc_stats(results: dict) -> dict:
    valid = {pid: r for pid, r in results.items() if not is_error_result(r)}
    errors = len(results) - len(valid)
    if len(valid) < len(PROMPTS) * MIN_VALID_RATIO:
        valid = {}
    total = len(valid)
    mentioned = sum(1 for r in valid.values() if r["mentioned"])
    cited = sum(1 for r in valid.values() if r.get("citationType") in LINK_CITATION_TYPES)
    mention_rate = (mentioned / total * 100) if total else 0
    citation_rate = (cited / total * 100) if total else 0

    area_stats = {}
    for area in AREAS:
        aps = [p for p in PROMPTS if p["area"] == area and p["id"] in valid]
        am = sum(1 for p in aps if valid[p["id"]]["mentioned"])
        ac = sum(1 for p in aps if valid[p["id"]].get("citationType") in LINK_CITATION_TYPES)
        area_stats[area] = {
            "total": len(aps),
            "mentioned": am,
            "cited": ac,
            "rate": (am / len(aps) * 100) if aps else 0,          # 하위호환: 멘션률
            "mention_rate": (am / len(aps) * 100) if aps else 0,
            "citation_rate": (ac / len(aps) * 100) if aps else 0,
        }

    return {
        "total": total,                  # 에러 응답을 뺀 유효 응답 수 (비율의 분모)
        "errors": errors,
        "mentioned": mentioned,
        "cited": cited,
        "rate": mention_rate,            # 하위호환: 기존 코드가 참조하던 'rate'는 멘션률
        "mention_rate": mention_rate,
        "citation_rate": citation_rate,
        "areas": area_stats,
    }


def stats_fields(s: dict) -> dict:
    """calc_stats 결과를 history.json 한 건에 저장되는 필드로 변환"""
    return {
        "rate": round(s["mention_rate"], 1),           # 하위호환
        "mention_rate": round(s["mention_rate"], 1),
        "citation_rate": round(s["citation_rate"], 1),
        "mentioned": s["mentioned"],
        "cited": s["cited"],
        "total": s["total"],
        "errors": s["errors"],
        "areas": {a: round(v["rate"], 1) for a, v in s["areas"].items()},               # 하위호환: 멘션률
        "areas_mention": {a: round(v["mention_rate"], 1) for a, v in s["areas"].items()},
        "areas_citation": {a: round(v["citation_rate"], 1) for a, v in s["areas"].items()},
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 통합 대시보드 HTML 생성 (전체 히스토리 기반, 매번 덮어씀)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def settings_changes(prev: dict, cur: dict) -> list:
    """직전 실행 대비 바뀐 측정 조건 목록 (예: ['model: gpt-4o-mini-search-preview → gpt-4o-mini'])"""
    return [f"{k}: {prev.get(k)} → {cur.get(k)}" for k in sorted(set(prev) | set(cur)) if prev.get(k) != cur.get(k)]


def build_setting_changes(history: list) -> dict:
    """{(날짜, 엔진): 직전 실행 대비 바뀐 조건 목록}. 조건 기록(settings)이 있는 같은 엔진의 연속된 두 실행끼리만 비교한다."""
    changes, last = {}, {}
    for e in sorted(history, key=lambda x: x["date"]):
        cur = e.get("settings")
        prev = last.get(e["engine"])
        if cur is not None and prev is not None and cur != prev:
            changes[(e["date"], e["engine"])] = settings_changes(prev, cur)
        if cur is not None:
            last[e["engine"]] = cur
    return changes


def add_entry(history: list, entry: dict) -> str:
    """같은 날짜·엔진 기록이 이미 있으면 유효 응답이 더 많은 쪽만 남긴다(같으면 새 기록).
    재실행이 정상 데이터를 더 나쁜 데이터로 덮어쓰거나, 같은 날 기록이 중복 집계되는 것을 막는다."""
    for i, old in enumerate(history):
        if old["date"] == entry["date"] and old["engine"] == entry["engine"]:
            if entry["total"] >= old.get("total", 0):
                history[i] = entry
                return "replaced"
            return "kept_old"
    history.append(entry)
    return "added"


def validate_history(history: list) -> list:
    """history.json 무결성 자동 검사. 문제 목록(문자열)을 반환하고, 비어 있으면 정상.
    ① 날짜·엔진 형식 ② 같은 날짜·엔진 중복 ③ 프롬프트 누락 ④ 저장된 집계 = 응답 원문 재계산 결과인지 ⑤ 멘션 판정 = 현재 브랜드 감지 규칙인지"""
    issues, seen = [], set()
    for e in history:
        tag = f'{e.get("date")} {e.get("engine")}'
        try:
            datetime.strptime(e["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            issues.append(f"{tag}: 날짜 형식 이상")
            continue
        if e.get("engine") not in ENGINES:
            issues.append(f"{tag}: 알 수 없는 엔진")
            continue
        if (e["date"], e["engine"]) in seen:
            issues.append(f"{tag}: 같은 날짜·엔진 기록이 중복됨 (대시보드에는 마지막 것만 보임)")
        seen.add((e["date"], e["engine"]))
        detail = e.get("detail")
        if not detail:
            issues.append(f"{tag}: 응답 원문(detail)이 없어 검증 불가")
            continue
        missing = [p["id"] for p in PROMPTS if p["id"] not in detail]
        if missing:
            issues.append(f"{tag}: 프롬프트 {missing} 결과 없음")
        fresh = stats_fields(calc_stats(detail))
        bad = [k for k in ("mentioned", "cited", "total", "errors", "mention_rate", "citation_rate") if e.get(k) != fresh[k]]
        if bad:
            issues.append(f"{tag}: 저장된 집계({', '.join(bad)})가 응답 원문 재계산 결과와 다름")
        flips = [pid for pid, r in detail.items() if not is_error_result(r) and r.get("mentioned") != parse_result(r["raw"])["mentioned"]]
        if flips:
            issues.append(f"{tag}: {flips} 멘션 판정이 현재 브랜드 감지 규칙과 다름 (규칙을 바꿨다면 재계산 필요)")
    return issues


def generate_dashboard(history: list) -> str:
    OVERALL_COLOR = "#1e293b"
    setting_changes = build_setting_changes(history)

    if not history:
        current_month = None
    else:
        current_month = sorted(e["date"] for e in history)[-1][:7]

    def engine_month_avg(eng, month):
        ents = [e for e in history if e["engine"] == eng and month and e["date"][:7] == month and e.get("total", 0) > 0]
        if not ents:
            return None
        m = sum(e.get("mention_rate", e["rate"]) for e in ents) / len(ents)
        c = sum(e.get("citation_rate", 0) for e in ents) / len(ents)
        mixed = len({json.dumps(e.get("settings"), sort_keys=True) for e in ents}) > 1   # 측정 조건이 다른 실행이 섞여 있는가
        return {"mention_rate": m, "citation_rate": c, "count": len(ents), "mixed": mixed}

    engine_month_stats = {eng: engine_month_avg(eng, current_month) for eng in ENGINES}
    valid_month_stats = [v for v in engine_month_stats.values() if v]
    overall_month = None
    if valid_month_stats:
        overall_month = {
            "mention_rate": sum(v["mention_rate"] for v in valid_month_stats) / len(valid_month_stats),
            "citation_rate": sum(v["citation_rate"] for v in valid_month_stats) / len(valid_month_stats),
            "count": sum(v["count"] for v in valid_month_stats),
            "mixed": any(v["mixed"] for v in valid_month_stats),
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
        mixed_note = '<div style="font-size:10px;color:#d97706;margin-top:4px" title="이 평균에는 모델·토큰 한도 등 측정 조건이 다른 실행이 섞여 있어 추세 비교에 주의가 필요합니다">⚠ 측정 조건이 다른 실행 포함</div>' if stats.get("mixed") else ""
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
          {mixed_note}
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
        ok_engines = [eng for eng in present if engines_here[eng].get("total", 0) > 0]   # 에러만 난 엔진은 평균에서 제외
        m_vals = [engines_here[eng].get("mention_rate", engines_here[eng]["rate"]) for eng in ok_engines]
        c_vals = [engines_here[eng].get("citation_rate", 0) for eng in ok_engines]
        if m_vals:
            avg_m = sum(m_vals) / len(m_vals)
            avg_c = sum(c_vals) / len(c_vals)
            am_color = "#059669" if avg_m >= 30 else "#dc2626"
            ac_color = "#059669" if avg_c >= 30 else "#dc2626"
            area_avg = {}
            for a in AREAS:
                a_vals = [engines_here[eng].get("areas", {}).get(a, 0) for eng in ok_engines]
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
              <td style="padding:10px 12px;text-align:center;color:#64748b;font-size:12px">{len(ok_engines)}개 엔진 평균</td>
              <td style="padding:10px 12px;color:#94a3b8;font-size:11px">{area_str}</td>
            </tr>"""
        for eng in present:
            entry = engines_here[eng]
            color = ENGINE_COLORS.get(eng, "#64748b")
            m_rate = entry.get("mention_rate", entry["rate"])
            c_rate = entry.get("citation_rate", 0)
            has_data = entry.get("total", 0) > 0
            m_color = "#059669" if m_rate >= 30 else "#dc2626"
            c_color = "#059669" if c_rate >= 30 else "#dc2626"
            areas = entry.get("areas", {})
            area_str = " · ".join([f'{a} {areas.get(a, 0):.0f}%' for a in AREAS])
            m_txt, c_txt = f"{m_rate:.0f}%", f"{c_rate:.0f}%"
            count_txt = f"{entry['mentioned']}/{entry['total']}"
            if not has_data:   # 전부 API 에러 → 0%가 아니라 '오류'로 표시
                m_txt = c_txt = "오류"
                m_color = c_color = "#94a3b8"
                count_txt = "–"
                area_str = "오류·잘림으로 집계 제외"
            if entry.get("errors"):
                count_txt += f' <span style="color:#d97706">· 오류·잘림 {entry["errors"]}건 제외</span>'
            # 어떤 조건(모델)으로 측정했는지 표시하고, 직전 실행과 조건이 달라졌으면 눈에 띄게 알린다
            settings = entry.get("settings") or {}
            model_txt = f'<div style="font-size:10px;color:#94a3b8;margin-top:3px">{settings["model"]}</div>' if settings.get("model") else ""
            changed = setting_changes.get((d, eng))
            if changed:
                change_txt = "; ".join(changed)
                model_txt += f'<div style="font-size:10px;color:#d97706;margin-top:2px;cursor:help" title="직전 실행 대비: {change_txt}">⚙ 측정 조건 변경</div>'
            rows += f"""
            <tr data-engine="{eng}">
              <td style="padding:10px 12px;font-weight:600">
                <a href="reports/{d}.html" style="color:#2563eb;text-decoration:none">{d} →</a>
              </td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{eng}</span>{model_txt}</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{m_color}">{m_txt}</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;font-size:14px;color:{c_color}">{c_txt}</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b;font-size:12px">{count_txt}</td>
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
            ents = [e for e in months_data[mk].get(eng, []) if e.get("total", 0) > 0]   # 에러만 난 실행은 월 평균에서 제외
            if ents:
                eng_avgs[eng] = {
                    "m": sum(e.get("mention_rate", e["rate"]) for e in ents) / len(ents),
                    "c": sum(e.get("citation_rate", 0) for e in ents) / len(ents),
                    "n": len(ents),
                    "mixed": len({json.dumps(e.get("settings"), sort_keys=True) for e in ents}) > 1,
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
            mixed_txt = '<div style="font-size:10px;color:#d97706">⚠ 측정 조건 혼합</div>' if v["mixed"] else ""
            m_rows += f"""
            <tr data-engine="{eng}">
              <td style="padding:10px 12px;font-weight:600;color:#334155">{mk}</td>
              <td style="padding:10px 12px;text-align:center"><span style="font-size:11px;font-weight:700;color:{color};background:{color}15;padding:3px 10px;border-radius:8px">{eng}</span></td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{m_color}">{v['m']:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;font-weight:800;color:{c_color}">{v['c']:.1f}%</td>
              <td style="padding:10px 12px;text-align:center;color:#64748b">{v['n']}회{mixed_txt}</td>
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
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="alternate icon" href="favicon.ico">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
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
  function applyFilter(eng) {{
    var rows = document.querySelectorAll('#' + tbodyId + ' tr[data-engine]');
    rows.forEach(function(tr) {{
      tr.style.display = (tr.getAttribute('data-engine') === eng) ? '' : 'none';
    }});
  }}
  buttons.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      buttons.forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      applyFilter(btn.getAttribute('data-engine'));
    }});
  }});
  var activeBtn = container.querySelector('.filter-btn.active') || buttons[0];
  if (activeBtn) applyFilter(activeBtn.getAttribute('data-engine'));
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
            if is_error_result(r):
                badge, bg, fg = "!", "#e2e8f0", "#475569"
                ctype = "오류·잘림 (집계 제외)"
            elif is_linked:
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
            aps = [p for p in PROMPTS if p["area"] == area and not is_error_result(detail.get(p["id"], {}))]
            am = sum(1 for p in aps if detail.get(p["id"], {}).get("mentioned", False))
            area_boxes += f'''<div style="text-align:center;padding:10px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0">
              <div style="font-size:16px;font-weight:800;color:{AREA_COLORS[area]}">{am_rate:.0f}% <span style="font-size:11px;color:#94a3b8;font-weight:400">/ {ac_rate:.0f}%</span></div>
              <div style="font-size:11px;color:#64748b">{area} ({am}/{len(aps)})</div></div>'''

        m_rate = entry.get("mention_rate", s["rate"])
        c_rate = entry.get("citation_rate", 0)
        m_color = "#059669" if m_rate >= 30 else "#dc2626"
        c_color = "#059669" if c_rate >= 30 else "#dc2626"
        m_txt, c_txt = f"{m_rate:.0f}%", f"{c_rate:.0f}%"
        error_note = ""
        if entry.get("errors"):
            error_note = f'<span style="font-size:12px;font-weight:700;color:#d97706">⚠ 오류·잘림 {entry["errors"]}건은 집계에서 제외됨</span>'
        if entry.get("total", 0) == 0:   # 유효 응답 부족 → 이 실행은 통계 제외
            m_txt = c_txt = "–"
            m_color = c_color = "#94a3b8"
            area_boxes = '<div style="grid-column:1/-1;text-align:center;padding:10px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0;font-size:12px;color:#94a3b8">오류·잘림으로 유효 응답이 부족해 통계에서 제외된 실행입니다</div>'
        cards_html += f"""
        <div style="margin-bottom:32px">
          <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px;flex-wrap:wrap">
            <span style="font-size:18px;font-weight:800;color:{color}">{engine}</span>
            <div>
              <span style="font-size:24px;font-weight:800;color:{m_color}">{m_txt}</span>
              <span style="font-size:11px;color:#94a3b8">멘션률</span>
            </div>
            <div>
              <span style="font-size:24px;font-weight:800;color:{c_color}">{c_txt}</span>
              <span style="font-size:11px;color:#94a3b8">인용률</span>
            </div>
            <span style="font-size:13px;color:#94a3b8">({s['mentioned']}/{s['total']})</span>
            {error_note}
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
<link rel="icon" type="image/svg+xml" href="../favicon.svg">
<link rel="alternate icon" href="../favicon.ico">
<link rel="apple-touch-icon" href="../apple-touch-icon.png">
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
                if e and e.get("total", 0) > 0:
                    m_rate = e.get("mention_rate", e["rate"])
                    c_rate = e.get("citation_rate", 0)
                    mc = "#059669" if m_rate >= 30 else "#dc2626"
                    cc = "#059669" if c_rate >= 30 else "#dc2626"
                    mention_row += f'<td style="text-align:center;padding:6px 8px;font-weight:800;font-size:13px;color:{mc};background:#f8fafc">{m_rate:.0f}%</td>'
                    citation_row += f'<td style="text-align:center;padding:6px 8px;font-weight:800;font-size:13px;color:{cc};background:#fdfdfd">{c_rate:.0f}%</td>'
                else:
                    label = "오류" if e else "–"   # 실행은 했지만 전부 API 에러 → '오류', 실행 기록 없음 → '–'
                    mention_row += f'<td style="text-align:center;padding:6px 8px;color:#94a3b8;background:#f8fafc;font-size:11px">{label}</td>'
                    citation_row += f'<td style="text-align:center;padding:6px 8px;color:#94a3b8;background:#fdfdfd;font-size:11px">{label}</td>'

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
                    elif is_error_result(r):
                        cells += '<td title="오류·잘림 (집계 제외)" style="text-align:center;padding:6px 8px;font-weight:700;color:#94a3b8">!</td>'
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
          <span style="color:#dc2626;font-weight:700">X</span> = 미언급 &nbsp;·&nbsp;
          <span style="color:#94a3b8;font-weight:700">!</span> = 오류·잘림(집계 제외)
        </div>"""

    # ── 변화 추이 차트 데이터 (전체 히스토리 기준, 오름차순) ──
    all_dates_sorted = sorted(by_date.keys())
    trend_all = []
    trend_by_prompt = {p["id"]: [] for p in PROMPTS}
    for d in all_dates_sorted:
        engines_here = by_date[d]
        m_vals = [e.get("mention_rate", e["rate"]) for e in engines_here.values() if e.get("total", 0) > 0]
        trend_all.append(round(sum(m_vals) / len(m_vals), 1) if m_vals else None)
        for p in PROMPTS:
            hits = []
            for e in engines_here.values():
                r = e.get("detail", {}).get(p["id"])
                if r is not None and not is_error_result(r):
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
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<link rel="alternate icon" href="favicon.ico">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
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
    rebuild_only = "--rebuild-only" in sys.argv

    date = datetime.now().strftime("%Y-%m-%d")
    print(f"\n{'='*50}\n  트라이그라운드 AEO 인용률 체커\n  {date}\n{'='*50}\n")

    output_dir = Path("aeo_results")
    output_dir.mkdir(exist_ok=True)
    history_path = output_dir / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    warnings, problems = [], []   # 데이터 결측 알림용 — 실행 끝에 GitHub Actions 경고/실패로 표시

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
            msg = "실행할 엔진이 없음 — API 키가 하나도 설정되지 않음"
            print(f"\n❌ {msg}")
            problems.append(msg)

        for engine in engines_to_run:
            print(f"\n▶ {engine} 체크 시작...")
            results = run_engine(engine)
            s = calc_stats(results)
            print(f"  → 멘션률: {s['mention_rate']:.1f}% ({s['mentioned']}/{s['total']}) | 인용률: {s['citation_rate']:.1f}% ({s['cited']}/{s['total']})")
            if s["total"] == 0:
                msg = f"{engine}: 유효 응답이 {len(results) - s['errors']}/{len(results)}개뿐이라 이번 실행이 통계에서 제외됨 (API 키/모델명/결제·한도 확인 필요)"
                print(f"  ⚠ {msg}")
                problems.append(msg)
            elif s["errors"]:
                msg = f"{engine}: 오류·잘림 {s['errors']}건 제외, {s['total']}/{len(results)}개로 집계됨"
                print(f"  ⚠ {msg}")
                warnings.append(msg)

            entry = {
                "date": date,
                "engine": engine,
                "settings": dict(CALL_SETTINGS[engine]),   # 이번 측정에 쓴 조건 — 나중에 조건이 바뀌면 비교할 수 있도록 함께 저장
                **stats_fields(s),
                "detail": results,
            }
            prev = next((e for e in reversed(history) if e["engine"] == engine and e.get("settings") and e["date"] < date), None)
            if prev and prev["settings"] != entry["settings"]:
                chg = "; ".join(settings_changes(prev["settings"], entry["settings"]))
                msg = f"{engine}: 측정 조건이 직전 실행과 달라짐 ({chg}) — 이전 데이터와 추세를 비교할 때 주의"
                print(f"  ⚙ {msg}")
                warnings.append(msg)
            status = add_entry(history, entry)
            if status == "replaced":
                print(f"  ℹ 오늘자 {engine} 기록이 이미 있어 이번 결과로 교체했습니다.")
            elif status == "kept_old":
                print(f"  ℹ 오늘자 {engine} 기록이 이번 결과보다 유효 응답이 많아 기존 기록을 유지했습니다.")

        # 저장 (재빌드 전용 모드에서는 새 데이터가 없으니 저장 생략)
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 히스토리 저장: {history_path}")

    # 저장된 데이터 자체의 무결성 자동 검사 (집계 불일치·중복·누락 등) — 문제가 있으면 Actions 경고로 표시
    for issue in validate_history(history):
        print(f"  ⚠ 데이터 검증: {issue}")
        warnings.append(f"데이터 검증: {issue}")

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

    # 이번 실행 요약 저장 (Slack 알림용). aeo_results/ 밖(저장소 루트)에 둬서 커밋 대상(`git add aeo_results/`)에 안 걸리는 실행 시점 임시 파일.
    status_path = Path("last_run_status.json")
    status_path.write_text(
        json.dumps({"date": date, "mode": "rebuild-only" if rebuild_only else "check", "warnings": warnings, "problems": problems}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # GitHub Actions 주석 문법 — 결측이 있어도 초록불로 조용히 지나가지 않게 한다 (로컬에선 그냥 텍스트로 출력됨)
    for w in warnings:
        print(f"::warning title=AEO 데이터 일부 결측::{w}")
    for pb in problems:
        print(f"::error title=AEO 실행 통계 제외::{pb}")
    if problems:
        sys.exit(1)   # 결과 저장·페이지 생성은 끝난 뒤이므로 데이터는 남고, Actions 실행만 실패로 표시됨


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Slack 알림 (GitHub Actions 실행 뒤 결과 전송용)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_slack_payload(status: dict, history: list) -> dict:
    """실행 요약(status) + 히스토리를 Slack Block Kit 메시지로 변환한다. Webhook 전송은 워크플로의 curl이 담당하고,
    이 함수는 순수하게 payload dict만 만들어서(네트워크 호출 없음) 테스트하기 쉽게 한다."""
    date = status.get("date", "-")
    mode = status.get("mode", "check")
    warnings_ = status.get("warnings", [])
    problems = status.get("problems", [])
    todays = {e["engine"]: e for e in history if e["date"] == date}

    title = f"🔧 AEO 페이지 재빌드 - {date}" if mode == "rebuild-only" else f"📊 트라이그라운드 AEO 체크 - {date}"

    fields = []
    for eng in ENGINES:
        e = todays.get(eng)
        if e is None:
            fields.append({"type": "mrkdwn", "text": f"*{eng}*\n(이번 실행에 없음)"})
        elif e.get("total", 0) == 0:
            fields.append({"type": "mrkdwn", "text": f"*{eng}*\n:warning: 전부 오류·잘림으로 제외됨"})
        else:
            err_note = f" · 오류 {e['errors']}건 제외" if e.get("errors") else ""
            fields.append({
                "type": "mrkdwn",
                "text": f"*{eng}*\n멘션 {e['mention_rate']:.0f}% · 인용 {e['citation_rate']:.0f}% ({e['mentioned']}/{e['total']}){err_note}",
            })

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "section", "fields": fields},
    ]
    if problems:
        text = "\n".join(f"• {p}" for p in problems)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":red_circle: *문제*\n{text}"}})
    if warnings_:
        text = "\n".join(f"• {w}" for w in warnings_)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f":warning: *주의*\n{text}"}})

    # GitHub Actions 환경변수가 있으면 대시보드·실행 로그 링크를 붙인다 (로컬 실행 시엔 조용히 생략)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    links = []
    if repo:
        # htmlpreview.github.io: GitHub Pages 설정 없이도 저장소의 raw HTML을 렌더링해서 보여주는 무료 서비스
        dash_url = f"https://htmlpreview.github.io/?{server}/{repo}/blob/main/aeo_results/index.html"
        links.append(f"<{dash_url}|대시보드 보기>")
    if repo and run_id:
        links.append(f"<{server}/{repo}/actions/runs/{run_id}|실행 로그>")
    if links:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(links)}]})

    fallback = title + (" - 문제 있음" if problems else " - 주의 필요" if warnings_ else " - 정상")
    return {"text": fallback, "blocks": blocks}


def print_slack_payload() -> None:
    """`python aeo_checker.py --slack-payload` — API를 호출하지 않고, 직전 main() 실행이 남긴
    last_run_status.json + history.json으로 Slack 메시지 JSON을 stdout에 출력한다."""
    status_path = Path("last_run_status.json")
    history_path = Path("aeo_results") / "history.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
    else:
        status = {"date": datetime.now().strftime("%Y-%m-%d"), "mode": "check", "warnings": [], "problems": ["last_run_status.json이 없음 — main()이 먼저 실행되어야 함"]}
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else []
    print(json.dumps(build_slack_payload(status, history), ensure_ascii=False))


if __name__ == "__main__":
    if "--slack-payload" in sys.argv:
        print_slack_payload()
    else:
        main()
