# APC 선생산 재고 대시보드 구축 프로젝트

## 1. 배경 / 목적
- 회사는 4분기 APC(생산설비 확장 공사) 기간 동안 **선생산 재고 진행 현황**을 엑셀로 관리해왔음.
- 매번 SAP에서 추출한 데이터(`Up_APC` 형태, "15" / "25" / "15 APC" 3개 테이블)를 `2B 제품` 시트와 `Pre 제품` 시트에 수동으로 반영.
- 시트/컬럼이 너무 많고 복잡해져 관리가 어려움 → **로컬 웹서버 기반 대시보드**로 전환.
- 실행 환경: 사내망에서 브라우저로 접속하는 로컬 웹서버 (외부 배포 아님).
- 데이터 갱신 방식: 사용자가 `Up_APC` 형태 파일(SAP 추출본)을 업로드하면 **자동으로 파싱 → DB 반영 → 대시보드 갱신**.
- 화면 요구사항: (1) 2B/Pre 제품별 월별 생산현황 **요약 화면**, (2) 고객사/N-code별 **상세 드릴다운 화면** 둘 다 필요.

## 2. 현재 원본 데이터 구조 분석 결과

### 2.1 인코딩 주의사항
- `2B_APC.csv`, `Pre_APC.csv`는 **CP949(한글 Windows) 인코딩**, CRLF/LF 혼용. UTF-8로 직접 읽으면 깨짐.
  → 파싱 시 `encoding='cp949'` 또는 `encoding='euc-kr'`로 읽고, 일부 깨진 바이트가 있을 수 있으니 `errors='ignore'` 옵션도 고려.
- `Up_APC.csv`는 ASCII, 구분자는 세미콜론(`;`).

### 2.2 `2B_APC.csv` (2B 제품 메인 시트)
- 총 77개 컬럼, 약 1,220 데이터 행.
- **헤더가 1~18행에 걸쳐 병합/줄바꿈된 다단(multi-row) 헤더** 구조 → 단순 1행 헤더로 읽으면 안 됨.
- 데이터 컬럼 그룹:
  - 스펙: Grade(ASTM/EN), Thickness(mm), N-code, Width(mm), Edge, Surface 등
  - 고객: Customer(Sold-to), Customer(Ship-to)
  - 월별 수량(2025-01 ~ 2026-12 등, kg 단위) — historical + forecast 혼재
  - 6개월 평균, 3개월 수량, 빈도(Frequency)
  - 생산 준비/계획 수량 (예: "JUL Production Plan quantity")
  - Order Balance / Produced Quantity / Production Balance Quantity (특정 기준일자 "24th JUN" 등 스냅샷 컬럼명 — **매월 갱신 시 헤더의 날짜 텍스트 자체가 바뀜**, 이 부분은 CLI 작업 시 사용자에게 재확인 필요)
  - 담당자: External PIC, Internal PIC
- **N-code 그룹 서브토탈 행 존재**: Grade 등 스펙 컬럼이 빈 행이 그룹 합계 행임. 파싱 시 이 행들을 "합계 행"으로 식별해서 분리 저장하거나, 원본 데이터에서 합계는 제외하고 대시보드에서 자체 계산하는 것을 권장.

### 2.3 `Pre_APC.csv` (Pre 제품 메인 시트)
- 총 69개 컬럼, 약 120 데이터 행. 구조는 2B와 유사하나 컬럼이 약간 다름(TS min/max(MPA) 컬럼 추가, Q4 Expect 컬럼 추가 등).
- 2B와 마찬가지로 다단 헤더 + 서브토탈 행 존재.

### 2.4 `Up_APC.csv` (SAP 추출 업데이트 파일)
- 한 파일 안에 **3개의 별도 테이블**이 순서대로 들어있음 (각 테이블 시작 전에 구분 라벨 행 존재):
  1. **"15" 테이블**: 수주 데이터. 컬럼: month, SDG Sold To Party, Ship To, SDG Sold To Party Name, Ship To Party, Country Code, SDG S/O Number, Material, POitem, Order Qty, RequestDeliveryDate, OTX Date Created, Order Type, Surface, Delivered Qty, Thickness, Created By, In Production Qty, Final Qty, Status Description, Reason, Ncode, Grade
  2. **"25" 테이블**: 생산/입고 실적. 컬럼: month, Batch, Coil No, Grade, N-code, OTX Order, Posting Date, Quantity, RM Surface, SDG Order, Ship-to, Ship-to party, SO Item, Sold-to, Sold-to party, Supplier Code, Thickness, Value, Width
  3. **"15 APC" 테이블**: 선생산 재고 진행현황 (메인 시트의 월별 컬럼을 채우는 원천 데이터로 추정). 컬럼: SDG S/O Number, Ncode, Customer PO No., Otx Item, Surface, Grade, Thickness, Width, Material, Description, Order Qty, In Production Qty, Produced Qty, In Transit, Transfered Qty, Final Qty, Production Balance, Warehouse STOCK Qty, RequestDeliveryDate, OTX Sales Order, month
