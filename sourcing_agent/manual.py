"""사람이 직접 확인한 정보(③ 수동 확인) 읽기/템플릿 생성.

알리바바·1688 상세 페이지처럼 자동 수집이 막힌 정보는
data/manual/<업체슬러그>.txt 에 복사해 붙여넣으면 검증 단계에서 사용됩니다.
"""
import re

from .paths import MANUAL

TEMPLATE = """# {name}
# 아래에 직접 확인한 정보를 자유롭게 붙여넣으세요. (비워두면 무시됩니다)
# 예: 알리바바 상세페이지 회사정보, 인증서 사진 내용, 채팅 답변, 화상 공장투어 메모 등
# '#' 으로 시작하는 줄은 무시됩니다.
# 링크: {urls}

"""


def slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "_", name.strip().lower(), flags=re.UNICODE)
    return s.strip("_")[:60] or "unknown"


def manual_path(candidate: dict):
    return MANUAL / f"{slugify(candidate.get('name', 'unknown'))}.txt"


def create_templates(candidates: list[dict]) -> list:
    MANUAL.mkdir(parents=True, exist_ok=True)
    created = []
    for c in candidates:
        p = manual_path(c)
        if not p.exists():
            p.write_text(TEMPLATE.format(name=c.get("name"), urls=", ".join(c.get("urls") or [])),
                         encoding="utf-8")
            created.append(p)
    return created


def read_manual(candidate: dict) -> str | None:
    p = manual_path(candidate)
    if not p.exists():
        return None
    lines = [l for l in p.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
    text = "\n".join(lines).strip()
    return text or None
