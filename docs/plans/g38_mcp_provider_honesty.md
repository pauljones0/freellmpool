# G38 plan v4.2 — implementation-review corrections

This checked-in plan supersedes v4.1 where they differ. Independent
implementation review found two assumptions that made v4.1's honesty promise
false:

1. The schema says unknown provider ids return an error, while v4.1 pinned a
   non-error models response when the registry was unreadable. Registry failure
   now resolves ids that are uniquely known from the configured pool or safely
   loaded extras; every other literal returns the explicit error `provider
   registry unavailable; cannot validate provider '<raw>'`. It never guesses
   that the literal is unknown. Ask and models share this verdict, and no chat,
   adapter, or transport call follows it. Both help strings say "unknown or
   unverifiable ids return an error."
2. `Provider` has no lowercase-id invariant. A configured provider's actual id
   spelling is the executable identity, so a unique case-insensitive pool match
   takes precedence over registry/extras canonical spelling. Configured ids
   differing only by case are ambiguous and return an error. This makes
   `Provider("GROQ", ...)` routable by `groq`, `GROQ`, or spaced/case variants.

Affected v4.1 pins are replaced: M5/H1 and empty-pool A5 expect the explicit
registry-unavailable error; A15 continues through the safely loaded `groq`
extra and lets the empty pool judge; M11 expects configured groq rows even with
a dead registry. New focused pins cover configured case spelling in models/ask,
case-collision refusal, dead-registry whitespace consistency, and zero
chat/transport calls. All other v4.1 precedence, alias, model-overwrite,
managed-snapshot, remaining schema text, and rendering contracts remain unchanged.

# G38 plan v4.1 — archived full pin-precision spec (superseded above)

v4: scope PASS; feasibility FAIL (S2-models site); completeness FAIL (5
one-line MUSTs). v4.1 applies ONLY these + both reviewers' minors (listed
at the end of §0). No design change; budget still 41.

v3: feasibility FAIL (F-BLOCK-1 universe/resolver, F-BLOCK-2 A5-text),
scope FAIL (MUST-1 F1 proof, MUST-6 budget), completeness FAIL (G14 text,
G24 silence + 12 partial). This v4 is COMPLETE (supersedes v1–v3; design
§1 + conventions §2 + pin table §3). All code cites re-verified this
round; slash-matrix markers probe-backed (/tmp/g38_probe2.py).

## 0. Deltas vs v3 (reviewer checklist)

- F1 DROPPED BY PROOF (scope MUST-1 conceded — the branch is vacuous, not
  unproven): `Route.name == f"{provider.id}/{model}"` (managed.py:128-130)
  and `snapshot()` rebuilds `pool.providers` grouped by `route.provider.id`
  (managed.py:407-414); `__init__` snapshots (:238) and pre-dispatch
  snapshots (mcp_server.py:572). Therefore snapshot-prefixes ⊆
  pool.providers for EVERY real pool incl. programmatic — the F1 universe
  term can never bind outside doubles that bypass `snapshot()`. The diet
  double is such a bypass (hand-built SimpleNamespace, test_mcp_diet.py:55).
- Diet churn (intent-preserving, replaces M15): test_mcp_diet.py:55 pool
  gains `providers=["p0","p1","p2","p3","p4"]` (str members; accepted by
  `_pool_known_ids`, managed_cli.py:121-123). The p1 filter (:61-64) then
  passes via the pool term; ALL assertions unchanged (20 rows). M15
  DROPPED with F1. S-b snapshot-prefix naming REMOVED (moot).
