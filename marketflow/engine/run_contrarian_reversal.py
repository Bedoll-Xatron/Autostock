"""역발상 반전 엔진 실행 진입점.

사용법:
  python run_contrarian_reversal.py
  python run_contrarian_reversal.py --top-n 60 --max-rsi 35
  python run_contrarian_reversal.py --no-telegram
"""
import argparse

from base_runner import ensure_engine_path, maybe_send_telegram

ensure_engine_path()

from contrarian_reversal import run, save_results  # noqa: E402


def _build_telegram_message(result: dict) -> str:
    stats = result['stats']
    lines = [
        f"↩️ 역발상 반전 결과 | {result['date']}",
        f"총 {stats['total']}개  고확률: {stats['high_prob']}개  평균과매도: {stats['avg_oversold']:.1f}",
        '',
    ]
    for s in result['signals'][:5]:
        lines.append(
            f"  [{s['market'][:2]}] {s['name']}  "
            f"RSI:{s['rsi']:.1f}  확률:{s['reversal_probability']:.0%}"
        )
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='역발상 반전 엔진')
    parser.add_argument('--top-n',       type=int, default=50, help='마켓별 하락 종목 수집 수')
    parser.add_argument('--max-rsi',     type=int, default=40, help='최대 RSI (과매도 기준)')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    result = run(top_n=args.top_n, min_rsi_threshold=args.max_rsi)
    save_results(result)
    maybe_send_telegram(result, _build_telegram_message, args.no_telegram)


if __name__ == '__main__':
    main()
