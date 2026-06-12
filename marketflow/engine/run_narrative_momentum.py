"""테마 모멘텀 엔진 실행 진입점.

사용법:
  python run_narrative_momentum.py
  python run_narrative_momentum.py --max-files 20 --min-news 2
  python run_narrative_momentum.py --no-telegram
"""
import argparse

from base_runner import ensure_engine_path, maybe_send_telegram

ensure_engine_path()

from narrative_momentum import run, save_results  # noqa: E402


def _build_telegram_message(result: dict) -> str:
    stats = result['stats']
    lines = [
        f"🔥 테마 모멘텀 결과 | {result['date']}",
        f"총 {stats['total']}개  Top테마: {stats['top_theme']}  감성: {stats['avg_sentiment']:+.2f}",
        '',
    ]
    for s in result['signals'][:5]:
        lines.append(f"  [{s['market'][:2]}] {s['name']}  {s['theme']}  {s['score']}점")
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='테마 모멘텀 엔진')
    parser.add_argument('--max-files',   type=int, default=15, help='참조할 jongga 파일 수')
    parser.add_argument('--min-news',    type=int, default=1,  help='최소 뉴스 점수')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    result = run(min_news_score=args.min_news)
    save_results(result)
    maybe_send_telegram(result, _build_telegram_message, args.no_telegram)


if __name__ == '__main__':
    main()