- 실제로는 "15, 25, 15 APC 이렇게 세 파일을 업로드"한다고 하셨으므로, **운영 환경에서는 3개의 별도 파일로 업로드될 가능성**이 높음 (현재 샘플은 합쳐진 1개 파일). → 업로드 UI는 3개 파일을 각각 받거나, 하나의 파일에 3개 섹션이 있는 경우도 모두 처리할 수 있도록 설계.

## 3. 확인이 더 필요한 부분 (CLI 작업 중 사용자에게 재질문 권장)
1. `2B_APC` / `Pre_APC`의 정확한 헤더-컬럼 매핑표 (현재 일부 셀이 줄바꿈/병합으로 깨져 있어 자동 추론에 한계가 있음). 가능하면 원본 엑셀 파일(.xlsx, 병합 정보 포함)을 받아서 분석하는 것이 CSV보다 훨씬 정확함.
2. "24th JUN" 같이 **매월 바뀌는 날짜가 헤더에 박혀있는 컬럼**들의 의미와 갱신 규칙.
3. Up_APC의 "15 APC" 테이블 데이터가 2B_APC/Pre_APC 메인 시트의 어떤 월별 컬럼(예: 2026-07 등)에 어떻게 매핑되어 들어가는지 (현재 SOP 수작업 로직).
4. N-code 서브토탈 행을 대시보드에서 어떻게 다룰지 (원본 그대로 저장 vs 제외 후 자체 집계).
5. 2B/Pre 외 추가 제품 시트가 더 있는지 여부 (현재는 2개만 확인됨).

## 4. 제안 기술 스택
- 백엔드: Python (FastAPI) — pandas/openpyxl로 엑셀/CSV 파싱에 강점, 로컬 서버 운영 용이
- DB: SQLite (단일 파일, 별도 DB서버 설치 불필요, 사내망 단일 서버에 적합) — 추후 인입 데이터 누적/이력 추적까지 염두에 두고 설계
- 프론트엔드: 서버사이드 템플릿(Jinja2) + Chart.js 또는 React(간단한 SPA) — 요약 화면(차트) + 상세 화면(테이블, 필터/드릴다운) 둘 다 필요
- 파일 업로드: 웹 UI에서 Up_APC(or 15/25/15APC 3개) 파일 업로드 → 서버에서 파싱 → DB upsert → 대시보드 자동 갱신
- 실행: `uvicorn`으로 사내망 IP:PORT 바인딩, 필요시 Windows 작업 스케줄러 또는 NSSM으로 서비스 등록

## 5. 단계별 작업 계획 (Claude Code CLI 실행용)

### Phase 0. 데이터 분석 보강
- [ ] 원본 .xlsx 파일이 있다면 받아서 병합 셀/헤더 구조를 정확히 재분석
- [ ] 2B_APC, Pre_APC 각각의 정확한 "논리적 컬럼명 ↔ CSV 컬럼 인덱스" 매핑표를 `docs/column_mapping.md`로 작성
- [ ] N-code 서브토탈 행 식별 규칙 정의 및 문서화

### Phase 1. 데이터 파싱 모듈
- [ ] `parsers/main_sheet_parser.py`: 2B_APC/Pre_APC CSV(또는 xlsx)를 읽어 정규화된 DataFrame으로 변환 (cp949 인코딩 처리 포함)
- [ ] `parsers/sap_upload_parser.py`: Up_APC 형태(15/25/15APC, 1개 또는 3개 파일)를 파싱하는 모듈
- [ ] 단위 테스트: 업로드된 샘플 CSV로 파싱 결과 검증

### Phase 2. DB 설계 및 적재
- [ ] SQLite 스키마 설계: products(2B/Pre), specs(grade/thickness/ncode/width 등), monthly_quantities(제품-스펙-연월-수량), target_quantities(Category/Grade/목표수량 - 수동 편집 가능 마스터), upload_history(업로드 메타), weekly_snapshot(Grade/N-code별 기준일 스냅샷, 전주 대비 비교용)
- [ ] N-code 원자 데이터(`n_code_detail`)와 Grade 합계(쿼리 기반 자동 SUM, 저장된 서브토탈 비신뢰)를 분리 설계
- [ ] 초기 데이터 적재 스크립트(`scripts/init_load.py`)로 현재 2B_APC/Pre_APC 내용을 DB에 적재
- [ ] Up_APC(15/25/15APC 개별 파일 3개, 한 파일 합본 케이스 모두 호환) 업로드 시 upsert + 스냅샷 적재 로직 구현

