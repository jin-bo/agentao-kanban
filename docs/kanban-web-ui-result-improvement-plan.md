# Kanban Web UI Result 改进计划

> Status: implemented in v0.1.8 — the card-detail Result / Changes /
> Transcripts sections, the result-state-aware empty hints, and the shared
> `kanban/result.py` summarizer all shipped. Retained as the design record.
>
> **Scope note (post-review):** the goal was narrow — promote the Web UI from
> "artifact browser" to "result entry point". Anything that changes the CLI
> JSON contract, the worktree layer, the card-detail dependency surface, or adds
> write actions was **out of scope here** and tracked in its own plan (see
> "Out of scope / follow-ups" at the end). P0 was read-only and self-contained.

## Current Implementation Audit (2026-05-12)

This design record has mostly moved from "planned" to "baseline":

- P0 Result API and card-detail Result section are implemented via
  `kanban/result.py`, `GET /api/cards/{card_id}/result`, and the Web detail
  sections.
- P1 Transcript and Diff surfaces are implemented via
  `GET /api/cards/{card_id}/traces`,
  `GET /api/cards/{card_id}/traces/{trace_id}/file`, and
  `GET /api/cards/{card_id}/diff`.
- P2 artifact polish is partially implemented: filename filtering, file-kind
  hints, inline text preview, copyable paths, newest-snapshot expansion, and
  refresh-stable expanded state exist.

Remaining gaps are now mostly product-shape gaps, not missing primitives:

- Card detail is still a long stacked document. The Result section already has
  some jump links, so this does not justify a full navigation component; only
  small in-context anchors are worth adding.
- Transcripts open as raw text in a new tab. There is no inline latest-transcript
  preview or role filter.
- The old out-of-scope items are still out of scope for this document unless
  they remain read-only. Operator writes and worktree mutations still need their
  own safety design.

## P3 Design: Focused Review Polish

P3 should be a small follow-up, not a broad "review workspace" rewrite. The
valuable work is to close the remaining workflow break in transcripts and finish
the artifact label polish. It should not introduce a new Web-only action schema,
and it should not add a dedicated detail navigation component.

### Product Goal

When a card has a result, the Web detail modal should make the existing read-only
surfaces easier to inspect:

1. Read the result summary and state.
2. Inspect the diff.
3. Read the latest transcript inline when the result is unclear.
4. Open or preview saved artifacts with readable snapshot labels.

The design remains read-only. Merge, prune, branch deletion, checkout,
requeue/block/move, and any daemon-racing write action stay outside this phase.

### P3a: Minimal In-Context Jump Links

Do not build a full section navigator, tab system, or sticky state/count control.
The current detail modal is a stacked document, and that structure is acceptable.
Only add small jump links where they remove obvious friction:

- Result should continue linking to Changes when the worktree state can produce
  a diff.
- Result should link to Artifacts when snapshots exist.
- Result should link to Transcripts when retained transcripts exist.
- Once the inline transcript viewer exists, the transcript link should land on
  the expanded latest transcript.

This is a JS/UI-only refinement. It should not add new API fields.

### P3b: Inline Transcript Viewer

Keep the existing raw transcript file endpoint, but add an inline viewer in the
Transcripts section:

- The newest transcript is expanded by default when present.
- Older transcripts are collapsed rows with role, timestamp, size, and open/copy
  controls.
- Add a role filter when there is more than one role.
- Inline content uses the existing file endpoint and the existing byte cap; 413
  renders a "too large for inline view" state with the on-disk path and raw-open
  link.
- The viewer is read-only text. No transcript editing, deletion, or redaction in
  this phase.

Security stays identical to P1: resolve files by exact `store.list_traces()`
match and serve through the same root/symlink/size validation path.

### P3c: Artifact Time Labels

Finish the remaining artifact polish by giving snapshot names a readable time
label while keeping raw names visible:

- Primary label: parsed timestamp, local time display.
- Secondary label: raw snapshot directory name.
- If parsing fails, fall back to the raw name without erroring.
- Keep `snapshot` as the stable API identifier; do not introduce mutable display
  names into file-serving routes.

### Explicit Non-Goals

- No full detail navigation component with section state/count badges.
- No tabs that mount/unmount sections.
- No Web-only `actions` field or action schema in `GET /api/cards/{card_id}/result`.
- No changes to `kanban result --json`, `next_steps`, or the shared summarizer
  contract.
- No new write routes.

### P3 Acceptance Criteria

- The latest transcript can be read inline; oversized transcripts return a clear
  inline state rather than breaking the modal.
