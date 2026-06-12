import yfinance as yf
import FinanceDataReader as fdr

def test_indices():
    print("Testing yfinance (VIX, S&P Futures)...")
    vix = yf.Ticker("^VIX").history(period="1d")
    print(f"VIX: {vix['Close'].iloc[-1] if not vix.empty else 'Failed'}")
    
    es = yf.Ticker("ES=F").history(period="1d")
    print(f"S&P 500 Futures: {es['Close'].iloc[-1] if not es.empty else 'Failed'}")
    
    print("\nTesting FinanceDataReader (USD/KRW, KOSPI)...")
    try:
        usd_krw = fdr.DataReader("USD/KRW")
        print(f"USD/KRW: {usd_krw['Close'].iloc[-1]}")
    except Exception as e:
        print(f"USD/KRW Failed: {e}")
        
    try:
        kospi = fdr.DataReader("KS11")
        print(f"KOSPI: {kospi['Close'].iloc[-1]}")
    except Exception as e:
        print(f"KOSPI Failed: {e}")

if __name__ == "__main__":
    test_indices()
