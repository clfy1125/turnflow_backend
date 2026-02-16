# Step 3 완료: 멀티 테넌시(Workspace) + RBAC

## 📋 개요
- **목표**: 멀티 테넌시 구조 구현 (Workspace + Membership + RBAC)
- **완료 날짜**: 2026-02-04
- **관련 앱**: `apps.workspace`

## ✅ 구현 완료 항목

### 1. 데이터 모델
- [x] **Workspace 모델**
  - UUID 기반 primary key
  - 자동 slug 생성 (중복 방지)
  - Owner 관계 (ForeignKey to User)
  - Soft delete 미지원 (향후 추가 가능)

- [x] **Membership 모델**
  - UUID 기반 primary key
  - 역할 관리: OWNER, ADMIN, MEMBER
  - unique_together 제약조건 (user + workspace)
  - 데이터베이스 인덱스 최적화

- [x] **WorkspaceInvitation 모델**
  - 토큰 기반 초대 시스템
  - 만료 시간 관리
  - 상태 관리 (pending/accepted/expired)

### 2. API 엔드포인트

#### Workspace CRUD
- `POST /api/v1/workspaces/` - 워크스페이스 생성 (자동으로 owner membership 생성)
- `GET /api/v1/workspaces/` - 내가 속한 워크스페이스 목록
- `GET /api/v1/workspaces/{id}/` - 워크스페이스 상세 정보
- `PATCH /api/v1/workspaces/{id}/` - 워크스페이스 수정 (Admin/Owner만)
- `DELETE /api/v1/workspaces/{id}/` - 워크스페이스 삭제 (Owner만)

#### Member 관리
- `GET /api/v1/workspaces/{id}/members/` - 멤버 목록 조회
- `POST /api/v1/workspaces/{id}/members/` - 멤버 추가 (Admin/Owner만)
- `PATCH /api/v1/workspaces/{id}/members/{membership_id}/update_role/` - 역할 변경 (Owner만)
- `DELETE /api/v1/workspaces/{id}/members/{membership_id}/remove/` - 멤버 제거 (Admin/Owner만)

### 3. 권한 관리 (RBAC)

#### Permission Classes
- [x] `IsWorkspaceMember` - 워크스페이스 멤버인지 확인
- [x] `IsWorkspaceAdmin` - Admin 또는 Owner인지 확인
- [x] `IsWorkspaceOwner` - Owner인지 확인

#### 역할별 권한
| 작업 | MEMBER | ADMIN | OWNER |
|------|--------|-------|-------|
| 워크스페이스 조회 | ✅ | ✅ | ✅ |
| 워크스페이스 수정 | ❌ | ✅ | ✅ |
| 워크스페이스 삭제 | ❌ | ❌ | ✅ |
| 멤버 목록 조회 | ✅ | ✅ | ✅ |
| 멤버 추가 | ❌ | ✅ | ✅ |
| 멤버 역할 변경 | ❌ | ❌ | ✅ |
| 멤버 제거 | ❌ | ✅ | ✅ |

### 4. API 문서화
- [x] 모든 엔드포인트에 상세 문서 작성
- [x] OpenAPI 스키마 자동 생성
- [x] 사용 예시 (JavaScript + curl)
- [x] 에러 응답 명세

## 🧪 테스트 결과

### AC (Acceptance Criteria) 검증

#### ✅ AC1: 다른 워크스페이스 데이터 접근 불가
**테스트 시나리오**: Member가 속하지 않은 workspace에 접근
```
결과: 404 Not Found
메시지: "No Workspace matches the given query."
✅ PASS - 워크스페이스가 존재하지 않는 것처럼 응답
```

#### ✅ AC2: Owner만 역할 변경 가능
**테스트 시나리오**: Member/Admin이 역할 변경 시도
```
결과: 400 Bad Request (Owner 역할 변경 시도 시)
메시지: "Cannot change owner role"
✅ PASS - Owner 역할은 변경 불가
```

