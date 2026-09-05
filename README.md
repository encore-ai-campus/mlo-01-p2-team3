# MLO 1기 2차 프로젝트

## 1. 팀 소개

### 팀명

- `mlo-01-p2-team3`

### 멤버

| 이름 | 역할 | GitHub |
|---|---|---|
| 김영주 | 팀장 | [escapedaily99](https://github.com/escapedaily99) |
| 김민서 | 팀원 | [choy1372-sudo](https://github.com/choy1372-sudo) |
| 이재원 | 팀원 | [vvjeffvv3](https://github.com/vvjeffvv3) |
| 조성현 | 팀원 | [seonghyeonjo2](https://github.com/seonghyeonjo2) |

## 2. 프로젝트 개요

### 프로젝트명

조직 데이터 품질 진단·정제 서비스

### 프로젝트 필요성

레거시 조직·인사 데이터는 공백, 코드·날짜 형식 불일치, 중복 ID, 미등록 값 등으로 품질 문제가 발생할 수 있다. 이 프로젝트는 API로 수집한 데이터를 일관된 기준으로 표준화하고, 자동 처리할 수 없는 데이터는 별도로 검토할 수 있도록 한다.

### 프로젝트 소개

API에서 JSON 데이터를 주기적으로 수집해 API item envelope와 `payload`를 포함한 원문 전체를 MongoDB Bronze에 그대로 저장한다. 이후 Silver에서 표준화·정규화·검증을 수행하고, 정상 데이터만 MySQL Gold에 반영한다. 처리 결과와 검토 대상은 JSON 보고서와 Django 대시보드로 확인한다.

```text
API JSON → MongoDB Bronze → MongoDB Silver → MySQL Gold → Django 대시보드
                                      └─ 오류·판단 불가 → 검토 후 재처리
```

### 프로젝트 목표

- API 기반 조직·담당자 데이터 수집
- 원본 보존과 표준화·정규화 규칙 적용
- 오류·판단 불가 데이터 격리 및 재처리
- 검증된 Gold 데이터와 품질 리포트 제공

## 3. 핵심 데이터 기준

- 논리 원본 필드 17개는 고정한다.
- Bronze에는 API item envelope와 `payload`를 포함한 원문 전체를 변경 없이 저장한다.
- `record_id`, `scheduled_release_at`은 API 호출 시 함께 오는 envelope 원문이고, `payload`에는 업무 필드 15개만 둔다.
- envelope 메타데이터는 보존하지만 업무 컬럼을 추가한 것으로 세지 않는다.
- Silver에는 표준 업무 필드 15개를 저장한다.
- `record_id`와 `scheduled_release_at`은 API 원문 추적용이며 최신성 판단에 사용하지 않는다.
- API가 별도의 버전 기준을 확정하기 전에는 최신 데이터를 자동 선택하지 않는다.
- 최신 오류 데이터나 동일 시각 충돌 데이터는 기존 정상값을 유지하고 검토한다.
- `api_call_id`, 오류 코드, 규칙 버전은 업무 필드와 분리해 관리한다.

## 4. 기술 스택

| 구분 | 기술 |
|---|---|
| 처리 | Python |
| 원본·표준 저장 | MongoDB |
| 업무 저장 | MySQL |
| 대시보드 | Django |
| 규칙 설정 | YAML |
| 협업 | GitHub |

## 5. 문서 안내

| 목적 | 문서 |
|---|---|
| 목표·범위 | [BRD](./docs/brd.md), [PRD](./docs/prd.md) |
| 전체 명세 | [data_spec.md](./docs/data_spec.md) |
| 필드 의미 | [데이터 필드 사전](./docs/data/data_field_dictionary.md) |
| API 수집 | [API 수집 기준](./docs/data/api_data_collection_guide.md) |
| 정규화 | [도메인 규칙](./docs/data/data_normalization_and_domain.md) |
| 저장 구조 | [ERD](./docs/erd.md) |
| 파이프라인 | [파이프라인 설계](./docs/architecture/data_pipeline_design.md) |
| 품질·검증 | [품질 진단](./docs/quality/data_quality_assessment.md), [검증 계획](./docs/quality/data_validation_plan.md) |
| 실행 순서 | [프로젝트 체크리스트](./project_checklist.md) |

## 6. WBS 및 요구사항 명세서

| 단계 | 작업 항목 | 담당자 |
|---|---|---|
| 1 | API 계약 확인 및 Bronze 수집 구현 | 이재원|
| 2 | Silver 표준화·정규화·검증 구현 | 김영주,김민서 |
| 3 | Gold 적재 및 Django 대시보드 구현 | 조성현,이재원,김영주|
| 4 | 테스트·리포트·README 정리 | 김영주,조성현,김민서 |

세부 요구사항은 [BRD](./docs/brd.md), [PRD](./docs/prd.md), [프로젝트 체크리스트](./project_checklist.md)를 따른다.

## 7. ERD

MongoDB Bronze·Silver와 MySQL Gold의 상세 컬렉션·테이블 구조는 [ERD 문서](./docs/erd.md)에 정의한다.

## 8. 주요 프로시저

1. API를 호출하고 item envelope·`payload` 전체를 Bronze에 원문 그대로 저장한다.
2. Silver에서 YAML 규칙과 Python 검증 모듈로 표준화·정규화한다.
3. 오류·판단 불가 데이터는 검토 큐에 격리하고, 정상 데이터만 Gold에 적재한다.
4. 실행 결과와 오류 내역을 JSON으로 기록하고 Django 대시보드에 제공한다.

## 9. 구현 순서

1. API 계약과 필드 목록 확인
2. API 호출·Bronze 원본 저장
3. Silver 정규화·중복·버전 판정
4. 검증된 Silver를 Gold에 적재
5. Django 대시보드 연결
6. JSON 보고서와 테스트 결과 확인

## 10. 수행 결과

- 테스트 결과와 시연 화면 링크는 구현 완료 후 추가한다.
- 실행 로그와 JSON 검증 보고서는 저장소의 결과 경로에 보관한다.

## 11. 한 줄 회고

| 이름 | 회고 |
|---|---|
| 김영주 | 올바른 구조 설계 및 기획의 중요성을 다시금 깨달았다. |
| 김민서 | 작성 예정 |
| 이재원 | 작성 예정 |
| 조성현 | Medallion Architecture의 각 Layer 구조와 설계 방법에 대해 깊이 고민해 볼 수 있어 좋았고, 협업에 대한 이해도도 높아진 것 같다. |

## 12. 주의사항

- API가 별도의 버전 기준을 확정하기 전에는 최신값을 자동 선택하지 않는다.
- 검토 대상 데이터를 Silver·Gold에 직접 입력하지 않는다.
- 원본 필드의 추가·삭제·이름 변경은 문서와 API 계약을 함께 갱신한 뒤 진행한다.

## 13. PPT 링크

PPT 링크: https://drive.google.com/file/d/1TNDmZOdiIwpixb-BAIv216qA4IE-Gt7I/view?usp=sharing