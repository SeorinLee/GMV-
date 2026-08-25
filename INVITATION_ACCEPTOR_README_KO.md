# TikTok Shop 초대장 크리에이터 조회 사용법

## 실행

1. `START_TIKTOK_AUTOMATION.bat`을 실행합니다.
2. 상단 **TikTok 로그인**에서 사용할 Chrome/Edge + US/UK 조합을 로그인합니다.
3. 상단 **초대장 조회**로 이동합니다.
4. 초대장명을 줄바꿈 또는 쉼표로 입력하고 **초대장 조회 시작**을 누릅니다.

사용자 PC에 Python, Node.js, npm을 설치할 필요가 없습니다. 배포 ZIP에 Worker 실행 파일과 Node.js 런타임이 모두 포함됩니다. ZIP 전체를 압축 해제한 후 실행해야 하며 Windows 10/11 64비트와 Chrome 또는 Microsoft Edge가 필요합니다.

GMV 조회와 초대장 조회는 `worker/gmv-worker.exe` 하나에서 함께 실행됩니다. 열린 사이트의 상단 메뉴에서 두 기능을 선택합니다. 구형 `invitation-worker` 프로세스(8001번 포트)는 통합 런처가 종료합니다.

## 배포 파일 만들기

관리자 또는 개발자는 `scripts/build-portable.ps1`을 실행합니다. 기존에 빌드된 내장 런타임을 검사한 후 다음 두 산출물을 만듭니다.

- `dist/TikTok-GMV-Portable/`
- `dist/TikTok-GMV-Portable.zip`

생성된 ZIP만 사용자에게 전달하면 됩니다. 소스 코드, 테스트 파일, 로컬 로그인/작업 데이터는 ZIP에 포함되지 않습니다.

범위 입력 `PJH_SZPEUKA_0811_1~10`도 지원합니다. Owner에 `_`가 있어도 오른쪽부터 Product, Date, Number를 파싱합니다.

## 조회 흐름

US 작업은 다음 순서로 이동합니다.

1. `https://seller-us.tiktok.com/affiliate/landing?shop_region=US`
2. 로그인 세션에서 TikTok이 제공하는 실제 Affiliate Target Invitation 주소 확인
3. `/affiliate/collaboration/target-invitation?...&tab=1`
4. Product(예: `SZPEUKA`) 검색 및 전체 페이지 탐색
5. Full Invitation Name Exact Match 확인
6. **Creator details** 클릭
7. **Invited creators** 전체 페이지와 각 행의 상품 추가·콘텐츠 게시 수 추출

UK는 로그인된 UK Affiliate 페이지의 실제 origin을 사용합니다. `shop_id`는 코드에 고정하지 않고 TikTok이 로그인 세션에서 제공한 주소의 값을 유지합니다.

## 결과 XLSX

`Results` Sheet에는 다음 열이 생성됩니다.

- Keyword
- Invitation
- Creator
- Nickname
- Creator ID
- Region
- Added products
- Posted content

기준 행은 **Invited creators**입니다. 각 Creator 행의 상품 추가 수 또는 콘텐츠 게시 수가 1 이상이면 해당 열에 `O`를 기록합니다. 오류 초대장은 `Errors` Sheet에 기록합니다.

한 작업은 자동화 브라우저 창 하나만 사용합니다. 다른 사이트 탭에서 새 작업을 시작하면 별도의 창과 작업으로 동시에 실행됩니다. ID와 비밀번호는 저장하지 않으며 기존 로그인 Cookie/Session만 사용합니다.

이전의 자동 Accept Userscript는 이번 조회 기능에서 사용하지 마세요. 관리 페이지 + Worker 방식만 실행하세요.
