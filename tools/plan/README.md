# 계획 도구

도담 1.0까지의 계획 문서와 일정 시뮬레이션을 만드는 스크립트다.

- **작업 분해**는 `wbs_data.py` 하나에 있다. 일마다 날짜 · 앞선 일 · 산출물 · 끝났다는 표시 · 맡는 쪽 · 공수가 있고, `base_start` · `base_end`는 시뮬레이션이 늘리기 전의 계획 길이다.
- **문서 다시 만들기**: `sh build_all.sh`. 날짜 · 앞선 일 순서 · 코드 같은 표기를 검사한 뒤 `docs/plan`의 Markdown과 HTML을 다시 쓴다. 검사에 걸리면 멈춘다.
- **일정 다시 재기**: `python3 montecarlo2.py`, `python3 scen.py`, `python3 anchors.py`로 경우별 결과와 이정표의 가운데 · 80% 날짜를 다시 내고, `python3 retime.py`로 작업 날짜를 가운데 추정으로 옮긴 뒤 `sh build_all.sh`를 돌린다.
- **가정**(구독 용량, 일 길이가 늘어나는 정도, 위험의 확률과 기간, 측정이 쓰는 구독 용량)은 `montecarlo.py` 위쪽에 숫자로 있다. 잰 값이 나오면 이 숫자를 고친다.
- **페이지 검사**: `python3 check_page.py <html> <폴더>`로 휴대폰 · PC 화면과 밝은 · 어두운 테마에서 에러와 가로 넘침을 본다(Playwright 필요).
