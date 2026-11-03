# PROJECT_LEAD_SELFVERIFICATION_RESEARCH.md

**Phase-1 Deliverable** — Synthesized from three parallel research streams (t_01eb797e, t_90c16ddc, t_e77facc5)

---

## 1. Problem Statement

Lead models in Hermes-Agent (Jared, default kanban profiles, and any role that dispatches or completes work on the kanban board) currently make **unverified causal claims** that reach the user without any mechanical gate. Example: Jared asserted "the MISSING-key guard caused a timeout" — the guard only checked `os.environ` while auth loads from `/etc/hermes/.env`; the real failure was a network read-timeout. Two unconnected observations were fused into a cause/effect claim with zero measurement. [Source: t_90c16ddc §0, SKILL.md line 106]

**Directive:** Leads must self-verify and filter inaccuracies **before the user catches them**. No claim should leave a lead role without a source pointer (card id, file:line, log line, timestamp, or live probe).

---

## 2. Ranked Options Table

| # | Option | Enforcement Strength | Status | Evidence / Citation |
|---|--------|---------------------|--------|---------------------|
| 1 | `agent.execution_guidance: true` (profile-level) | Mechanical convention — byte-stable system prompt block injected at session start; verification-gated completion | **EXISTS-NOW** | t_90c16ddc §1.1; config docs: `agent.execution_guidance` |
| 2 | Kanban review flow (`kanban.review_dispatch: true` + `sdlc-review`) | Hard gate — `kanban_request_changes` routes back to implementer; same-card review does not count toward block-loop | **EXISTS-NOW** | t_01eb797e §(c); config.yaml line 125 (active); kanban-worker-lanes docs |
| 3 | Goal-mode judge + completion contract + `/goal gate` | Hard gate (quality gate: deterministic shell command must exit 0 before judge called); judge verdict advisory during loop | **EXISTS-NOW** | t_01eb797e §(d); goals docs |
| 4 | Tool-use hooks: `pre_tool_call` block + shell `fail_closed: true` | Hard gate — blocks tool execution at gateway | **EXISTS-NOW** | t_01eb797e §(a); hooks docs |
| 5 | `agent.tool_use_enforcement` (model-family-keyed) | Mechanical convention — injects "no end-of-turn promises" guidance at prompt assembly | **EXISTS-NOW** | t_90c16ddc §1.2; config docs |
| 6 | `reasoning_effort: high` (global) + per-model overrides | Advisory only — controls thinking depth, not verification | **EXISTS-NOW** | t_01eb797e §(f); config.yaml lines 122, 1048 |
| 7 | Bot Mode per-profile model pinning | Config-level hard pin (which model runs); not a verification gate | **EXISTS-NOW** | t_01eb797e §(g); bot-mode docs |
| 8 | Notification hooks (shell/gateway/outbound) flagging unverifiable claims | Advisory — user-written policy code required; `fail_closed` can be hard gate on control events | **EXISTS-NOW** | t_01eb797e §(h); hooks docs |
| 9 | Claim-level citation requirement before factual claims | **NEEDS-FEATURE** — no native gate exists; only advisory persona text in profile.yaml | **NEEDS-FEATURE** | t_01eb797e §(b); t_90c16ddc §2 |
| 10 | Self-consistency passes (reflection/critique/multi-pass) | **NEEDS-FEATURE** — no config key or documented mechanism | **NEEDS-FEATURE** | t_01eb797e §(e); t_90c16ddc §2 |
| 11 | Lead-role `AGENTS.md` with mandatory-read-before-stating rule | **NEEDS-FEATURE** — no researcher/lead AGENTS.md exists; convention only in jared profile | **NEEDS-FEATURE** | t_e77facc5 §2 |
| 12 | Self-report QA template (per-claim source + line + verbatim quote) | **NEEDS-FEATURE** — only board-local `✅ QA VERIFIED` comment convention exists | **NEEDS-FEATURE** | t_e77facc5 §2; jared/AGENTS.md line 42 |
| 13 | Adversarial self-review step before user-facing claims | **NEEDS-FEATURE** — no documented hook or mechanism | **NEEDS-FEATURE** | t_e77facc5 §2; t_90c16ddc §2 |
| 14 | Numbered ledger-id citations (grounded-citations skill) | **EXISTS-NOW** skill; mechanical rendering, model never types URLs | **EXISTS-NOW** | t_e77facc5 §2; grounding-rationale.md |
| 15 | Single source of truth for lead model pin (reconcile board vs FŐMODELLEK) | **EXISTS-NOW partial / CONFLICTING** — two different researcher pins | **RECOMMENDATION** | t_e77facc5 §1; btcusd-jev/AGENTS.md vs model-fit.md |

