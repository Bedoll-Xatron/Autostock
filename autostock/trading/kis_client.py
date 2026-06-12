"""KIS REST API 클라이언트 — 토큰 관리, 잔고 조회."""
import json
import time
from datetime import date
from pathlib import Path
from typing import Optional
import httpx
from autostock import config
from autostock.hitl.telegram_bot import get_kis_simulated
from autostock.logger import get_logger

log = get_logger(__name__)

_token_cache: dict = {"access_token": None, "expires_at": 0}
_TOKEN_FILE = Path(__file__).parents[2] / "logs" / ".kis_token_cache.json"


def _base_url() -> str:
    return config.KIS_BASE_URL_SIM if get_kis_simulated() else config.KIS_BASE_URL_REAL


def _load_token_from_file() -> bool:
    """파일에서 토큰 로드. 유효하면 in-memory 캐시에 채우고 True 반환."""
    try:
        if not _TOKEN_FILE.exists():
            return False
        data = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        if data.get("access_token") and time.time() < data.get("expires_at", 0):
            _token_cache["access_token"] = data["access_token"]
            _token_cache["expires_at"] = data["expires_at"]
            return True
    except Exception:
        pass
    return False


def _save_token_to_file() -> None:
    """in-memory 캐시를 파일에 저장."""
    try:
        _TOKEN_FILE.parent.mkdir(exist_ok=True)
        _TOKEN_FILE.write_text(
            json.dumps(_token_cache, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("KIS 토큰 파일 저장 실패: %s", e)


def get_access_token() -> str:
    """KIS OAuth2 토큰 발급 (in-memory → 파일 → API 순으로 캐시 확인)."""
    now = time.time()
    # 1. in-memory 캐시 확인
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]
    # 2. 파일 캐시 확인 (다른 프로세스가 발급한 토큰 재사용)
    if _load_token_from_file():
        log.info("KIS 토큰 파일 캐시 사용 (simulated=%s)", get_kis_simulated())
        return _token_cache["access_token"]

    # 3. API 호출
    url = f"{_base_url()}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": config.KIS_APP_KEY,
        "appsecret": config.KIS_APP_SECRET,
    }
    resp = httpx.post(url, json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    # KIS 토큰 유효기간 24시간, 10분 여유
    _token_cache["expires_at"] = now + 86400 - 600
    _save_token_to_file()
    log.info("KIS 토큰 발급 완료 (simulated=%s)", get_kis_simulated())
    return _token_cache["access_token"]


def _headers(tr_id: str) -> dict:
    return {
        "authorization": f"Bearer {get_access_token()}",
        "appkey": config.KIS_APP_KEY,
        "appsecret": config.KIS_APP_SECRET,
        "tr_id": tr_id,
        "content-type": "application/json",
    }


def get_balance() -> dict:
    """잔고 조회. 주문 가능 현금 및 보유 종목 반환."""
    simulated = get_kis_simulated()
    tr_id = "VTTC8434R" if simulated else "TTTC8434R"
    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    resp = httpx.get(url, headers=_headers(tr_id), params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_holding_qty(ticker: str) -> int:
    """보유 수량 조회 (SELL 시 전량 매도 기준)."""
    try:
        data = get_balance()
        holdings = data.get("output1", [])
        for h in holdings:
            if h.get("pdno") == ticker:
                return int(h.get("hldg_qty", 0))
        return 0
    except Exception as e:
        log.error("get_holding_qty %s failed: %s", ticker, e)
        return 0


def get_all_holdings() -> list[dict]:
    """KIS 잔고에서 보유 종목 전체 목록 조회.

    Returns:
        [{"ticker": "005930", "name": "삼성전자", "qty": 5, "avg_price": 75000.0}, ...]
    """
    try:
        data = get_balance()
        result = []
        for h in data.get("output1", []):
            qty = int(h.get("hldg_qty", 0))
            if qty <= 0:
                continue
            result.append({
                "ticker": h.get("pdno", ""),
                "name": h.get("prdt_name", ""),
                "qty": qty,
                "avg_price": float(h.get("pchs_avg_pric", 0)),
            })
        return result
    except Exception as e:
        log.error("get_all_holdings failed: %s", e)
        return []


def get_available_cash() -> Optional[float]:
    """주문 가능 현금 조회.
    예수금(dnca_tot_amt)이 마이너스이거나 0인 경우, 순자산(nass_amt) 또는 주문가능금액(prvs_rcdl_excc_amt)을 반환합니다.
    API 오류(500 등) 시 None 반환 — 호출자는 None을 잔고 0과 구분해 처리해야 함.
    """
    try:
        data = get_balance()
        output2 = data.get("output2", [{}])
        if not output2:
            return 0.0

        res = output2[0]
        cash = float(res.get("dnca_tot_amt", 0))

        if cash <= 0:
            nass = float(res.get("nass_amt", 0))
            psbl = float(res.get("prvs_rcdl_excc_amt", 0))
            cash = max(nass, psbl)

        return cash
    except Exception as e:
        log.error("get_available_cash failed: %s", e)
        return None


def get_current_price(ticker: str) -> float:
    """현재가 조회 (장중 실시간)."""
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
    }
    try:
        resp = httpx.get(url, headers=_headers("FHKST01010100"), params=params, timeout=10)
        resp.raise_for_status()
        price = float(resp.json().get("output", {}).get("stck_prpr", 0))
        return price
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            log.warning("get_current_price %s 실패 (서버 오류 %d)", ticker, e.response.status_code)
        else:
            log.error("get_current_price %s 실패: %s", ticker, e)
        return 0.0
    except Exception as e:
        log.error("get_current_price %s 실패: %s", ticker, e)
        return 0.0


async def get_current_price_async(ticker: str) -> float:
    """현재가 비동기 조회."""
    import httpx as _httpx
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    try:
        async with _httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_headers("FHKST01010100"), params=params, timeout=10)
            resp.raise_for_status()
            return float(resp.json().get("output", {}).get("stck_prpr", 0) or 0)
    except _httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            log.warning("get_current_price_async %s 실패 (서버 오류 %d)", ticker, e.response.status_code)
        else:
            log.error("get_current_price_async %s 실패: %s", ticker, e)
        return 0.0
    except Exception as e:
        log.error("get_current_price_async %s 실패: %s", ticker, e)
        return 0.0


