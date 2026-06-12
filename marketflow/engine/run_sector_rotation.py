"""섹터 로테이션 엔진 실행 진입점.

사용법:
  python run_sector_rotation.py
  python run_sector_rotation.py --max-files 20
  python run_sector_rotation.py --no-telegram
"""
import argparse

from base_runner import ensure_engine_path, maybe_send_telegram

ensure_engine_path()

from sector_rotation import run, save_results  # noqa: E402


def _build_telegram_message(result: dict) -> str:
    stats = result['stats']
    lines = [
        f"🔄 섹터 로테이션 결과 | {result['date']}",
        f"총 {stats['total']}개  {stats['sector_count']}개 섹터  평균RS: {stats['avg_rs']:.1f}",
        '',
    ]
    markup = [s for s in result['signals'] if s['rotation_phase'] == 'markup'][:5]
    if markup:
        lines.append('📈 Markup 섹터:')
        for s in markup:
            lines.append(f"  [{s['sector']}] {s['name']}  RS:{s['relative_strength']:.1f}")
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='섹터 로테이션 엔진')
    parser.add_argument('--max-files',   type=int, default=15, help='참조할 jongga 파일 수')
    parser.add_argument('--no-telegram', action='store_true')
    args = parser.parse_args()

    result = run(max_files=args.max_files)
    save_results(result)
    maybe_send_telegram(result, _build_telegram_message, args.no_telegram)


if __name__ == '__main__':
    main()