---

## 3. Detailed Sections Per Research Angle

### 3.1 Hermes-Agent Mechanics Inventory (t_01eb797e)

**Source:** `/home/endre/.hermes/kanban/attachments/t_01eb797e/inventory.md`  
**Docs consulted:** hooks, goals, kanban, kanban-worker-lanes, bot-mode, configuration pages; researcher profile config.yaml & profile.yaml

**Key findings (6 of 8 EXISTS-NOW):**

| Mechanism | Status | Enforcement |
|-----------|--------|-------------|
| Tool-use hooks (gateway/plugin/shell/outbound) | EXISTS-NOW | `pre_tool_call` block + shell `fail_closed: true` = hard gate |
| Evidence/citation requirement | NOT-FIND / NEEDS-FEATURE | — |
| Verification/QA gates (verify_on_stop, pre_verify, kanban review flow, write_approval) | EXISTS-NOW | Hard gate at completion/review; verify_on_stop = advisory nudge |
| Goal-mode judge + contracts + quality gates | EXISTS-NOW | Quality gates = hard gate; judge gate before goal-mode kanban_complete = hard gate |
| Self-consistency passes | NOT-FIND / NEEDS-FEATURE | — |
| reasoning_effort settings | EXISTS-NOW | Advisory only |
| Model-pinning per role (Bot Mode) | EXISTS-NOW | Config-level hard pin per profile |
| Notification hooks flagging unverifiable claims | EXISTS-NOW | Advisory; `fail_closed` can be hard gate |

**Strongest native enforcement points (no config changes required):**
- `pre_tool_call` block / shell `fail_closed` (tool execution)
- Kanban review flow + goal-mode judge gate before `kanban_complete` (task completion)
- `/goal gate` quality gates (deterministic shell command must pass)

**Config citations (researcher profile):**
- `agent.max_turns`: config.yaml:911
- `agent.pre_verify`: config.yaml:1031 (commented)
- `agent.reasoning_effort`: config.yaml:1048 (active)
- `agent.verify_on_stop`: config.yaml:1016 (commented)
- `kanban.review_dispatch`: config.yaml:125 (active)
- `model.default`: config.yaml:82
- `model.reasoning_effort`: config.yaml:122
- `reasoning_overrides`: config.yaml:1056 (commented)

---

### 3.2 Prompt/Skill-Layer Rules for Binding Self-Verification (t_90c16ddc)

**Source:** `/home/endre/.hermes/kanban/attachments/t_90c16ddc/findings.md`  
**Docs consulted:** prompt-assembly, skills system, personality/SOUL.md, context-files, configuration (execution-discipline-guidance, tool-use-enforcement, tool-loop-guardrails), persistent memory, architecture

**What EXISTS-NOW and can bind a rule:**

1. **Execution-Discipline Guidance (`agent.execution_guidance`)** — Closest documented binding mechanism. Injects a system-prompt block for model families with observed agentic failure modes (skipping read-back verification, claiming completeness despite count mismatches, declaring done without verifying acceptance criteria). Block chosen once at session start, keyed on model name → byte-stable for conversation life. Opt-in: `"auto"` (default list: gpt, codex, grok, deepseek, kimi, qwen, glm, minimax, mimo, mistral, muse), `true` (all models), or substring list. Independent of `tool_use_enforcement`. Gap: covers writes, counts, completion gates — not free-form prose claims about causes.

2. **Tool-Use Enforcement (`agent.tool_use_enforcement`)** — Injects "make tool calls immediately; keep working until complete; never end turn with a promise." Google operational guidance (Gemini/Gemma) adds verify-before-edit. Relevance: "never end a turn with a promise" is the documented analogue of "don't ship an unverified claim" — but at action level, not claim level.

3. **Prompt Assembly Tiers (stable → context → volatile)** — Skills live in **stable** tier (cached system prompt). SOUL.md = slot #1 identity. AGENTS.md = context tier (discovered from CWD, merged chain). MEMORY.md/USER.md = volatile (frozen snapshot at session start). Architecture principle "Prompt Stability": system prompt does not change mid-conversation except explicit `/model`.

