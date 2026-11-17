"""Each test about the pilot fails because of the behaviour it tests.

For every test in test_pilot_record.py and test_pilot_report.py this file names one broken variant of the
implementation, of the data or of a document. The check copies the files, breaks them that one way, runs that
one test in the copy and expects it to fail. A test that still passed on its broken variant would be a test
that does not test what its name says. A test that runs once for each language has to fail in the language
whose file the variant breaks.

The table for people: python tests/test_pilot_variants.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECORD, REPORT = "test_pilot_record.py", "test_pilot_report.py"
SCRIPT = "bench/dodambench/pilot_record.py"
DATA = "bench/records/pilot-2026-11-17.json"
SIM = "tools/plan/montecarlo2.py"
ESTIMATES = "tools/plan/pilot_replan.py"
ESTIMATED = "tools/plan/pilot_replan.json"
EARLIER = "tools/plan/claude_only.json"
RENDER = "tools/pilot/render.py"
KO, EN = "tools/pilot/pilot.ko.md.tmpl", "tools/pilot/pilot.en.md.tmpl"
DOC_KO, DOC_EN = "docs/pilot/2026-11-17-pilot.ko.md", "docs/pilot/2026-11-17-pilot.en.md"
CHARTER_KO, CHARTER_EN = "docs/charter.ko.md", "docs/charter.en.md"
QUOTE_KO, QUOTE_EN = "tools/pilot/charter.ko.tmpl", "tools/pilot/charter.en.tmpl"
PLAN_DOC = "docs/plan/2026-10-29-plan-to-1.0.ko.md"
PLAN_PAGE = "docs/plan/2026-10-29-plan-to-1.0.ko.html"
RERENDER = "render the document again"

# (test file, test, what is broken, [(file, text, replacement), ...] and RERENDER where the document must follow)
VARIANTS = [
    (RECORD, "test_the_measured_values_of_a_run_are_copied",
     "the whole run's time is copied from another field",
     [(SCRIPT, '"wall_seconds": _number(record.get("wall_seconds")', '"wall_seconds": _number(record.get("harbor_wall_seconds")')]),
    (RECORD, "test_lists_that_may_hold_paths_or_names_become_counts",
     "a list is copied as it is instead of counted",
     [(SCRIPT, "    return len(value)\n", "    return value\n")]),
    (RECORD, "test_no_local_path_name_or_secret_of_the_records_reaches_the_output",
     "the folder of the run is copied into the output",
     [(SCRIPT, '        "language": lang,\n', '        "language": lang, "harbor_job": record.get("harbor_job"),\n')]),
    (RECORD, "test_a_record_field_that_is_not_plain_is_refused_instead_of_copied",
     "text is copied without checking its shape",
     [(SCRIPT, "    if not isinstance(value, str) or not shape.fullmatch(value):", "    if not isinstance(value, str):")]),
    (RECORD, "test_the_batches_come_out_in_time_order_and_the_runs_in_the_order_of_the_language_list",
     "the runs stay in the order they were read",
     [(SCRIPT, '    runs.sort(key=lambda r: order.index(r["language"]))\n', "")]),
    (RECORD, "test_a_language_run_in_two_batches_is_refused",
     "a language that was run twice is accepted",
     [(SCRIPT, "    if len(set(seen)) != len(seen):", "    if False:")]),
    (RECORD, "test_the_failed_checks_are_named_from_the_judge_s_details",
     "the passed checks are listed instead of the failed ones",
     [(SCRIPT, 'if test.get("outcome") == "passed":', 'if test.get("outcome") != "passed":')]),
    (RECORD, "test_what_a_judge_said_is_dropped_unless_it_is_one_plain_sentence",
     "whatever the judge said is kept",
     [(SCRIPT, "said = first[0] if first and SAID.fullmatch(first[0]) else None", "said = first[0] if first else None")]),
    (RECORD, "test_problems_names_paths_addresses_and_local_names",
     "paths and addresses are no longer looked for",
     [(SCRIPT, '        elif PATH_LIKE.search(text) or "/" in text or not PLAIN.fullmatch(text):', "        elif False:")]),
    (RECORD, "test_the_script_writes_nothing_when_a_local_name_is_in_the_values",
     "the file is written although a local name was found",
     [(SCRIPT, "    if found:\n", "    if False:\n")]),
    (RECORD, "test_the_committed_data_holds_one_run_of_each_of_the_seven_languages",
     "the data file is dated another day",
     [(DATA, '"date_utc": "2026-11-17"', '"date_utc": "2026-11-18"')]),
    (RECORD, "test_the_committed_data_holds_no_local_detail",
     "the data file carries a local path",
     [(DATA, '"what": "measured values of pilot runs', '"what": "/Users/somebody measured values of pilot runs')]),
    (RECORD, "test_the_committed_data_is_what_the_script_writes",
     "the data file has another layout number than the script",
     [(DATA, '"schema": 2', '"schema": 3')]),
    (RECORD, "test_starts_that_are_not_counted_are_kept_apart_from_the_runs",
     "a start that is not counted is put among the runs",
     [(SCRIPT, "        others.extend(batch_runs)", "        runs.extend(batch_runs)")]),
    (RECORD, "test_a_step_that_ended_with_an_error_keeps_the_kind_of_the_error_and_not_its_text",
     "the kind of an error is cut at the wrong place",
     [(SCRIPT, 'kind = str(error).split(":", 1)[0]', 'kind = str(error).split(" ", 1)[0]')]),
    (RECORD, "test_where_in_a_step_the_limits_were_read_is_counted_in_replies",
     "the wrong lines are counted as replies of the model",
     [(SCRIPT, '        if kind == "assistant":', '        if kind == "user":')]),
    (RECORD, "test_a_batch_cannot_be_both_counted_and_not_counted",
     "a batch given both ways is accepted",
     [(SCRIPT, '    if {b["started_utc"] for b in batches} & {b["started_utc"] for b in other_batches}:', "    if False:")]),
    (RECORD, "test_the_committed_data_tells_every_start_of_the_day_that_left_a_record",
     "the first start of the day is missing from the data file",
     [(DATA, '"batch_started_utc": "2026-11-17T07:20:23Z"', '"batch_started_utc": "2026-11-17T07:25:23Z"')]),
    (RECORD, "test_the_committed_data_says_where_the_weekly_readings_fell",
     "a reading is said to come after more replies than its step had",
     [(DATA, '"replies": 4,', '"replies": 2,')]),

    (REPORT, "test_a_finished_task_ends_on_its_day_and_nothing_waits_for_it",
     "the simulation ignores which tasks are finished",
     [(SIM, '        if it["id"] in finished:', "        if False:")]),
    (REPORT, "test_a_finished_task_takes_no_capacity",
     "the simulation ignores which tasks are finished",
     [(SIM, '        if it["id"] in finished:', "        if False:")]),
    (REPORT, "test_a_task_never_takes_fewer_days_than_its_shortest_length",
     "the simulation ignores the shortest length of a task",
     [(SIM, 'min_days.get(it["id"], 0))', "0)")]),
    (REPORT, "test_a_run_can_stop_at_a_named_task_with_the_same_dates_up_to_it",
     "the simulation always runs to the last task",
     [(SIM, "    target_k = IDX[until]", '    target_k = IDX["rel_10"]')]),
    (REPORT, "test_runs_per_night_follow_the_guard",
     "a run starts when the agent time has reached the limit, not only below it",
     [(ESTIMATES, "        if len(inside) * agent_seconds < limit_seconds:", "        if len(inside) * agent_seconds <= limit_seconds:")]),
    (REPORT, "test_runs_per_night_end_for_every_working_time",
     "no run is started when a run's working time is long",
     [(ESTIMATES, "            if t + wall_seconds > night_seconds:", "            if t + wall_seconds > night_seconds or agent_seconds > 2000:")]),
    (REPORT, "test_usage_adds_up_the_measured_values",
     "readings that showed the token are not added up",
     [(ESTIMATES, '"readings_with_the_token": sum(r["process_watch"]["readings_with_the_token"] for r in watched),',
       '"readings_with_the_token": 0,')]),
    (REPORT, "test_the_weekly_bound_rests_on_what_lies_wholly_between_the_two_readings",
     "the bound is worked out as if the readings bracketed all six later runs",
     [(ESTIMATES, "    between = round(between, 1)\n", "    between = round(sum(_agent(r) for r in later), 1)\n")]),
    (REPORT, "test_usage_tells_the_whole_day_and_scopes_the_watch_to_the_runs_it_covered",
     "the starts that nothing watched are not counted",
     [(ESTIMATES, '"starts_without_a_watch": len(unwatched),', '"starts_without_a_watch": 0,')]),
    (REPORT, "test_capacity_follows_from_the_mean_working_time",
     "the nights are rounded down",
     [(ESTIMATES, "        nights = math.ceil(total / per_night) if per_night else None",
       "        nights = math.floor(total / per_night) if per_night else None")]),
    (REPORT, "test_the_next_measurements_resolve_what_they_are_said_to_within_the_guard",
     "every run of the night is taken to lie between the first and the last reading",
     [(ESTIMATES, "    between = n1 - 1\n", "    between = n1\n")]),
    (REPORT, "test_the_committed_usage_and_capacity_are_what_the_script_computes",
     "a committed figure differs from what the script computes",
     [(ESTIMATED, '"agent_seconds_mean": 79.2', '"agent_seconds_mean": 79.3')]),
    (REPORT, "test_the_committed_schedule_is_what_the_simulation_gives_for_its_first_seed",
     "the committed file names another number of rolled tasks than the simulation rolls",
     [(ESTIMATED, '"tasks_rolled": 140', '"tasks_rolled": 141')]),
    (REPORT, "test_the_schedule_is_run_for_the_plan_s_capacity_and_for_today_s_and_each_effect_apart",
     "the shared account is counted with another capacity than the charter's",
     [(ESTIMATED, '"claude_shared": 4', '"claude_shared": 3')]),
    (REPORT, "test_the_noise_published_is_the_widest_spread_between_the_seeds",
     "a smaller noise is published than the seeds show",
     [(ESTIMATED, '"chances_move_up_to_points": 5', '"chances_move_up_to_points": 2')]),
    (REPORT, "test_the_forecast_states_its_seeds_and_the_plan_s_dates_are_one_of_them",
     "the forecast names another seed than the plan's",
     [(ESTIMATED, '"plan_seed": 2029', '"plan_seed": 2030')]),
    (REPORT, "test_the_plan_s_milestone_dates_are_what_one_command_gives",
     "the forecast rolls the task that was added after it was made",
     [("tools/plan/montecarlo.py", 'if not os.environ.get("DODAM_PLAN_ALL_TASKS"):', "if False:")]),
    (REPORT, "test_the_plan_document_is_what_its_scripts_write",
     "the plan's text differs from the committed document",
     [("tools/plan/plan_content.py", "**가장 큰 위험 셋:**", "**가장 큰 위험 넷:**")]),
    (REPORT, "test_the_plan_says_which_tasks_its_dates_come_from_and_under_which_capacity",
     "the plan says its dates come from every task",
     [(PLAN_DOC, "그 가운데 140개로 냈고", "그 가운데 141개로 냈고")]),
    (REPORT, "test_the_document_is_what_the_data_renders",
     "a figure in the documents was changed by hand",
     [(DOC_KO, "평균 79.2초", "평균 79.3초"), (DOC_EN, "79.2 seconds on average", "79.3 seconds on average")]),
    (REPORT, "test_a_template_holds_no_figure_of_its_own",
     "a figure is written into the templates",
     [(KO, "## 한눈에\n", "## 한눈에 7\n"), (EN, "## At a glance\n", "## At a glance 7\n")]),
    (REPORT, "test_a_changed_number_in_the_data_changes_the_document",
     "the documents no longer show how long the uncounted complete run took",
     [(RENDER, '                f[f"e{n}.{key}"] = one(value)', '                f[f"e{n}.{key}"] = one(0.0)'), RERENDER]),
    (REPORT, "test_both_languages_carry_the_same_figures",
     "a figure differs between the two languages",
     [(DOC_EN, "223.9 seconds", "223.8 seconds")]),
    (REPORT, "test_the_document_refuses_data_it_was_not_written_for",
     "the document is rendered although the token was seen",
     [(RENDER, '    if used["safety"]["readings_with_the_token"] or used["safety"]["leak_hits"] or used["safety"]["containers_left"]:',
       "    if False:")]),
    (REPORT, "test_the_document_refuses_data_that_would_make_its_scoped_statements_untrue",
     "the document is rendered although the watch did not cover what it says",
     [(RENDER, '    if used["safety"]["watched_runs"] != len(runs) or used["safety"]["starts_without_a_watch"] != today["not_counted"]:',
       "    if False:")]),
    (REPORT, "test_the_document_stands_on_its_own",
     "a code stands in the documents",
     [(DOC_KO, "## 한눈에\n", "## 한눈에 CWE\n"), (DOC_EN, "## At a glance\n", "## At a glance CWE\n")]),
    (REPORT, "test_wherever_the_document_gives_a_chance_at_the_plan_s_capacity_it_gives_today_s_beside_it",
     "the table for today's conditions is left out",
     [(KO, "@table claude_shared\n", ""), (EN, "@table claude_shared\n", "")]),
    (REPORT, "test_the_lines_about_the_pilot_in_a_hand_kept_document_are_rendered_from_the_data",
     "a figure in the charter, the README and the owner's list was changed by hand",
     [(CHARTER_KO, "한 번에 0.21%포인트보다 작다는 것만 알고", "한 번에 0.17%포인트보다 작다는 것만 알고"),
      (CHARTER_EN, "less than 0.21 percentage point, and the tool", "less than 0.17 percentage point, and the tool"),
      ("README.ko.md", "2041년 1월", "2040년 12월"), ("README.md", "January 2041", "December 2040"),
      ("TODO.md", "지켜본 7번에서는", "지켜본 9번에서는")]),
    (REPORT, "test_wherever_the_charter_gives_a_chance_at_the_plan_s_capacity_it_gives_today_s_beside_it",
     "the charter gives the plan's capacity where it should give today's",
     [(QUOTE_KO, "{{claude_shared.after_x1.", "{{planning.after_x1."), (QUOTE_EN, "{{claude_shared.after_x1.", "{{planning.after_x1.")]),
    (REPORT, "test_the_owner_s_list_scopes_what_it_says_about_the_token",
     "the owner's list says the token was never visible that day",
     [("TODO.md", "프로세스 목록을 지켜본 7번에서는 토큰이 명령줄에 보이지 않았다. 지켜보지 않은 앞의 2번에서는 보였을 것이다.",
       "토큰은 실행하는 동안 명령줄에 한 번도 보이지 않았고,")]),
    (REPORT, "test_a_changed_figure_in_the_data_is_missed_in_the_charter",
     "a figure of the charter is written by hand instead of coming from the data",
     [(QUOTE_KO, "{{next.same.runs}}", "46"), (QUOTE_EN, "{{next.same.runs}}", "46")]),
    (REPORT, "test_the_dates_in_the_history_row_of_charter_version_0_7_are_the_recorded_output",
     "a date in the history row of charter version 0.7 differs from the recorded output",
     [(CHARTER_KO, "두 회사면 2033-08-31", "두 회사면 2033-09-07"), (CHARTER_EN, "with two companies it is 2033-08-31", "with two companies it is 2033-09-07")]),
    (REPORT, "test_the_recorded_earlier_dates_are_what_their_script_gives",
     "the recorded output names another date than the script gives",
     [(EARLIER, '"p50": "2038-11-10"', '"p50": "2038-11-17"')]),
    (REPORT, "test_no_document_says_the_earlier_dates_could_not_be_reproduced",
     "the history row says again that the earlier dates could not be reproduced",
     [(CHARTER_KO, "그 계산을 저장소에 넣었고, 그것으로 세 날짜가 그대로 다시 나온다.", "그 계산은 다시 낼 수 없었다."),
      (CHARTER_EN, "It has been put into the repository, and it gives the three dates again exactly.", "It could not be reproduced.")]),
    (REPORT, "test_the_charter_says_what_explains_the_distance_between_the_earlier_date_and_the_range",
     "the charter is rendered although the earlier date lies outside the range of ten seeds as well",
     [(RENDER, '    if not alone["plan_scenario_scaled"]["earliest"] <= old <= alone["plan_scenario_scaled"]["latest"]:', "    if False:")]),
    (REPORT, "test_the_owner_s_item_about_a_new_token_says_what_is_known_and_not_known_about_revoking",
     "the owner's item says a new token ends the old one",
     [("TODO.md", "모르는 것: 새 토큰을 만들면 옛 토큰이 끝나는지.", "새 토큰을 만들면 옛 토큰은 끝난다.")]),
    (REPORT, "test_the_plan_s_rows_about_the_pilot_carry_what_the_estimates_give",
     "the plan's row of the finished pilot gives another weekly bound than the estimates",
     [("tools/plan/wbs_data.py", "한 번에 0.21%포인트보다 작고", "한 번에 0.17%포인트보다 작고")]),
    (REPORT, "test_the_words_of_the_plan_s_rows_about_the_pilot_hold_no_figure_of_their_own",
     "a figure of the plan's rows about the pilot is written by hand instead of coming from the estimates",
     [("tools/plan/pilot_rows.py", "허락하는 {night_runs}번", "허락하는 46번")]),
    (REPORT, "test_the_plan_s_sentences_about_the_pilot_say_what_was_measured",
     "a sentence of the plan gives another weekly bound than the estimates",
     [(PLAN_DOC, "한 번에 0.21%포인트보다 작다는 위쪽 한계만 안다", "한 번에 0.17%포인트보다 작다는 위쪽 한계만 안다")]),
    (REPORT, "test_the_dates_the_charter_keeps_are_the_plan_s_milestone_dates",
     "a date the charter keeps is changed by hand: the 1.0 date in the Korean table, the 80% date in the English paragraph",
     [(CHARTER_KO, "| ~ 2032년 2월 25일 | 1.0 |", "| ~ 2032년 3월 25일 | 1.0 |"),
      (CHARTER_EN, "with 80% probability is January 18, 2034.", "with 80% probability is January 25, 2034.")]),
    (REPORT, "test_every_figure_in_the_charter_s_part_on_the_schedule_is_written_from_the_estimates_or_named",
     "a figure of the charter's part on the schedule that has no computation is changed by hand",
     [(CHARTER_KO, "벤치마크 과제 30개(웹 20", "벤치마크 과제 31개(웹 20"),
      (CHARTER_EN, "30 benchmark tasks (20 web", "31 benchmark tasks (20 web")]),
    (REPORT, "test_the_plan_s_page_is_what_its_scripts_write",
     "a date in the plan's page is changed by hand",
     [(PLAN_PAGE, "2032년 2월 25일", "2032년 3월 25일")]),
    (REPORT, "test_no_statement_that_was_withdrawn_is_stated_as_current",
     "the owner's list says again that every month with Claude alone makes 1.0 about two weeks later",
     [("TODO.md", "ChatGPT 설정에서", "Claude만 쓰는 한 달마다 1.0이 두 회사로 할 때보다 약 2주 늦어진다. ChatGPT 설정에서")]),
    (REPORT, "test_the_owner_s_item_on_the_second_company_gives_what_the_charter_gives",
     "the owner's item gives another range for two companies than the estimates",
     [("TODO.md", "2033년 7월~2033년 9월이다", "2033년 7월~2033년 10월이다")]),
    (REPORT, "test_the_first_night_says_how_often_each_language_is_run",
     "the document says again that every language is repeated more than six times",
     [(DOC_KO, "언어 4개는 7번, 나머지 3개는 6번 되풀이하므로", "언어마다 6번 넘게 되풀이하므로")]),
    (REPORT, "test_the_document_says_when_other_work_can_only_raise_the_weekly_figure",
     "the condition under which other work can only raise the figure is left out",
     [(DOC_KO, "주간 한도의 한 주가 두 읽기 사이에 새로 시작하지 않는 한, ", ""),
      (DOC_EN, "Unless the week of the weekly limit starts anew between the two readings, other work", "Other work")]),
    (REPORT, "test_where_every_seed_gives_the_same_date_the_documents_say_so_instead_of_giving_a_range",
     "a document may say \"with every seed\" although the seeds give different dates",
     [(RENDER, '            if found["earliest"] == found["latest"]:    # only then may a document say "with every seed"',
       "            if True:")]),
    (REPORT, "test_the_date_at_twice_the_capacity_is_given_as_a_range_over_the_seeds",
     "the charter gives the date at twice the capacity as one month again",
     [(CHARTER_KO, "2030년 12월~2031년 1월 사이로 당겨진다", "2031년 1월로 당겨진다")]),
    (REPORT, "test_the_readme_gives_the_80_percent_dates_beside_the_middle_dates",
     "the 80% date in the README is changed by hand",
     [("README.ko.md", "80% 확률로는 2034년 1월까지다", "80% 확률로는 2034년 2월까지다"),
      ("README.md", "with 80% probability by January 2034", "with 80% probability by February 2034")]),
    (REPORT, "test_the_plan_s_row_for_the_one_night_measurement_says_what_the_charter_says",
     "the plan's row says again that an unmoved figure shows the baseline measurement fits the weekly limit",
     [("tools/plan/wbs_data.py", "기준 측정 630번이 14%포인트보다 적게 쓴다는 것(시범 날 남아 있던 여유는 15%포인트였다)",
       "기준 측정이 주간 한도에 든다는 것")]),
    (REPORT, "test_the_seeds_run_side_by_side_give_what_each_gives_alone",
     "the seed a run starts from depends on the seeds that run beside it",
     [(ESTIMATES, "[(cap, seed, sims) for seed in seeds]", "[(cap, seeds[0] * (n + 1), sims) for n, seed in enumerate(seeds)]")]),
    (REPORT, "test_the_parts_of_a_full_check_reject_whatever_the_whole_check_rejects",
     "the parts of the check take the comparison's seeds and numbers of runs from the recorded file again",
     [("tools/plan/claude_only.py", '    jobs = _jobs(SEEDS, SIMS, MORE_SIMS)\n    found = recorded.get("comparison")',
       '    found = recorded.get("comparison")\n    jobs = _jobs(found["seeds"], found["sims"], found["more_sims"])'),
      ("tools/plan/claude_only.py", "_assemble(kept, SEEDS, SIMS, MORE_SIMS)",
       '_assemble(kept, found["seeds"], found["sims"], found["more_sims"])')]),
]

COPIED = ("bench/dodambench", "bench/records", "bench/pyproject.toml", f"bench/tests/{RECORD}", f"bench/tests/{REPORT}",
          "tools/plan", "tools/pilot", "docs", "README.md", "README.ko.md", "TODO.md")


def copy_of_the_files(folder: Path) -> Path:
    for name in COPIED:
        source, target = ROOT / name, folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return folder


def run_the_test(folder: Path, file: str, test: str) -> tuple[int, str]:
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rfp", "-p", "no:cacheprovider", f"tests/{file}::{test}"],
                          cwd=folder / "bench", capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def break_it(folder: Path, edits: list) -> None:
    for edit in edits:
        if edit == RERENDER:
            subprocess.run([sys.executable, "render.py"], cwd=folder / "tools" / "pilot", check=True, capture_output=True)
            continue
        name, text, replacement = edit
        target = folder / name
        assert target.is_file(), f"{name}, which this variant breaks, is not in the repository"
        now = target.read_text(encoding="utf-8")
        assert text in now, f"{name} no longer holds the text this variant breaks"
        target.write_text(now.replace(text, replacement), encoding="utf-8")


def try_a_variant(variant) -> tuple[int, str]:
    """Break a fresh copy the one way the variant names and run its test there."""
    file, test, what, edits = variant
    with tempfile.TemporaryDirectory() as folder:
        folder = copy_of_the_files(Path(folder))
        try:
            break_it(folder, edits)
        except AssertionError as cannot:
            return -1, str(cannot)
        return run_the_test(folder, file, test)


@pytest.fixture(scope="session")
def outcomes() -> dict:
    """What every variant gave. The variants do not depend on each other, so they run side by side."""
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        return dict(zip((v[1] for v in VARIANTS), pool.map(try_a_variant, VARIANTS)))


# The workflow runs this check in a job of its own, beside the other tests; there the job that runs every
# other test sets this to "skip". Everywhere else the check runs with the rest.
IN_ANOTHER_JOB = os.environ.get("DODAM_PILOT_VARIANTS") == "skip"
OWN_ASSERTION = re.compile(r"(?m)^E +(AssertionError|assert |Failed: )")
OTHER_ERROR = re.compile(r"(?m)^E +(?!AssertionError)[A-Za-z.]*(Error|Exception)\b")


@pytest.mark.skipif(IN_ANOTHER_JOB, reason="this check runs in a job of its own")
@pytest.mark.parametrize("file,test,what,edits", VARIANTS, ids=[v[1] for v in VARIANTS])
def test_a_test_fails_on_its_broken_variant(outcomes, file, test, what, edits):
    code, output = outcomes[test]
    assert code != -1, output
    last = output.strip().splitlines()[-1] if output.strip() else ""
    assert code == 1 and re.search(r"\d+ failed", last) and "error" not in last, \
        f"{test} did not fail when {what}:\n{output[-1500:]}"
    assert OWN_ASSERTION.search(output) and not OTHER_ERROR.search(output), \
        f"{test} failed when {what}, but not on an assertion of its own:\n{output[-1500:]}"
    # A test that runs once for each language must fail in the language whose file the variant breaks.
    cases = {case: state for state, case in re.findall(rf"(?m)^(PASSED|FAILED) \S+::{test}\[([^\]]+)\]", output)}
    for lang in sorted({language_of(edit[0]) for edit in edits if edit != RERENDER} - {None}):
        own = [state for case, state in cases.items() if re.search(rf"(^|-){lang}$", case)]
        assert not own or "FAILED" in own, f"{test} did not fail in its {lang} case when {what}:\n{output[-1500:]}"


def language_of(name: str) -> str | None:
    """The language of a document or template, or None for a file both languages share."""
    if ".ko." in name or name in ("README.ko.md", "TODO.md"):
        return "ko"
    if ".en." in name or name == "README.md":
        return "en"
    return None


def test_every_test_about_the_pilot_has_a_broken_variant():
    named = {(file, test) for file, test, _, _ in VARIANTS}
    for file in (RECORD, REPORT):
        text = (ROOT / "bench" / "tests" / file).read_text(encoding="utf-8")
        for test in re.findall(r"(?m)^def (test_\w+)\(", text):
            assert (file, test) in named, f"{test} has no broken variant"
    assert len(named) == len(VARIANTS)


def test_the_copy_runs_the_tests_as_the_repository_does(tmp_path):
    """Unbroken, a test passes in the copy, so a failure above comes from the variant and not from copying."""
    folder = copy_of_the_files(tmp_path)
    code, output = run_the_test(folder, RECORD, "test_the_committed_data_is_what_the_script_writes")
    assert code == 0, output[-1500:]
    code, output = run_the_test(folder, REPORT, "test_the_lines_about_the_pilot_in_a_hand_kept_document_are_rendered_from_the_data")
    assert code == 0, output[-1500:]


if __name__ == "__main__":
    print("| Test | What is broken | Where |")
    print("|---|---|---|")
    for file, test, what, edits in VARIANTS:
        where = ", ".join(sorted({e[0] for e in edits if e != RERENDER}))
        print(f"| `{test}` | {what} | `{where}` |")
