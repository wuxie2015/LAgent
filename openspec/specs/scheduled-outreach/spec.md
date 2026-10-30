# scheduled-outreach Specification

## Purpose
TBD - created by archiving change scheduled-user-outreach. Update Purpose after archive.
## Requirements
### Requirement: Durable schedules and management
The system SHALL persist one-time, daily and weekly reminder or agent schedules with timezone, configured channel, content and status, and provide create/list/update/pause/resume/cancel/history. It SHALL reject invalid or ambiguous time rules and prevent duplicate creates for the same request key.
#### Scenario: Restart and edit
- **WHEN** a user creates a schedule, restarts, and changes its time
- **THEN** the schedule remains available with the updated next execution time and an explicit timezone.
#### Scenario: Pause or cancel
- **WHEN** a task is paused or cancelled before execution or notification retry
- **THEN** it SHALL NOT execute or send that pending notification.

### Requirement: Safe background execution
The system SHALL start a stoppable scheduler with the CLI and SHALL prevent concurrent workers for one database. Reminder execution SHALL NOT call the model. Agent execution SHALL use isolated context and only the configured allowed tools.
#### Scenario: Due reminder
- **WHEN** a reminder becomes due while input is idle
- **THEN** it is claimed once and notification is attempted without waiting for another user message.
#### Scenario: Interrupted agent execution
- **WHEN** a worker restarts after an agent execution was interrupted
- **THEN** its execution is recorded as unknown and is not automatically replayed.

### Requirement: Missed schedules and notification reliability
The system SHALL persist generated results before delivery, retry only failed delivery with bounded backoff, and mark tasks beyond their grace interval missed without replaying a backlog.
#### Scenario: Delivery failure
- **WHEN** notification fails after an agent result has been saved
- **THEN** delivery may retry within the configured limit while the agent is not executed again.
#### Scenario: Long downtime
- **WHEN** a repeating task is overdue beyond its grace period on startup
- **THEN** a missed run is recorded and the next occurrence advances to the future.

### Requirement: Configured notification channels
The system SHALL support console, SMTP email and configurable Webhook delivery, using credentials only from environment configuration and reporting unavailable channels before schedule creation.
#### Scenario: Missing SMTP configuration
- **WHEN** a user selects email without the required SMTP settings
- **THEN** schedule creation fails clearly and does not report a reminder as arranged.
