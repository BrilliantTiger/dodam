# 할 일

## 지금 (2026년 10월 29일 ~ 2027년 2월 24일)

- [x] **10/29~11/1** — 조사 · 헌장 · 문서 커밋(헌장 v0.6까지)
- [x] **10/29~11/1** — 방향 고정 시기 조사: 구독 약관과 자동 실행, 측정 실행기와 과제 형식, 측정 도구 라이선스
- [ ] **11/2~11/9** — 구독 결제와 도메인 · GitHub 조직 확보(소유자), 과제 형식 확정, 측정기 뼈대 시작. Claude Code와 Codex CLI로 시험 실행이 되면 끝. 11/5 현재: 과제 형식과 측정기 뼈대는 끝났다. 측정 실행기 Harbor 0.25.0에서 정답 풀이는 1.0점, 아무것도 안 하는 실행은 0점을 받고, 취약 풀이는 그 취약점이 잡힌다. 채점은 네트워크가 없는 별도 컨테이너에서 하고, 에이전트는 일하는 동안 모델 서버에만 연결된다. 남은 것은 구독 계정으로 한 번 돌리기
- [ ] **11/2~11/23** — 측정 기계(성능 측정 전용 서버, 소유자 결제)와 기존 언어 7개 실행 이미지. 같은 프로그램을 10번 돌린 시간 편차가 2% 안이면 끝. 11/5 현재: 기존 언어 7개의 실행 이미지가 모두 끝났다(베이스 이미지와 패키지 버전 고정, 채점할 때는 네트워크 없이 빌드). 시간 편차는 측정 기계가 오면 잰다
- [ ] **11/8~11/13** — 견본 과제 하나(회원가입)를 끝까지. 11/5 현재: 과제 형식 · 변환기 · 채점기 · 과제 검사 도구는 끝났고, 기존 언어 7개의 정답 · 취약 · 고장 풀이 78개가 모두 실행 이미지 안에서 의도대로 판정된다. 남은 것은 구독 계정으로 한 번 돌려 숫자를 내는 것
- [ ] **11/15~12/11** — 과제 10개(웹 6 · CLI · 데이터 처리 4), BaxBench 과제 옮기기, 측정기 완성(공격 · 정적 분석 · 중복 · 데이터 · 속도)
- [ ] **11/29~12/6** — 실행 시범과 구독 용량 재기(한 번 실행이 구독 한도를 몇 % 쓰는지), 11월 일정 다시 잡기
- [ ] **12/12~1/1** — 기존 언어 측정 630번: AI 모델 3개가 과제 10개를 기존 언어 7개(Python · TypeScript · Java · C# · Go · Rust · C++)로 3번씩 짠다. 도담이 넘어야 할 숫자다. 공개 저장소 표본 수집 시작
- [ ] **1/2~1/11** — 실패 분류, 통계 설계, 잰 구독 용량으로 1.0 날짜 다시 내기
- [ ] **1/8~2/3** — 과제 30개(비공개 9개 분리), 문법 후보 비교(명세 크기 실험 포함), 선행 구현(Unison · Go Capslock · Ur/Web · Lamdera · Convex) 소스 읽기
- [ ] **2/4~2/17** — 방향 고정 결과 보고서
- [ ] **2/18~2/24** — 헌장 확정판(명세 크기 규칙, 위협 모델), 방향 고정 관문 판정

## 소유자가 하실 일

- [ ] **11월 첫 주** — 구독 결제: Claude Max 20x(월 200달러)와 ChatGPT Pro(월 200달러), 회사마다 계정 하나. Google AI Ultra는 아직 사지 않는다(아래 문의 답을 받은 뒤)
- [ ] **11월 첫 주** — 두 계정 설정: claude.ai 개인정보 설정에서 대화를 학습에 쓰지 않게 하고 추가 사용 과금(usage credits)을 끈다. ChatGPT 설정에서 "Improve the model for everyone"을 끄고, Codex 설정의 "Include environments"도 끈다
- [ ] **11월 첫 주** — Google에 자동 실행 문의 보내기. Google AI 개발자 포럼(https://discuss.ai.google.dev)의 Google Antigravity 게시판에 아래 영어 글을 새 글로 올린다(공식 이메일 창구는 없다). Google One 회원이면 고객센터 채팅(https://one.google.com/support)에도 보내고 문의 번호를 적어 둔다. 허락을 글로 받기 전에는 Google AI Ultra를 사지 않는다. 4주 안에 답이 없거나 안 된다고 하면 Google 개인 요금제는 쓰지 않는다

  <details><summary>보낼 글(영어)</summary>

  **Subject:** Is unattended use of Antigravity CLI (`agy -p`) on a Google AI Ultra plan allowed for an open-source benchmark?

  Hello,

  I plan to subscribe to Google AI Ultra for an open-source research project, Dodam, a new programming language. Before I buy, I would like a written answer, because the documentation leaves my case open.

  What we would do:

  - Run the official, unmodified Antigravity CLI in headless mode (`agy -p ...`) on a private server that I own, signed in with my own single Google AI Ultra account. No account sharing, no account rotation, no second account.
  - Each run asks the CLI to write one small program, such as a sign-up API, in one of seven languages. The program is then tested in a separate container that has no credentials.
  - Runs are sequential, at most a few hundred a month, within the plan's normal limits. Overage credits stay off.
  - Our own scripts start a container, run `agy -p` in it and collect the files it wrote. We may use the open-source evaluation harness Harbor to start that container. The Antigravity CLI is the only program that talks to Google's service.
  - We would publish scores per language, with the CLI version, model name and date. We would not use any output to train other models.

  Questions:

  1. Is this allowed under the Google AI Ultra plan and the Antigravity terms? Your FAQ says headless mode "is intended for direct scripting and CI workflows" and that "wrapping `agy` inside third-party coding agents or applications ... is not supported under consumer plans". Does running `agy -p` from our own scripts, or from a harness that only starts the container, count as wrapping?
  2. If it is allowed, what is the supported way to sign in inside a Linux container that has no desktop keyring?
  3. If it is not allowed on Google AI Ultra, which Google plan supports this use without API keys?

  Thank you,
  [name]

  </details>

- [ ] **11월 첫 주** — GitHub 조직 `dodam-lang` 만들기(https://github.com/organizations/new), 도메인 `dodam-lang.org` 사기
- [ ] **11월 중** — 성능 측정 전용 서버 결제(한 달 약 330유로)
- [ ] **11/28까지** — 시범 실행 준비(두 가지, 각 2분 안팎). 시범을 돌릴 컴퓨터에서 한다. 측정 기계가 그때까지 없으면 지금 이 Mac에서 한다. 먼저 터미널에서 `mkdir -p ~/.dodam/codex`로 도담 전용 폴더를 만든다.
  1. Claude: 터미널에서 `claude setup-token`을 실행하고, 브라우저에서 도담용 Claude 계정(Max 20x)으로 승인한다. 화면에 나온 토큰은 채팅에 붙여 넣지 말고 `~/.dodam/agent.env` 파일에 `CLAUDE_CODE_OAUTH_TOKEN=토큰` 한 줄로 저장한다(`chmod 600 ~/.dodam/agent.env`).
  2. Codex: 터미널에서 `CODEX_HOME=~/.dodam/codex codex login`을 실행하고, 브라우저에서 도담용 ChatGPT 계정(Pro)으로 승인한다. 로그인 정보가 다른 용도의 Codex 로그인과 섞이지 않게 도담 전용 폴더에 저장된다.
- [ ] **2027년 1월 말까지** — 변리사 상표 의견 받기(한국에 'DoDam' 2025-12-15 출원, '(주)도담솔루션' 등록이 있다)
