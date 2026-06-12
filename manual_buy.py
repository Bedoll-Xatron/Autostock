"""수동 매수 실행 스크립트."""
import json
from datetime import date
from autostock.trading.kis_client import get_current_price, place_order
from autostock.db.supabase import save_held_position
from autostock.trading.trailing_stop import _fixed_stop

orders = [
    ("178320", "서진시스템", 20),
    ("007810", "코리아써키트", 15),
]

results = []
for ticker, name, qty in orders:
    try:
        price = get_current_price(ticker)
        resp = place_order(ticker, "BUY", qty, price)
        msg = resp.get("msg1", "")
        print(f"[{name}({ticker})] 주문 성공: qty={qty}, 현재가={price:,.0f}원")
        print(f"  응답: {json.dumps(resp, ensure_ascii=False)}")

        stop = _fixed_stop(price)
        saved = save_held_position({
            "ticker": ticker,
            "name": name,
            "qty": qty,
            "avg_price": price,
            "entry_price": price,
            "stop_price": stop,
            "peak_price": price,
            "phase": "stop",
            "entry_date": date.today().isoformat(),
        })
        print(f"  held_positions 저장: {saved}")
        results.append({"ticker": ticker, "name": name, "qty": qty, "price": price, "ok": True})
    except Exception as e:
        print(f"[{name}({ticker})] 주문 실패: {e}")
        results.append({"ticker": ticker, "name": name, "qty": qty, "ok": False, "err": str(e)})
    print()

print("=== 최종 결과 ===")
for r in results:
    status = "✅" if r["ok"] else "❌"
    qty_str = str(r.get("qty", ""))
    name_str = r["name"]
    ticker_str = r["ticker"]
    price_str = f'{r["price"]:,.0f}원' if r.get("price") else ""
    print(f"{status} {name_str}({ticker_str}): {qty_str}주 @ {price_str}")
