"""The closed list of places in the repository that name a container tool or the container machine: every
function, class or top of a tracked Python, shell or workflow file in which such a name appears, with the reason it
is there. A new place fails the lock until it is entered here with its reason."""

PLACES: dict[tuple[str, str], str] = {
    ('bench/tests/frame_places.py', '<top>'):
        'this list itself, whose reasons name the container tool',
    ('.github/workflows/ci.yml', '<file>'):
        "the automatic check builds the image (the one step outside the frame) and runs the jobs that start containers under the frame's refusing command",
    ('bench/dodambench/convert.py', 'to_harbor'):
        "names the image of a task for Harbor's own file, the docker_image key; starts nothing",
    ('bench/dodambench/frame.py', '<top>'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', '_docker'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', '_start'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'env'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'give_space_back'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'open'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'read_command'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'run'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/frame.py', 'shim'):
        'the frame itself: the one place that reads, checks, writes and starts container commands',
    ('bench/dodambench/harbor_patch.py', '<top>'):
        "names the files of Harbor's docker environment that the patch changes; starts nothing",
    ('bench/dodambench/pilot.py', '_by_project'):
        "the pilot's named exception: lists Harbor's compose containers by their compose project, which carry no mark of the frame",
    ('bench/dodambench/pilot.py', 'harbor_command'):
        "names Harbor's docker environment on Harbor's command line; Harbor starts its containers through compose, the pilot's named exception",
    ('bench/dodambench/pilot.py', 'image_present'):
        'the pilot asks whether an image is there before it starts; it asks and starts nothing',
    ('bench/dodambench/pilot.py', 'remove'):
        "the pilot's named exception: removes Harbor's compose containers of its own trials by their compose projects, inside the frame's batch",
    ('bench/dodambench/vet.py', 'docker_create_cmd'):
        "writes the words of the vetting tool's container, which go to the frame's run only",
    ('bench/dodambench/vet.py', 'judge_in_image'):
        "every container command of the vetting tool goes through the frame's run",
    ('bench/dodambench/vet.py', 'machine_memory_mb'):
        "asks the container machine's memory through the frame's run",
    ('bench/experiments/cli_separation.py', 'in_container'):
        "the experiment's container commands go through the frame's run",
    ('bench/experiments/memory_overshoot.py', 'judged'):
        "the experiment's container commands go through the frame's run",
    ('bench/experiments/removal_time.py', 'main'):
        "the experiment's container commands go through the frame's run",
    ('bench/tests/frame_standins.py', '<top>'):
        "stand-ins for the container tool and the container machine in the frame's tests; start nothing",
    ('bench/tests/frame_standins.py', '__init__'):
        "stand-ins for the container tool and the container machine in the frame's tests; start nothing",
    ('bench/tests/frame_standins.py', 'machine_calls'):
        "stand-ins for the container tool and the container machine in the frame's tests; start nothing",
    ('bench/tests/frame_standins.py', 'tools'):
        "stand-ins for the container tool and the container machine in the frame's tests; start nothing",
    ('bench/tests/harbor_exec_probe.py', '<top>'):
        "builds Harbor's docker command without starting it, for the leak test",
    ('bench/tests/harbor_uses_probe.py', 'build_lock'):
        "reads which lock Harbor's docker environment uses, without starting it",
    ('bench/tests/test_cli_convert.py', '<top>'):
        "compares the converter's and the vetting tool's commands word for word; starts nothing",
    ('bench/tests/test_cli_convert.py', 'test_a_command_line_task_carries_its_caps_onto_the_path_the_measurement_uses'):
        "compares the converter's and the vetting tool's commands word for word; starts nothing",
    ('bench/tests/test_cli_convert.py', 'test_a_web_task_carries_its_caps_onto_the_path_the_measurement_uses'):
        "compares the converter's and the vetting tool's commands word for word; starts nothing",
    ('bench/tests/test_cli_convert.py', 'test_a_web_task_converts_as_before_and_is_vetted_in_a_capped_container'):
        "compares the converter's and the vetting tool's commands word for word; starts nothing",
    ('bench/tests/test_cli_convert.py', 'test_hidden_tests_and_the_case_runner_go_to_the_verifier_only'):
        "compares the converter's and the vetting tool's commands word for word; starts nothing",
    ('bench/tests/test_cli_isolation.py', '_image_ready'):
        "asks through the frame's run whether the image is there",
    ('bench/tests/test_cli_isolation.py', 'in_the_image'):
        "every container command of the judge's container tests goes through the frame's run or popen",
    ('bench/tests/test_cli_isolation.py', 'reader_command'):
        "writes the words of the judge's helper, which go to the frame's run only, with its machine folders named by keyword",
    ('bench/tests/test_cli_isolation.py', 'spy'):
        "watches the vetting tool's commands on their way to the frame's run",
    ('bench/tests/test_cli_isolation.py', 'test_what_one_case_reads_of_what_the_container_was_given_shows_nothing_to_the_next'):
        "its volume and container go through the frame's run",
    ('bench/tests/test_cli_judge.py', 'test_the_tests_helper_is_given_the_judged_containers_folder_and_nothing_else_of_the_machine'):
        "compares the helper's command word for word; starts nothing",
    ('bench/tests/test_cli_runtimes.py', '_image_ready'):
        "asks through the frame's run whether the image is there",
    ('bench/tests/test_cli_runtimes.py', 'test_no_helper_may_be_named_like_a_module_of_the_judges_python'):
        "its container goes through the frame's run",
    ('bench/tests/test_frame.py', '<top>'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'shim'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'started'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_a_batch_stops_itself_when_a_reading_crosses_its_line'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_a_container_whose_id_never_reached_its_file_is_found_by_the_exact_mark_of_the_run'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_a_named_volume_made_through_the_frame_is_removed_with_it_and_no_other'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_a_process_inside_a_frame_run_joins_that_batch_and_waits_for_no_lock'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_naming_another_run_in_the_environment_does_not_let_a_process_into_it'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_the_container_calls_of_one_process_are_one_batch_until_it_is_closed'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame.py', 'test_the_container_command_of_a_frame_run_works_with_an_environment_of_its_own'):
        "the frame's own tests, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', '<top>'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'made'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_command_that_names_a_container_or_a_volume_passes_only_for_the_batchs_own'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_folder_named_on_the_call_is_a_work_tree_or_a_folder_below_the_temporary_area_and_nothing_wider'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_named_volume_passes_only_when_the_batch_made_it'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_runs_record_stays_while_one_of_its_volumes_could_not_be_removed'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_stopped_batch_starts_nothing_by_any_way'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_a_volume_given_the_short_way_is_written_the_long_way_and_only_ro_or_rw_follow_it'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_copying_keeps_a_trailing_dot_and_checks_the_side_of_this_computer'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_every_row_of_the_table_is_written_from_what_was_read_and_reads_back_the_same'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_folders_of_the_container_machine_are_named_by_keyword_for_a_container_of_this_batch_only'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_in_the_automatic_check_the_command_on_the_path_refuses_every_call'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_no_file_in_the_run_folder_lets_a_command_pass_its_check'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_no_word_reaches_the_tool_that_was_not_written_from_the_table'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_several_names_are_each_confirmed_and_written_after_a_double_dash'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_the_code_starts_the_tool_by_its_fixed_path_not_by_the_path'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_the_home_folder_is_the_accounts_and_not_what_the_environment_says'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_the_tool_is_started_without_what_would_point_it_at_another_daemon'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_frame_table.py', 'test_what_the_table_does_not_have_is_refused_and_never_reaches_the_tool'):
        "the tests of the frame's table, whose commands reach a stand-in through the frame",
    ('bench/tests/test_gemini.py', '<top>'):
        "a stand-in container tool for the Gemini job's tests and the frame's settings for them; starts nothing",
    ('bench/tests/test_gemini.py', '__init__'):
        "a stand-in container tool for the Gemini job's tests and the frame's settings for them; starts nothing",
    ('bench/tests/test_gemini.py', 'test_the_containers_of_an_attack_run_are_removed_by_its_own_label_only'):
        "a stand-in container tool for the Gemini job's tests and the frame's settings for them; starts nothing",
    ('bench/tests/test_gemini.py', 'world'):
        "a stand-in container tool for the Gemini job's tests and the frame's settings for them; starts nothing",
    ('bench/tests/test_gemini.py', 'write_settings'):
        "a stand-in container tool for the Gemini job's tests and the frame's settings for them; starts nothing",
    ('bench/tests/test_harbor_patch.py', '<top>'):
        "names Harbor's docker files and a stand-in of them for the patch's tests; starts nothing",
    ('bench/tests/test_harbor_patch.py', 'fake_install'):
        "names Harbor's docker files and a stand-in of them for the patch's tests; starts nothing",
    ('bench/tests/test_harbor_patch.py', 'target_of'):
        "names Harbor's docker files and a stand-in of them for the patch's tests; starts nothing",
    ('bench/tests/test_harbor_patch.py', 'test_an_entry_script_that_changes_what_harbor_runs_is_refused'):
        "names Harbor's docker files and a stand-in of them for the patch's tests; starts nothing",
    ('bench/tests/test_pilot.py', '<top>'):
        "a stand-in Harbor and stand-in compose containers for the pilot's tests; starts nothing",
    ('bench/tests/test_pilot.py', '__call__'):
        "a stand-in Harbor and stand-in compose containers for the pilot's tests; starts nothing",
    ('bench/tests/test_pilot.py', 'remove'):
        "a stand-in Harbor and stand-in compose containers for the pilot's tests; starts nothing",
    ('bench/tests/test_proctable.py', '<top>'):
        "process tables written as text for the process watch's tests; starts nothing",
    ('bench/tests/test_proctable.py', 'tables'):
        "process tables written as text for the process watch's tests; starts nothing",
    ('bench/tests/test_proctable.py', 'test_the_processes_whose_command_line_holds_the_token_are_found'):
        "process tables written as text for the process watch's tests; starts nothing",
    ('bench/tests/test_vet.py', '_image_ready'):
        'a stand-in container tool given to the frame, and commands compared word for word; starts nothing real',
    ('bench/tests/test_vet.py', 'fake_docker'):
        'a stand-in container tool given to the frame, and commands compared word for word; starts nothing real',
    ('bench/tests/test_vet.py', 'run'):
        'a stand-in container tool given to the frame, and commands compared word for word; starts nothing real',
    ('bench/tests/test_vet.py', 'test_image_judge_runs_the_verifier_judge_without_network'):
        'a stand-in container tool given to the frame, and commands compared word for word; starts nothing real',
    ('tools/desk/check_lock.py', 'judge'):
        'names the container tool among the programs the desk may not start; starts nothing',
    ('tools/gemini/job.py', 'attack_batch'):
        "runs an attack run's agy inside a batch of the frame, with the frame's command first on agy's path",
    ('tools/gemini/job.py', 'remove_run_containers'):
        "removes the containers that carry the attack run's own label, a second removal beside the frame's",
}