### Phase 3. 백엔드 API
- [ ] FastAPI 앱 골격 (`app/main.py`)
- [ ] 업로드 API: `POST /upload` (Up_APC 파일 1~3개 수신 → 파싱 → DB 반영 → 결과 리턴)
- [ ] 조회 API: 제품별/월별 요약, 고객사별/N-code별 상세 데이터

### Phase 4. 프론트엔드 대시보드
- [ ] 요약 화면(관리자용): Standard/Precision 탭, Grade 단위 자동 집계표(Target/Order/월별 Production Order(15 APC Order Qty 합계, 매주 재집계)/Order Balance/Input Rate/Produced/Progress%) + Sum/Total
- [ ] 전주 대비 증감(Δ) 표시: 주요 지표(Order, Produced, Progress%)에 전주 스냅샷 대비 변화량/화살표 표시
- [ ] 상세 화면(실무자용): Grade 클릭 → N-code별 드릴다운(개별 수량/진행률), 고객사/N-code 필터·검색
- [ ] 업로드 화면: 15 / 25 / 15 APC 3개 파일 개별 업로드 슬롯 + 처리 결과/오류 표시 + 기준일(as_of_date) 자동 표시
- [ ] 자유 텍스트 코멘트 입력 영역(선택)

### Phase 5. 로컬 서버 배포
- [ ] requirements.txt, 실행 스크립트 작성
- [ ] 사내망 접속을 위한 호스트/포트 설정 가이드 (`README.md`)
- [ ] (선택) Windows 서비스 등록 방법 문서화

## 6. 실제 보고서 사례 분석 (6/19일 기준 주간 비축 현황 보고)
사용자가 실제로 작성/배포하는 보고서 텍스트를 확인함. 이 보고서가 대시보드의 **요약 화면(Summary)**이 자동으로 만들어내야 할 핵심 산출물로 보임.

### 6.1 보고서 구조 (Category → Grade별 집계표)
- **Category**: `Standard`(일반재, 2B_APC와 매칭 추정) / `Precision`(정밀재, Pre_APC와 매칭 추정)
- Category별로 **Grade**(316L(N), 304/304DL, 316L, 321, 309, 301HT5 등) 단위로 행 구성, 마지막에 **Sum 행**, 전체 **Total 행**
- 컬럼 구성:
  - `Target Quantity (MT)`: 그레이드별 4분기 비축 목표 수량
  - `Order from SDG (MT, Progress %)`: SDG 시스템상 주문 수량 및 목표 대비 진행률(%)
  - `Production Order from OTX`: 월별(DEC~JUL) OTX 생산오더 수량 + Sum + `Order Balance`(=Sum-주문 차이, 음수 가능) + `Input Rate %`(생산오더Sum/주문 대비 투입률)
  - `Produced (MT, Progress %)`: 실제 생산 완료 수량 및 목표 대비 진행률(%)
  - `Original Plan (FEB~JUN)`: 원래 월별 비축 계획 수량(비교 기준선)
- **Standard/Precision 각각 Sum 행 + 전체 Total 행**이 별도로 존재 → 대시보드 요약 화면의 **KPI 카드/롤업 합계 로직과 정확히 일치해야 함**

### 6.2 보고서 본문에서 언급된 핵심 지표/문구 (대시보드에 KPI로 구현 필요)
- 목표 대비 주문 진행률, 생산 진행률 (%, Produced/Target 등 — 위 표의 Progress 컬럼)
- "계획 대비 N톤 선행/후행 중" 같은 **계획선 대비 누적 생산 비교** (Original Plan 누적 vs 실제 누적 생산)
- "월말 기준 예상 비축량" 같은 **추세 기반 예측치** 코멘트 (이번 버전에서는 자동 계산까지는 필수 아니나, 추후 확장 고려)
- 생산 잔량(Order - Produced) 및 특정 N-code(예: 316Li) 제외한 "실질 가용 잔량/일수 환산" — 수동 코멘트 영역으로, 1차 버전에서는 텍스트 메모 필드로만 지원 가능
- **Input Rate(생산오더 Sum/주문 대비, %)**: 주문 대비 생산오더가 얼마나 투입되었는지 보는 별도 비율 → Progress(%)와 혼동되지 않도록 명확히 분리 표기 필요
- Precision(정밀재)은 별도로 **Pre-production Thickness Status**(중간재/최종두께 구분), 고객사 그룹(Alupro/Rolltech/TGI/EK, Tenneco/TE 등) 기준의 **소요량 계획 표**가 추가로 존재 → 향후 상세 화면에 "고객사 그룹별 정밀재 계획" 탭 추가 검토

### 6.3 안건/코멘트 섹션
- 보고서 하단에 안건(4분기 조기오더 대응, 2R 전환 검토, Consignment 고객사 조정 등) 텍스트 섹션이 있음.
- → 대시보드는 수치 자동화가 메인이지만, **자유 텍스트 코멘트/안건 입력란**(수기 작성 영역)을 요약 화면 하단에 추가하면 기존 보고서 작성 워크플로우를 그대로 흡수 가능. (선택 기능, Phase 4 이후 검토)