4. **Skills Skeleton** — SKILL.md sections: When to Use / Procedure / Pitfalls / Verification. Skills load as "skills guidance" in system prompt (stable tier). Draft rule already in Jared's `btcusd-jev-project-manager` SKILL.md: Pitfalls "Saját állítások ön-ellenőrzése" line 106; Always-On Rules line 23.

5. **SOUL.md** — Identity layer, verbatim injection, durable per-HERMES_HOME. Explicitly *not* for task rules (those belong in AGENTS.md).

6. **AGENTS.md / Context Files** — Per-project instructions, discovered from CWD, injected every turn, threat-pattern scanned (blocked if matched; SOUL.md warned-only).

7. **Tool-Loop Guardrails** — Warning on repeated identical failing calls; hard stop on unattended gateway/cron.

**What NEEDS-FEATURE (no documented mechanism):**
- No claim-level citation gate
- No "re-read before answering" enforcement hook
- No testability hook for rule adherence (no evidence field / citation attestation)
- No model-agnostic enforcement (execution_guidance/tool_use_enforcement are model-family-keyed)
- Mid-session binding gap: changes to skills/context files take effect only at compaction or new session

**Recommended wording for rollout (from skill Pitfalls/Verification):**
1. Trigger: "Before any cause/effect or factual claim, name the source: card id, file:line, log line, timestamp, or live probe."
2. Hedging rule: "Until measured, say 'suspicion: X may cause Y' — never 'X caused Y'."
3. Re-read rule: "Re-read your own answer before sending; drop every claim without a source."
4. Failure example (anti-pattern): "The MISSING-key timeout claim was fabricated: the guard checked only `os.environ`, auth loaded from `/etc/hermes/.env`, the real failure was a network read-timeout — correlating the two without measurement invented a cause/effect."
5. Completion link: tie to verification-gated done — "done" requires every claim in the answer to carry a source pointer.

**Placement options for rollout:**
- **A (Skill-layer only):** Extend Pitfalls/Verification in Jared's SKILL.md — zero config change; binds only when skill loaded; subject to compaction delay.
- **B (Profile-level, RECOMMENDED):** `agent.execution_guidance: true` in Jared's config.yaml + claim-sourcing rule in skill Pitfalls/Verification (stable tier). Documented mechanism closest to "BIND across project-lead models."
- **C (Identity-level):** SOUL.md entry — binds across all profiles, but scoped to voice/identity per docs, not task rules.

---

### 3.3 Model-Selection & Mechanical Grounding (t_e77facc5)

**Source:** `/home/endre/.hermes/kanban/attachments/t_e77facc5/findings.md`  
**Sources read:** model-registry skill + references, providers-and-models.md, project-context-files.md, grounding-rationale.md, config.yaml, AGENTS.md files (default, btcusd-jev, jared profile), kanban task t_238c3d99

**Model-Selection — EXISTS-NOW:**
- Model-registry skill with task-fit evidence table (`model-fit.md`, stamped 2026-10-30, sourced from "user observations + own tests")
- Live model lists: Opencode GO (28 models, free: `beam-free`, `iceman-contributor-free`); NVIDIA NIM (85 models, all free with `NVIDIA_API_KEY`)
- Provider docs: auth per provider, key-resolution order (managed `.env` > profile `.env`), model aliases, fallback chain
- Live config: `model.default: mimo-v2.6-flash`, `provider: novita`, `reasoning_effort: high` (config.yaml lines 78, 111, 115); per-model `reasoning_overrides` dict (YAML only, no CLI)
- User model policy (model-fit.md §Politika): free model but best-fit per task; paid only with explicit permission; strict provider/key pairing; paid-guard script blocks non-`:free` OpenRouter; redundancy = same model on multiple providers
- Empirical reasoning-effort observations (capacity/format only): nemotron-3.5-lightning:free needs max_tokens 2048+; beam-free fails long QA; gpt-oss-20b returns content=null on short prompts
- Proven lead-adjacent model: `jev-1.13-free` via OpenCode SystemOne: 5/5 HTTP 200, $0 (t_238c3d99)
- Board role map (btcusd-jev/AGENTS.md, user decision 2026-11-01): researcher → `minimaxai/minimax-m3.1-flash` @ NIM; writer → `glm-5.3-flash` @ opencode-go; checker → `claude-haiku-5-5` @ opencode-go; structure generator → `nvidia/nemotron-3-ultra-550b-a55b` @ NIM (mandatory numeric QA)
- FŐMODELLEK decision (model-registry SKILL.md lines 45-46, user decision 2026-11-01): Fin = `zai-org/glm-5.3-flash` @ Novita; Jared = `xiaomimimo/mimo-v2.6-flash` @ Novita (line 45). Worker profile defaults: coder + researcher = `nvidia/nemotron-3.5-super-vl-120b-a12b` @ NIM; writer + verifier = `nvidia/nemotron-3-ultra-550b-a55b` @ NIM; debugger = `opencode-go/glm-5.3-flash` (line 46).

