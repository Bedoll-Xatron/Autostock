"""
수급 모멘텀 엔진 실행 진입점
==============================
사용법:
  python run_flow_momentum.py               # 기본 실행
  python run_flow_momentum.py --top-n 50    # 마켓별 50개 후보
  python run_flow_momentum.py --min-flow 2  # flow_score 2점 이상만 저장
  python run_flow_momentum.py --no-telegram # 텔레그램 알림 비활성화
"""
import argparse

from base_runner import ensure_engine_path, maybe_send_telegram

ensure_engine_path()

from flow_momentum import run, save_results  # noqa: E402


def _build_telegram_message(result: dict) -> str:
    stats   = result['stats']
    signals = result['signals']
    strong  = [s for s in signals if s['signal_strength'] == 'strong']
    mod     = [s for s in signals if s['signal_strength'] == 'moderate']

    lines = [
        f"📊 수급 모멘텀 결과 | {result['date']}",
        f"총 {stats['total']}개  강:{stats['strong']} 중:{stats['moderate']} 약:{stats['weak']}",
        '',
    ]

    if strong:
        lines.append('🔥 강한 수급 종목:')
        for s in strong[:5]:
            lines.append(
                f"  [{s['market'][:2]}] {s['name']}  {s['score']}점 "
                f"외:{s['foreign_flow']:+.0f}억 기:{s['institution_flow']:+.0f}억"
            )

    if mod:
        lines.append('')
        lines.append('📈 보통 수급 종목:')
        for s in mod[:3]:
            lines.append(
                f"  [{s['market'][:2]}] {s['name']}  {s['score']}점 "
                f"외:{s['foreign_flow']:+.0f}억 기:{s['institution_flow']:+.0f}억"
            )

    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='수급 모멘텀 엔진')
    parser.add_argument('--top-n',     type=int,  default=40, help='마켓별 후보 종목 수 (default 40)')
    parser.add_argument('--min-flow',  type=int,  default=1,  help='최소 flow_score (default 1)')
    parser.add_argument('--no-telegram', action='store_true', help='텔레그램 알림 비활성화')
    args = parser.parse_args()

    result = run(top_n=args.top_n, min_flow_score=args.min_flow)
    save_results(result)
    maybe_send_telegram(result, _build_telegram_message, args.no_telegram)


if __name__ == '__main__':
    main()