### 6.4 TASK 갱신 사항
- Phase 4(프론트엔드 대시보드) 요약 화면 요구사항을 아래로 구체화:
  - [ ] Standard/Precision 두 Category 탭(또는 토글)
  - [ ] Grade별 표: Target / Order(SDG) / 월별 Production Order(OTX) / Order Balance / Input Rate / Produced / Progress%, 우측에 Sum·Total 자동 합계
  - [ ] Original Plan 누적선 대비 실제 누적 생산 비교 차트(목표선 vs 실적선)
  - [ ] 자유 텍스트 코멘트 입력 영역(선택)
- **Target Quantity(MT) = 고정 상수값** (사용자 확인 완료). Grade별 4분기 비축 목표량으로, CSV/SAP 추출 데이터에서 계산되는 값이 아니라 **수동 설정값**임.
  - → DB에 `target_quantities` 마스터 테이블(Category, Grade, Target Qty(MT)) 별도 설계 필요. 코드 내 하드코딩이 아니라 **대시보드 관리자 화면(또는 설정 파일/DB 테이블)에서 수정 가능하게 구현** (분기/연도 바뀔 때마다 갱신해야 하므로).
  - Phase 2(DB 설계)에 `target_quantities` 테이블 추가, Phase 4에 목표값 편집 UI(간단한 설정 폼) 추가.
- **용어 매핑 확인 완료** → 8번 섹션 "확정된 설계 결정사항" 참고.

## 8. 확정된 설계 결정사항 (사용자 답변 반영)

1. **원본 .xlsx 제공 불가** → CSV 기반으로 파싱 로직 작업. 헤더 다단구조/병합 셀 추정에는 한계가 있을 수 있으므로, Phase 1 파싱 모듈 작업 시 결과를 샘플로 사용자에게 확인받는 검증 단계를 반드시 거칠 것.
2. **Grade 기준 = `Grade (ASTM)` 컬럼** (메인 시트 기준 확정). 보고서/대시보드의 모든 Grade 집계는 이 컬럼 기준으로 통일.
3. **"Production Order from OTX" = Up_APC "15 APC" 테이블의 `Order Qty` 열 합계**, **매주 새로 집계**(누적이 아니라 매번 그 시점 기준 재계산). → 파서/집계 로직에서 "15 APC" 업로드 시점마다 Order Qty SUM(Grade(ASTM) 기준 GROUP BY)을 다시 계산해서 저장.
4. **"24th JUN" 류 날짜 헤더 = 파일 업데이트할 때마다 바뀌는 스냅샷 라벨**. 즉 고정 컬럼이 아니라 "최근 업로드 기준일"을 의미함 → DB 설계 시 컬럼명에 날짜를 박지 말고, `as_of_date`(기준일) 필드를 갖는 형태로 정규화. 대시보드에는 "기준일: 2026-06-24" 식으로 동적 표시.
5. **N-code 서브토탈 = 이중 설계 확정**:
   - 관리자/요약 화면: N-code 합산 → Grade 단위 자체 집계(자동 계산, 원본 서브토탈 행 그대로 신뢰하지 않고 대시보드가 직접 SUM)
   - 실무자 상세 화면: N-code별 드릴다운 뷰 제공 (Grade 클릭 → 해당 Grade의 N-code 리스트 + 개별 수량/진행률)
   - → Phase 2 DB 스키마에 `n_code_detail`(원자 단위 데이터) 테이블을 두고, Grade 합계는 쿼리/뷰로 즉시 계산(저장된 서브토탈 값 사용 안 함)
6. **2B/Pre 외 추가 시트 여부**: 아직 미확인 (열려있는 질문으로 유지, 필요시 추가 시트 확보되는 대로 동일 파이프라인 확장)
7. **Up_APC 업로드 = 15 / 25 / 15 APC 각각 별도 파일 3개로 업로드**. → 업로드 UI는 3개의 개별 파일 입력 슬롯(또는 멀티 파일 업로드 후 파일명/내용으로 자동 타입 식별)으로 설계. 1개 파일에 3테이블이 합쳐진 케이스(현재 샘플)도 호환되게 파서는 양쪽 다 지원.
8. **업로드 이력 누적 저장 필요** ("지난주 대비 변화량" 비교 요구) → SQLite에 `upload_snapshots` 개념 도입:
   - 매 업로드(주 단위)마다 `as_of_date` 기준으로 스냅샷 저장 (덮어쓰지 않고 append)
   - 대시보드에 "전주 대비 증감(Δ)" 컬럼/지표 추가 (Order, Produced, Progress% 등 주요 지표 전부)
   - Phase 2 DB 스키마에 `upload_history`(업로드 메타) + `weekly_snapshot`(Grade/N-code별 기준일 스냅샷 수치) 테이블 추가
   - Phase 4 요약 화면에 "전주 대비" 비교 카드/화살표 표시 UI 추가

