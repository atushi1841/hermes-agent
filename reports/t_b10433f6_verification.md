# t_b10433f6 report

## 背景
nightly-critic (4baf143523e0) が 2026-09-30 に HTTP 400: credit insufficient balance=10941 required=23444 で FAILED。freellmapi プロバイダの課金枯渇を検知せず、フォールバック連動が RuntimeError で終了していた。

## 実装
`cron/scheduler.py::_resolve_job_runtime` のフォールバック連動条件に `FailoverReason.billing` / `FailoverReason.auth_permanent` を追加。`agent/error_classifier.py::classify_api_error` を参照して分類し、課金枯渇検出後はフォールバック链へ自動切り替え。

## verification_evidence

$ git -C /home/atushi/.hermes/hermes-agent log --oneline -1
c1825a9157 fix(cron): walk fallback chain on billing exhaustion (HTTP 400 credit insufficient)

$ cd /home/atushi/.hermes/hermes-agent && python3 -m pytest tests/cron/test_cron_pinned_job_fallback.py -q --tb=short 2>&1 | tail -3
22 passed in 24.65s

$ cd /home/atushi/.hermes/hermes-agent && python3 -m pytest tests/cron/test_scheduler.py -q --tb=short 2>&1 | tail -3
93 passed, 2 warnings in 166.37s

$ git -C /home/atushi/.hermes/hermes-agent diff c1825a9157 -- cron/scheduler.py tests/cron/test_cron_pinned_job_fallback.py | head -5
(empty: working tree clean, commit c1825a9157 is HEAD)

$ git -C /home/atushi/.hermes/hermes-agent status --short cron/scheduler.py tests/cron/test_cron_pinned_job_fallback.py
exit=1 (no changes: working tree matches commit c1825a9157)

## 判定 (t_b10433f6)
- 課金枯渇発生時: job FAILED ではなくフォールバック完了 (exit_code=0)
- テスト billing 分類追加: 0 failed (22+93 passed)
- 後方互換: 既存 auth / transient net パス不変
- リスク: 低 (条件追加のみ、既存 auth/transient net パス不変)