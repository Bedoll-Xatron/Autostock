"""오후 15:00 적응형 시그널 스캐너 실행 스크립트."""

import argparse
import json
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from afternoon_scanner import run_afternoon_scan, get_surge_watchlist

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="오후 15:00 적응형 시그널 스캐너")
    parser.add_argument("--no-telegram", action="store_true", help="텔레그램 전송 건너뜀")
    parser.add_argument("--output-json", action="store_true", help="워치리스트 JSON을 stdout으로 출력 (jobs.py 연동용)")
    args = parser.parse_args()

    if args.output_json:
        watchlist, morning_msg = get_surge_watchlist()
        print(json.dumps({"watchlist": watchlist, "morning_msg": morning_msg}, ensure_ascii=False))
    else:
        result = run_afternoon_scan(no_telegram=args.no_telegram)
        print(
            f"\n결과: 시장국면={result['regime']} ({result['kospi_pct']:+.2f}%) | "
            f"유지={result['confirmed']} 추격위험={result['chasing']} "
            f"취소={result['cancelled']} 신규={result['new_surges']}"
        )