**Explicit Gap — NEEDS-FEATURE:**
- No registry entry, benchmark, or doc measures **long-context fact retention** or **epistemic discipline** for lead/project-lead roles. Any claim about "best for lead self-verification on long context" would be a guess — reported explicitly without guessing.
- ~~Pin conflict~~ **RESOLVED BY DESIGN** (verified 2026-11-01, t_c830ad77): the two layers are NOT in conflict — model-registry SKILL.md line 46 explicitly makes FŐMODELLEK the profile-level source of truth ("Ez felülírja a FYI model-role map profil-szintjét; a szerep-térkép kártya-szintű set-model override-ként tovább él"), and btcusd-jev/AGENTS.md carries the role map as card-level set-model overrides. Live read-back 2026-11-01: researcher/coder = `nvidia/nemotron-3.5-super-vl-120b-a12b` @ NIM, writer/verifier = `nvidia/nemotron-3-ultra-550b-a55b` @ NIM, debugger = `opencode-go/glm-5.3-flash` — all matching FŐMODELLEK. Neither layer references long-context retention or epistemic discipline (that gap is unchanged). The original draft also misattributed FŐMODELLEK to model-fit.md lines 45-46; actual location = model-registry SKILL.md lines 45-46.

**Mechanical Grounding — EXISTS-NOW:**
- AGENTS.md files board-wide (default, btcusd-jev, btcusd-ml, btcusd-server, us_tech100-steps, us_tech100-server) + profile-level only for jared. **No researcher/lead AGENTS.md exists.** Hermes loads AGENTS.md from CWD only; parent/subdirectory copies ignored.
- Closest self-report QA template: jared/AGENTS.md §Eljárások 1 — every Done requires `✅ QA VERIFIED — <kinek> — <dátum> — <1 mondat bizonyíték>` comment; rejection → `❌ QA PENDING` + `❌ QA:` prefix + NEW card. Convention lives in one board's AGENTS.md, not a Hermes mechanism.
- Grounded-citations skill: numbered ledger ids (register at retrieval, render mechanically; model never types URL); cite per supported sentence while writing (ALCE, arXiv:2305.14627); verbatim quotes for figures/dates/names (WebGPT, arXiv:2112.09332); Perplexity formatting conventions ([1] after terminal punctuation, max 3 ids/sentence).
- Verifier gate: default/AGENTS.md §Csapat/memória — "Worker-bizonyíték = SESSION, nem a kanban log; verifier gate = kötelező."

**Explicit Gap — NEEDS-FEATURE:**
- No mandatory-read AGENTS.md convention for lead roles
- No self-report QA template (QA VERIFIED comment is board-local, not enforced step)
- No adversarial self-review hook before user-facing claims

**Candidate Rollout List (for rollout task):**
1. Create lead-role/researcher AGENTS.md with mandatory-read-before-stating rule, per-claim self-report QA template (source + line + quote), adversarial self-review before any user-facing claim. (NEEDS-FEATURE)
2. Single source of truth for lead model pin: reconcile board role map (minimax-m3.1-flash) with FŐMODELLEK (nemotron-3.5-super-vl-120b); record decision with date + evidence. (EXISTS-NOW partial / CONFLICTING)
3. Keep `reasoning_effort: high` for lead profiles (already live globally); add per-model `reasoning_overrides` only where evidence supports it. (EXISTS-NOW mechanism)
4. Log every lead-role model choice (task type + result + date + source) in model-fit.md before rollout, per skill's logging rule. (POLICY)
5. Ground lead citations via numbered ledger ids (register at retrieval, render mechanically), per grounding-rationale.md. (EXISTS-NOW skill)