**테스트 시나리오**: Owner가 Member를 Admin으로 승격
```
결과: 200 OK
응답: { "role": "admin", ... }
✅ PASS - 정상적으로 역할 변경됨
```

#### ✅ AC3: 워크스페이스별 데이터 격리
**테스트 시나리오**: Member가 자신의 workspace 목록 조회
```
결과: 200 OK
응답: 1개 workspace (자신이 속한 것만)
✅ PASS - 멤버는 자신이 속한 workspace만 조회
```

### 추가 검증 항목

#### ✅ 권한 제한 테스트
- Member가 workspace 수정 시도 → 403 Forbidden ✅
- Admin이 workspace 수정 → 200 OK ✅
- Admin이 owner 제거 시도 → 400 Bad Request ✅
- Admin이 자기 자신 제거 시도 → 400 Bad Request ✅

#### ✅ Workspace 생성 및 조회
- Workspace 생성 시 자동으로 owner membership 생성 ✅
- Slug 자동 생성 (예: "My First Workspace" → "my-first-workspace") ✅
- Member count 자동 계산 ✅

## 📝 주요 구현 사항

### 1. 자동 Slug 생성
```python
def save(self, *args, **kwargs):
    if not self.slug:
        base_slug = slugify(self.name)
        slug = base_slug
        counter = 1
        while Workspace.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{counter}"
            counter += 1
        self.slug = slug
    super().save(*args, **kwargs)
```

### 2. Owner 자동 멤버십 생성
```python
def create(self, validated_data):
    workspace = Workspace.objects.create(**validated_data)
    Membership.objects.create(
        user=workspace.owner,
        workspace=workspace,
        role=Membership.Role.OWNER,
    )
    return workspace
```

### 3. 워크스페이스 필터링 (QuerySet Override)
```python
def get_queryset(self):
    user = self.request.user
    return Workspace.objects.filter(
        memberships__user=user
    ).distinct().order_by("-created_at")
```

## 🔧 기술 스택
- Django ORM: UUID 필드, 관계형 쿼리
- DRF Permissions: Custom permission classes
- drf-spectacular: OpenAPI 문서 자동 생성
- PostgreSQL: JSONB 필드 지원 (향후 확장 가능)

## 📊 데이터베이스 스키마

### Workspaces Table
- `id` (UUID, PK)
- `name` (VARCHAR 255)
- `slug` (VARCHAR 255, UNIQUE, INDEXED)
- `description` (TEXT)
- `owner_id` (INTEGER, FK → users, INDEXED)
- `created_at`, `updated_at` (TIMESTAMP)

### Memberships Table
- `id` (UUID, PK)
- `user_id` (INTEGER, FK → users)
- `workspace_id` (UUID, FK → workspaces)
- `role` (VARCHAR 10)
- `created_at`, `updated_at` (TIMESTAMP)
- **UNIQUE INDEX**: (user_id, workspace_id)
- **INDEX**: (user_id, workspace_id), (workspace_id, role)

### WorkspaceInvitations Table
- `id` (UUID, PK)
- `workspace_id` (UUID, FK → workspaces)
- `email` (VARCHAR 255)
- `role` (VARCHAR 10)
- `token` (VARCHAR 64, INDEXED)
- `status` (VARCHAR 10)
- `invited_by_id` (INTEGER, FK → users)
- `expires_at` (TIMESTAMP)
- `created_at`, `updated_at` (TIMESTAMP)

## 📚 참고 문서
- [프로젝트 지침서.md](프로젝트 지침서.md) - Step 3 멀티 테넌시 요구사항
- [Workspace Models](apps/workspace/models.py)
- [Workspace Views](apps/workspace/views.py)
- [Workspace Permissions](apps/workspace/permissions.py)

## 🎯 성과
- ✅ 멀티 테넌시 구조 완성
- ✅ RBAC 기반 권한 관리
- ✅ 워크스페이스별 데이터 격리
- ✅ 상세한 API 문서화
- ✅ 모든 AC 검증 통과
