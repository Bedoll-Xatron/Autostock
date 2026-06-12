"""다중 AI 모델 구동 지원 (OpenAI / Gemini)."""
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI

from autostock import config
from autostock.logger import get_logger

log = get_logger(__name__)

def get_basic_llm():
    """기본 분석용 가벼운 모델 (gemini-2.0-flash 또는 gpt-4o)"""
    provider = config.AI_PROVIDER
    if provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=config.GEMINI_BASIC_MODEL,
            google_api_key=config.GEMINI_API_KEY,
            temperature=0.0,
            max_retries=3
        )
    else:
        return ChatOpenAI(
            model=config.OPENAI_MODEL,
            api_key=config.OPENAI_API_KEY,
            temperature=0.0,
            max_retries=3
        )

def get_boss_llm():
    """토론 및 최종 검토용 무거운 모델 (gemini-1.5-pro 또는 gpt-4o)"""
    provider = config.AI_PROVIDER
    if provider == "gemini":
        return ChatGoogleGenerativeAI(
            model=config.GEMINI_BOSS_MODEL,
            google_api_key=config.GEMINI_API_KEY,
            temperature=0.0,
            max_retries=3
        )
    else:
        return ChatOpenAI(
            model=config.OPENAI_MODEL,
            api_key=config.OPENAI_API_KEY,
            temperature=0.0
        )
