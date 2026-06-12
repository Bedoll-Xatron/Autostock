import os
from tavily import TavilyClient
from dotenv import load_dotenv

def main():
    load_dotenv()
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("Error: TAVILY_API_KEY not found in .env")
        return

    print(f"Testing Tavily with API Key: {api_key[:10]}...")
    try:
        tavily = TavilyClient(api_key=api_key)
        response = tavily.search(query="삼성전자 주식 최신 뉴스", search_depth="advanced", max_results=3)
        print("Search successful!")
        results = response.get("results", [])
        for i, r in enumerate(results):
            print(f"[{i+1}] {r.get('title')}")
    except Exception as e:
        print(f"Search failed: {e}")

if __name__ == "__main__":
    main()
