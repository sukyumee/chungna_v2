# 청라 식물공장 통합 모니터링 v4

Dash 기반 5탭 웹 모니터링 앱.

## 탭 구성

| 탭 | 설명 |
|---|---|
| 📡 실시간 환경 | FastAPI(`168.107.55.96:8000`)에서 온습도 fetch → 재배대 도면에 실시간 표시 (30초 자동갱신) |
| 🌿 재배 현황 | 재배대별 정식일 / 성장 단계 도면 (클릭 시 상세 + 수확 예측) |
| 📦 생산 이력 | Google Sheets 수확 데이터 테이블 + 차트 |
| 🤖 AI Agent | Claude API 채팅 (재배 현황 컨텍스트 포함) |
| 📊 온습도 통계 | 시간대별 / 계절별 평균 온습도 분포 (시간 슬라이더 + 애니메이션) |

## 로컬 실행

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:8050
```

## 환경변수 설정

| 변수 | 설명 |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API 키 (AI Agent 탭) |
| `GOOGLE_SERVICE_ACCOUNT_KEY` | 서비스 계정 JSON 내용 또는 파일 경로 (생산 이력 탭) |

로컬 개발 시: `service-account-key.json` 파일을 앱 폴더에 배치해도 됩니다.

## FastAPI 엔드포인트 설정

`app.py` 상단의 `FASTAPI_SENSOR_ENDPOINT` 변수를 실제 엔드포인트로 수정하세요.

```python
FASTAPI_SENSOR_ENDPOINT = "/api/realtime"
```

지원 응답 형식:
- **형식 A**: `{"beds": {"1": {"temp": 20.5, "hum": 78.3}, ...}}`
- **형식 B**: `[{"bed_id": 1, "temperature": 20.5, "humidity": 78.3}, ...]`

## Vercel 배포

1. GitHub에 push
2. [vercel.com](https://vercel.com) → New Project → GitHub repo 연결
3. Environment Variables 추가:
   - `ANTHROPIC_API_KEY`
   - `GOOGLE_SERVICE_ACCOUNT_KEY` (JSON 내용 전체 붙여넣기)
4. Deploy
