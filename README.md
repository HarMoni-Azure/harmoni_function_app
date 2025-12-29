[# HARMoni ](https://github.com/HarMoni-Azure/.github/blob/main/README.md) 

## 1. 전체 아키텍처 개요

<img width="1647" height="865" alt="이미지" src="https://github.com/user-attachments/assets/9479da72-0d4e-4921-9011-6f36d8c075e9" />

### 데이터 흐름

1. Wear OS 센서 데이터 수집
2. Azure Functions (HTTP Trigger)로 데이터 수신
3. Azure Blob Storage에 Raw JSON 데이터 적재
4. Databricks Auto Loader를 통한 데이터 로딩 및 모델 자동 고도화
5. Delta Lake 기반 Bronze → Silver → Gold Layer 구성
6. Feature Engineering 및 ML 학습
7. TFLite 변환 후 Edge 디바이스 적용

---

## 2. 비용 관리 중심 아키텍처 설계

본 프로젝트는 **비용 절감이 아닌 비용 관리(Cost Governance)**를 핵심 설계 기준으로 삼았다.

### 설계 배경
- IoT 특성상 이벤트 수 증가 시 비용이 기하급수적으로 증가
- 모든 데이터를 실시간 처리할 경우 비용 폭증 가능성 존재

### 적용 전략
- 실시간 처리가 반드시 필요한 **알림(Event)** 과  
  배치 처리 가능한 **데이터 적재(Data Lake)** 를 명확히 분리
- Azure Functions 기반 이벤트 수집으로 서버리스 비용 최적화
- Databricks는 필요한 시점에만 실행
- 전체 프로젝트 예산 상한: **120만 원**

→ 실제 사용 비용은 예산 대비 약 **20% 수준**으로 관리

---

## 3. Data Architecture (Medallion Architecture)

### Bronze Layer (Raw Zone)
- Wear OS 원본 센서 데이터
- JSON 형태로 Blob Storage 적재
- Auto Loader 사용
- 데이터 추적성과 재처리 가능성 확보

### Silver Layer
- 센서 데이터 정합성 검증
- 시간 기준 정렬
- 결측치 및 이상치 처리
- 학습 가능한 구조로 정제

### Gold Layer
- Sliding Window 기반 Feature 생성
- 시계열 특성 반영
- ML 학습용 Feature Table 구성

---

## 4. Feature Engineering

- Sliding Window 기반 시계열 분할
- Time Shifting을 활용한 데이터 증강
- 가속도·자이로 센서 기반 파생 Feature 생성
- 낙상 패턴 강조 Feature 설계

---

## 5. Machine Learning

- 낙상 여부 이진 분류 모델 학습
- MLflow 기반 실험 관리
- Validation Metrics:
  - Accuracy: 0.977
  - Precision: 0.967
  - Recall: 1.000
  - F1-score: 0.983

※ 단일 실험 데이터 기준 성능으로,  
실제 서비스 적용 시 추가 데이터 확보를 통한 재검증 필요
(수집 디바이스 Galaxy Watch)
---

## 6. 모델 경량화 및 배포

- TensorFlow 모델 → TensorFlow Lite 변환
- Wear OS 및 Edge Device 환경 고려
- 온디바이스 추론 구조 설계
- 네트워크 의존 최소화

---

## 7. IoT 프로젝트 관점에서의 쟁점 분석

IoT 데이터를 활용하는 프로젝트에서 핵심 쟁점은 다음 네 가지로 정리된다.

### 1) 데이터 수집 신뢰성
- 네트워크 불안정 시 데이터 유실 가능성
- 디바이스 오프라인 및 재부팅 조건 처리 미흡
- 중복 전송 문제 존재

**개선/적용**
- 날짜·순서 기반 파티셔닝으로 중복 데이터 관리
- 향후 Queue 기반 재전송 로직 필요

---

### 2) 스키마 변화와 데이터 품질
- 누락값, 이상치, JSON 깨짐 이슈
- 스키마 변경에 대한 명시적 버전 관리 부족

**아쉬운 점**
- 스키마 버전 관리 체계 미흡

**개선 방향**
- 스키마 버전 컬럼 추가
- Delta Lake의 Schema Evolution 적극 활용

---

### 3) 실시간 vs 배치의 경계
- 모든 데이터를 실시간 처리 시 비용 폭증
- 알림은 반드시 실시간 처리 필요

**잘한 점**
- 실시간 알림과 배치 적재 흐름을 명확히 분리
- 의사결정이 필요한 지점만 실시간 처리

---

### 4) 비용 관리 (핵심)
- IoT 이벤트 증가 시 비용 기하급수적 증가
- 이벤트 수 증가 시 예상 비용 산정 필요

**향후 과제**
- 이벤트 증가 시 비용 시뮬레이션
- Queue / Batch 전환 기준 명확화
- 다른 수집 아키텍처 대안 검토

---

## 8. 심사위원 코멘트 반영 사항

- 비용 관점의 트레이드 오프 설계가 인상적
- 이상치 판정 플로우 자동화 방향 긍정적
- 대형 현장뿐 아니라 소규모 현장에도 적용 가능성
- 품질 관리, 다중 워치 환경 보안 시나리오 보완 필요
- 개인정보 활용 시 작업자 동의 프로세스 필요
- 특정 산업군에 특화될 경우 고객 니즈 충족 가능성 높음
- 한달 예상 비용 시나리오 미비
---

## 9. 한계 및 개선 방향

- 실제 산업 현장 데이터 규모 제한
- 네트워크 장애 대응 로직 미흡
- 다중 디바이스 보안 시나리오 미구현
- 개인정보 동의 및 접근 제어 정책 미적용

---

## 10. 기대 효과 및 확장 방향

- 산업 현장 낙상 사고 대응 시간 단축
- 소규모·대규모 현장 모두 적용 가능한 구조
- 품질 관리 및 안전 모니터링 시스템으로 확장 가능
- 심박·위치 데이터 결합을 통한 고도화 가능

---

## 11. 정리

HARMoni는 단순 IoT 데이터 분석 프로젝트가 아닌,  
**실제 서비스 환경에서 발생할 수 있는 데이터·비용·운영 이슈를 고민한 실무 지향형 프로젝트**이다.

본 프로젝트를 통해  
Azure 기반 데이터 파이프라인 설계, 비용 관리 전략, IoT 아키텍처 트레이드 오프에 대한 경험을 축적했다.
