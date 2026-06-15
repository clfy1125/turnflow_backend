# 외부 임포터 변환기 — 업스트림 동기화 가이드

타사 바이오링크(인포크/리틀리/링크트리) 공개 페이지를 우리 페이지 포맷으로 복사해오는
**변환 규칙**은 외부 리서치 레포에서 벤더링한다. 이 문서는 그 출처/경계/동기화 절차를 정의한다.

- **업스트림**: [`Changus99/TurnflowLinkCopy`](https://github.com/Changus99/TurnflowLinkCopy)
  (`src/convert*.py` + `src/social_registry.py`)
- **핀(pin) 상태**: [`_sync.lock.json`](./_sync.lock.json) — 파일별 마지막 동기화/검토 ref.
- **동기화 도구**: [`scripts/sync_importers.py`](../../../../scripts/sync_importers.py) / `make sync-importers`

---

## 1. 포팅 경계 — 무엇이 벤더링되고 무엇이 우리 것인가

| 파일 | 출처 | 모드 | 비고 |
|---|---|---|---|
| `social_registry.py` | `src/social_registry.py` | **verbatim** | SNS 타입→id 매핑/값 정규화/지원키. 인포크·리틀리·링크트리 공용. |
| `litly.py` | `src/convert_litly.py` | **verbatim** | 업스트림과 바이트 동일. |
| `linktree.py` | `src/convert_linktree.py` | **verbatim** | 업스트림과 바이트 동일. |
| `inpock.py` | `src/convert.py` | **selective** | 레지스트리 변경만 반영. `/api/r/` eager 해석 **제외**(아래 §3). |
| `dispatch.py` / `builder.py` / `reupload.py` / `__init__.py` | — | **백엔드 글루** | 우리 것. 업스트림에 없음. fetch 디스패치 / Page·Block 생성 / 이미지 재업로드. |

- **verbatim** 파일은 손으로 편집하지 않는다. 업스트림 `src/<file>@<ref>` 와 줄 단위로 동일해야 하며,
  동기화는 사실상 "파일 교체"다. 그래서 향후 diff/재동기화가 깨끗하다.
- 업스트림 변환기들은 공용 레지스트리를 `from social_registry import ...` (flat) 로 참조한다.
  이 패키지에서 그 flat import 가 해석되도록 [`__init__.py`](./__init__.py) 가 변환기 import 전에
  `social_registry` 를 `sys.modules` 에 flat 이름으로 등록한다(shim). 덕분에 벤더 파일을 **무수정**으로 둘 수 있다.

## 2. 왜 git subtree/submodule 이 아니라 "제어된 벤더링"인가

업스트림은 `docs/sources/**` 샘플이 22만+ 줄인 **리서치 모노레포**라 subtree/submodule 은 전체를
우리 트리/체크아웃으로 끌어온다. 게다가 한 브랜치에 **우리가 원하는 변경(SNS 레지스트리)과 원치 않는
변경(인포크 `/api/r/` eager)** 이 섞여 있어 "ref 통째 pull" 은 위험하다. → 변환기 파일만 핀-기반으로
가져오되, **적용 전 변경분(diff)을 사람이 검토**하는 방식을 쓴다.

## 3. 인포크 selective 정책 (중요)

`inpock.py` 는 verbatim 이 **아니다**. 가져오는 것: 공용 `social_registry` 기반 SNS 매핑.
**제외하는 것**: 업스트림 main 의 `/api/r/` 추적링크 **즉시(eager) 해석**
(`resolve_inpock_redirect` / `preresolve_inpock_links` / `NetworkDownError`).

> 이유: 복사 시점에 한 IP 에서 추적링크 수백~수천 건을 즉시 해석하면 inpock 봇탐지/레이트리밋에 밴.
> 업스트림 자체 문서(`docs/inpock-link-copy-backend-spec.md`)도 "지금 코드 그대로 대량 출시 ❌,
> 출시 전 lazy `/r/{id}` 구조로 전환 필수" 라고 명시. 그 lazy 백엔드 기능은 **별도 과제**다.

`_sync.lock.json` 의 inpock `ref` 는 "여기까지 업스트림을 검토했다"는 마커일 뿐, content-equal 이 아니다.

## 4. 동기화 절차

사전 준비: 로컬에 업스트림 클론이 있어야 한다(없으면 스크립트가 `--url` 로 임시 클론).
기본 경로는 `../../TurnflowLinkCopy` (repo 루트 기준). `LINKCOPY_REPO=` 로 덮어쓸 수 있다.

```bash
# 1) 무엇이 바뀌었는지 본다 (dry-run, 적용 안 함)
make sync-importers REF=origin/main
#   또는 아직 머지 전이면:  REF=origin/feat/<branch>
#   파일별로 `git diff <pinned>..<REF> -- src/<file>` 를 출력한다.

# 2) verbatim 파일(litly/linktree/social_registry)을 그 ref 로 갱신 + lock 갱신
make sync-importers REF=origin/main APPLY=1
#   inpock 은 자동 적용되지 않는다 — diff 만 보여준다.

# 3) inpock 변경분이 있으면: 위 diff 에서 레지스트리/매핑 관련만 손으로 반영하고
#    (/api/r/ 류는 무시), 검토 완료를 lock 에 기록:
python scripts/sync_importers.py --repo ../../TurnflowLinkCopy --ref origin/main --mark-reviewed inpock.py

# 4) 검증
EXTERNAL_IMPORT_MOCK_MODE=true make test      # 임포터 테스트
make format && make lint-fix
```

스크립트는 verbatim 파일이 핀과 어긋나면(누가 손으로 고쳤으면) 경고한다.

## 5. 현재 핀

전부 `feat/sns-expand-clone-infra @ 53fea16` (2026-06-15) 기준. 이 브랜치가 main 에 머지되면
이후 동기화는 `REF=origin/main` 으로 한다. 자세한 값은 `_sync.lock.json`.
