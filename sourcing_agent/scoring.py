"""점수 합산은 LLM이 아니라 코드로 — 항상 같은 기준이 적용되도록.

점수표는 구매 방식(sourcing_type)별로 config/scoring/<sourcing_type>.yaml 에 있습니다.
"""
import yaml

from .paths import CONFIG

SCORING_DIR = CONFIG / "scoring"
DEFAULT_SOURCING_TYPE = "oem"


def available_sourcing_types() -> list[str]:
    return sorted(p.stem for p in SCORING_DIR.glob("*.yaml"))


def load_rubric(sourcing_type: str = DEFAULT_SOURCING_TYPE) -> dict:
    path = SCORING_DIR / f"{sourcing_type}.yaml"
    if not path.exists():
        raise ValueError(f"sourcing_type={sourcing_type!r} 점수표가 없습니다. "
                         f"사용 가능: {', '.join(available_sourcing_types())}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def rubric_text(rubric: dict) -> str:
    lines = [f"- {k} ({v['label']}, 최대 {v['max']}점): {v['guide']}" for k, v in rubric["items"].items()]
    rp = rubric["risk_penalty"]
    lines.append(f"- risk_penalty ({rp['label']}, {rp['min']}~0점): {rp['guide']}")
    text = "\n".join(lines)
    keys = ", ".join(f'"{k}"' for k in rubric["items"])
    text += f"\n\nscores 에는 위 항목 key({keys})를 모두 채우세요."
    if rubric.get("verify_focus"):
        text += f"\n\n## 이번 구매 방식({rubric.get('label', '')})에서 중점 확인할 것\n{rubric['verify_focus']}"
    return text


def compute(verification: dict, rubric: dict) -> dict:
    scores = verification.get("scores", {}) or {}
    clean = {}
    for key, item in rubric["items"].items():
        try:
            v = float(scores.get(key, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        clean[key] = max(0, min(v, item["max"]))
    try:
        penalty = float(verification.get("risk_penalty", 0) or 0)
    except (TypeError, ValueError):
        penalty = 0
    penalty = max(rubric["risk_penalty"]["min"], min(penalty, 0))

    total = round(sum(clean.values()) + penalty, 1)
    g = rubric["grades"]
    grade = "A" if total >= g["A"] else "B" if total >= g["B"] else "C" if total >= g["C"] else "D"
    return {"scores": clean, "risk_penalty": penalty, "total": total, "grade": grade}