- G29-shape REVISION (v1–v3 2-tuple is AMBIGUOUS — proof: M2's live shape
  `"groq"→["groq"]` pass vs a dead-registry `"groq"` skip produce
  IDENTICAL `(list, None)` tuples but the models empty-text differs
  (unmatched vs legacy). No shipped code uses the plan-level 2-tuple
  (`_resolve_cli_filter` returns `(effective, rejected)` — untouched).
  v4 helper returns verdict pairs: `("pass", canonical) | ("skip",
  literals) | ("error", text)`. H1/H2 restated on the new shape.
- G24 CLOSED with +0 pins: 1-list-spy second asserts folded into M9 + A19
  (same folding precedent scope passed for N-TYPE).
- Budget 41 (42 − M15; M16 kept with justification line; G24 +0).
- A5/A15/G14: full verbatim incl `; see .env.example for the env vars`
  (router.py:1274-1277). S2: both descriptions FULL-quoted (v4.0 wrongly said
  NEW property — v4.1: REPLACE at the :445-450 injection, see one-liners).
- Slash markers: A8a/A8b/A9/A11 all `UnknownModel` (probe-backed).
- v4.1 one-liners: S2-models is a description REPLACE at the :445-450
  injection (live schema already has provider; the :412 literal is NOT
  the site). New S2-models: `Only list routes for this provider id (keeps
  the compact surface fully usable; unknown ids return an error).`
  (append-pattern matches S2-ask). A2/A2-2nd + M11 join the §2
  groq-in-pool row list. M10 = `make_pool(tmp_path, ids=("groq",))`;
  A12 = bare `make_pool(tmp_path)` (defaults suffice — validation is
  REG-based, snapshot succeeds). G24 spies: `wraps=` passthrough on
  `freellmpool.mcp_server.validate_mcp_provider`, `call_count == 1`, 2nd
  positional `== [raw]`. A15: `wraps=pool.chat` re-raise (handle_message
  converts to A5-full; assert recorded `providers == ["GROQ"]`). A7 via
  `_DEFAULT_ALIASES` (config.py:189-190); A9 cite qualified
  (config.py:206-209). A16 4th input concrete `{"a": 1}` (non-empty —
  `{}` is falsy→absent). A18 joins Chat Mock list. A13 "provider EXACT".
  L1 split marker restated. M10 rows quoted (`1 chat routes (groq: 1)` +
  `groq/free`). Policy vars named (`FREELLMPOOL_POLICY_*`). A11 rationale
  = chutes ∉ POOL ids. Helper TOTAL on the `list[str]` domain (call
  sites guarantee strs). H1 extras-raising short-circuits (registry
  first) — retained as simultaneous-failure never-raise proof. skip+spaces
  (dead + `" groq "`) filters RAW → legacy text: stated-unpinned
  (dead-only divergence).
- G32: reasoned disposition (A19 pins ask-side verbatim; spaces add no
  branch). TOCTOU word REMOVED, replaced by routing story. R2 drop
  explicit in §3; N6 grep pointer restored; `""` line cites restored.

## 1. Design

- Helper (new, managed_cli.py): `validate_mcp_provider(pool: Any,
  literals: list[str]) -> tuple[str, list[str] | str]`. TOTAL on the
  `list[str]` domain (never raises; both call sites guarantee strs).
  Decision tree (drop-in):
  `try: registry = load_registry(effective_env()) except Exception:
  return ("skip", list(literals))` — dead registry SKIPS (verify-A19
  precedent). Else `extra = _cli_extra_ids()` (TOTAL inside);
  `for pid in _pool_known_ids(pool): extra.setdefault(pid.lower(), pid)`;
  `canonical, unknown = resolve_provider_ids(literals, registry, extra)`;
  unknown → `("error", "unknown provider '" + ", ".join(unknown) + "'. Known
  registry ids: " + ", ".join(sorted(registry)))`; else `("pass",
  canonical)`. Universe U = REG ∪ EXTRA ∪ POOL, lookup strip+lower,
  precedence REG > EXTRA (user-catalog, then external slug+name, then
  plugin — first-wins setdefault) > POOL (provider_registry.py:40-43,
  managed_cli.py:85-105,153-155). Seams read as managed_cli module
  globals (patch-visible); mcp_server top-level from-imports the helper
  (no cycle: managed_cli never imports mcp_server) → G24 spies patch
  `freellmpool.mcp_server.validate_mcp_provider`.
- Models call-site shape (`_tool_models`, :606): `only = args.get(
  "provider")`; `if isinstance(only, str) and only.strip(): verdict,
  payload = validate(pool, [only]); if verdict == "error": return
  _text(payload, is_error=True); eff = payload[0] if verdict == "pass"
  else only; ids = [i for i in ids if i.split("/",1)[0] == eff]`;
  `if not ids: return _text(f"no chat routes for provider '{eff}'")
  if <pass> else _text("no providers configured")`. Non-string provider
  → guard fails → unfiltered (NO type gate on models — :611-613 asymmetry
  with ask, stated).
- Ask call-site shape (`_tool_ask`, :671): prompt-check unchanged → model
  resolve unchanged (`_resolve_model`; 123/absent-models → `(None,None)`
  :563-564, never raises) → model-overwrite wins when `p_filter` (raw
  provider IGNORED entirely — A2 incl. raw 123) → else provider shape:
  `None`/falsy-non-string (None/0/False/[]) → absent (`providers=None`);
  `isinstance str and not strip()` (`""`/`"   "`) → absent (A18 CHANGE:
  `"   "` no longer forwards — models-parity `:612` guard, G36-N8
  uniformity); truthy non-string → type error (N-TYPE gate); else
  `validate(pool, [provider])`: error → is_error; skip → `[raw]`;
  pass → canonical. Dead gate sits INSIDE validate (after TYPE — A16-2nd).
- Order chain (N-TYPE): prompt → resolve → model-overwrite → shape
  (absent/type) → dead-skip → universe. Pinned pairwise: overwrite>type
  (A2-2nd), type>dead (A16-2nd), prompt>all (S-A blanket).
- Routing story (R1-only; TOCTOU removed): validation runs inside the
  direct handler post-snapshot (`_call_tool` :571-576/:596 → handler);
  the router forwards the SAME args dict (`:644`) so the validated
  handler sees exactly what the client sent; one call, no re-validation,
  no interleaving. R1 asserts string-equality (single-dispatch fact).
  R2/R3 dropped (§3). Type/whitespace/slash rows through router NOT
  pinned (envelope doesn't transform args).
- Verbatim: lookup key strip+lower; echo RAW literal (helper builds
  error_text — helper responsibility). Justified: self-echo, no
  amplification (echo ≤ input), CLI parity. No sanitize/truncate.
- G28 rule: G38 pins precedence + its own text exactly; G28-owned text by
  marker: `UnknownModel` present + provider-text absent (neither `unknown
  provider` nor `no chat routes` substring). Never transcribe G28's
  sentence (quote-now declined: G28 owns/rewords it; quoting buys churn).
- Managed dead registry: NO PIN — "managed dead-registry dies at
  pre-dispatch snapshot() (-32603, pre-existing dispatch behavior, out of
  G38's tool-contract scope)". Stray keys: NO PIN — "tools ignore unknown
  args by design (forward-compat); a tools-wide unknown-arg convention is
  a separate goal" (non-goal closure grep: exactly `:611`/`:675` +
  schema — N6 pointer restored).
- M16 justification (scope MUST-6): absent×full is a distinct G38-owned
  branch — M3 pins absent×summary, M6 pins present×full; no existing pin
  covers absent×full. M15 dropped with F1 (was its proof vehicle).
- G32 disposition: NO ask-side spaced-echo pin — ask-side verbatim echo
  is pinned by A19 (whole-string RAW both tools); spaces add no branch
  (strip applies to the lookup key only, echo always RAW, shared helper).
- M14: user (M12) + external-mocked (M14b/c, ONE shared item slug=`extx`
  name=`Ext X`: slug-key + name-key) + plugin stated-identical-by-
  construction (identity `id.lower()→id`, same shape — no pin); managed
  extras never match routes — stated (G36-verify-philosophy).

## 2. Pin conventions (all pins)

- S-A: every A/R pin carries valid `prompt="hi"` (R1 inside args.args).
  S-B: `model` ABSENT unless the pin names a model input (A2,A6–A11).
- Harness: ALL M/A/R/S pins via `handle_message(pool, tools/call)`
  envelope (incl. pre-dispatch snapshot); H1/H2 helper-direct unit.
- S-C fixtures: default-legacy = `Pool(providers, quota=quota, env=env)`
  (kwargs — router.py:254-260) with conftest 4 doubles (alpha, beta, gee,
  free) + conftest env (ALPHA/BETA/GEE_KEY set) + quota store. DEFAULT
  FIXTURE RULE: every pin uses default-legacy unless it names another.
  Named: empty pool = `Pool([], quota=quota, env=env)`; groq-in-pool =
  default + `Provider("groq","Groq","openai","https://groq.test/v1",
  (Model("m1"),Model("m2"),), key_env="GROQ_KEY")` + `GROQ_KEY` in env
  (M4 header `2 chat routes (groq: 2)`; M6/M11/A2/A2-2nd/A3/A8b/A10 rows); custom =
  default + `Provider("CustomX","CustomX","openai",
  "https://customx.test/v1",(Model("cx1"),))` (M12); managed =
  `test_managed_runtime.make_pool` (M10 = `make_pool(tmp_path, ids=("groq",))`;
  A12 = bare `make_pool(tmp_path)`, defaults suffice); diet = completed double
  (§0); external item = `ExternalProvider(name="Ext X", slug="extx",
  category=None, url=None, base_url=None, description="", model_count=0,
  best_rpd=0, best_rpm=0, best_tpd=0, generous_score=0)` (catalog.py:75-87)
  via patched `freellmpool.catalog.load_external_catalog`; user catalog
  via patched `freellmpool.config.load_catalog` (both lazy-live);
  Provider ctor = models.py:26 (id/label/adapter/base_url/models +
  key_env/auth/key_optional/extra_env); alias seam = `FREELLMPOOL_ALIAS_XM`
  in the POOL env dict (never os.environ). Chat Mock (A2/A3/A4/A10/A12/
  A17/A18) returns canned `SimpleNamespace(text="t", provider_id="p",
  model="m", cached=False)` and captures `providers`. Live-registry pins
  (M1/M2/M7/M10/M14a/H2/L1) assume clean os.environ for `FREELLMPOOL_POLICY_*`
  (policy overlay can replace the registry set, policy_updates.py:244-257;
  16 shipped ids incl. groq+gemini — verified).
- G24 spies: `wraps=` passthrough on
  `freellmpool.mcp_server.validate_mcp_provider`; assert `call_count == 1`
  and 2nd positional `== [raw]`.
- A15 spy: `wraps=pool.chat` re-raise (handle_message converts the raise to
  A5-full; assert recorded `providers == ["GROQ"]`).
- N-DEAD: dead registry = patch `freellmpool.managed_cli.load_registry`
  → raise OSError. H1 additionally patches `config.load_catalog` +
  `catalog.load_external_catalog` + `plugins.registered_providers` →
  raise (simultaneous injection; all four targets verified to exist).
- S-D exact templates: unknown = `unknown provider '{RAW}'. Known
  registry ids: {IDS}`; unmatched = `no chat routes for provider
  '{CANON}'`; type = `'provider' must be a string`; A5-full =
  `NoProvidersConfigured: no provider has an API key set; see .env.example
  for the env vars`; M4 header = `f"{len(ids)} chat routes ({summary})"`
  (:623); S2-ask = `Optional free provider id to restrict to (e.g. groq;
  unknown ids return an error).`; S2-models = `Only list routes for
  this provider id (keeps the compact surface fully usable; unknown ids return an
  error).` (REPLACE at the :445-450 injection — the live site, not the :412 literal).
- `""` split contract (G36): call sites treat `""`/whitespace as ABSENT
  (M3/A4/A18; :612 models guard, :676 ask falsy path, G36-N8 uniformity);
  the HELPER treats `""` as UNKNOWN (H2 — resolve :45-46 empty-key rule).

## 3. Full pin table (41 = 15M + 21A + R1 + S2 + H1 + H2 + L1)

M1 NOSUCH → is_error unknown-template (RAW echo, live IDS). M2
groq-routeless → unmatched `groq` non-error + unfiltered-empty keeps old
text (packed: same non-error family). M3 ["","   "] → unfiltered. M4 GROQ
→ groq rows + first line == `2 chat routes (groq: 2)`. M5 dead+NOSUCH →
old text non-error. M6 full+groq → text == `groq/m1\ngroq/m2`. M7
full+groq-unmatched → unmatched wins. M8 [null,123,["x"],{"a":1}]
parametrized → unfiltered summary (no models type gate — stated). M9
"groq,NOSUCH" → unknown whole + G24-2nd (§2 spy: wraps=, count 1, 2nd ==
["groq,NOSUCH"]). M10 managed triple on make_pool(tmp_path,
ids=("groq",)): NOSUCH→error; GROQ→`1 chat routes (groq: 1)` + `groq/free`;
gemini→unmatched (live REG covers). M11 dead+GROQ (groq-in-pool, rows
present) → old text. M12 custom + "customx" → full text ==
`CustomX/cx1` (legacy route shape :610). M13 "  NOSUCH  " → echo
'  NOSUCH  ' is_error. M14 unmatched-canonical triple: (a) GROQ →
unmatched 'groq' (input case ≠ echo case); (b) "extx" → 'extx'
(slug-key); (c) "Ext X" → 'extx' (name-key; shared item §2). M16 ""+full
→ unfiltered full list (§1 justification). A1 NOSUCH → is_error +
uncalled (Mock assert_not_called). A2 model groq/m + NOSUCH → proceeds
["groq"] + 2nd: provider 123 ignored too (overwrite>type). A3 " groq " →
forwards ["groq"]. A4 "" → absent (None captured). A5 dead+NOSUCH empty →
A5-full EXACT + real-chat call proof (validation-error would differ).
A6 model=123 + NOSUCH → is_error + uncalled (resolve→(None,None)
:563-564, no model error precedes). A7 model auto + NOSUCH → is_error +
uncalled + 2nd: gpt-4o-mini same (builtin `_DEFAULT_ALIASES`
config.py:189-190, no env). A8a absent + NOSUCH/m → UnknownModel marker.
A8b groq + NOSUCH/m → UnknownModel marker. A8c NOSUCH + NOSUCH/m →
PROVIDER exact wins + uncalled. A9 GROQ/m no provider → UnknownModel
marker (case-sensitive split config.py:206-209). A10 alias XM→groq/m +
NOSUCH → proceeds ["groq"]. A11 chutes/m no provider → UnknownModel
marker (chutes ∉ POOL ids — split consults pool). A12 managed " groq "
(bare make_pool(tmp_path)) → forwards ["groq"] + managed NOSUCH →
is_error + uncalled. A13 NOSUCH+task=BOGUS → provider EXACT
(unknown-template) + uncalled. A14 NOSUCH + routing="bogus"+max_tokens=-5
→ provider error + uncalled (no clamp/default asserts; `_routing_arg`
benign :530-532). A15 dead+GROQ empty → A5-full + §2 wraps=-spy records
providers==["GROQ"] RAW (skip proof). A16 [123,true,["groq"],{"a":1}] →
type EXACT + is_error + 2nd: 123+dead → STILL type (type>dead). A17
[None,0,False,[]] → absent. A18 "   " → absent (CHANGE stated §1). A19
"groq,NOSUCH" → unknown whole + G24-2nd (§2 spy). R1 router ask+NOSUCH → string-EQUAL A1 text (same-fixture
diff). R2 DROPPED (scope: single-dispatch fact needs one proof). R3
DROPPED (envelope early-returns pre-date G38; no router code). S1 DROPPED
(disposition §1). S2 help ask+models provider descriptions FULL-EQUAL
§2 quotes (parsed JSON). H1 exotic (object() pool, all sources raising —
extras short-circuit behind registry-first, retained as simultaneous-failure
never-raise proof) → ("skip", ["NOSUCH"]), never raise. H2 [""] →
("error", "unknown provider ''. Known registry ids: ..."). D1 DROPPED
(disposition §1). L1 IDS == sorted(live load_registry()) parsed from M1
text (substring after `Known registry ids: `, split `, `).

## File scope / docs / gate (unchanged)

managed_cli helper + mcp_server 2 sites + 2 schema strings (:118 ask
literal + :449 models injection) + tests/test_mcp_provider_filter.py +
diet 1-line completion + GOALS.md. Zero other existing-test churn. Gate
mirrors g37; rc0 + 2-axis review + push. skip+spaces (dead + `" groq "`)
filters RAW → legacy text: stated-unpinned (dead-only divergence).
