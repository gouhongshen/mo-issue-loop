# MatrixOne Challenger Runner

Runner infrastructure for independent black-box testing of MatrixOne issue fixes.

## Intended Execution Model

- The local issue loop manages assignments and the Challenger agent.
- Ephemeral GitHub Actions runners host the exact MatrixOne build under test.
- The Challenger uses private connectivity to execute SQL tests and collect evidence.
- Restricted lifecycle controls support node termination, restart, and diagnostics.
- Each test run records source identity and preserves its results before cleanup.

Keep model credentials, private issue-loop configuration, and production data out of
this repository and runner artifacts.
