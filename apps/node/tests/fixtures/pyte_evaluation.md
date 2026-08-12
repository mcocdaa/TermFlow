# pyte evaluation fixture (M2.4, plan §19 M2 + §22)

Bounded parser comparison for [`pyte`](https://github.com/selectel/pyte)
behind the future `TerminalObservationPort`, per the 0.2.0 Agent Broker plan:
"Run a bounded parser comparison fixture for `pyte` (and retain the existing
parser if license, split-sequence, or rendering semantics do not fit)".

The executable side of this fixture lives in
`../test_pyte_evaluation.py`; this file records the facts and the decision.

## 1. pyte facts (verified 2026-08-12)

| Fact | Value | Source |
|---|---|---|
| Current version | `0.8.2` | PyPI: https://pypi.org/project/pyte/ |
| Released | 2023-11-12 | PyPI release history |
| License | LGPL-3.0 ("OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)") | PyPI classifiers |
| Python support | `>=3.8` | PyPI "Requires" |
| GitHub releases | none (version tags only) | https://github.com/selectel/pyte/releases |
| Maintenance | no release in ~3 years before this evaluation | PyPI release history |

## 2. Environment result

```
$ UV_DEFAULT_INDEX=https://pypi.org/simple uv run --no-sync python -c "import pyte"
ModuleNotFoundError: No module named 'pyte'
```

pyte is **not importable** in the TermFlow project environment and is not a
project dependency. M2.4 must not modify `pyproject.toml`/`uv.lock`, so the
comparison tests in `test_pyte_evaluation.py` are recorded as **SKIP** here;
they run in any environment where pyte is importable.

The comparison tests were **executed and passed (5/5)** in a scratch venv
with `pyte==0.8.2` installed (no project files touched), so the executable
side of this fixture is verified green when pyte is present:

```
$ uv venv /tmp/pyte-venv && uv pip install --python /tmp/pyte-venv/bin/python pyte
$ /tmp/pyte-venv/bin/python -m pytest apps/node/tests/test_pyte_evaluation.py
→ 5 passed
```

## 3. Decision criteria (executable)

1. **License fit** — pyte is LGPL-3.0 (copyleft). Plan §22 requires pinned
   version/license/SBOM checks before adoption, and the reuse invariants
   (§22.1) keep provider types out of `packages/protocol`. The TermFlow repo
   ships no LICENSE file against which a compatibility comparison can be
   recorded, so this gate is **not cleared** without an explicit legal
   review.
2. **Split-sequence handling** — pyte's `Stream.feed` accepts chunked input;
   the fixture feeds split escape sequences across chunk boundaries and
   asserts parser state survives (SGR attributes, cursor motion, and a
   parameter/final-byte split). This runs whenever pyte is importable.
3. **Rendering semantics vs. the existing A-side approach** — the A side
   does **not** maintain a stateful screen parser: tmux renders captures
   (`capture-pane` without `-e`, so no escape sequences) and raw control-mode
   chunks are served for `since` reads from the bounded ring without
   re-rendering (`apps/node/src/termflow_node/tmux/capture.py`). pyte would
   therefore be an *added* renderer whose output must be proven equivalent to
   tmux's rendering (a real-pane comparison), not a replacement for a custom
   parser. pyte also provides none of the tmux stream cursor/gap/incarnation
   semantics the Observation Service needs (plan §22) — TermFlow keeps those
   regardless.

## 4. ADOPTION DECISION: retain the existing parser

**Decision: RETAIN the existing parser** (tmux control-mode stream + bounded
byte ring + tmux-rendered bounded captures). Do not adopt pyte in 0.2.0.

Evidence:

- pyte cannot be imported in the project environment, so the mandatory
  compatibility fixture cannot pass today (SKIP recorded above).
- License gate (LGPL-3.0 copyleft, no TermFlow license file, plan §22
  pinned-version/license checks) is not cleared.
- There is no existing custom screen-state parser for pyte to replace; the
  A-side rendering is delegated to tmux, so pyte would add a second,
  divergence-prone renderer instead of removing burden.
- pyte does not provide stream cursors/gap/incarnation semantics (§22.1);
  TermFlow owns those either way.

Re-open criteria: if a live screen-state renderer becomes a real requirement
(not served by tmux-rendered captures), re-evaluate pyte behind
`TerminalObservationPort` in an environment where this fixture **passes**
(version pinned, license reviewed, split-sequence test green, and a
real-pane rendering comparison against tmux green).

## 5. Evaluation run record

```
pytest apps/node/tests/test_pyte_evaluation.py
→ 5 skipped in the TermFlow environment (pyte is not importable; see
  test_pyte_evaluation.py reason)
→ 5 passed in the pyte-enabled scratch venv (pyte==0.8.2, 2026-08-12)
```
