"""What the plan's rows about the pilot say, written from the pilot's estimates (pilot_replan.json).

The rows of the finished pilot, of the measurement that follows it and of the re-planning are kept in
wbs_data.py like every other row, but their figures and dates are not written there by hand: a test fails
unless those rows carry exactly what `rows()` gives for the committed estimates. The words below hold no
digit; every figure stands where a {name} is.

Usage: python3 pilot_rows.py   (prints the rows, to copy into wbs_data.py after the estimates changed)
"""
import datetime as dt
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDS = {
    "b_pilot": {
        "out": "회원가입 과제를 기존 언어 {languages}개와 모델 하나로 한 번씩 돌린 결과, 한 번 실행에 든 시간과 구독 사용량 표. "
               "한 번 실행에 AI가 일한 시간은 평균 {agent_seconds}초, 전체 시간은 평균 {wall_seconds}초였다",
        "done": "{pilot_day_long}에 끝났다. 시간과 토큰은 쟀다. 구독 한도는 위쪽 한계만 안다(주간 한도는 한 번에 "
                "{weekly_bound}%포인트보다 작고, {window_hours}시간 한도는 도구가 알려 주지 않는다). "
                "주당 쓸 수 있는 AI 작업일은 재지 못했고, 세션 기록으로 잰다",
        "start": "{pilot_day}", "end": "{pilot_day}",
    },
    "b_night": {
        "out": "같은 과제를 같은 모델로 하룻밤에 측정기의 지킴이가 허락하는 {night_runs}번, 다른 밤에 가장 큰 모델로 "
               "{large_runs}번 돌린 결과",
        "done": "주간 사용률이 그 밤에 움직이지 않으면 기준 측정 {baseline_runs}번이 {baseline_below}%포인트보다 적게 쓴다는 것(시범 날 "
                "남아 있던 여유는 {room}%포인트였다), 같은 실행을 되풀이했을 때의 흩어짐, 큰 모델이 이 과제에서 일하는 시간이 잰 값의 몇 "
                "배인지 안다. 계정 하나가 한 주에 내는 양은 이 측정이 아니라 세션 기록으로 잰다",
        "start": "{day_after_the_pilot}", "end": "{pilot_row_end}",
    },
    "pm_replan_nov": {
        "out": "시범에서 잰 시간으로 다시 판단한 {row_month}월 일정",
        "done": "{pilot_day_long}에 다시 판단했다. {row_month}월과 그 뒤의 날짜는 계획의 기준 용량에서의 계획 날짜로 그대로 두고, "
                "옮길지는 계정 하나가 한 주에 내는 양을 잰 뒤에 판단한다",
        "start": "{pilot_day}", "end": "{pilot_day}",
    },
}


def figures(replan: dict) -> dict:
    used, cap, sched = replan["usage"], replan["capacity"], replan["schedule"]
    day = dt.date.fromisoformat(sched["pilot_day"])
    plain = lambda value: f"{int(value):,}" if float(value).is_integer() else f"{value:,}"
    return {
        "languages": plain(cap["assumptions"]["languages"]),
        "agent_seconds": f"{used['agent_seconds_mean']:.1f}", "wall_seconds": f"{used['wall_seconds_mean']:.1f}",
        "weekly_bound": plain(used["weekly"]["one_run_below_points"]),
        "window_hours": plain(used["five_hour"]["guard_window_hours"]),
        "night_runs": plain(cap["next_measurements"]["same_model"]["runs"]),
        "large_runs": plain(cap["next_measurements"]["largest_model"]["runs"]),
        "baseline_runs": plain(cap["assumptions"]["runs"]),
        "baseline_below": plain(cap["next_measurements"]["same_model"]["baseline_below_points_if_unmoved"]),
        "room": plain(cap["next_measurements"]["same_model"]["room_on_the_pilot_day_points"]),
        "pilot_day": sched["pilot_day"], "pilot_day_long": f"{day.year}년 {day.month}월 {day.day}일",
        "day_after_the_pilot": str(day + dt.timedelta(days=1)), "pilot_row_end": sched["pilot_row"]["end"],
        "row_month": str(dt.date.fromisoformat(sched["baseline_row"]["start"]).month),
    }


def rows(replan: dict | None = None) -> dict:
    """For each of the plan's rows about the pilot, the fields whose figures come from the estimates."""
    if replan is None:
        replan = json.loads((HERE / "pilot_replan.json").read_text(encoding="utf-8"))
    known = figures(replan)
    return {task: {field: words.format(**known) for field, words in fields.items()} for task, fields in WORDS.items()}


if __name__ == "__main__":
    for task, fields in rows().items():
        for field, text in fields.items():
            print(f"{task}.{field} = {text!r}")
