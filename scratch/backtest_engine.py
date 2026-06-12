import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def run_balanced_backtest(years=5, target_profit=0.20, stop_loss=-0.08):
    print(f"=== {years}년 '균형잡힌 원석 발굴' 백테스트 시작 ===", flush=True)
    
    end_date = datetime.today().strftime("%Y-%m-%d")
    start_date = (datetime.today() - timedelta(days=years*365)).strftime("%Y-%m-%d")
    
    try:
        print("시장 및 종목 리스트 로딩 중...", flush=True)
        kospi = fdr.DataReader('KS11', start_date, end_date)
        kospi['MA60_KOSPI'] = kospi['Close'].rolling(60).mean()
        
        stocks = fdr.StockListing('KRX')
        marcap_col = next((c for c in stocks.columns if 'mar' in c.lower()), None)
        stocks = stocks.sort_values(marcap_col, ascending=False)
        target_stocks = stocks.iloc[400:1200]
        sample_stocks = target_stocks.sample(n=60) # 샘플 확대
    except Exception as e:
        print(f"로드 에러: {e}")
        return

    results = []
    total = len(sample_stocks)
    
    for i, (idx, row) in enumerate(sample_stocks.iterrows(), 1):
        ticker = row['Code']
        name = row['Name']
        print(f"[{i}/{total}] {name}({ticker}) 분석 중...", end='\r', flush=True)
        
        try:
            df = fdr.DataReader(ticker, start_date, end_date)
            if len(df) < 200: continue
            
            # 지표 계산
            df['MA20'] = df['Close'].rolling(20).mean()
            df['MA60'] = df['Close'].rolling(60).mean()
            df['MA120'] = df['Close'].rolling(120).mean()
            df['VolMA20'] = df['Volume'].rolling(20).mean()
            
            # 시장 필터 (KOSPI 60일선 위)
            df = df.join(kospi[['MA60_KOSPI', 'Close']], rsuffix='_KOSPI')
            df['Market_OK'] = df['Close_KOSPI'] > df['MA60_KOSPI']
            
            # 매수 조건 (정배열 + 거래량 2.5배 + 변동성 축소)
            df['Range'] = (df['High'] - df['Low']) / df['Close']
            df['VCP'] = df['Range'].rolling(10).mean() < df['Range'].rolling(30).mean()
            
            df['Buy_Signal'] = (df['Market_OK']) & \
                               (df['Close'] > df['MA20']) & \
                               (df['MA20'] > df['MA60']) & \
                               (df['Volume'] > df['VolMA20'] * 2.5) & \
                               (df['VCP'])
            
            signals = df[df['Buy_Signal']].index
            stock_returns = []
            
            for sig_date in signals:
                entry_idx = df.index.get_loc(sig_date) + 1
                if entry_idx >= len(df): continue
                
                buy_price = df.iloc[entry_idx]['Open']
                if buy_price <= 0: continue
                
                for day in range(1, 41):
                    curr_idx = entry_idx + day
                    if curr_idx >= len(df): break
                    
                    curr_price = df.iloc[curr_idx]['Close']
                    ret = (curr_price / buy_price) - 1
                    
                    if ret >= target_profit or ret <= stop_loss or day == 40:
                        stock_returns.append(ret)
                        break
            
            if stock_returns:
                results.append({
                    'name': name,
                    'trades': len(stock_returns),
                    'avg_return': np.mean(stock_returns),
                    'win_rate': len([r for r in stock_returns if r > 0]) / len(stock_returns)
                })
        except:
            continue
            
    print("\n" + "="*50, flush=True)
    if results:
        res_df = pd.DataFrame(results)
        print(f"최종 원석 백테스트 결과 (균형 모드)")
        print(f"평균 수익률: {res_df['avg_return'].mean()*100:.2f}%", flush=True)
        print(f"평균 승률: {res_df['win_rate'].mean()*100:.2f}%", flush=True)
        print(f"총 매매 횟수: {res_df['trades'].sum()}회", flush=True)
        
        print("\n성적이 좋았던 중소형 원석:")
        best = res_df.sort_values('avg_return', ascending=False).head(5)
        for _, r in best.iterrows():
            print(f"- {r['name']}: {r['avg_return']*100:.2f}% (승률 {r['win_rate']*100:.1f}%)")
    else:
        print("결과가 없습니다. 조건을 더 완화해야 할 것 같습니다.", flush=True)
    print("="*50, flush=True)

if __name__ == "__main__":
    run_balanced_backtest()