---

## 4. Concrete Rollout Recommendations for Lead-Role Profiles

*This section is the handoff input for the rollout task (t_c830ad77 / t_90fa9499). Each recommendation is an actionable, specific edit.*

### 4.1 Jared Profile (btcusd-jev-project-manager)

| # | Action | File | Edit |
|---|--------|------|------|
| 1 | Enable execution-discipline guidance for all models | `/home/endre/.hermes/profiles/jared/config.yaml` | Add under `agent:`: `execution_guidance: true` |
| 2 | Extend skill Pitfalls/Verification with claim-sourcing rule | `/home/endre/.hermes/profiles/jared/skills/productivity/btcusd-jev-project-manager/SKILL.md` | In `## Pitfalls` → add `### Verification` subsection with 5 rules from §3.2 (Trigger, Hedging, Re-read, Failure example, Completion link) |
| 3 | Create lead-role AGENTS.md | `/home/endre/.hermes/profiles/jared/AGENTS.md` (new file or extend) | Add mandatory-read-before-stating rule, per-claim self-report QA template (source file + line + verbatim quote), adversarial self-review step before any user-facing claim, numbered ledger-id citation practice |
| 4 | Reconcile model pin to single source of truth | `/home/endre/.hermes/kanban/boards/btcusd-jev/AGENTS.md` AND `/home/endre/.hermes/profiles/jared/config.yaml` | Update board role map researcher pin to match FŐMODELLEK (`nemotron-3.5-super-vl-120b-a12b` @ NIM) OR update FŐMODELLEK to match board; record decision with date + evidence in model-fit.md |
| 5 | Log lead-role model choice in registry | `/home/endre/.hermes/skills/model-registry/references/model-fit.md` | Append entry: task type "lead synthesis/source-critique", model chosen, result, date 2026-11-01, source "this deliverable + board decision" |

### 4.2 Default Kanban Profile (board-level)

| # | Action | File | Edit |
|---|--------|------|------|
| 1 | Create board-level lead/researcher AGENTS.md | `/home/endre/.hermes/kanban/boards/default/AGENTS.md` | Add mandatory-read rule, per-claim QA template, adversarial self-review, ledger-id citations |
| 2 | Ensure kanban.review_dispatch stays true | `/home/endre/.hermes/config.yaml` (or profile config) | Verify line 125 `kanban.review_dispatch: true` remains active |
| 3 | Adopt grounded-citations ledger for all lead claims | `/home/endre/.hermes/kanban/boards/default/AGENTS.md` | Reference grounding-rationale.md conventions; require numbered ledger ids for factual claims |

### 4.3 Other Kanban-Handling Profiles (btcusd-ml, btcusd-server, us_tech100-steps, us_tech100-server)

| # | Action | File | Edit |
|---|--------|------|------|
| 1 | Create profile-specific AGENTS.md with lead rules | Each board's AGENTS.md | Same mandatory-read + QA template + adversarial review + ledger-id pattern |
| 2 | Enable execution_guidance in each profile config | Each profile's config.yaml | Add `agent.execution_guidance: true` (or model-specific substring list) |
| 3 | Pin lead model per FŐMODELLEK decision | Each profile's config.yaml | `model.default: nvidia/nemotron-3.5-super-vl-120b-a12b` (researcher/coder) or `nvidia/nemotron-3-ultra-550b-a55b` (writer/verifier) with provider `nvidia` and base_url `https://integrate.api.nvidia.com/v1` |

---

## 5. Risks, Open Questions, and Feature Requests

### Risks
1. **Prompt Stability binding gap:** Changes to skills/AGENTS.md do not take effect mid-conversation — only at next compaction or new session. A rule added today binds only from the next session. [t_90c16ddc §2]
2. **Model-family substring dependency:** `execution_guidance`/`tool_use_enforcement` auto lists may not include the chosen lead model (e.g., `nemotron-3.5-super-vl` not in default list). Requires `true` or explicit substring opt-in per profile. [t_90c16ddc §2]
3. **False positives from goal-mode judge:** Judge is an LLM reading prose — may verdict "✓ Goal achieved" while work remains. [t_01eb797e §(d)]
4. **Citation gate absence:** No native mechanism blocks unsourced claims at prompt/tool layer. Any enforcement today is advisory persona text or post-hoc convention. [t_01eb797e §(b); t_90c16ddc §2]
5. **Split source of truth for model pins:** Board role map vs. FŐMODELLEK decision conflict creates ambiguity at rollout time. [t_e77facc5 §1]