def get_quote_detail(ticker: str) -> dict:
    """현재가 상세 조회 (현재가, 시가, 고가, 저가 반환)."""
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
    }
    try:
        resp = httpx.get(url, headers=_headers("FHKST01010100"), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("output", {})
        return {
            "price": float(data.get("stck_prpr", 0)),
            "open": float(data.get("stck_oprc", 0)),
            "high": float(data.get("stck_hgpr", 0)),
            "low": float(data.get("stck_lwpr", 0)),
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code >= 500:
            log.warning("get_quote_detail %s 실패 (서버 오류 %d)", ticker, e.response.status_code)
        else:
            log.error("get_quote_detail %s 실패: %s", ticker, e)
        return {"price": 0.0, "open": 0.0, "high": 0.0, "low": 0.0}
    except Exception as e:
        log.error("get_quote_detail %s 실패: %s", ticker, e)
        return {"price": 0.0, "open": 0.0, "high": 0.0, "low": 0.0}


def get_volume_rank(limit: int = 30) -> list[dict]:
    """실시간 거래량 상위 종목 조회 (KRX 기준)."""
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/volume-rank"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "000000",
        "FID_INPUT_PRICE_1": "0",
        "FID_INPUT_PRICE_2": "0",
        "FID_VOL_CNT": "0",
        "FID_INPUT_DATE_1": "0",
    }
    try:
        resp = httpx.get(url, headers=_headers("FHPST01710000"), params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json().get("output", [])
        result = []
        for r in rows:
            ticker = r.get("mksc_shrn_iscd", "")
            if not ticker.isdigit() or len(ticker) != 6:
                continue
            result.append({"ticker": ticker, "name": r.get("hts_kor_isnm", "")})
            if len(result) >= limit:
                break
        return result
    except Exception as e:
        log.error("get_volume_rank 실패: %s", e)
        return []


def market_sell(ticker: str, qty: int) -> dict:
    """시장가 매도 주문 (손절용)."""
    simulated = get_kis_simulated()
    tr_id = "VTTC0801U" if simulated else "TTTC0801U"
    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_DVSN": "01",   # 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0",
    }
    log.info("market_sell: %s qty=%d simulated=%s", ticker, qty, simulated)
    resp = httpx.post(url, headers=_headers(tr_id), json=body, timeout=10)
    resp.raise_for_status()
    
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise ValueError(f"KIS 시장가매도 거절: {data.get('msg1')} ({data.get('msgcd')})")
        
    return data


def _tick_size(price: int) -> int:
    """KRX 호가 단위 반환."""
    if price < 2_000:       return 1
    if price < 5_000:       return 5
    if price < 20_000:      return 10
    if price < 50_000:      return 50
    if price < 200_000:     return 100
    if price < 500_000:     return 500
    return 1_000


def round_to_tick(price: float) -> int:
    """호가 단위로 내림 처리."""
    p = int(price)
    tick = _tick_size(p)
    return (p // tick) * tick


def limit_buy(ticker: str, qty: int, price: int) -> dict:
    """지정가 매수 주문. 응답에 ODNO(주문번호) 포함."""
    simulated = get_kis_simulated()
    tr_id = "VTTC0802U" if simulated else "TTTC0802U"
    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_DVSN": "00",           # 지정가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(price),
    }
    log.info("limit_buy: %s qty=%d price=%d simulated=%s", ticker, qty, price, simulated)
    resp = httpx.post(url, headers=_headers(tr_id), json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise ValueError(f"KIS 지정가매수 거절: {data.get('msg1')} ({data.get('msgcd')})")
    return data


def get_order_fill_qty(order_no: str) -> int:
    """주문번호로 당일 체결 수량 조회."""
    simulated = get_kis_simulated()
    tr_id = "VTTC8001R" if simulated else "TTTC8001R"
    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
    today = date.today().strftime("%Y%m%d")
    params = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "INQR_STRT_DT": today,
        "INQR_END_DT": today,
        "SLL_BUY_DVSN_CD": "02",    # 매수
        "INQR_DVSN": "01",
        "PDNO": "",
        "CCLD_DVSN": "00",           # 전체(체결+미체결)
        "ORD_GNO_BRNO": "",
        "ODNO": order_no,
        "INQR_DVSN_3": "",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    try:
        resp = httpx.get(url, headers=_headers(tr_id), params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json().get("output1", [])
        return sum(int(r.get("tot_ccld_qty", 0)) for r in rows)
    except Exception as e:
        log.error("get_order_fill_qty %s 실패: %s", order_no, e)
        return 0


def cancel_order(order_no: str, ticker: str, qty: int, price: int) -> bool:
    """지정가 주문 취소."""
    simulated = get_kis_simulated()
    tr_id = "VTTC0803U" if simulated else "TTTC0803U"
    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/order-rvsecncl"
    body = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "KNO_ORD_NO": order_no,
        "ORD_DVSN": "00",
        "RVSE_CNCL_DVSN_CD": "02",  # 취소
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(price),
        "PDNO": ticker,
        "QTY_ALL_ORD_YN": "Y",
    }
    try:
        resp = httpx.post(url, headers=_headers(tr_id), json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") == "0":
            log.info("cancel_order: %s 취소 성공", order_no)
            return True
        log.warning("cancel_order: %s 취소 실패 — %s", order_no, data.get("msg1"))
        return False
    except Exception as e:
        log.error("cancel_order %s 실패: %s", order_no, e)
        return False


def get_intraday_5min(ticker: str, count: int = 20) -> list[dict]:
    """당일 5분봉 조회 (장중 09:15 이후 사용).

    Returns:
        [{"close": float}, ...] 최신 봉이 마지막, 최대 count개.
        조회 실패 시 빈 리스트 반환.
    """
    import pytz
    from datetime import datetime as _dt
    kst = pytz.timezone("Asia/Seoul")
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
        "FID_INPUT_HOUR_1": _dt.now(kst).strftime("%H%M%S"),
        "FID_PW_DATA_INCU_YN": "N",
    }
    try:
        resp = httpx.get(url, headers=_headers("FHKST03010200"), params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("output2", [])
        bars = [{"close": float(it.get("stck_prpr", 0))} for it in items if it.get("stck_prpr")]
        return bars[-count:] if len(bars) > count else bars
    except Exception as e:
        log.warning("[%s] 5분봉 조회 실패: %s", ticker, e)
        return []


def get_premarket_price(ticker: str) -> float:
    """시간외단일가 조회 (08:00 전후 사용). 실패 시 0.0 반환."""
    url = f"{_base_url()}/uapi/domestic-stock/v1/quotations/inquire-overtime-price"
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
    }
    try:
        resp = httpx.get(url, headers=_headers("FHPST02310000"), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("output", {})
        price = float(data.get("ovtm_untp") or data.get("stck_prpr") or 0)
        return price
    except Exception as e:
        log.debug("[%s] 시간외단일가 조회 실패: %s", ticker, e)
        return 0.0


def place_order(ticker: str, action: str, qty: int, price: float) -> dict:
    """
    현금 매수/매도 주문. KIS API 호출 후 에러(rt_cd!='0') 발생 시 즉시 예외 발생.
    빠른 체결을 위해 무조건 '시장가(01)' 주문으로 넣습니다.
    """
    simulated = get_kis_simulated()
    if action == "BUY":
        tr_id = "VTTC0802U" if simulated else "TTTC0802U"
    else:
        tr_id = "VTTC0801U" if simulated else "TTTC0801U"

    url = f"{_base_url()}/uapi/domestic-stock/v1/trading/order-cash"
    body = {
        "CANO": config.KIS_CANO,
        "ACNT_PRDT_CD": config.KIS_ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_DVSN": "01",  # 01 = 시장가
        "ORD_QTY": str(qty),
        "ORD_UNPR": "0",   # 시장가는 무조건 0원
    }

    log.info("place_order: %s %s qty=%d price=MARKET simulated=%s", action, ticker, qty, simulated)
    resp = httpx.post(url, headers=_headers(tr_id), json=body, timeout=10)
    resp.raise_for_status()
    
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise ValueError(f"KIS 주문 거절: {data.get('msg1')} ({data.get('msgcd')})")
        
    return data
