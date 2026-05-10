# Kanban Web UI Result 改进计划

> Status: proposed for `0.1.8-dev`. This plan follows the latest CLI
> result/worktree/artifacts updates and defines how the Web UI should expose
> the same operator model.

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

响应应与 CLI `kanban result --json` 对齐：

```json
{
  "card_id": "...",
  "title": "...",
  "status": "done",
  "blocked_reason": null,
  "summary": "...",
  "outputs": ["..."],
  "worktree": {
    "branch": "kanban/<card-id>",
    "base_commit": "...",
    "state": "detached",
    "path": null
  },
  "artifacts": ["workspace/raw/<card-id>/artifacts-..."],
  "transcripts": ["workspace/raw/<card-id>/worker-...md"],
  "next_steps": ["kanban worktree diff <card-id>", "..."]
}
```

Implementation notes:

- Prefer extracting the shared summarizer from `kanban/cli.py` into a small
  reusable module instead of duplicating logic in `kanban/web.py`.
- Keep the endpoint read-only and available without `--enable-writes`.
- Preserve the same state vocabulary as CLI: `none`, `not-git`, `active`,
  `detached`, `missing`.
- Add tests that compare the Web result payload against CLI result semantics for
  fresh, detached, missing, and transcript/artifact-bearing cards.

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
- next-step actions rendered as readable commands or buttons

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

UI behavior:

- Show latest transcript in card detail.
- Support opening raw transcript in a new tab or inline read-only viewer.
- Surface transcript count in Result.
- Convert CLI next step `kanban traces <card-id> --latest` into a Web action
  when traces exist.

Security notes:

- Use strict path validation, similar to artifacts file serving.
- Do not serve arbitrary files from `workspace/raw`.
- Add size cap for inline display.

## P1: Read-Only Diff View

Add:

```text
GET /api/cards/{card_id}/diff
```

Goal: Web equivalent of:

```bash
kanban worktree diff <card-id>
```

UI behavior:

- Add a Changes tab/section in card detail.
- Support both active worktree branches and detached preserved branches.
- Show clear errors for `none`, `not-git`, and `missing`.
- Keep it read-only; no merge, checkout, branch delete, or prune operation in
  this phase.

Testing:

- Active branch with diff.
- Detached branch with diff.
- Missing branch returns actionable error.
- Non-git board returns stable no-diff state, not 500.

## P1: Dependency UX

The Add Card modal now supports `depends_on`, but read-side dependency context is
still thin.

Improvements:

- Render `depends_on` as clickable chips in card detail, not raw UUIDs.
- Show reverse dependencies: cards this card unblocks.
- On board cards, show compact blocked-by/unblocks indicators.
- In Add Card, allow searching by title text, not only full UUID or unique id
  prefix.
- Keep DONE cards hidden from default dependency suggestions, but allow explicit
  full-id paste.

## P2: Artifact Browser Polish

Improve the existing artifacts surface:

- Add filename/path filter.
- Add file type hints for text, image, JSON, logs, and binary files.
- Inline preview small text files.
- Show a clear "too large for inline view" state for files over the response cap.
- Add copyable local path for snapshot and file.
- Preserve expanded snapshot state across detail refreshes, which the current UI
  already starts to do.

## P2: Operator Actions

Only after the read-only result surfaces are stable, consider small write
actions under `--enable-writes`:

- move
- requeue
- block
- unblock

Do not add merge, prune, branch deletion, checkout, or filesystem cleanup to the
first write expansion. Those actions have a larger blast radius and should remain
CLI-only until there is an explicit safety design.

## API Design Principles

- Web result semantics should follow CLI result semantics.
- Result aggregation should be shared code where practical.
- Read endpoints must not require `--enable-writes`.
- File-serving endpoints must validate path segments and reject traversal,
  absolute paths, symlinks, and oversized inline payloads.
- Empty states are product behavior, not afterthoughts. They should explain the
  lifecycle: active directory, detached branch, saved artifacts, retained
  transcript.

## Suggested Implementation Order

1. Extract shared result summarizer.
2. Add `GET /api/cards/{card_id}/result`.
3. Add Result section to card detail.
4. Reframe Artifacts under Result and improve empty states.
5. Add transcript listing/file serving.
6. Add read-only diff endpoint and UI.
7. Improve dependency read-side UX.
8. Polish artifacts filtering/preview/copy affordances.
9. Evaluate limited write actions under `--enable-writes`.

## Acceptance Criteria

- A user can open a card in Web UI and answer:
  - Did this card produce a result?
  - Where are the code changes?
  - Is the worktree active, detached, missing, absent, or unavailable?
  - Are there saved artifacts?
  - Is there a transcript?
  - What is the next review/debug command or Web action?
- The Web result payload matches CLI `kanban result --json` semantics.
- No read-only Web route requires `--enable-writes`.
- Artifact and transcript file serving have path traversal and size-limit tests.
- Detached worktree state is presented as preserved result, not as missing work.