## 11. Forecast 탭 — 데이터 분석 기반 설계

### 11.1 데이터 분석 결과 요약 (실제 파일 기준)

**2B_APC (N-code 1,086개) — 현재 파일 6개월 기준**

| 패턴 | N-code 수 | 비율 |
|---|---|---|
| 완전 비활성 (6개월 내내 수요=0) | 400 | 36.8% |
| 간헐적 수요 (6개월 중 3~5개월 0) | 513 | 47.2% |
| 규칙적 수요 (안정적) | 173 | 15.9% |

> ⚠️ 위 패턴은 6개월 기준이므로 신뢰도 낮음. 2024~2026 전체 실적 적재 후 재분류 필수.
> 현재 "비활성"으로 분류된 N-code가 실제로는 Q4 집중 수요를 가진 계절형일 수 있음.

**Q4 데이터 커버리지 (2B_APC, 현재 파일 기준)**

| 레이어 | 수량 | 커버 N-code |
|---|---|---|
| ① 확정수주 (ZSCM015+ZSCM104) | 16,618 MT | 483개 (44.5%) |
| ② Customer FCST | 8,379 MT | 334개 (30.8%) |
| ③ 통계 예측 가능 (과거실적 있음) | 9,527 MT 추정 | 298개 (27.4%) |
| 아무 데이터 없음 (비활성) | - | 452개 (41.6%) |

**Pre_APC (N-code 92개)**
- 현재 파일 내 과거 데이터는 2026년부터만 존재 (2025년 전부 0) — 25 테이블 이력 확보 시 대폭 개선 예상
- Customer FCST 없음, **PIC FCST(Q4 Expect 컬럼)가 주요 예측 소스**: 30개 N-code, 399 MT
- 확정수주: 6개 N-code, 130 MT

---

### 11.2 N-code의 본질 및 모델 방향 확정

**N-code = 고객 + 스펙(Grade + Thickness + Width)의 조합**

N-code 단위 판매 실적은 이미 "특정 고객이 특정 스펙을 얼마나 샀는가"의 완결된 시계열.
Grade로 올려 집계하면 고객별 구매 패턴이 섞이고, 다시 N-code로 배분할 때 고객 신호가 희석됨.
→ **N-code 단위 직접 예측이 원칙. Grade 집계는 보조 수단(계절 지수 추출, 신규 N-code fallback)으로만 사용.**

| 모델 | 확정 역할 | 이유 |
|---|---|---|
| Croston/TSB | ✅ N-code 단위 직접 예측 핵심 모델 | 데이터 84%가 간헐적. 고객 구매 패턴 보존 |
| EWMA | ✅ N-code 단위 직접 예측 (규칙적) | 단기 예측 단순·강건 |
| Holt-Winters | ✅ Grade 단위 계절 지수 추출 전용 | N-code 직접 적용 아님. 계절 지수를 Croston/EWMA 결과에 보정 승수로 적용 |
| Grade 하향 배분 | ⚠️ 신규/이력 전무 N-code에만 fallback | 고객 신호 없을 때만 사용 |

---

### 11.3 확정 예측 모델 — N-code 직접 예측 + 계절 지수 보정

#### 전체 구조

```
[4-레이어 복합 모델]
Q4 N-code별 예측 = ① 확정수주  →  ② Customer/PIC FCST 보충  →  ③ 통계 예측  →  ④ Consignment 조정
```

| 레이어 | 소스 | 적용 조건 | 로직 |
|---|---|---|---|
| ① 확정수주 | 15 파일 (ZSCM015 + ZSCM104) | 항상 우선 | 월별 수주량 그대로 |
| ② Forecast 보충 | 2B: Customer FCST (col 38~40) / Pre: PIC FCST (col 52) | ①이 0인 월 | max(①, ②) — 중복 제거 |
| ③ 통계 예측 | 2024~2026 H1 전체 25 테이블 실적 | ①②가 모두 0인 월 | N-code 직접 예측 + 계절 지수 보정 (아래 참조) |
| ④ Consignment 조정 | BTB PO short/long 플래그 | 해당 N-code | 예측값 + 1개월치 버퍼 |

#### ③ 통계 예측 상세