- The transcript section supports role filtering when multiple roles are present.
- Result has minimal jump links to existing read-only sections where useful,
  without adding an API schema for actions.
- Existing `next_steps` and CLI JSON output remain unchanged.
- Artifact snapshots show human-readable timestamps while preserving the raw
  snapshot id.
- All P3 routes remain available without `--enable-writes`; no new write route is
  introduced.

### P3 Test Plan

- Transcript viewer expands the newest transcript, supports role filtering, and
  handles 413 responses.
- Result jump links render only when their target data exists or the target
  section is meaningful for the current worktree state.
- Artifact snapshot labels parse valid `artifacts-<utc-stamp>` names and fall
  back cleanly for malformed names.

## P4 Design: Dependency UX

P4 should make card dependencies visible and navigable in the Web UI without
adding write actions. This is the right follow-up after P3 because dependency
context affects everyday planning/review work, stays mostly read-only, and does
not introduce daemon or worktree mutation risk.

### Product Goal

When a card is blocked by or unblocks other cards, the Web UI should make that
relationship obvious:

- A reviewer can jump from a card to its dependencies.
- A reviewer can see which cards depend on the current card.
- The board view can distinguish ordinary blocked cards from dependency-blocked
  cards.
- Add Card can find dependency targets by title, not only by pasted card id.

### P4a: Card Detail Dependency Links

Enhance the card detail payload and modal:

- Keep `depends_on` as the existing list of card ids.
- Add a Web-only `dependents` field computed from `store.list_cards()`.
- Render `depends_on` as clickable chips using card title + short id.
- Render `dependents` as clickable chips in a separate "Unblocks" row.
- Unknown/stale dependency ids should render as short id chips with a stale
  marker, not break the modal.

Implementation notes:

- Compute `dependents` in the Web serialization layer, not in the markdown store.
- Avoid changing the card file format.
- Clicking a dependency chip should open that card's detail modal directly.

### P4b: Board Dependency Indicators

Add low-noise indicators on board cards:

- Show a blocked-by count when `depends_on` is non-empty and unresolved.
- Show an unblocks count when other cards depend on this card.
- Keep this as compact metadata, not a second dependency graph view.
- Use existing card status and dependency data; do not add a new scheduler rule
  in this phase.

### P4c: Add Card Dependency Search

Improve the Add Card modal's `depends_on` input:

- Support title-text search over existing cards.
- Let users add multiple dependencies as chips.
- Preserve manual card-id entry for copy/paste workflows.
- Prevent duplicate selected dependencies in the UI.

### P4 Acceptance Criteria

- Card detail shows both dependencies and dependents with clickable chips.
- Stale dependency ids are visible and do not cause 500s or JS render failures.
- Board cards expose compact dependency-blocked and unblocks indicators.
- Add Card can search by title and still accepts pasted ids.
- No card mutation endpoints beyond the existing add-card flow are introduced.

### P4 Test Plan

- Web card detail includes `dependents` for cards with reverse dependencies.
- Detail rendering handles known, unknown, and duplicate dependency ids.
- Board serialization exposes enough dependency metadata for the indicators.
- Add Card dependency selection serializes to the existing `depends_on` payload.

## Context

最新 CLI 更新把“卡片结果”收敛为一等概念：

- `kanban result <card-id>` 统一展示 status、summary、outputs、worktree
  state、artifacts、transcripts 和 next steps。
- `kanban show <card-id>` 在有结果时嵌入 `result:` 区块。
- `kanban worktree list` 的空态文案明确说明：没有 active worktree
  directory 不代表结果丢失；detach 后分支、artifacts、transcripts 仍可查。

Web UI 已经开始补齐这条路径：

- Add Card 模态支持 `depends_on`。
- 卡片详情支持浏览 artifact snapshots。
- 后端新增 artifacts listing/file endpoints。

但 Web 目前仍缺少与 CLI `kanban result` 等价的上层 Result 视图。用户在 Web
里能看到 artifacts，却不能先回答“这张卡的结果是什么、在哪里、下一步该做什么”。

## Product Goal

让 Web UI 成为 `kanban result <card-id>` 的图形化入口：

- 一眼看懂卡片是否产出结果。
- 一眼看懂 worktree 是 active、detached、missing、none 还是 not-git。
- 不需要知道结果分散在 card outputs、worktree branch、raw artifacts、
  retained transcripts 和 events 里。
- 所有高风险操作继续默认只读；写操作必须受 `--enable-writes` 控制。

## P0: Web Result API

新增：

```text
GET /api/cards/{card_id}/result
```

