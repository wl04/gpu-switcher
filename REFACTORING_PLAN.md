# gpu-switcher — Refactoring plan

Refactoring plan for the existing codebase, aligned with the development
principles in [AGENTS.md](../AGENTS.md). **Behavior-preserving except where a
bug fix or error-semantics change is explicitly marked** (those follow TDD:
failing test first). Public contracts — D-Bus protocol, polkit action,
managed rule format, `.deb` layout, UI behavior — are **not** changed.

Status: ready for implementation on your confirmation. Nothing has been
implemented yet.

## Audit summary

| Area | Finding | AGENTS.md principle |
|---|---|---|
| `detect.py` / helper | `MANAGED_RULE`, `MUTTER_TAG`, `MUTTER_ENV`, `RULE_DIRS`, `_read` and the conflict scan are duplicated in both processes | DRY — one rule implemented once |
| `detect.py` | Pure parsing is fused with `subprocess`/filesystem I/O; only `primary_card_from_line` is unit-tested | tests pick scenarios by risk; external services replaced in unit tests |
| `detect.py` | `Gpu.tagged` / `Gpu.driver` are computed but never read; `tagged` costs N `udevadm` calls per launch | YAGNI |
| `detect.py` | `running_primary()` returns `None` for both "no journal line" and "journalctl failed" | distinguish absence of data from error |
| `main.py` | `_build_content` is one ~150-line method; when no GPUs are found `auto_radio` is never created, so **Reset/`_revert_selection` crash** (AttributeError) | methods split by responsibility; bug fix via reproducing test |
| `main.py`, `detect.py`, `bus.py` | module docstrings carry milestone history ("M1/M2/M3") | no history in code comments — keep current rationale |
| helper | IPC contract constants (`SERVICE_NAME`/`OBJECT_PATH`/`INTERFACE`) duplicated in `bus.py` without a check that both sides agree | duplicated contract must be verified, not assumed |
| helper | `_resolve_gpu` mixes a pure PCI-format validation with `/sys` reads | testable pure core + thin I/O wrapper |
| `po2mo.py`, `i18n.py` | zero tests, though both are pure Python | one behavior per test; risk-based scenarios |
| `Makefile` / project | no `test` target; no project-level check documentation | project checks defined by local instructions, recorded in the project's own rules |
| `build-deb.sh` / `debian/control` / README | version `0.1.0-22` hardcoded in three places | single source of truth |

## Phases

### Phase 1 — Project checks and rules (foundation, no code behavior change)

