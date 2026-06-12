"""Gemini 기반 뉴스 분석기"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List

from dotenv import load_dotenv
from google import genai
from google.genai import types

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.env')

log = logging.getLogger(__name__)


class GeminiAnalyzer:
    def __init__(self):
        load_dotenv(_ENV_PATH)
        api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("GEMINI_BASIC_MODEL", "gemini-2.5-flash")

        if not api_key:
            log.warning("GEMINI_API_KEY 미설정 — 키워드 fallback 모드로 동작")
            self.client = None
            return

        try:
            self.client = genai.Client(api_key=api_key)
        except Exception as e:
            log.error("GeminiAnalyzer 초기화 실패: %s", e)
            self.client = None

    @staticmethod
    def _parse_json_response(text: str) -> Dict:
        """LLM 응답에서 JSON을 안전하게 추출한다."""
        # 1단계: 그대로 파싱
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2단계: ```json ``` 코드블록 제거 후 재시도
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 3단계: 첫 '{' ~ 마지막 '}' 구간 추출
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])

        raise json.JSONDecodeError("JSON 추출 실패", text, 0)

    async def analyze_news(self, stock_name: str, news_items: List[Dict]) -> Dict:
        """뉴스 기반 종목 호재/악재 점수를 분석한다.

        Args:
            stock_name: 종목명
            news_items: [{"title": "...", "summary": "..."}, ...]

        Returns:
            {"score": int, "reason": str, "themes": list[str], "source": str}
        """
        if not news_items:
            return {"score": 0, "reason": "뉴스 없음", "themes": [], "source": "none"}

        # Gemini 초기화 실패 → 바로 키워드 분석
        if not self.client:
            log.debug("Gemini client 없음, 키워드 분석 사용 (%s)", stock_name)
            result = self._fallback_keyword_analysis(news_items)
            result["source"] = "keyword_fallback"
            return result

        # 뉴스 텍스트 조합
        news_text = ""
        for i, item in enumerate(news_items, 1):
            title = item.get("title", "")
            summary = item.get("summary", "")
            news_text += f"[뉴스 {i}] {title}\n{summary}\n\n"

        prompt = f"""당신은 한국 주식 시장 전문 애널리스트입니다.
아래는 [{stock_name}] 종목의 최신 뉴스입니다.

{news_text}

위 뉴스를 종합 분석하여 해당 종목의 호재/악재 점수를 매겨주세요.

점수 기준:
- 3점: 확실한 호재 (대규모 수주, 어닝 서프라이즈, 신약 승인, M&A)
- 2점: 긍정적 호재 (실적 개선, 신사업 기대감, 테마 상승 모멘텀)
- 1점: 중립적 소식 (일반 뉴스, 큰 영향 없음)
- 0점: 악재 또는 호재 없음

[테마 예시]
로봇, AI반도체, 2차전지, 방산, 우주항공, 바이오, 그룹재편, M&A,
HBM, 전력반도체, 태양광, 원전, 조선, 리튬, 자율주행, 엔터, 게임, 제약

테마 규칙:
- 테마는 반드시 1~3개만 추출할 것
- 뉴스 내용과 직접 관련된 테마만 넣을 것
- 관련 테마가 없으면 빈 배열 []로 반환할 것