```
[사전 작업] Grade 단위 계절 지수 추출 (Holt-Winters, 1회 계산 후 캐싱)
    ↓
    seasonal_index[grade][month] = Q4 월이 연평균 대비 몇 배인지
    예: 316L 10월 seasonal_index = 1.18 → 10월은 연평균 대비 18% 높음

[N-code 단위 직접 예측]
    ↓
    활성 비율 계산 (2024~2026 H1 전체 이력 기준)
    ↓
    활성 비율 ≥ 50% (규칙적)
        → EWMA (α=0.35) × seasonal_index
        → model: "EWMA_S"

    활성 비율 10~49% (간헐적)
        → Croston/TSB × seasonal_index
        → model: "CROSTON_S"

    활성 비율 1~9% (희소)
        → Croston/TSB 결과 그대로 (seasonal_index 적용 신뢰도 낮음)
        → model: "CROSTON"

    활성 비율 0% (이력 전무, 신규 N-code)
        → Grade 하향 배분 fallback
           (Grade Holt-Winters 예측 × 유사 스펙 N-code 비중)
        → model: "GRADE_FALLBACK"

[SPLY 병기] (참고 기준선, 대시보드에 항상 표시)
    → N-code 2025 Q4 실적 × (2026 H1 평균 / 2025 H1 평균)
    → 통계 예측치와 나란히 표시, 실무자가 최종 판단
```

**구현 코드 골격**
```python
def forecast_ncode(ncode, grade, history, target_months):
    ncode_series = history[ncode]                        # N-code 시계열 (고객 신호 보존)
    grade_series = history.groupby('grade')[grade].sum() # Grade 집계 (계절 지수용)
    active_ratio = (ncode_series > 0).mean()

    # 계절 지수 추출 (Grade 단위 Holt-Winters, 사전 캐싱)
    hw = ExponentialSmoothing(
        grade_series, trend='add', seasonal='add', seasonal_periods=12
    ).fit()
    seasonal_index = {m: hw.season_[m % 12] for m in target_months}

    # N-code 직접 예측
    if active_ratio >= 0.5:
        base = ewma(ncode_series, alpha=0.35, periods=3)
        forecast = [base[i] * seasonal_index[m] for i, m in enumerate(target_months)]
        model_used = "EWMA_S"

    elif active_ratio >= 0.1:
        base = croston_tsb(ncode_series, periods=3)
        forecast = [base[i] * seasonal_index[m] for i, m in enumerate(target_months)]
        model_used = "CROSTON_S"

    elif active_ratio > 0:
        forecast = croston_tsb(ncode_series, periods=3)  # 계절 지수 미적용
        model_used = "CROSTON"

    else:  # 이력 전무 — Grade 하향 배분 fallback
        grade_fc = hw.forecast(3)
        weight = get_similar_ncode_weight(ncode, grade, history)
        forecast = [g * weight for g in grade_fc]
        model_used = "GRADE_FALLBACK"

    # SPLY 참고 기준선
    sply = calc_sply(ncode_series)

    return forecast, sply, model_used
```

**계절 지수가 의미하는 것**
- Grade 전체 수요에서 Q4 월이 평균 대비 얼마나 높은지를 추출
- 예: 316L의 10월 seasonal_index = 1.18 → 개별 N-code 예측값에 ×1.18 적용
- N-code 자체 데이터만으론 계절성 신호가 약한 문제를 Grade 집계로 보완
- 희소 N-code(활성 1~9%)는 계절 지수 자체도 신뢰도 낮으므로 미적용

### 11.4 신뢰도 등급 (대시보드 표시용)

| 등급 | 조건 | 의미 |
|---|---|---|
| 🟢 HIGH | 확정수주 ≥ 70% | 거의 확정 |
| 🟡 MID | FCST 포함 ≥ 70%, 통계 보충 ≤ 30% | 신뢰할 만함 |
| 🔴 LOW | 통계 보충 > 50% (EWMA_S / CROSTON_S) | 주의 필요 |
| ⚪ STAT-C | CROSTON (희소, 계절지수 미적용) | 참고용 |
| ⬛ FALLBACK | GRADE_FALLBACK (이력 전무) | 참고용 |

---

### 11.5 Forecast 탭 DB 스키마

