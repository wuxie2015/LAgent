## 1. Persistent timing core
- [x] 1.1 Implement timezone-aware once/daily/weekly rules, SQLite schedule management and execution records with deduplicated claims; verify restart/missed/DST/management behavior.

## 2. Notification delivery
- [x] 2.1 Implement console, SMTP and Webhook adapters using configuration-only targets, bounded network timeouts and safe errors; verify using local fakes.

## 3. Application integration
- [x] 3.1 Implement background scheduler, isolated agent execution, bounded notification retries and recovery with deterministic tests.
- [x] 3.2 Integrate schedule tool, settings, CLI lifecycle, prompt and README with regression tests.

## 4. Acceptance
- [x] 4.1 Review specification compliance and code quality, fix findings through Luna, run full tests, refresh structure cache, append development log, stage explicit paths and archive the change.
