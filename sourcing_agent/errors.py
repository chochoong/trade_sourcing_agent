"""오류를 대시보드·터미널에 보여줄 한국어 문구로 바꿈.

파이썬/라이브러리 원문("[Errno 2] No such file or directory: ...", "Connection error." 등)이
그대로 화면에 나가지 않게, 무엇이 문제고 무엇을 하면 되는지 적은 문구로 바꾼다.
"""
import re
import sqlite3

import anthropic

_ERRNO = re.compile(r"\[(?:Errno|WinError) -?\d+\]\s*")


class SourcingError(Exception):
    """사람이 읽는 문구로 바꾼 오류. 원래 예외는 __cause__ 에 남음 (디버깅용)."""


def _where(e: OSError) -> str:
    return f": {e.filename}" if getattr(e, "filename", None) else ""


def _api_detail(e) -> str:
    msg = getattr(e, "message", None) or str(e)
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") or msg
    return str(msg)


def friendly(e: BaseException) -> str:
    """예외 → 화면에 보여줄 한 줄 문구 (Errno·예외 클래스 이름 없음)."""
    from .buyagent_export import BuyagentNotFound
    from .llm import LLMResponseError

    if isinstance(e, SourcingError):
        return str(e)
    if isinstance(e, BuyagentNotFound):
        return str(e)
    if isinstance(e, LLMResponseError):
        text = str(e).split("\n--- 원문 ---", 1)[0]          # 원문 덤프는 화면에 안 보임
        return f"AI 응답을 읽지 못했습니다 ({_ERRNO.sub('', text)[:200]})"

    # ── Anthropic API ──
    if isinstance(e, anthropic.AuthenticationError):
        return "Anthropic API 키가 올바르지 않습니다. .env 의 ANTHROPIC_API_KEY 를 확인하세요."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "이 API 키로는 해당 모델·기능을 쓸 수 없습니다. Anthropic 콘솔에서 키 권한을 확인하세요."
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic API 사용 한도를 넘었습니다. 몇 분 뒤 다시 실행하세요."
    if isinstance(e, anthropic.APITimeoutError):
        return "Anthropic 서버 응답이 너무 늦어 중단했습니다. 잠시 뒤 다시 실행하세요."
    if isinstance(e, anthropic.APIConnectionError):
        return "Anthropic 서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 실행하세요."
    if isinstance(e, anthropic.BadRequestError):
        detail = _api_detail(e)
        if re.search(r"credit balance", detail, re.I):
            return "Anthropic API 크레딧이 부족합니다. 콘솔(Plans & Billing)에서 충전한 뒤 다시 실행하세요."
        return f"Anthropic API 가 요청을 거부했습니다 (설정 확인 필요: MODEL·WEB_SEARCH_TOOL). 상세: {detail[:200]}"
    if isinstance(e, anthropic.NotFoundError):
        return "요청한 모델을 찾지 못했습니다. .env 의 MODEL 값을 확인하세요."
    if isinstance(e, anthropic.APIStatusError):
        if getattr(e, "status_code", 0) in (500, 502, 503, 529):
            return "Anthropic 서버가 일시적으로 혼잡하거나 장애 중입니다. 잠시 뒤 다시 실행하세요."
        return f"Anthropic API 오류(HTTP {getattr(e, 'status_code', '?')}): {_api_detail(e)[:200]}"

    # ── 파일 ──
    if isinstance(e, FileNotFoundError):
        return f"파일을 찾지 못했습니다{_where(e)}. 경로가 맞는지, 조사 폴더가 지워지지 않았는지 확인하세요."
    if isinstance(e, PermissionError):
        return (f"파일을 쓰거나 읽을 권한이 없습니다{_where(e)}. "
                "엑셀·브라우저 등에서 그 파일을 열어 두었다면 닫고 다시 실행하세요.")
    if isinstance(e, IsADirectoryError):
        return f"파일 자리에 폴더가 있습니다{_where(e)}. 경로를 확인하세요."
    if isinstance(e, OSError):
        return f"파일·네트워크 작업에 실패했습니다{_where(e)}. 잠시 뒤 다시 실행하세요."

    # ── 공용 DB ──
    if isinstance(e, sqlite3.OperationalError):
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return "공용 DB 를 다른 작업이 쓰고 있습니다. 잠시 뒤 다시 실행하세요."
        return f"공용 DB 작업에 실패했습니다 ({str(e)[:200]}). trade_hub 의 DB 경로(TRADE_DB)를 확인하세요."

    # ── 그 밖: 우리가 쓴 한국어 문구(ValueError 등)는 그대로, Errno 표기만 지움 ──
    if isinstance(e, (ValueError, KeyError, RuntimeError)) and str(e):
        return _ERRNO.sub("", str(e))
    return f"예상하지 못한 오류가 생겼습니다 ({type(e).__name__}: {_ERRNO.sub('', str(e))[:200]})"
