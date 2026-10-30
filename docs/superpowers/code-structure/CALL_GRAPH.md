# Call Graph

Static best-effort relationships. Confirm dynamic behavior in source when needed.

## scripts/verify_memory_model.py
- main -> finish

## src/application/agent.py
- Agent.ask -> AgentStateError, CompletedTurnStorageError
- Agent._parse_tool_call -> AgentStateError

## src/application/cli.py
- main -> _show_model_event, _show_tool_event, memory_command, schedule_command

## src/application/context_builder.py
- wire_messages -> compact
- ContextBuilder.build -> compact, select, wire_messages

## src/common/config.py
- Settings.from_env -> _boolean, _bounded_float, _bounded_int, _load_dotenv, _memory_directory, _nonempty, _path_from_root, _positive_float, _positive_int, _timezone
- _bounded_int -> _positive_int
- _bounded_float -> _positive_float

## src/common/logging_config.py
- configure_logging -> get_logger

## src/common/prompt_loader.py
- load_system_prompt -> PromptLoadError

## src/domain/memory/conversation.py
- new_turn -> message_record

## src/domain/memory/fact.py
- validate_fact -> evidence_messages
- merge_facts -> fact_key, validate_fact

## src/domain/memory/privacy.py
- sensitive -> redact

## src/domain/memory/state.py
- MemoryState.from_dict -> MemoryError

## src/domain/schedule.py
- utc_iso -> _parse
- normalize_scheduled_time -> _parse, _valid_local_candidates, _zone, utc_iso
- validate_rule -> _repeat_value, _weekdays
- validate_recurrence -> _parse, _zone, validate_rule
- next_occurrence -> _parse, _valid_local_candidates, _zone, utc_iso, validate_recurrence

## src/service/notifications.py
- NotificationDispatcher.__init__ -> _no_header_newlines
- NotificationDispatcher.validate_channel -> NotificationError
- NotificationDispatcher.send -> _no_header_newlines
- NotificationDispatcher._send_email -> NotificationError
- NotificationDispatcher._send_webhook -> NotificationError, _NoRedirectHandler

## src/service/ollama.py
- OllamaClient.chat -> OllamaError

## src/service/schedule_store.py
- ScheduleStore.create -> _json, _now, _strict_grace, _strict_text, list
- ScheduleStore.get -> _strict_text
- ScheduleStore.update -> _json, _now, _strict_grace, _strict_text, list
- ScheduleStore._set_status -> _now
- ScheduleStore.history -> _strict_text
- ScheduleStore.claim_due -> _now, _strict_grace
- ScheduleStore.save_result -> _now, _strict_text
- ScheduleStore.notification_due -> _now
- ScheduleStore.notification_failed -> _now
- ScheduleStore.mark_failed -> _now
- ScheduleStore.recover_interrupted -> _now

## tests/test_agent.py
- AgentTests.test_automatically_executes_and_evaluates_tool_result -> FakeModel, FakeSearchTool
- AgentTests.test_reports_tool_execution_events -> FakeModel, FakeSearchTool
- AgentTests.test_reports_model_execution_events -> FakeModel, FakeSearchTool
- AgentTests.test_executes_same_round_tool_calls_concurrently_and_preserves_order -> BarrierSearchTool, ParallelModel

## tests/test_layered_memory.py
- LayeredMemoryTests.test_ten_turns_then_eleventh_running_and_compacted -> Extractor, fact, finish
- LayeredMemoryTests.test_no_facts_still_archives_with_receipt -> Extractor, finish
- LayeredMemoryTests.test_failed_extraction_preserves_history_and_retry -> Extractor, fact, finish
- LayeredMemoryTests.test_fact_dedup_conflict_correction_and_scope -> fact, finish
- LayeredMemoryTests.test_fake_sources_secret_role_and_unsupported_change_rejected -> fact, finish
- LayeredMemoryTests.test_task_transitions_evidence_and_interleaved_question -> Extractor, finish, task
- LayeredMemoryTests.test_reset_preserves_facts_tasks_but_blocks_continuation -> fact, finish, task
- LayeredMemoryTests.test_forget_prevents_old_reextraction_allows_new -> fact, finish
- LayeredMemoryTests.test_context_truncation_preserves_tool_pairs_and_current_input -> finish
- LayeredMemoryTests.test_checkpoint_failure_means_tool_not_run -> Model, Tool
- LayeredMemoryTests.test_result_save_failure_does_not_replay_tool -> Model, Tool
- LayeredMemoryTests.test_revision_conflict_unknown_version_and_archive_integrity -> finish
- LayeredMemoryTests.test_archive_written_but_commit_failed_is_reused -> Extractor, finish
- LayeredMemoryTests.test_cli_clear_confirmation_and_memory_commands -> fact, finish
- LayeredMemoryTests.test_extractor_isolated_tools_schema_and_invalid_output -> Model
- LayeredMemoryTests.test_fragmented_compaction_is_bounded_and_resumable -> Extractor, finish
- LayeredMemoryTests.test_main_loop_limit_and_failures_are_terminal_turns -> Loop, Model, Tool
- LayeredMemoryTests.test_keyboard_interrupt_persisted -> Model
- LayeredMemoryTests.test_pending_purge_blocks_new_turn_and_restart_retries -> finish
- LayeredMemoryTests.test_task_failure_is_visible_and_retried -> Extractor, finish, task
- LayeredMemoryTests.test_task_projection_fits_large_output_without_losing_source_ids -> finish
- LayeredMemoryTests.test_fake_file_completion_rejected_without_tool -> finish, task
- LayeredMemoryTests.test_expired_facts_and_disputed_status_in_context -> fact, finish
- LayeredMemoryTests.test_correction_can_resolve_conflict_by_restating_old_value -> fact, finish
- LayeredMemoryTests.test_plan_source_cannot_be_rephrased_as_current_fact -> fact, finish
- LayeredMemoryTests.test_gateway_uses_maintenance_timeout_json_mode_and_no_tools -> Response
- LayeredMemoryTests.test_completed_answer_survives_maintenance_storage_error -> Model
- LayeredMemoryTests.test_archive_orphans_are_diagnostic_only -> finish
- LayeredMemoryTests.test_nonterminal_task_states_and_scope_validation -> fact, finish, task
- LayeredMemoryTests.test_empty_proposal_for_explicit_waiting_task_is_not_silent_success -> Extractor, finish

## tests/test_memory.py
- MemoryTests.test_restart_restores_all_messages_with_current_system_prompt -> Model

## tests/test_schedule_tool.py
- ScheduleToolTests.test_agent_natural_language_tool_call_gets_receipt -> FakeModel

## tests/test_scheduler.py
- SchedulerTests.setUp -> FakeDispatcher
- SchedulerTests.test_second_worker_fails_to_start_while_first_owns_lock -> FakeDispatcher
- SchedulerTests.test_stop_joins_worker_and_releases_lock_for_next_worker -> FakeDispatcher
- SchedulerTests.test_stop_keeps_lock_while_manual_tick_executor_is_in_flight -> FakeDispatcher
- SchedulerTests.test_pause_cancel_during_send_reconciles_success_and_failure -> FakeDispatcher
- SchedulerTests.test_stop_overlapping_recovery_prevents_late_worker_start -> FakeDispatcher

## tests/test_shell.py
- ShellTests.test_agent_dispatches_command_arguments_and_returns_result_to_model -> Model

## tests/test_web_search.py
- WebSearchToolTests.test_maps_tavily_results -> _Response