반드시 아래 JSON 형식으로만 응답하세요:
{{"score": 2, "reason": "이유 한 줄", "themes": ["테마1", "테마2"]}}"""

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.client.models.generate_content,
                    model=self.model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                    ),
                ),
                timeout=60,
            )
            parsed = self._parse_json_response(response.text)
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed else {}
            parsed["source"] = "gemini"
            # Gemini rate-limit 방지 (최대 20 RPM 준수)
            await asyncio.sleep(3.0)
            return parsed

        except asyncio.TimeoutError:
            log.warning("Gemini 타임아웃 60초 초과, 키워드 분석으로 대체 (%s)", stock_name)
            result = self._fallback_keyword_analysis(news_items)
            result["source"] = "keyword_fallback"
            return result

        except json.JSONDecodeError as e:
            log.warning("Gemini JSON 파싱 실패, 키워드 분석으로 대체 (%s): %s", stock_name, e)
            result = self._fallback_keyword_analysis(news_items)
            result["source"] = "keyword_fallback"
            return result

        except Exception as e:
            log.warning("Gemini 에러, 키워드 분석으로 대체 (%s): %s", stock_name, e)
            result = self._fallback_keyword_analysis(news_items)
            result["source"] = "keyword_fallback"
            return result

    def _fallback_keyword_analysis(self, news_items: List[Dict]) -> Dict:
        """키워드 기반 백업 분석 (Gemini 실패 시 사용)."""
        if not news_items:
            return {"score": 0, "reason": "No news", "themes": []}

        positive_kw = [
            "흑자전환", "실적개선", "어닝서프라이즈", "사상최대", "호실적",
            "수주", "계약체결", "공급계약", "MOU", "신약", "임상성공",
            "FDA승인", "특허", "기술이전", "외국인매수", "기관매수",
        ]
        negative_kw = [
            "횡령", "배임", "상장폐지", "관리종목", "적자전환", "적자확대",
            "검찰", "수사", "기소", "대량매도",
        ]

        # 전체 뉴스 텍스트 합치기
        full_text = ""
        for item in news_items:
            full_text += item.get("title", "") + " " + item.get("summary", "") + " "

        # 부정 키워드 → 즉시 0점
        for kw in negative_kw:
            if kw in full_text:
                return {"score": 0, "reason": f"Negative news detected: {kw}", "themes": []}

        # 긍정 키워드 카운트
        hit = sum(1 for kw in positive_kw if kw in full_text)
        score = min(1 + hit, 3)

        return {"score": score, "reason": "Keyword analysis", "themes": []}


async def run_news_analysis(stocks: List) -> List[Dict]:
    """필터링된 후보 종목의 뉴스를 수집하고 Gemini로 분석한다.

    Args:
        stocks: StockData 리스트 (code, name 속성 필요)

    Returns:
        분석 결과 리스트 [{"name", "code", "score", "reason", "themes"}, ...]
    """
    from collectors import get_stock_news

    analyzer = GeminiAnalyzer()
    results = []
    start_time = time.time()

    log.info("=" * 70)
    log.info("  뉴스 기반 Gemini 분석 시작 (%d개 종목)", len(stocks))
    log.info("=" * 70)

    for i, stock in enumerate(stocks, 1):
        name = stock.name
        code = stock.code

        # a. 뉴스 수집
        news_list = get_stock_news(code, name, limit=3)
        news_items = [
            {"title": n.title, "summary": n.summary}
            for n in news_list
        ]

        # b. Gemini 분석
        result = await analyzer.analyze_news(name, news_items)

        score = result.get("score", 0)
        reason = result.get("reason", "")
        themes = result.get("themes", [])
        source = result.get("source", "unknown")

        results.append({
            "name": name,
            "code": code,
            "score": score,
            "reason": reason,
            "themes": themes,
            "source": source,
        })

        # d. 진행 상황 출력
        log.info("  [%d/%d] %s 분석 완료... score=%s (%s)", i, len(stocks), name, score, source)

    elapsed = time.time() - start_time

    # 결과 테이블 출력
    log.info("")
    log.info("=" * 70)
    log.info("  %2s | %-12s | %4s | %-24s | 이유", "#", "종목명", "점수", "테마")
    log.info("-" * 70)
    for i, r in enumerate(results, 1):
        themes_str = ", ".join(r["themes"]) if r["themes"] else "-"
        reason_short = r["reason"][:30] + ("..." if len(r["reason"]) > 30 else "")
        log.info("  %2d | %-12s | %2s/3 | %-24s | %s", i, r["name"], r["score"], themes_str, reason_short)
    log.info("=" * 70)
    log.info("  소요 시간: %.1f초", elapsed)

    return results


if __name__ == "__main__":
    from config import SignalConfig
    from collectors import get_top_gainers

    config = SignalConfig()
    kospi = get_top_gainers("KOSPI", config)
    kosdaq = get_top_gainers("KOSDAQ", config)
    combined = sorted(kospi + kosdaq, key=lambda x: x.change_pct, reverse=True)

    # 상위 3개만 테스트
    test_stocks = combined[:3]
    log.info("\n  테스트 대상: %s\n", ", ".join(s.name for s in test_stocks))

    asyncio.run(run_news_analysis(test_stocks))