### Open Questions
1. Should the claim-sourcing rule live in SOUL.md (identity, all profiles) or AGENTS.md (project-specific) or skill (role-specific)? Each has different persistence/truncation behavior.
2. What is the truncation cap for AGENTS.md injection? (context_file_max_chars: dynamic floor 20k / ceiling 500k; subdirectory hints capped at 32k) — will the rule survive truncation?
3. Does the verifier gate (default/AGENTS.md) currently execute automatically, or is it a human convention? If convention, how to mechanize?
4. Should the adversarial self-review be a plugin hook (`pre_llm_call`/`transform_llm_output`) or a goal-mode quality gate?
5. How to handle the mid-session binding gap for rules added during active work? `/reload-skills` refreshes slash commands but not system prompt content until compaction.

### Feature Requests (NEEDS-FEATURE items)
1. **Native claim-level citation gate** — Config key or hook that requires a source pointer before a factual claim can be delivered. (t_01eb797e §(b); t_90c16ddc §2)
2. **Self-consistency / reflection passes** — Configurable multi-pass critique or "reconsider" mechanism before final output. (t_01eb797e §(e); t_90c16ddc §2)
3. **Model-agnostic enforcement** — `execution_guidance`/`tool_use_enforcement` should support `true` for all models without substring matching. (t_90c16ddc §2)
4. **Mid-session rule refresh** — Mechanism to reload stable-tier prompt content (skills, AGENTS.md) without full compaction. (t_90c16ddc §2)
5. **Lead-role AGENTS.md convention** — Documented, discoverable AGENTS.md for researcher/lead roles with mandatory-read enforcement. (t_e77facc5 §2)
6. **Self-report QA template as enforced step** — Not just a comment convention; a structured attestation field on `kanban_complete`. (t_e77facc5 §2)
7. **Adversarial self-review hook** — Documented hook point (e.g., `pre_user_facing_claim`) that runs a critique pass before delivery. (t_e77facc5 §2; t_90c16ddc §2)
8. **Long-context fact-retention benchmark** — Registry task type for "lead self-verification on long context" with measurable evidence. (t_e77facc5 §1)

---

## Appendix: Source Traceability

| Research Task | Artifact | Key Sections |
|---------------|----------|--------------|
| t_01eb797e | `/home/endre/.hermes/kanban/attachments/t_01eb797e/inventory.md` | Full mechanics inventory (8 mechanisms) |
| t_90c16ddc | `/home/endre/.hermes/kanban/attachments/t_90c16ddc/findings.md` | Prompt/skill-layer binding analysis |
| t_e77facc5 | `/home/endre/.hermes/kanban/attachments/t_e77facc5/findings.md` | Model-selection + mechanical grounding |

**Document location:** `/home/endre/.hermes/hermes-agent/website/docs/PROJECT_LEAD_SELFVERIFICATION_RESEARCH.md`

**Generated by:** writer profile, task t_4eeea692  
**Date:** 2026-11-01 (Europe/Budapest, CET)

---

## 6. Rollout Status (Fin, t_c830ad77, 2026-11-01 — read-back verified)

Phase-2 rollout executed by Fin (default profile) on lead-role scope only. Constraint honored: no config changes outside lead-role profiles without user approval.

### 6.1 Changed files (read-back list)

