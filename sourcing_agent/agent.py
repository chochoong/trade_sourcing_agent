"""다른 에이전트(오케스트레이터)에서 호출할 진입점.

    from sourcing_agent import find_suppliers
    result = find_suppliers({"product": "...", "product_en": "...", ...})

진행 로그는 stderr 로만 나가고, 반환값/CLI stdout 에는 JSON 만 나옵니다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from .channels import assess_channels
from .discover import discover, prefilter
from .llm import LLM
from .manual import create_templates, slugify
from .paths import OUTPUT
from .report import write_reports
from .scoring import DEFAULT_SOURCING_TYPE, load_rubric
from .verify import verify_one

load_dotenv()

# supplier: 수입용 공급처 발굴 (현재 지원)
# buyer:    수출용 바이어 발굴 (예정 — prompts/·config/ 에 모드별 파일을 추가해 확장)
SUPPORTED_MODES = ("supplier",)


def log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("소싱 조건은 YAML/dict 형식이어야 합니다.")
    if not (request.get("product_en") or request.get("product")):
        raise ValueError("소싱 조건에 product 또는 product_en 이 필요합니다.")
    mode = request.get("mode") or "supplier"
    if mode not in SUPPORTED_MODES:
        raise NotImplementedError(
            f"mode={mode!r} 는 아직 지원하지 않습니다. 지원: {', '.join(SUPPORTED_MODES)}")
    # 구매 방식: oem(공장 맞춤 제작) / small_batch(소량 도매) — config/scoring/<값>.yaml 점수표 사용
    sourcing_type = request.get("sourcing_type") or DEFAULT_SOURCING_TYPE
    load_rubric(sourcing_type)  # 점수표가 없으면 여기서 바로 오류
    return {**request, "mode": mode, "sourcing_type": sourcing_type}


def new_run_dir(request: dict) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = OUTPUT / f"{stamp}_{slugify(request.get('product_en') or request.get('product') or 'run')}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_discover(request: dict, run_dir=None, log=log_stderr) -> dict:
    """② 검색 + ③ 1차 필터. 수동 확인용 템플릿도 생성."""
    request = validate_request(request)
    llm = LLM()
    run_dir = Path(run_dir) if run_dir else new_run_dir(request)
    run_dir.mkdir(parents=True, exist_ok=True)
    rubric = load_rubric(request["sourcing_type"])
    log(f"[1/2] 후보 업체 검색 중... (구매 방식: {rubric.get('label', request['sourcing_type'])})")
    candidates = discover(llm, request, rubric)
    shortlist = prefilter(candidates, request)
    (run_dir / "request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "shortlist.json").write_text(json.dumps(shortlist, ensure_ascii=False, indent=2), encoding="utf-8")
    templates = create_templates(shortlist)
    log(f"      후보 {len(candidates)}곳 → 1차 필터 후 {len(shortlist)}곳")
    return {"run_dir": str(run_dir), "candidates": len(candidates),
            "shortlist": shortlist, "manual_templates": [str(p) for p in templates]}


def run_verify(run_dir, log=log_stderr) -> dict:
    """④ 검증 + ⑤ 리포트. data/manual/ 에 채운 정보가 있으면 함께 사용."""
    run_dir = Path(run_dir)
    request = validate_request(json.loads((run_dir / "request.json").read_text(encoding="utf-8")))
    shortlist = json.loads((run_dir / "shortlist.json").read_text(encoding="utf-8"))
    llm, rubric = LLM(), load_rubric(request["sourcing_type"])

    results = []
    for i, c in enumerate(shortlist, 1):
        log(f"[2/2] 검증 중 ({i}/{len(shortlist)}): {c.get('name')}")
        results.append(verify_one(llm, c, request, rubric))

    channels = []
    if request.get("assess_channels", True):   # 요청에 assess_channels: false 면 건너뜀 (API 호출 1회 절약)
        log("[+] 구매 사이트 비교 중...")
        channels = assess_channels(llm, request, log=log)

    files = write_reports(results, request, run_dir, channels=channels)
    top = sorted(results, key=lambda r: r.get("total") or 0, reverse=True)[:3]
    return {"run_dir": str(run_dir), "files": files,
            "top3": [{"name": r.get("name"), "total": r.get("total"), "grade": r.get("grade"),
                      "contacts": r.get("contacts"), "urls": r.get("urls")} for r in top],
            "channels": [{"site": c["site"], "total": c["total"], "search_url": c["search_url"]} for c in channels[:3]]}


def find_suppliers(request: dict, log=log_stderr) -> dict:
    """검색 → 필터 → 검증 → 리포트를 한 번에 실행 (수동 정보 없이)."""
    d = run_discover(request, log=log)
    return run_verify(d["run_dir"], log=log)
