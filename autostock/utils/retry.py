import asyncio
import functools
import random
import time
from autostock.logger import get_logger

log = get_logger("retry")

def async_retry(max_retries=3, base_delay=1.0, max_delay=10.0, exceptions=(Exception,)):
    """
    비동기 함수를 위한 지능형 재시도 데코레이터 (Exponential Backoff).
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    retries += 1
                    if retries > max_retries:
                        log.error("[%s] 최종 실패 (%d회 시도): %s", func.__name__, retries, e)
                        raise
                    
                    # 지수 백오프 + 지터(jitter) 추가
                    delay = min(max_delay, base_delay * (2 ** (retries - 1)))
                    jitter = delay * 0.1 * random.uniform(-1, 1)
                    final_delay = max(0.1, delay + jitter)
                    
                    log.warning("[%s] 실패 (%d/%d): %s. %.1f초 후 재시도...", 
                                func.__name__, retries, max_retries, e, final_delay)
                    await asyncio.sleep(final_delay)
        return wrapper
    return decorator

def sync_retry(max_retries=3, base_delay=1.0, max_delay=10.0, exceptions=(Exception,)):
    """
    동기 함수를 위한 지능형 재시도 데코레이터.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    retries += 1
                    if retries > max_retries:
                        log.error("[%s] 최종 실패 (%d회 시도): %s", func.__name__, retries, e)
                        raise
                    
                    delay = min(max_delay, base_delay * (2 ** (retries - 1)))
                    jitter = delay * 0.1 * random.uniform(-1, 1)
                    final_delay = max(0.1, delay + jitter)
                    
                    log.warning("[%s] 실패 (%d/%d): %s. %.1f초 후 재시도...", 
                                func.__name__, retries, max_retries, e, final_delay)
                    time.sleep(final_delay)
        return wrapper
    return decorator
