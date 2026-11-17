# 소유자가 하실 일

AI가 하는 일은 AI가 리니어 도담 팀의 프로젝트 "방향 고정"(2027년 2월 24일까지)에 일감으로 만들고 진행한다. 이 목록에는 소유자의 결제 · 계약 · 계정 · 승인만 둔다. 도담에는 새 돈을 쓰지 않는다(11/17 소유자 결정). 이미 쓰는 Claude 구독 말고는 0원이 되게 계획을 맞춘다.

## 남은 일

- [ ] **정하실 때** — 시범에 쓴 Claude 토큰을 새로 만들지 정하기. 시범 날(11/17) 앞서 시작한 2번은 토큰을 명령줄에 실어 넘기는 판의 실행 도구로 돌렸다. 그동안 토큰이 이 Mac mini의 프로세스 목록에 보였을 것이고, 그때는 프로세스 목록을 지켜보지 않아 얼마나 오래 보였는지, 누가 읽었는지는 알 수 없다. 프로세스 목록은 그 기계에서 도는 프로그램만 읽을 수 있다. 이것을 막는 수정은 같은 날 병합했다. 새로 만드는 것은 계정 일이라 소유자가 정한다
- [ ] **소개 페이지를 병합하기 전에** — 소개 페이지를 조직의 자체 도메인 아래에 공개해도 되는지 정하기. 저장소가 BrilliantTiger 조직에 있어서, GitHub Pages로 올리면 주소가 조직이 쓰는 도메인 아래의 `/dodam/`이 된다. 도담은 개인 프로젝트인데 페이지가 조직 도메인으로 보이게 된다. 고를 수 있는 길은 셋이다. 그대로 공개한다. `dodam-lang.org`를 사서 그 주소로 연결한다(새 돈이 든다). 저장소를 따로 만든 조직이나 계정으로 옮겨 그쪽 주소로 연다. 정해 주시면 나머지는 AI가 한다
- [ ] **소개 페이지를 병합하기 전에** — 소개 페이지 브랜치에 남은 옛 커밋을 어떻게 할지 정하기. 실수로 들어간 컴파일된 파이썬 파일을 빼려고 처음 커밋 두 개를 다시 만들어 덮어썼다. 컴파일된 파일에는 그것을 만든 기계의 폴더 경로가 들어갈 수 있다. 덮어쓰기 전의 커밋은 브랜치에서는 보이지 않지만 커밋 번호를 알면 GitHub가 아직 내주고, 그 커밋을 가리키는 자동 검사 실행 기록도 남아 있다. 고를 수 있는 길은 셋이다. 그대로 둔다. 자동 검사 실행 기록만 지운다(AI가 할 수 있고, 되돌릴 수 없다). GitHub 지원에 옛 커밋을 지워 달라고 요청한다(계정 주인만 할 수 있다)
- [ ] **하고 싶을 때(안 해도 된다)** — GitHub 계정 설정의 Emails에서 "Keep my email addresses private" 켜기. 브라우저에서 병합 버튼을 누르면 병합 커밋에 계정의 메일 주소가 적히는데, 이 설정을 켜면 GitHub가 주는 noreply 주소가 대신 적힌다. 터미널에서 만드는 커밋은 이미 noreply 주소로 나간다
- [ ] **비공개 과제 저장소를 검사하기 전에** — BrilliantTiger 조직의 GitHub 결제 확인. 비공개 저장소에서는 GitHub Actions가 "최근 결제가 실패했거나 지출 한도를 올려야 한다"며 시작되지 않았다(11/17). 이 조직의 다른 비공개 저장소도 자동 검사가 막혀 있을 수 있다. 도담 공개 저장소는 무료라 문제없다
- [ ] **리니어 체험판이 끝나기 전에** — 요금제 정하기. 지금은 Business 체험판이라 팀 수 제한이 없다. 무료로 돌아가면 팀 2개 · 일감 250개 제한인데, 워크스페이스에는 지금 팀이 3개다. 리니어 안내로는 지워지는 것은 없지만, 일감이 250개를 넘으면 새 일감을 만들 수 없다. Basic은 1인 월 10달러(1년 단위 결제)로 팀 5개 · 일감 무제한이다(리니어 요금 · 안내 페이지와 워크스페이스 팀 목록, 11/17 확인). 돈을 쓰지 않으려면 도담을 따로 만든 무료 워크스페이스로 옮긴다(워크스페이스 만들기와 연결은 소유자, 옮기기는 AI). 무료 워크스페이스에도 일감 250개 제한은 그대로라, 일감을 크게 묶어 그 안에서 쓴다
- [ ] **2027년 1월 말까지** — 변리사 상표 의견 받기(한국에 'DoDam' 2025-12-15 출원, '(주)도담솔루션' 등록이 있다). 저장소는 이미 'Dodam'이라는 이름으로 공개했다
- [ ] **보류(새 돈을 쓰지 않기로 해서)** — ChatGPT Pro(월 200달러)는 사지 않는다(11/17 결정). 다시 정하시면 바로 Codex를 붙여 검토와 측정에 쓴다. 그때는 이 Mac mini의 터미널에서 `mkdir -p ~/.dodam/codex`와 `CODEX_HOME=~/.dodam/codex codex login`을 차례로 실행하고, 브라우저에서 도담용 ChatGPT 계정으로 승인한다. 로그인 정보가 다른 용도의 Codex 로그인과 섞이지 않게 도담 전용 폴더에 저장된다. Claude만 쓰는 한 달마다 1.0이 두 회사로 할 때보다 약 2주 늦어진다. ChatGPT 설정에서 "Improve the model for everyone"을 끄고, Codex 설정의 "Include environments"도 끈다
- [ ] **보류(새 돈을 쓰지 않기로 해서)** — Google에 자동 실행 문의 보내기. 원래 11월 첫 주였는데, 11/17에 당분간 Claude끼리만 하고 새 돈을 쓰지 않기로 정해 미뤘다. Google AI Ultra(월 약 250달러)를 사지 않는 한 보낼 필요가 없고, 글은 다시 정하실 때 쓰도록 남겨 둔다. Google AI 개발자 포럼(https://discuss.ai.google.dev)의 Google Antigravity 게시판에 아래 영어 글을 새 글로 올린다(공식 이메일 창구는 없다). Google One 회원이면 고객센터 채팅(https://one.google.com/support)에도 보내고 문의 번호를 적어 둔다. 허락을 글로 받기 전에는 Google AI Ultra를 사지 않는다. 4주 안에 답이 없거나 안 된다고 하면 Google 개인 요금제는 쓰지 않는다

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

- [ ] **저장소가 자리를 잡으면** — 바깥 AI가 오도록 알릴 곳 정하기. AI가 알릴 곳 후보와 글을 드리면 고르신다. 바깥에 글을 올리는 일이라 소유자가 정한다
- [ ] **나중에** — 도메인 `dodam-lang.org` 사기. 저장소 공개에는 필요 없고, 웹사이트를 열 때 있으면 된다

## 끝난 일

- [x] **11/17** — 시범 실행에 쓸 Claude 토큰을 만들었다. 그 토큰으로 같은 날 시범 측정을 돌렸다(회원가입 과제를 기존 언어 7개로 한 번씩 돌린 7번을 세고, 그 앞에 Python으로 시작한 2번은 세지 않는다). 프로세스 목록을 지켜본 7번에서는 토큰이 명령줄에 보이지 않았다. 지켜보지 않은 앞의 2번에서는 보였을 것이다. 실행이 남긴 파일에서는 9번 모두 토큰이 나오지 않았다
- [x] **11/17** — 리니어에 공개 팀 "Dodam"을 만들었다
- [x] **11/17** — GitHub: 새 조직을 만들지 않고, 이미 쓰는 BrilliantTiger 조직에 저장소를 둔다
- [x] **11/17** — Claude 구독: 소유자의 개인 Claude Max 20x 계정을 도담 계정으로 쓴다. 따로 사지 않는다(회사마다 계정 하나)
- [x] **11/17** — Claude 계정 설정: 대화를 학습에 쓰지 않게 꺼 두었다(소유자 기억). 추가 사용 과금(usage credits)도 꺼져 있다(11/17 확인)
