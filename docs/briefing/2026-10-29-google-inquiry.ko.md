# Google에 보낼 자동 실행 문의 (2026-10-29)

소유자가 보내실 글입니다. 아래 영어 본문을 그대로 붙여 넣으면 됩니다.

## 왜 묻나

- Google의 개인 요금제(Google AI Pro · Ultra)로는 2026년 6월 18일부터 Gemini CLI를 쓸 수 없다. 뒤를 이은 도구가 Antigravity CLI(`agy`)다.
- Antigravity 안내는 "`agy -p`(화면 없이 돌리는 방식)는 직접 쓴 스크립트와 CI용"이라고 하면서, "다른 코딩 에이전트나 앱으로 감싸는 것과 계정 돌려쓰기 자동화는 개인 요금제에서 지원하지 않는다"고 적었다. 약관은 "제3자 도구로 서비스에 접속하는 것은 계약 위반이고 계정 정지 사유"라고 적었다.
- 도담 벤치마크는 측정 프로그램이 컨테이너를 띄우고 그 안에서 공식 CLI를 돌린다. 이것이 "직접 쓴 스크립트"인지 "감싸기"인지 문서로는 가릴 수 없다. Google 개발자 포럼에는 Ultra 구독자가 Antigravity 접근을 막혔다는 글이 여럿 있다.
- 그래서 허락을 글로 받기 전에는 Google AI Ultra(월 약 250달러)를 사지 않는다.

## 어디로 보내나

1. **Google AI 개발자 포럼의 Google Antigravity 게시판**(https://discuss.ai.google.dev)에 새 글로 올린다. 공개 글이라 Google 직원의 답이 기록으로 남는다.
2. Google One 회원이면 **Google One 고객센터 채팅**(https://one.google.com/support)에도 같은 글을 보내고, 받은 문의 번호를 적어 둔다. 상담원이 Antigravity는 다루지 못한다고 답할 수 있다.

## 답에 따라 하는 일

- **허락하면:** Google AI Ultra를 사서 세 번째 회사 모델로 쓴다. 답을 받은 화면이나 글 주소를 남긴다.
- **안 된다고 하거나 4주(2026년 12월 초) 안에 답이 없으면:** Google 개인 요금제는 쓰지 않는다. 세 번째 모델을 어떻게 채울지(Google의 기업용 요금제, 다른 회사 구독, 같은 회사의 다른 모델)는 AI가 약관을 조사해 정하고 알린다. 그동안 측정은 Claude와 OpenAI 모델로 3개를 채운다.

## 보낼 글 (영어)

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
