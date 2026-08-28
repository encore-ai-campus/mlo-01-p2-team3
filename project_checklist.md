# 프로젝트 구현 체크리스트

## 1. 고정 기준

- 논리 API 원본 17개 필드 고정
- Silver 업무 필드 15개 고정
- API item envelope·`payload`를 포함한 Bronze 원문 수정 금지
- `record_id`, `scheduled_release_at`은 envelope 원문, `payload`는 업무 필드 15개만 포함
- `release_at`은 `scheduled_release_at`의 문서상 별칭이며 별도 필드로 저장하지 않음
- `api_call_id`·`record_id`·`scheduled_release_at`은 추적용이며 최신 판정에 사용하지 않음
- 오류·판단 불가 데이터는 검토 후 재처리

## 2. 문서 확인

| 완료 | 확인 항목 | 문서 |
|---|---|---|
| [ ] | 프로젝트 목표와 범위 | [BRD](./docs/brd.md), [PRD](./docs/prd.md) |
| [ ] | 원본 필드·표준 필드 | [데이터 필드 사전](./docs/data/data_field_dictionary.md) |
| [ ] | 정규화·버전·오류 규칙 | [도메인 규칙](./docs/data/data_normalization_and_domain.md) |
| [ ] | API 수집 기준 | [API 수집 기준](./docs/data/api_data_collection_guide.md) |
| [ ] | 저장 구조 | [ERD](./docs/erd.md) |
| [ ] | 파이프라인 | [파이프라인 설계](./docs/architecture/data_pipeline_design.md) |
| [ ] | 품질·검증 | [품질 진단](./docs/quality/data_quality_assessment.md), [검증 계획](./docs/quality/data_validation_plan.md) |

## 3. 구현 순서

```text
API 호출 → 계약 검사 → Bronze → Silver → staging → Gold → Django
                                      └─ Review → 규칙 반영 → 재처리
```

## 4. API·Bronze

- [ ] API 주소·인증·페이지·증분 기준값 설정
- [ ] `api_call_id` 발급
- [ ] `429`, `408`, `5xx`, 네트워크 오류 재시도
- [ ] envelope·`payload`의 논리 17개 키·타입 검사
- [ ] 계약 오류 페이지 격리
- [ ] API envelope·`payload` 전체 Bronze 저장
- [ ] 응답 해시로 재수신 중복 방지

## 5. Silver 정규화

- [ ] ID·문자열·날짜·상태·NULL 정규화
- [ ] `미사용 → N` 매핑
- [ ] `UNKNOWN`, 오류 이름, 비정상 날짜 검토 처리
- [ ] API가 명시한 별도 버전 기준으로 최신 정상 전체 레코드 선택
- [ ] 최신 오류·지연 도착·동일 시각 충돌 격리
- [ ] 서로 다른 버전의 필드 혼합 금지
- [ ] 규칙 버전과 계보 기록

## 6. Gold·Django

- [ ] Silver 승인 건수를 staging에 적재
- [ ] PK·FK·조직 계층·조직당 담당자 1명 검사
- [ ] 실패 시 트랜잭션 취소·기존 Gold 유지
- [ ] 증분 미수신만으로 Gold 삭제 금지
- [ ] 조직·담당자·품질 화면 구현
- [ ] Gold와 대시보드 집계 대사

## 7. 검증·재처리

- [ ] 동일 실행 재실행 시 중복 Gold 0건
- [ ] 최신 정상 버전만 현재값 교체
- [ ] 최신 오류·지연 도착이 기존값을 변경하지 않음
- [ ] 검토 승인 후 새 `api_call_id`로 Bronze부터 재처리
- [ ] 실행 JSON 보고서 생성
- [ ] `hr_review_queue`, `hr_lineage_links` 조회 확인

## 8. 종료 기준

- [ ] 원본 17개와 Silver 15개 대응 검증
- [ ] 오류·판단 불가 승인 전 Gold 반영 0건
- [ ] 버전 오선택·필드 혼합 0건
- [ ] 계층별 건수 차이를 모두 설명
- [ ] README와 실행 방법 정리
