"""Claude API 호출 래퍼 (웹 검색 툴 + JSON 응답 파싱)."""
import json
import os
import re

from anthropic import Anthropic

MAX_CONTINUATIONS = 5  # pause_turn 이어받기 최대 횟수


class LLMResponseError(ValueError):
    """응답이 잘렸거나, 거절됐거나, JSON 으로 읽을 수 없을 때.

    업체 1곳 단위로 복구 가능한 오류 (API 키 오류처럼 전체를 멈춰야 하는 오류와 구분).
    """


class LLM:
    def __init__(self):
        self.client = Anthropic()  # ANTHROPIC_API_KEY 환경변수 사용
        self.model = os.getenv("MODEL", "claude-sonnet-5-5")
        self.search_tool = os.getenv("WEB_SEARCH_TOOL", "web_search_20260209")
        # 단계별 검색 한도: 후보 검색은 20곳 이상을 모아야 하고, 검증은 1곳을 여러 출처로 교차 확인해야 함
        self.max_searches = {
            "discover": int(os.getenv("MAX_SEARCHES_DISCOVER", "15")),
            "verify": int(os.getenv("MAX_SEARCHES_VERIFY", "12")),
            "channels": int(os.getenv("MAX_SEARCHES_CHANNELS", "8")),
            "quality": int(os.getenv("MAX_SEARCHES_QUALITY", "10")),   # 제품 품질·리뷰 (모델 1개마다 1번)   # 구매 사이트 비교 (실행 1회에 1번)
        }

    def ask_json(self, system: str, user: str, use_search: bool = True, max_tokens: int = 32000,
                 stage: str = "verify"):
        """Claude에게 질문하고 응답에서 JSON을 추출해 반환. stage 로 검색 한도를 고름."""
        messages = [{"role": "user", "content": user}]
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system)
        if use_search:
            kwargs["tools"] = [{
                "type": self.search_tool,
                "name": "web_search",
                "max_uses": self.max_searches[stage],
            }]

        text_parts = []
        # 검색이 길어지면 서버가 pause_turn 으로 끊어 보내므로 이어서 요청
        for _ in range(MAX_CONTINUATIONS):
            # max_tokens 가 크면 비스트리밍 요청은 HTTP 타임아웃 위험이 있어 스트리밍으로 받음
            # (모델의 thinking 토큰도 max_tokens 에 포함되므로 한도를 넉넉히 잡음)
            with self.client.messages.stream(messages=messages, **kwargs) as stream:
                resp = stream.get_final_message()
            text_parts += [b.text for b in resp.content if b.type == "text"]
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        else:
            raise LLMResponseError(
                f"검색이 {MAX_CONTINUATIONS}회 이어받기 안에 끝나지 않았습니다. MAX_SEARCHES_DISCOVER/VERIFY 를 줄여 보세요.")

        if resp.stop_reason == "refusal":
            detail = getattr(resp, "stop_details", None)
            raise LLMResponseError(f"모델이 응답을 거절했습니다: {detail}")
        if resp.stop_reason == "max_tokens":
            raise LLMResponseError(
                f"응답이 max_tokens({max_tokens})에서 잘렸습니다. 후보 수를 줄이거나 max_tokens 를 늘리세요.")

        # 웹 검색 인용(citation) 때문에 텍스트가 여러 블록으로 쪼개져 오므로
        # 구분자 없이 이어 붙여야 JSON 문자열 중간에 줄바꿈이 끼지 않음
        return extract_json("".join(text_parts))


def extract_json(text: str):
    """응답 텍스트에서 JSON 블록을 찾아 파싱."""
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = m.group(1) if m else text
    # 코드블록이 없으면 첫 [ 또는 { 부터 마지막 ] 또는 } 까지
    if not m:
        starts = [i for i in (candidate.find("["), candidate.find("{")) if i != -1]
        if starts:
            start = min(starts)
            end = max(candidate.rfind("]"), candidate.rfind("}"))
            candidate = candidate[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMResponseError(f"JSON 파싱 실패: {e}\n--- 원문 ---\n{text[:2000]}") from e