```sql
-- 전년도 판매실적 원본 (25 테이블 누적, 2024~)
CREATE TABLE sales_history (
    id INTEGER PRIMARY KEY,
    upload_date DATE,
    month TEXT,               -- '2024-10' 등
    ncode TEXT,
    grade_astm TEXT,
    sold_to TEXT,
    ship_to TEXT,
    quantity_kg REAL,
    thickness REAL,
    width REAL
);

-- Grade 단위 예측 결과 (Step 1 결과 캐싱)
CREATE TABLE grade_forecast_cache (
    id INTEGER PRIMARY KEY,
    as_of_date DATE,
    product_type TEXT,
    grade_astm TEXT,
    target_month TEXT,
    forecast_kg REAL,
    sply_kg REAL,             -- SPLY 참고 기준선
    model_used TEXT,          -- 'HOLT_WINTERS' / 'EWMA'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- N-code별 모델 분류 (전체 이력 분석 후 저장)
CREATE TABLE ncode_model_config (
    ncode TEXT PRIMARY KEY,
    product_type TEXT,
    grade_astm TEXT,
    active_ratio REAL,
    has_seasonality BOOLEAN,
    model_type TEXT,          -- 'TOPDOWN_HW'/'TOPDOWN_EWMA'/'CROSTON_TSB'/'SIMILAR_SPEC'/'ZERO'
    ncode_weight REAL,        -- 최근 6개월 기준 Grade 내 비중
    last_updated TIMESTAMP
);

-- Forecast 스냅샷 (매 업로드 시 저장, 전주 대비 비교용)
CREATE TABLE forecast_snapshots (
    id INTEGER PRIMARY KEY,
    as_of_date DATE,
    product_type TEXT,
    ncode TEXT,
    grade_astm TEXT,
    target_month TEXT,
    layer1_order_kg REAL,
    layer2_fcst_kg REAL,
    layer3_stat_kg REAL,
    layer4_consignment_adj REAL,
    total_forecast_kg REAL,
    sply_kg REAL,
    model_used TEXT,
    confidence TEXT,
    manual_override_kg REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### 11.6 Forecast 탭 화면 설계

```
[Forecast 탭]
┌──────────────────────────────────────────────────────────┐
│  기준일: 2026-06-24  │  대상: 26Q4 (OCT / NOV / DEC)    │
│  제품: [2B ▼]  Grade: [전체 ▼]  신뢰도: [전체 ▼]        │
└──────────────────────────────────────────────────────────┘

[KPI 카드]
┌────────────┬────────────┬────────────┬────────────┐
│ Q4 총 예측 │ 확정수주   │ FCST 커버  │ 통계 보충  │
│ XX,XXX MT  │ XX%        │ XX%        │ XX%        │
└────────────┴────────────┴────────────┴────────────┘

[Grade 단위 요약 테이블]
Grade  │ N-code수 │ OCT예측 │ NOV예측 │ DEC예측 │ Q4합계 │ SPLY참고 │ 신뢰도분포
────────────────────────────────────────────────────────────────────────────────
316L   │  461     │ X,XXX   │ X,XXX   │ X,XXX   │ X,XXX  │  X,XXX   │ 🟢●🟡●🔴○

↓ Grade 클릭 → N-code 드릴다운

[N-code 상세 테이블]
N-code │ ①확정수주 │ ②FCST │ ③통계(모델)            │ ④조정 │ 최종예측 │ SPLY  │ 신뢰도    │ Override
N13883 │  1,200   │   500 │    0  (—)             │    0  │  1,700  │ 1,500 │ 🟡 MID    │ [입력]
N15446 │      0   │     0 │  800  (EWMA_S)        │    0  │    800  │   700 │ 🔴 LOW    │ [입력]
N7205  │      0   │     0 │  300  (CROSTON_S)     │    0  │    300  │   250 │ 🔴 LOW    │ [입력]
N7888  │      0   │     0 │  150  (CROSTON)       │    0  │    150  │   100 │ ⚪ STAT-C │ [입력]
N17612 │      0   │     0 │   80  (GRADE_FALLBACK)│    0  │     80  │     0 │ ⬛FALLBACK│ [입력]

