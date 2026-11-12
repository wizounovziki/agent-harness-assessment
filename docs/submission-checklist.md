# Submission Checklist

## Local deliverables

- [x] Runnable Python CLI and FastAPI service.
- [x] Three validated mock tools and deterministic model provider.
- [x] Real official SDK adapter with automatic SDK retries disabled.
- [x] Approval, restart recovery, idempotency, execution budgets and traces.
- [x] Automated success, failure, approval, API and limit tests.
- [x] English implementation report, architecture and workflow images.
- [x] README, dependency lock, environment template and Postman collection.
- [x] Final release archives and checksum verification.
- [x] Clean installation from the source archive: 61 tests passed.
- [x] Actual Postman collection run: 27 requests and 46 assertions passed.

## Live model verification

- [x] DeepSeek `deepseek-flash` tested through Chat Completions: 6/6 expected outcomes.
- [x] 16 actual model requests; 10,246 prompt and 1,331 completion tokens.
- [x] Zero creation attempts before approval; approval creates one mock incident; rejection creates none.
- [x] Persistent tool failures end in FAILED; one-request budget ends in LIMIT_EXCEEDED.
- [x] Model declaration, request settings, per-case results and sanitized traces published.
- [x] Project tree, module guide, API collection/OpenAPI contract and Mock Dataset overview added to the English report.
- [x] Provider credential supplied only through hidden input; exact credential scan covers files and ZIP entries.

Evidence: [live model results](./live-model-results.json), [API collection](./api-reference.md), [implementation report](./implementation-report.md), [credential scan](./credential-scan.json).

## External publication

- [x] Submission repository selected: https://github.com/wizounovziki/agent-harness-assessment
- [x] Dataset submission method selected: include the small dataset under `data/` in the same repository.
- [x] Publish the complete project to `main`.
- [x] Verify repository and dataset access without authentication: twelve raw files and the complete source ZIP downloaded successfully.
- [x] Install from the public source download and run all 61 tests successfully.

Publication state and public URLs are recorded in `publication.json` and `validation-results.json`.

Repository: https://github.com/wizounovziki/agent-harness-assessment

Dataset: https://github.com/wizounovziki/agent-harness-assessment/tree/main/data