| # | File | Change | Read-back evidence |
|---|------|--------|--------------------|
| 1 | `~/.hermes/config.yaml` | `agent.execution_guidance: true` (default/Fin lead profile) | `hermes config get agent.execution_guidance` → `true` (2026-11-01) |
| 2 | `~/.hermes/profiles/jared/config.yaml` | `agent.execution_guidance: true` (earlier rollout run) | line 10 `execution_guidance: true` ✓ |
| 3 | `~/.hermes/profiles/jared/AGENTS.md` | §Saját állítások ön-ellenőrzése (lines 59-69); wording fixes: Hungdózási→Hedging-szabály, sohasam→sohasem, carries→hordoz, idegesítő idzés→verbatim idézet, tathyások→ledger-id-k | grep + patch diffs ✓ |
| 4 | `~/.hermes/profiles/jared/skills/productivity/btcusd-jev-project-manager/SKILL.md` | §Verification (lines 112-120): 5-rule claim-sourcing block (Trigger/Hedging/Re-read/Failure example/Completion link) | lines present ✓ |
| 5 | `~/.hermes/kanban/boards/default/AGENTS.md` | §self-verification (lines 23-31); wording fixes: Hungdózás→Hedging, idzés→idézet, user-felőli→user-felé menő | grep + patch diffs ✓ |
| 6 | `~/.hermes/kanban/boards/btcusd-jev/AGENTS.md` | NEW §self-verification appended; stale line "profiles are GO/mimo by default" corrected to live FŐMODELLEK profile defaults + card-level-override note | patch diff ✓ |
| 7-10 | `~/.hermes/kanban/boards/{btcusd-ml, btcusd-server, us_tech100-steps, us_tech100-server}/AGENTS.md` | §self-verification present (earlier run); wording fixes: Hungdózás→Hedging, idzés→idézet | grep + patch diffs ✓ |
| 11 | `~/.hermes/skills/model-registry/references/model-fit.md` | appended "Lead-role self-verification rollout (Fin, t_c830ad77, 2026-11-01)" entry incl. live pin read-back + open Fin glm-vs-mimo question | patch diff ✓ |
| 12 | this document | citation fix (FŐMODELLEK = model-registry SKILL.md lines 45-46, NOT model-fit.md), pin-conflict corrected to resolved-by-design, §6 added | patch diffs ✓ |

### 6.2 §4 recommendations — status per action

| Sec | Action | Status |
|-----|--------|--------|
| 4.1#1 | jared `execution_guidance: true` | DONE (earlier run) |
| 4.1#2 | jared SKILL.md claim-sourcing rules | DONE (earlier run) |
| 4.1#3 | jared lead AGENTS.md | DONE (earlier run) |
| 4.1#4 | reconcile model pin | **RESOLVED — no change needed**: FŐMODELLEK (model-registry SKILL.md line 46) already defines profile-level defaults as source of truth with the board role map as card-level set-model layer; live profile pins verified matching (2026-11-01 read-back). See §3.3 correction. |
| 4.1#5 | log lead-role model choice in model-fit.md | DONE (this run) |
| 4.2#1 | default board AGENTS.md lead rules | DONE (earlier run) + wording fixes (this run) |
| 4.2#2 | `kanban.review_dispatch` stays true | VERIFIED: `~/.hermes/config.yaml` line 119 `review_dispatch: true` |
| 4.2#3 | grounded-citations ledger rule in default board AGENTS.md | DONE (earlier run) |
| 4.3#1 | other boards' AGENTS.md lead rules | DONE — all 6 boards now carry the section (btcusd-jev added this run) |
| 4.3#2 | `execution_guidance` in researcher/coder/writer/verifier/debugger configs | **PENDING USER APPROVAL** — outside lead-role scope per task constraint; surfaced as triage card |
| 4.3#3 | pin worker/lead models per FŐMODELLEK in profile configs | **ALREADY LIVE — no change needed** (this run's read-back: researcher/coder = `nvidia/nemotron-3.5-super-vl-120b-a12b` @ NIM, writer/verifier = `nvidia/nemotron-3-ultra-550b-a55b` @ NIM, debugger = `opencode-go/glm-5.3-flash`; triad matches FŐMODELLEK line 46) |
| — | default/Fin `execution_guidance: true` | DONE (this run) |

### 6.3 Open items (user decision required — not touched)

1. **Fin model discrepancy:** model-registry SKILL.md line 45 (FŐMODELLEK) records Fin = `zai-org/glm-5.3-flash` @ Novita, but live `~/.hermes/config.yaml` runs `xiaomimimo/mimo-v2.6-flash` @ novita. One of the two is stale; both directions are user decisions — left untouched and logged in model-fit.md.
2. **Worker-profile `execution_guidance`** (researcher/coder/writer/verifier/debugger): recommended by §4.3#2 but requires user approval per task constraint; triage card **t_ae700acd** created (also carries the Fin model-pin question).
3. NEEDS-FEATURE gaps (claim-level citation gate, self-consistency passes, adversarial self-review hook, mid-session rule refresh, lead-role AGENTS.md convention, structured QA attestation, long-context benchmark) remain feature-request material — see §5.