[전주 대비 변화량]
N-code별 이번주 예측 vs 지난주 예측 Δ 표시
```

---

### 11.7 Phase 추가 — Forecast 구현

#### Phase 4-F. Forecast 엔진 (백엔드)
- [ ] `forecast/data_prep.py`: 2024~2026 H1 전체 25 테이블을 `sales_history` 테이블에 적재. N-code × 월 시계열로 변환 (전 기간 동등 가중치)
- [ ] `forecast/pattern_classifier.py`: N-code별 패턴 분류 (활성 비율 계산, 계절성 검정 ACF). 결과를 `ncode_model_config` 저장
- [ ] `forecast/models.py`: 모델 구현
  - `holt_winters_seasonal_index(grade_series)`: Grade 단위 계절 지수 추출 전용 (statsmodels ExponentialSmoothing)
  - `ewma_s(ncode_series, seasonal_index, alpha=0.35, periods=3)`: EWMA + 계절 지수 보정
  - `croston_s(ncode_series, seasonal_index, periods=3)`: Croston/TSB + 계절 지수 보정
  - `croston_tsb(ncode_series, periods=3)`: Croston/TSB 단독 (희소 N-code용)
  - `grade_fallback(ncode, grade_fc, history)`: Grade 하향 배분 (이력 전무 fallback)
  - `calc_sply(ncode_series)`: 전년 동기 × 트렌드 보정 (항상 병기)
- [ ] `forecast/engine.py`: 4-레이어 조합 + 계층적 하향 배분 + 신뢰도 산정 + Override 처리
- [ ] `forecast/snapshot.py`: 매 업로드 시 스냅샷 저장, 전주 대비 Δ 계산
- [ ] API: `GET /forecast/summary` (Grade 단위 집계 + SPLY 병기), `GET /forecast/detail/{grade}` (N-code 드릴다운), `POST /forecast/override`
- [ ] `scripts/model_refresh.py`: 신규 이력 데이터 추가 시 N-code 패턴 재분류 실행

#### Phase 4-G. Forecast 프론트엔드
- [ ] KPI 카드 (Q4 총 예측, 레이어별 커버 비중)
- [ ] Grade 요약 테이블 + SPLY 컬럼 + 신뢰도 분포 시각화
- [ ] N-code 드릴다운 테이블 (레이어별 수치 분해 + 모델명 + SPLY + 신뢰도 배지 + Override 입력)
- [ ] 전주 대비 Δ 섹션

#### 미확인 사항 (Forecast 관련)
- [ ] **2024 데이터 적재 방식** — 연도별 파일로 분리돼 있는지, 누적 파일인지
- [ ] **Consignment 거래선 목록 확정** — 현재 BTB PO short/long 플래그로 식별, 실제 업체명 목록 확인 필요
- [ ] **Customer FCST 입수 방식** — 현재는 2B_APC 시트 내 컬럼(col 38~40)으로 관리. 향후 별도 파일로 분리 업로드 필요한지 확인
- [ ] **PIC FCST** (Pre_APC col 52 "Q4 Expect") 입력 담당자 및 갱신 주기

### 11.8 구현 현황 (2026-07-08 기준 — 실제 진행 상황)

**①②④ 레이어는 이미 이번 작업 이전부터 구현/운영 중이었음**: `app/routers/dashboard.py`의 `_build_q4_forecast()` + `/api/q4-ncodes/{category}` (26Q4 Order Forecast 카드, Grade 클릭 → N-code 드릴다운). Customer FCST/Consignment Stock은 col 38~40/col 52 임베디드 컬럼이 아니라 **별도 CSV 업로드(`Customer_Forecast.csv`/`Consignment_Stock.csv`) + 대시보드 수동 입력** 방식으로 이미 운영되고 있음(11.7 미확인 사항 1건은 사실상 해소됨).

**③ 레이어(통계 예측)를 이번에 신규 구현** — 단, 11.2~11.6의 최신 설계(N-code 직접예측 + Holt-Winters 계절지수 + SPLY)가 아니라, **더 이전 버전의 단순 모델**로 구현함:
- `app/forecast_engine.py`: `select_stat_model()`(활성월수 기준 EWMA/CROSTON/ZERO 3분류), `ewma_forecast()`, `croston_forecast()`, `assign_confidence()`
- 히스토리 소스: `sales_history`(2024~2026H1 누적, 미구현)가 아니라 **`monthly_quantities`의 2026-01~06 (6개월)** — `sap_production`(25 테이블)이 로컬/사내 PC 어디서도 한 번도 업로드된 적이 없어 실제로 쓸 수 있는 유일한 이력 소스였음
- `app/routers/dashboard.py`: `compute_ncode_q4_forecast()`(N-code 단위 4-레이어 합성), `_build_q4_forecast()`/`api_q4_ncodes()`에 Stat Fill/신뢰도 결과 연결
- `templates/index.html`: 26Q4 카드에 Stat Fill 소계 + 신뢰도(HIGH/MID/LOW) 배지 컬럼, N-code 드릴다운에 ①②③최종예측/신뢰도 요약 테이블 추가

**Holt-Winters 계절지수 / SPLY는 구현하지 않음 — 데이터 부재로 현재 수학적으로 계산 불가능**:
- DB 전체에 **2025년 데이터가 0건** (`sap_production` 0건, `sap_orders` 월 범위 없음, `monthly_quantities`는 2026년 12개월뿐)
- SPLY(전년 동기 대비)는 2025 실적이 있어야 정의 가능 → 지금 만들면 전 N-code가 N/A
- Holt-Winters 계절 지수는 통상 2개 시즌(24개월+) 이상의 이력이 필요 → 지금은 1년(그마저 7~12월은 실적이 아니라 CSV의 계획값)뿐이라 통계적으로 무의미
- → **2024~2025 25.csv(또는 동등 이력) 확보 시 재작업**. 그 전까지는 위 6개월 EWMA/Croston 버전이 운영 버전.

**이번 회차에 명시적으로 미룬 것**: 수동 Override 입력, `forecast_snapshots`/`ncode_model_config`/`grade_forecast_cache` 테이블(주차별 이력·Δ), 별도 사이드바 "Forecast" 탭(기존 26Q4 카드 확장으로 대체).

## 10. 참고 - 원본 샘플 파일 위치
- `2B_APC.csv`, `Pre_APC.csv`, `Up_APC.csv` (CP949 인코딩 원본)
- 작업 시 UTF-8 변환 캐시: 프로젝트 내 `data/raw/` 폴더에 원본 보관, `data/processed/`에 변환 결과 보관 권장