1. Add a `test` target to the `Makefile`:
   `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
2. Document the project checks in the **README's Development section**
   (the project's instructions; no separate `gpu-switcher/AGENTS.md` —
   sole maintainer, public repo): check commands (`make test`,
   `make locale`, `make package`), test conventions (stdlib `unittest`,
   `Operation_Behavior_Context` names), and the manual end-to-end
   verification steps (real GNOME session, polkit prompt, journal check).
   A dedicated project `AGENTS.md` can be added later if agent-assisted
   work grows.
3. Keep the test suite on stdlib `unittest` — no new dependencies (YAGNI).

**Verify:** `make test` green on the current 3 tests.

### Phase 2 — DRY: one shared udev-rule module

1. Create `src/udev_rules.py` (pure stdlib, **no `gi`**, no GUI import):
   - constants: `MANAGED_RULE`, `MUTTER_TAG`, `MUTTER_ENV`, `RULE_DIRS`;
   - `read_text(path) -> str` (the duplicated `_read`);
   - `rule_conflicts(unreadable_files) -> list[str]` — the one shared
     conflict scan, with the single real difference exposed as a narrow
     parameter: `"skip"` (app side: unreadable files are not a verdict) vs
     `"flag"` (helper: root must treat an unreadable rule as a conflict).
2. `detect.py` and `gpu_switcher_helper.py` drop their local copies and
   import the shared module. Import plumbing is explicit:
   - the app wrapper already puts `/usr/lib/gpu-switcher` on `sys.path`;
     in the source tree tests do the same;
   - the helper adds its parent directory (`/usr/lib/gpu-switcher` when
     installed) to `sys.path` and imports `udev_rules` — documented in its
     docstring.
3. Packaging: install `udev_rules.py` in **both** build paths
   (`tools/build-deb.sh` and `debian/rules`); both currently carry their own
   install list, so the new file must be added to each.
4. New unit tests (temp-dir rule trees):
   - `RuleConflicts_FindsRule_SettingMutterTag`
   - `RuleConflicts_FindsRule_SettingLegacyEnvVariable`
   - `RuleConflicts_IgnoresCommentLines`
   - `RuleConflicts_SkipsManagedRuleFile`
   - `RuleConflicts_LaterDirectoryShadowsEarlierSameBasename`
   - `RuleConflicts_SkipsUnreadableFile_InAppMode`
   - `RuleConflicts_FlagsUnreadableFile_InHelperMode`

**Verify:** `make test`; existing app/helper behavior unchanged (same
constants, same scan semantics).

### Phase 3 — `detect.py`: pure cores, thin I/O wrappers, fewer moving parts

1. Extract pure functions (no subprocess/fs in their signature):
   - `parse_lspci_names(output: str) -> dict` (out of `lspci_names`);
   - `last_primary_card(lines: list[str]) -> str | None` (the "latest
     matching line wins" loop, currently inlined in `running_primary`);
   - `managed_rule_pci_from_text(text: str) -> str | None` (out of
     `managed_rule_pci`);
   - `is_gnome_wayland(session_type: str, gnome_shell_present: bool) -> bool`.
   The existing thin wrappers (`lspci_names`, `running_primary`, ...) stay
   as the I/O boundary with documented docstrings.
2. **YAGNI (decided: remove):** drop the unused `Gpu.tagged` and
   `Gpu.driver` fields, the `card_tags()` wrapper and the per-card
   `udevadm info` call from `detect()`. Neither field is read anywhere;
   `tagged` also costs N `udevadm` subprocesses per launch and is
   semantically confusing (stale at session start). `driver` is trivially
   re-addable if a UI badge is wanted later.
3. **Error semantics (decided: distinguish):** a failed `journalctl` is
   currently indistinguishable from "no selection line". Change
   `running_primary()` to return `(card, error)` and add
   `Detection.running_card_error: str | None` so the UI can say "could not
   read the session journal" instead of the misleading "unknown (verify
   after restart)". Additive, non-breaking `Detection` change.
4. New unit tests, one behavior each:
   - `ParseLspciNames_ExtractsName_FromFullPciAddress`
   - `ParseLspciNames_ExtractsName_FromDomainOmittedAddress`
   - `ParseLspciNames_StripsRevisionAndIdSuffix`
   - `ParseLspciNames_SkipsUnparseableLine`
   - `LastPrimaryCard_ReturnsCard_FromLastMatchingLine`
   - `LastPrimaryCard_ReturnsNone_WhenNoLineMatches`
   - `ManagedRulePciFromText_ExtractsPci_FromRuleText`
   - `ManagedRulePciFromText_ReturnsNone_WhenRuleTextAbsent`
   - `IsGnomeWayland_ReturnsTrue_WhenWaylandSessionAndShellPresent`
   - `IsGnomeWayland_ReturnsFalse_WhenX11Session`
   - `IsGnomeWayland_ReturnsFalse_WhenShellMissing`
   - `RunningPrimary_ReturnsError_WhenJournalReadFails`
   - `RunningPrimary_ReturnsNoError_WhenJournalHasNoSelectionLine`
   - wrapper tests with `subprocess.run` replaced by a stub (return-code
     failure, missing binary) — absence vs error must not be masked.

**Verify:** `make test`; `detect()` output identical for the same machine
state (manual smoke on a real session before/after).

### Phase 4 — Helper: testable core, verified IPC contract

1. Import the shared `udev_rules` module (Phase 2); delete duplicated
   constants, `_read`, `_rule_conflicts`.
2. Split `_resolve_gpu` into a pure `is_valid_pci_format(pci) -> bool`
   plus the `/sys` resolution step; document the validation contract.
3. Add a contract test that both sides of the IPC agree (shared constants
   are unavoidable across processes, so they are **verified**, not
   deduplicated): `BusContract_MatchesHelperServiceNameAndPathAndInterface`
   comparing `gpu_switcher.bus` against `gpu_switcher_helper`.
4. New unit tests:
   - `RuleText_ProducesReferenceRuleLine_ForValidPci` (golden string vs
     the reference script format);
   - `ResolveGpu_ReturnsNone_ForMalformedPci`;
   - `ResolveGpu_ReturnsNone_WhenPciNotADisplayController` (stubbed `/sys`);
   - `Translator_ReturnsIdentity_WhenCatalogMissing`.
5. Message composition in `handle_set_primary` / `handle_set_auto` stays
   inline (localized, user-facing, few branches) — no extra layer (KISS).

**Verify:** `make test`; helper still serves `Status` / `SetPrimary` /
`SetAuto` identically (manual e2e on a real session, incl. polkit denial).

### Phase 5 — UI (`main.py`): smaller builders, one bug fix

1. Split `_build_content` into builders by responsibility: session warning,
   GPU rows, running-primary note, buttons, verification hint. Same class,
   same widget tree — no visual or behavioral change.
2. **Bug fix (TDD):** when no GPUs are detected, `auto_radio` is never
   created, so `Reset to auto` / `_revert_selection` raise AttributeError.
   - Extract the selection logic into pure, unit-testable functions:
     `initial_selection(detection) -> str | None` and
     `selection_after_failure(detection) -> str | None`.
   - Fix: always create the auto row (a single "System default (auto)"
     row with no GPU rows is harmless and makes Reset work).
   - Tests: `InitialSelection_ReturnsConfiguredCard_WhenGpuPreferred`,
     `InitialSelection_ReturnsAuto_WhenNoGpuMatches`,
     `SelectionAfterFailure_ReturnsConfiguredCard_WhenPreferredExists`,
     `SelectionAfterFailure_ReturnsAuto_WhenNoPreferredGpu`.
   - Manual e2e: run the app in a session with no `/sys/class/drm` cards
     (or simulate via a stub) and click Reset.
3. Replace milestone-history docstrings ("M1/M2/M3") with contract
   descriptions (what the module does, why the non-obvious parts exist).

**Verify:** `make test`; full manual UI pass (select → Apply → banner →
refresh; cancel path restores selection; Log out stays disabled until a
successful refresh).

### Phase 6 — Tests for `po2mo.py` and `i18n.py`

1. `po2mo.py` (pure Python; a broken `.mo` silently breaks i18n):
   - `ParseJoins_MultiLineMsgstr_IntoSingleString`
   - `ParsePo_UnescapesNewlinesAndQuotes`
   - `ParsePo_RaisesValueError_OnUnterminatedString`
   - `WriteMo_ProducesCatalogReadableByGettext` (round-trip through
     `gettext`).
2. `i18n.py`:
   - `RequestedLanguage_PrefersLcAllOverLcMessagesAndLang`
   - `RequestedLanguage_FallsBackToEnglish_WhenNoLocaleSet`
   - `Localedirs_ListsInstalledDirBeforeSourceTree`

**Verify:** `make test`.

### Phase 7 — Packaging and docs

1. `build-deb.sh`: derive `VERSION` from `debian/control` (`Version:` line)
   instead of hardcoding — one source of truth; `debian/control` keeps the
   version.
2. README: make install examples version-agnostic (or point to
   `debian/control`), and document `make test`.
3. `PLAN.md` / README: update file-layout and "Development" sections to
   reflect `udev_rules.py`, the `test` target and the decisions below
   after the refactor lands.
4. Both build paths (`tools/build-deb.sh`, `debian/rules`) install the new
   module — keep them in sync (Phase 2).

**Verify:** `make package` builds a `.deb`; `dpkg-deb --info`/`--contents`
sanity; install on a test system.

## Order of work

Per AGENTS.md work order, each phase is self-contained:

1. failing test for any behavior change (Phase 5 bug fix; Phase 3 error
   semantics) →
2. implementation →
3. `make test` + `make locale` + `make package` →
4. manual smoke on a real GNOME/Wayland machine before proceeding to the
   next phase.

## Non-goals

- No Rust rewrite, no KDE/X11 support, no CI infrastructure, no new
  runtime dependencies, no D-Bus/policy/protocol changes, no visual
  redesign, no optimization without a measured need.
