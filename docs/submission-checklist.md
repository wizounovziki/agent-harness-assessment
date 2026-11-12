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
- [x] Clean installation from the source archive: 57 tests passed.
- [x] Actual Postman collection run: 27 requests and 46 assertions passed.

## Deferred verification

Live model smoke verification is deferred for this delivery at the user's request. The official SDK adapter and its offline HTTP contract tests are included.

## External publication

- [x] Submission repository selected: https://github.com/wizounovziki/agent-harness-assessment
- [x] Dataset submission method selected: include the small dataset under `data/` in the same repository.
- [ ] Publish the complete project to `main`.
- [ ] Verify repository and dataset access without authentication.

Publication state and public URLs are recorded in `publication.json` and `validation-results.json`.
