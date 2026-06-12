"""스크리너 결과 JSON 저장"""

import json
import os
from datetime import datetime
from pathlib import Path

from models import ScreenerResult


def save_result_to_json(result: ScreenerResult) -> None:
    """ScreenerResult를 날짜별 파일 + latest 파일로 저장한다."""

    # 저장 디렉토리
    data_dir = Path(__file__).resolve().parent.parent / "data"
    os.makedirs(data_dir, exist_ok=True)

    # JSON 데이터 생성
    data = result.to_dict()
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")

    json_str = json.dumps(data, ensure_ascii=False, indent=2)

    # 1. 날짜별 파일
    date_str = result.date.strftime("%Y%m%d")
    daily_path = data_dir / f"jongga_v2_results_{date_str}.json"
    daily_path.write_text(json_str, encoding="utf-8")
    print(f"  저장: {daily_path}")

    # 2. Latest 파일 (항상 덮어쓰기)
    latest_path = data_dir / "jongga_v2_latest.json"
    latest_path.write_text(json_str, encoding="utf-8")
    print(f"  저장: {latest_path}")
