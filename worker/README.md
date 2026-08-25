# Unified GMV + Invitation Worker

One Python worker process does all real work: Excel parsing, GMV lookup automation,
Invitation creator extraction (Playwright, US Chrome / UK Edge), and Excel output.
Both feature sets share port 8000, the same profile/session root, and the same job store.
The obsolete standalone `invitation-worker` process on port 8001 must not be started.
Runs on a host with **Google Chrome** and **Microsoft Edge** installed (never on Vercel — spec §8).

## Modules (`src/gmv/`)

| Module | Responsibility | Spec |
|---|---|---|
| `gmv_parser.py` | Raw GMV string → `ParsedGmv` (Decimal, range-max, open-ended) | §4, §5 |
| `creator.py` | Username normalize (`@`/URL/case) + exact matching | §2, §16 |
| `excel_engine.py` | Column detect, dedupe, openpyxl format-preserving write | §2, §3, §15 |
| `config.py` | US_CHROME / UK_EDGE profiles, row-level account resolution | §6, §7, §10 |
| `automation/` | Selector fallbacks, DOM+network extractors, session driver | §11, §16 |
| `job_runner.py` | Dedupe, per-account split, concurrency, retry orchestration | §10, §11, §15 |
| `service.py` | Job ↔ store bridge, per-row records, retry merge | §13, §15 |
| `store.py` | Local-fs job persistence (restart recovery) | §13, §14, §17 |
| `login_manager.py` | Manual (headed) login, verify, reset — no stored credentials | §9 |
| `api.py` / `worker_main.py` | FastAPI app + uvicorn entrypoint | §1, §9, §13 |

## Invitation Creator Extractor

`invitation_acceptor.py`, `invitation_acceptor_service.py`, and
`automation/invitation_inspector_session.py` implement ordered range parsing, durable
pause/resume/retry, product search, full-name exact matching, and Creator details extraction.
The Results sheet uses Invited creators as its row source and marks non-zero product/content
activity from each invited row with `O`. Jobs reuse the same four login profiles
as GMV but run in independent disposable profile clones, one visible browser page per Job.

## Setup

```powershell
cd worker
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chrome msedge   # uses installed channels
```

## Run

```powershell
$env:GMV_STORAGE_ROOT = "$env:LOCALAPPDATA\TikTokGMV\jobs"
$env:GMV_PROFILE_ROOT = "$env:LOCALAPPDATA\TikTokGMV\profiles"
.\.venv\Scripts\python.exe -m gmv.worker_main         # http://127.0.0.1:8000
```

Log in first (opens a real browser you control):

```
POST /profiles/US_CHROME/login      # US jobs: log in using the opened Chrome
POST /profiles/UK_EDGE/login        # UK jobs only: log in using the opened Edge
```

Only the profile selected for a job needs to be logged in. Chrome/US and Edge/UK have separate
session storage and separate busy reservations.

### Chrome/US security puzzle

The login action confirms and saves only the Seller Center account. It never opens the Affiliate
security hand-off or Creator Search. The US puzzle begins only after the user presses GMV Lookup
Start. The worker opens the Seller/Affiliate security entry and first checks whether a challenge is
actually present. With no puzzle it opens Creator Search immediately. With a puzzle it stops all
navigation and waits up to three minutes for manual completion. If TikTok lands on `/errorpage`
after successful verification, the worker preserves the verified state, refreshes the Seller
hand-off once, and then opens Creator Search. It never loops the Error-page Retry action or uses
the obsolete `target-invitation` route. The web app opens the results screen immediately, showing
all uploaded Creator names while verification is in progress.

## Test / lint

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

## Key environment variables

| Var | Default | Purpose |
|---|---|---|
| `GMV_STORAGE_ROOT` | `storage/jobs` | job files + json |
| `GMV_PROFILE_ROOT` | `storage/profiles` | per-profile browser data dirs (isolated) |
| `GMV_US_AFFILIATE_URL` | affiliate.tiktok.com…?shop_region=US | US Affiliate Center |
| `GMV_UK_AFFILIATE_URL` | affiliate.tiktok.com/platform/homepage?shop_region=GB | UK Affiliate Center |
| `GMV_HEADLESS` | `0` | `1` = headless job runs |
| `GMV_WATCHDOG_SECONDS` | `45` | 한 Creator의 브라우저 호출이 멈췄다고 판단할 시간 |
| `GMV_SESSION_RESTARTS` | `2` | 고착/브라우저 오류 시 같은 Creator에서 세션을 재시작할 최대 횟수 |
| `GMV_SESSION_START_SECONDS` | `75` | 퍼즐이 없을 때 브라우저 시작/Creator 화면 준비 제한 시간 |
| `GMV_SESSION_CLOSE_SECONDS` | `10` | 고착 세션 종료 제한 시간 |

실제 TikTok 퍼즐이 감지된 동안에는 watchdog 시간이 연장됩니다. 퍼즐이 없는데 검색
입력창만 잠깐 사라진 경우에는 인증 대기로 분류하지 않고 Creator 화면을 자동 복구합니다.
Windows 기본 실행 데이터는 `%LOCALAPPDATA%\TikTokGMV`에 저장되어 OneDrive 동기화와
Chromium 프로필 잠금의 영향을 받지 않습니다. `START_TIKTOK_AUTOMATION.bat`는 기존 프로젝트 내부의
로그인 프로필과 작업 기록이 있으면 첫 실행 때 새 위치로 복사합니다.

> Selectors in `automation/selectors.py` and the UK affiliate URL are **unverified** against
> the live DOM — confirm against a logged-in session before production (docs/decisions.md D10).