响应携带与 CLI `kanban result --json` 相同的字段语义。**Payload shape is fixed
for P0** — no flexible alternatives:

- `branch` is the full `kanban/<uuid>`.
- `artifacts` / `transcripts` stay **absolute-path string arrays** (this is what
  the CLI summarizer already returns: `TraceInfo.path` is `str(path)`, and
  `MarkdownBoardStore.raw_root` defaults to `board_dir.parent / "raw"`, with the
  board dir `resolve()`d — so don't *imply* a `workspace/raw/...` literal).
- For the UI, add **sibling maps** `artifact_display_paths` /
  `transcript_display_paths` keyed by the absolute path, value = path relative to
  the board / git-root. These are Web-API-only; the CLI JSON is untouched.
- `next_steps` reuses the CLI summarizer's command strings (see
  `_summarize_card_result` in `kanban/cli/rendering.py` — the
  `"kanban worktree diff <id>  # ..."` list). This plan **does not change the
  CLI JSON contract**; if the UI wants structured actions, derive a separate
  `actions` field in the Web API layer without touching the CLI output.

```json
{
  "card_id": "...",
  "title": "...",
  "status": "done",
  "blocked_reason": null,
  "summary": "...",
  "outputs": ["..."],
  "worktree": {
    "branch": "kanban/<uuid>",
    "base_commit": "...",
    "state": "detached",
    "path": null
  },
  "artifacts": ["/abs/.../workspace/raw/<uuid>/artifacts-..."],
  "transcripts": ["/abs/.../workspace/raw/<uuid>/<role>-<ts>.md"],
  "artifact_display_paths": {"/abs/.../workspace/raw/<uuid>/artifacts-...": "workspace/raw/<uuid>/artifacts-..."},
  "transcript_display_paths": {"/abs/.../workspace/raw/<uuid>/<role>-<ts>.md": "workspace/raw/<uuid>/<role>-<ts>.md"},
  "next_steps": ["kanban worktree diff <id>  # review changes on the preserved branch", "..."]
}
```

Implementation notes:

- **Extract a lightweight shared summarizer first**, but keep the CLI output
  byte-for-byte the same. `_summarize_card_result` / `_worktree_state` /
  `_list_artifact_dirs` currently live in `kanban/cli/rendering.py` and take an
  `argparse.Namespace` (they read `args.board`). Refactor them to take a
  `board_dir: Path` (or a tiny context object) and move them into a new
  `kanban/result.py`; `kanban/cli/rendering.py` imports from there and is
  expected to produce identical `kanban result` / `kanban show` output (add a
  regression test if one doesn't already cover it).
- **Artifacts-root resolution rule for P0** — there are two roots in the codebase
  today: the CLI/`_worktree_state` path uses `git_root / "workspace" / "raw"`,
  while `MarkdownBoardStore.raw_root` (used by the store's `list_traces`) and
  `web.py:_artifacts_root_for` use `board_dir.parent / "raw"`. To avoid both a
  CLI regression *and* a Result-vs-Artifacts data-source mismatch:
  - The **CLI** keeps resolving artifacts via `git_root / "workspace" / "raw"` —
    no behavior change for non-standard layouts.
  - The **Web Result endpoint** resolves artifacts the same way the existing Web
    artifacts endpoint and the store do (`board_dir.parent / "raw"`), so the
    Result and Artifacts panels never disagree.
  - Add a test for a non-standard board layout (board dir not at
    `<git_root>/workspace/board`) asserting CLI and Web each stay on their
    respective root and neither 500s.
  - (Reconciling the two roots project-wide is a separate cleanup, out of scope.)
- Keep the endpoint read-only and available without `--enable-writes`.
- Preserve the same state vocabulary as CLI: `none`, `not-git`, `active`,
  `detached`, `missing`.
- Add tests in `tests/test_web.py` (and extend `tests/test_cli_result.py`) that
  assert the Web result payload carries the same field values as `kanban result
  --json` for the same card — parametrize over fresh, detached, missing, and
  transcript/artifact-bearing cards.

## P0: Card Detail Result Section

Move card detail toward this order:

1. Result
2. Artifacts
3. Recent events
4. Metadata
5. Goal / acceptance criteria / blocked reason as needed

The Result section should show:

- status and blocked reason
- worker summary
- worktree state badge
- branch name and worktree path when present
- outputs count/list
- artifacts count with jump link to Artifacts section
- transcripts count with jump link to Transcripts section once implemented
- next steps rendered as copyable command lines (the strings the summarizer
  already returns). Read-only ones (`kanban worktree diff`, `kanban traces`) may
  later become in-app buttons once those Web routes exist (P1); `git merge` /
  `kanban worktree prune` stay copy-only — never one-click — because their blast
  radius is larger than the read-only surface this plan ships.

Worktree state copy:

| State | Web copy |
|---|---|
| `active` | Worktree directory is still active; review in-progress changes. |
| `detached` | Directory released; result branch is preserved. |
| `missing` | Recorded branch no longer resolves; stale metadata likely needs pruning. |
| `none` | No worktree was attached to this card. |
| `not-git` | Board is not inside a Git repository; worktree isolation is unavailable. |

## P0: Replace Artifact-First Mental Model

The existing Artifacts panel should stay, but it should be nested under the
broader Result story:

- If no artifacts exist, explain why based on result state.
- If artifacts exist, show newest snapshot expanded by default.
- Use human-friendly timestamp labels where possible, while keeping raw snapshot
  names visible in secondary text.
- Make the empty state clear that artifacts only cover gitignored deliverables
  saved during detach, not all code changes.

## P1: Transcript Browser

Add:

```text
GET /api/cards/{card_id}/traces
GET /api/cards/{card_id}/traces/{trace_id}/file
```

Note: the API path segment is `traces` (matching the CLI `kanban traces`); the
UI labels this surface "Transcripts". Keep that mapping consistent — don't
introduce a third term.

The listing endpoint should reuse `store.list_traces(card_id)`, which returns
`TraceInfo(card_id, role, at, path, size)` — but note `list_traces` only sorts
by **filename glob**, and only the `latest=True` path picks `max(.. , key=at)`.
So the endpoint must **explicitly sort the result by `TraceInfo.at` descending**
before returning it; don't rely on glob order to define "latest transcript".
Return absolute `path` plus a board-relative `display_path` (same convention as
the Result API), and the per-file `size`.

UI behavior:

- Show latest transcript in card detail.
- Support opening raw transcript in a new tab or inline read-only viewer.
- Surface transcript count in Result.
- Once this route exists, the `kanban traces <id> --latest` next-step line in the
  Result section can become an in-app link instead of a copyable command.

Security notes:

- Mirror the artifacts file-serving validation in `web.py` (reject `..`,
  absolute paths, leading slash, symlink leaves, intermediate symlinks that
  escape the directory).
- `trace_id` is a filename, not a directory — validate it against a
  `TRACE_FILE_NAME_RE` (`<role>-<utc-stamp>.md`) **or**, simpler and safer,
  resolve it by exact match against `store.list_traces()` output rather than
  trusting the segment.
- Do not serve arbitrary files from `workspace/raw`.
- Add a byte cap for inline display, same shape as `_ARTIFACT_FILE_MAX_BYTES`
  (return 413 with the on-disk path when exceeded).

## P1: Read-Only Diff View

This is **independent of P0** — the Result section ships without it. It also
touches the worktree layer, not just a Web route, so split it into two tasks:

**P1a — worktree-layer prep (no Web change):** add a `timeout` to the `git`
subprocess calls in `WorktreeManager.diff_summary` (`kanban/worktree/__init__.py`)
and cover the timeout + existing error paths with tests. `diff_summary` shells
out to `git diff --stat`, `git diff HEAD`, and `git ls-files`; a hung repo would
otherwise tie up a FastAPI threadpool worker indefinitely.

**P1b — Web route:**

```text
GET /api/cards/{card_id}/diff
```

Web equivalent of `kanban worktree diff <card-id>`.

UI behavior:

- Add a Changes tab/section in card detail.
- Support both active worktree branches and detached preserved branches.
- Show clear errors for `none`, `not-git`, and `missing`.
- Keep it read-only; no merge, checkout, branch delete, or prune operation in
  this phase.

Implementation notes:

- `WorktreeManager.diff_summary` only raises `WorktreeDiffError` (missing
  `base_commit`, ref not found, `git diff` failure); it does **not** distinguish
  `none` / `not-git` / `missing`. The handler must first compute the worktree
  state via the shared summarizer and return the matching error before calling
  `diff_summary` — don't rely on the exception text for classification.
- Add an inline byte cap on the diff body (same shape as the artifacts/transcript
  cap): a large change set produces a large `git diff --stat` + untracked listing.

Testing:

- Active branch with diff.
- Detached branch with diff.
- Missing branch returns actionable error.
- Non-git board returns stable no-diff state, not 500.
- Card with no worktree (`none`) returns a clean empty state, not an error.

## P2: Artifact Browser Polish

Improve the existing artifacts surface:

- Add filename/path filter.
- Add file type hints for text, image, JSON, logs, and binary files.
- Inline preview small text files.
- Show a clear "too large for inline view" state for files over the response cap.
- Add copyable local path for snapshot and file.
- Preserve expanded snapshot state across detail refreshes, which the current UI
  already starts to do.

## API Design Principles

- Web result semantics should follow CLI result semantics (same field values for
  the same card).
- Result aggregation should be shared code where practical (target: a
  lightweight `kanban/result.py`); the refactor must not change CLI output.
- Read endpoints must not require `--enable-writes`. This plan adds no write
  endpoints.
- File-serving endpoints must validate path segments and reject traversal,
  absolute paths, symlinks, and oversized inline payloads.
- File-path arrays in API responses are absolute strings (matching the CLI
  summarizer); the board/git-root-relative variants ride alongside as sibling
  maps (`artifact_display_paths` / `transcript_display_paths`), Web-API-only.
  Don't imply a fixed `workspace/raw/...` shape — the raw root is
  `board_dir.parent / "raw"` by default and the board dir is `resolve()`d.
- Any endpoint that shells out to `git` (diff) must pass a subprocess timeout —
  web handlers run on the FastAPI threadpool and a hung repo would exhaust it.
- Empty states are product behavior, not afterthoughts. They should explain the
  lifecycle: active directory, detached branch, saved artifacts, retained
  transcript.

## Suggested Implementation Order

P0 (one PR, read-only, self-contained):

1. Extract a lightweight `kanban/result.py`: move `_summarize_card_result` /
   `_worktree_state` / `_list_artifact_dirs` off `argparse.Namespace` onto
   `board_dir: Path`. `kanban/cli/rendering.py` imports from it and produces
   **identical** CLI output (add a regression test). Do **not** change the CLI
   JSON shape, and do **not** change the CLI's artifacts root — see the
   "Artifacts-root resolution rule for P0" above (CLI stays on
   `git_root/workspace/raw`; the Web endpoint uses the store's
   `board_dir.parent/raw`).
2. Add `GET /api/cards/{card_id}/result` (read-only, no `--enable-writes`).
3. Add the Result section to card detail (the new top section).
4. Reframe Artifacts under Result and improve empty states.

P1 (separate PRs, each independent of P0):

5. Transcript browser: `GET /api/cards/{card_id}/traces` +
   `/traces/{trace_id}/file`, sorted by `TraceInfo.at` desc, with path
   validation and a byte cap.
6. P1a: add a `timeout` to `WorktreeManager.diff_summary` git calls + tests.
   P1b: `GET /api/cards/{card_id}/diff` + Changes section in card detail.

P2 (later, polish):

7. Artifact browser filtering / preview / copyable paths.

Anything else (CLI JSON restructure, write/operator actions, and the safety
design those need) is **out of scope** — see below.

## Out of scope / follow-ups

These were considered and deliberately split out so the Result work stays small
and read-only:

- **Structured `next_steps` + CLI JSON restructure** — making the summarizer
  return `{kind, ...}` records and reshaping `kanban result --json` is a
  pre-1.0 CLI break. Defer until the UI has a real need that cannot be handled
  with local JS links and copyable command strings.

Operator write safety, operator write actions, and Web worktree mutations moved
to `docs/kanban-web-write-actions-safety-plan.md`.

## Acceptance Criteria (P0)

- A user can open a card in the Web UI and answer:
  - Did this card produce a result?
  - Where are the code changes? (branch, worktree path if active)
  - Is the worktree `active` / `detached` / `missing` / `none` / `not-git`?
  - Are there saved artifacts?
  - Is there a transcript?
  - What is the next review/debug command (shown as a copyable line)?
- The Web result payload carries the same field values as `kanban result --json`
  for the same card — after normalizing the Web-only additions (the
  `*_display_paths` maps) and on the standard board layout where both sides
  resolve the same raw root. Verified by a test parametrized over fresh /
  detached / missing / artifact- and transcript-bearing cards.
- A non-standard board layout (board dir not at `<git_root>/workspace/board`):
  CLI and Web each stay on their own artifacts root and neither 500s.
- `kanban result` / `kanban show` output is unchanged by the `kanban/result.py`
  extraction (regression test).
- No Web route added by this plan requires `--enable-writes`.
- Artifact file serving keeps its path-traversal and size-limit tests; the new
  Result endpoint surfaces absolute-path arrays (`artifacts` / `transcripts`)
  plus the relative `artifact_display_paths` / `transcript_display_paths` maps —
  not object arrays, and never an implied `workspace/raw/...` literal.
- Detached worktree state is presented as a preserved result, not as missing work.
