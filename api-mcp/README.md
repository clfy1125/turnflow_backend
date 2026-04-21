# company-api-mcp

Company API 문서를 AI 도구(Cursor, Claude Code)가 컨텍스트 효율적으로 참조할 수 있는 검색형 MCP 서버입니다.
OpenAPI 스펙 전체(~100k 토큰)를 컨텍스트에 넣는 대신, 4개의 검색 tool로 상시 점유를 ~2k 토큰으로 줄입니다.

---

## 프론트엔드 개발자용 설치 (3단계)

### Step 1 — MCP 설정 파일 추가

`.cursor/mcp.json.example`을 복사해 `.cursor/mcp.json`으로 저장하거나, 기존 파일에 병합합니다.

```jsonc
{
  "servers": {
    "company-api-docs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/YOUR_ORG/api-mcp",
        "company-api-mcp"
      ],
      "env": {
        "OPENAPI_URL": "https://pro-earwig-presently.ngrok-free.app/api/schema/"
      }
    }
  }
}
```

> `YOUR_ORG`을 이 레포를 호스팅하는 GitHub 조직/유저명으로 교체하세요.

### Step 2 — Cursor 재시작

Cursor → Settings → MCP → Reload, 또는 IDE를 재시작합니다.

### Step 3 — 확인

AI에게 "API 태그 목록 알려줘"라고 물어보세요. `list_tags` tool을 호출한 뒤 태그 목록을 답합니다.

---

## 사용 시나리오

> **질문:** "Auto DM 캠페인 생성 API 어떻게 써?"

AI 처리 흐름:

1. `list_tags()` → `auto-dm` 태그가 있음을 확인 (소비: ~500 토큰)
2. `search_endpoints("campaign", tag="auto-dm")` → `integrations_auto_dm_campaigns_create` 발견 (소비: ~1k 토큰)
3. `get_endpoint("integrations_auto_dm_campaigns_create")` → request body / response 스키마 완전 전개 (소비: ~800 토큰)
4. AI가 실제 필드명·타입·예시값을 포함한 정확한 사용법 답변

전체 소비: ~2.3k 토큰 vs 스펙 전체 로드 시 ~100k 토큰.

---

## Tool 목록

| Tool | 설명 | 예상 응답 토큰 |
|------|------|---------------|
| `list_tags()` | 태그 인덱스 + 각 태그의 operation 수 | ~500 |
| `search_endpoints(query, tag?, method?)` | 키워드/태그/메서드 검색, 최대 20개 반환 | ~1–2k |
| `get_endpoint(operation_id)` | operation 전체 상세, `$ref` 모두 해소 | ~500–1k |
| `get_schema(name)` | 특정 컴포넌트 스키마, `$ref` 해소 | ~200–500k |

---

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `OPENAPI_URL` | `https://pro-earwig-presently.ngrok-free.app/api/schema/` | OpenAPI 스펙 URL (YAML/JSON) |
| `SPEC_CACHE_TTL` | `3600` | 재fetch 간격(초) |

ngrok URL이 일시 다운되면 마지막 캐시로 응답하고 응답에 `stale: true`가 포함됩니다.

---

## 로컬 개발

```bash
# 의존성 설치
uv sync

# MCP 서버 실행 (stdio 모드)
uv run company-api-mcp

# 스모크 테스트
uv run python smoke_test.py

# MCP Inspector로 tool 직접 호출
npx @modelcontextprotocol/inspector uv run company-api-mcp
```
