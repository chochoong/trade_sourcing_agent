"""CLI

  python -m sourcing_agent run      requests/example.yaml   # 한 번에 전부
  python -m sourcing_agent discover requests/example.yaml   # 검색+필터만 (수동 정보 넣을 때)
  python -m sourcing_agent verify   output/<실행폴더>        # 검증+리포트
  python -m sourcing_agent from-db  [품목번호 ...]           # 공용 DB(trade_hub)의 sourcing 품목 처리
  python -m sourcing_agent channels output/<실행폴더>        # 기존 결과에 구매 사이트 순위만 추가
  python -m sourcing_agent export-buyagent output/<실행폴더> [--write]  # 구매대행 모니터로 내보내기

stdout 에는 결과 JSON 만, 진행 로그와 안내는 stderr 로 출력합니다.
(다른 에이전트가 stdout 을 그대로 JSON 으로 파싱할 수 있도록)
실패 시 stdout 에 {"error": ...} 를 내고 종료 코드 1 을 반환합니다.
"""
import argparse
import json
import sys

import yaml

from .agent import find_suppliers, log_stderr, run_discover, run_verify
from .errors import SourcingError


def main() -> int:
    # Windows 콘솔(cp949)에서도 한글 JSON 이 깨지지 않도록
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="sourcing_agent", description="공급처 발굴 에이전트")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "discover"):
        s = sub.add_parser(name)
        s.add_argument("request", help="소싱 조건 YAML 파일")
    s = sub.add_parser("verify")
    s.add_argument("run_dir", help="discover 가 만든 output/ 아래 폴더")
    s = sub.add_parser("channels", help="이미 조사한 실행 폴더에 구매 사이트 순위만 추가 (API 호출 1회)")
    s.add_argument("run_dir", help="results.json 이 있는 output/ 아래 폴더")
    s = sub.add_parser("export-buyagent", help="조사 결과의 상품 페이지·단가를 구매대행 모니터(buyagent) 카탈로그로 내보내기")
    s.add_argument("run_dir", help="results.json 이 있는 output/ 아래 폴더")
    s.add_argument("--category", help="buyagent categories.toml 의 카테고리 키 (생략하면 품목명으로 추정)")
    s.add_argument("--buyagent", help="buyagent 폴더 (기본 ../buyagent/buyagent 또는 환경변수 BUYAGENT_DIR)")
    s.add_argument("--write", action="store_true", help="실제로 파일에 쓰기 (없으면 추가·갱신 미리보기만)")
    s.add_argument("--allow-non-retail", action="store_true",
                   help="retail 이 아닌 조사 결과도 내보내기 (도매가 → memo 에 '소량 사입 결과(도매가)' 표시)")
    s.add_argument("--min-score", type=float,
                   help="이 점수 미만 판매처는 제외 (기본 25, 환경변수 BUYAGENT_MIN_SCORE)")
    s = sub.add_parser("from-db", help="공용 DB에서 sourcing 단계 품목을 읽어 처리하고 결과를 DB에 기록")
    s.add_argument("ids", nargs="*", type=int, help="품목 번호 (생략하면 아직 조사하지 않은 sourcing 품목 전부)")
    s.add_argument("--type", default="small_batch", dest="sourcing_type",
                   help="구매 방식: small_batch(소량 도매, 기본) / retail(소매 직구) / oem")
    s.add_argument("--max-qty", type=int, dest="max_order_qty",
                   help="첫 구매 최대 수량 (1차 필터에 사용, 기본: small_batch 50 / retail 10 / oem 제한 없음)")
    s.add_argument("--dry-run", action="store_true", help="DB에서 만든 소싱 조건만 출력 (API 호출 없음)")
    s.add_argument("--country", default="China", choices=["China", "Japan"],
                   help="공급 국가 (기본 China). Japan 이면 일본어 검색·일본 구매 사이트·일본 기업정보로 조사")
    s.add_argument("--no-buyagent", action="store_true", help="retail 조사 후 buyagent 로 자동 내보내기 하지 않음")
    a = p.parse_args()

    try:
        if a.cmd in ("run", "discover"):
            with open(a.request, encoding="utf-8") as f:
                request = yaml.safe_load(f)

        if a.cmd == "from-db":
            from .hub import run_from_db   # trade_hub 패키지가 필요한 명령만 늦게 불러옴
            out = run_from_db(a.ids, a.sourcing_type, a.max_order_qty, a.dry_run, to_buyagent=not a.no_buyagent, country=a.country)
        elif a.cmd == "export-buyagent":
            from .buyagent_export import export
            out = export(a.run_dir, a.category, a.buyagent, a.write, a.allow_non_retail, a.min_score)
            if not a.write:
                log_stderr("미리보기입니다. 파일에 추가하려면 --write 를 붙이세요.")
        elif a.cmd == "channels":
            from .llm import LLM
            from .report import add_channels
            out = add_channels(a.run_dir, LLM(), log=log_stderr)
        elif a.cmd == "run":
            out = find_suppliers(request)
        elif a.cmd == "discover":
            out = run_discover(request)
            log_stderr("\n다음 단계:")
            log_stderr("  1) data/manual/ 의 .txt 파일에 직접 확인한 정보를 붙여넣으세요 (선택)")
            log_stderr(f"  2) python -m sourcing_agent verify {out['run_dir']}")
            out = {k: v for k, v in out.items() if k != "shortlist"}
        else:
            out = run_verify(a.run_dir)
    except Exception as e:
        from .errors import friendly
        cause = e.__cause__ if isinstance(e, SourcingError) and e.__cause__ else e
        log_stderr(f"오류: {friendly(e)}")
        print(json.dumps({"error": {"type": type(cause).__name__, "message": friendly(e)}}, ensure_ascii=False))
        return 1

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
