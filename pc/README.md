# 시드 실시간 감시 (기숙사 PC)

기숙사 Windows PC에서 켜두면, 정규장에는 한국투자증권 실시간 시세로 **몇 초 안에**, 프리·애프터마켓에는 **1분마다** 확인해서 알림을 보내요.
알림 규칙은 GitHub 감시와 같고, 이 프로그램이 켜져 있는 동안 GitHub 감시는 알림을 쉬어서 같은 알림이 두 번 오지 않아요. PC가 꺼지면 10분 안에 GitHub 감시가 다시 알림을 맡아요.

## 처음 한 번 설정

1. **파이썬 확인**: 명령 프롬프트에서 `python --version` 이 나오면 돼요. 없으면 python.org 에서 설치하고, 설치 첫 화면에서 **Add python.exe to PATH** 를 꼭 체크하세요.
2. **프로그램 받기**: GitHub `seed` 저장소 → 초록색 **Code** → **Download ZIP** → 원하는 곳(예: `C:\seed`)에 압축 풀기.
3. **한국투자증권 앱키**: 한국투자증권 계좌로 KIS Developers 에서 Open API 를 신청하면 **앱키**와 **시크릿키**를 받아요.
4. **GitHub 토큰** (관심종목을 자동으로 읽어오는 용도):
   GitHub 오른쪽 위 프로필 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
   - Repository access: **Only select repositories** → `seed`
   - Permissions → Repository permissions → **Contents: Read-only**
   - 만들어진 토큰(`github_pat_...`)을 복사해 두세요.
5. **설정 파일**: `pc` 폴더의 `config.example.ini` 를 복사해서 이름을 `config.ini` 로 바꾸고, 메모장으로 열어 앱키·시크릿키·ntfy 토픽·GitHub 토큰을 채워요.
6. **시험 실행**: `test.bat` 를 더블클릭하면 필요한 걸 설치하고, 폰으로 "시드 PC 감시 시작" 알림이 와요.

## 매일 쓰기

- `run.bat` 를 더블클릭하면 검은 창이 뜨고 감시를 시작해요. **창을 닫으면 멈춰요.** 최소화해 두세요.
- PC를 켤 때 자동으로 시작하려면: **Win + R** → `shell:startup` 입력 → 열린 폴더에 `run.bat` 의 **바로 가기**를 넣어요.
- **절전 끄기**: 설정 → 시스템 → 전원 → 화면 및 절전 → 전원 연결 시 절전을 **안 함**으로. 절전에 들어가면 감시도 멈춰요.
- 무슨 일이 있었는지는 같은 폴더의 `seed.log` 에 남아요.

## 알아둘 점

- 관심종목과 알림선은 GitHub 의 `watchlist.csv` 를 10분마다 읽어와요. 시드에서 바꾸고 CSV를 붙여넣으면 PC에도 10분 안에 반영돼요.
- 실시간 시세 항목(`tr_id`)의 기본값은 거래소 정규장 체결가예요. 프리·애프터마켓은 1분마다 네이버 시세로 확인해요.
- 앱키·시크릿키·토큰이 들어 있는 `config.ini` 는 절대 다른 사람에게 보내거나 GitHub 에 올리지 마세요.
- 프로그램이 바뀌면 ZIP 을 다시 받아 덮어쓰면 돼요. `config.ini` 는 그대로 두세요.
