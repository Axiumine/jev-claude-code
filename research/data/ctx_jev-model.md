**Brief: TypeSafe Jev via OpenRouter, for use in Claude Code (checked 2026-09-30)**

Bottom line: Jev is not a coding LLM. TypeSafe says "There is no `model: jev-latest` setting that turns your coding agent into a Jev-powered agent" (docs.typesafe.ai/introduction/coding-agents). It fits as a hook-side gate, such as permission auto-approval.

**1. API**
- Decisions API: `POST https://openrouter.ai/api/alpha/decisions`, header `Authorization: Bearer $OPENROUTER_API_KEY`.
  - Request: `{"model":"typesafe/jev-1.13","state":{...}|"text"|[...],"questions":{"is_bug":{"type":"noul","instructions":"...","criteria":{"true":"...","false":"..."}},"team":{"type":"choice","instructions":"...","criteria":{"payments":"...","frontend":"..."}},"urgency":{"type":"score","instructions":"...","criteria":["low","mid","high"]}}}`.
  - Optional fields: `provider` (routing preferences), `session_id` and `user` (max 256 chars), `trace`.
  - Response: `{"id":"gen-dec-...","model":"typesafe/jev-1.13-20260917","provider":"TypeSafe","answers":{"is_bug":{"type":"noul","noul":0.96},"team":{"type":"choice","choice":"payments","confidence":0.67,"probabilities":{...}},"urgency":{"type":"score","score":1.99,"confidence":0.99,"probabilities":{...},"legend":{...}}},"usage":{"input_tokens":476,"output_tokens":70,"cost":0.00001999}}`.
  - Sources: openrouter.ai/docs/guides/community/jev-tutorial and the Decisions API reference under /docs/api/api-reference/alphadecisions/.
- System One API: `POST https://openrouter.ai/api/v1/systemone`. It uses TypeSafe's own schema (bare `model:"jev-1.13"`, a string `state`) with the same Bearer key.
  - It adds `id`, `provider` and `usage.cost` to the response.
  - `client.models.list()` does not work through OpenRouter (docs/guides/community/typesafe-sdk).
- Model IDs:
  - `typesafe/jev-1.13` resolves to the dated snapshot `-20260917`.
  - `~typesafe/jev-latest` is an alias that moves when a new release ships. Pin the version if you tuned thresholds.
  - `typesafe/jev-router` is a different product. It is a chat-completions model router (`POST /v1/chat/completions`) that "picks the best model and reasoning effort for each request". It runs on Jev, has a 1M context window and accepts text, image, file, audio and video. Its models-API price is `-1` (variable), while the model-page FAQ says zero. Treat its pricing as unverified.
- Jev 1.13 and `jev-latest` are `text->decisions`. Chat completions for them are not documented. Use Decisions or System One.
- Switching a TypeSafe-only client: trivial. Set `baseURL=https://openrouter.ai/api` and use the OpenRouter key. Env vars `TYPESAFE_BASE_URL` and `TYPESAFE_API_KEY` also work. The direct API is `https://api.typesafe.ai/v1/systemone`. Caveats: no `models.list`, and the Decisions path needs the `typesafe/` prefix.

**2. Pricing and limits**
- Price is $0.042 per million input tokens with free output, the same on OpenRouter and direct (models API, docs.typesafe.ai/models). A typical 400-token gate call costs about $0.0000168.
- OpenRouter needs prepaid credits. I found no Jev-specific free tier or OpenRouter Decisions rate limit; the reference lists 429 responses without numbers. Generic free-model caps (20 RPM, 50 or 1000 RPD) apply to `:free` models, and Jev is not one.
- TypeSafe direct limits: 40 requests per second and 100K tokens per second. TypeSafe warns these "adjust dynamically" without notice.
- Context:
  - OpenRouter lists 32k.
  - Direct: 64k total per request, and 32k for `state` plus the longest question.
- Choice allows at most 255 options. Score takes 2 to 10 levels.
- Batching: several questions per request are answered in parallel and cannot see each other's answers. TypeSafe claims 12.2x cheaper and 10x faster than single calls (cookbooks/parallel_questions). There is no multi-state batch endpoint.
- Request size: a 413 error exists but no byte limit is published. Input is text or JSON only.

**3. Latency and accuracy**
- Official OpenRouter blog (jev-vs-llm-when-to-use-each), 60 support tickets: p50 194ms, p95 633ms, 98.3% intent accuracy, 100% escalation, $0.0248 per 1,000 calls.
  - GPT Luna: p50 1,106ms.
  - Claude Opus: p50 1,957ms.
- Prompt injection detection, 40 messages: Jev scored 40/40.
- OpenRouter claims the Jev Router completed 82% more tasks than Auto Router (BigGo summary; I could not open the original).
- Independent sources:
  - dev.to (8 days of tests): Jev scored 72.5%, against 74.5 to 76.0% for mid-price LLMs.
    - Banking77: 80.9%.
    - Social science: about 11.6 F1 points behind the best model.
    - Median ECE 0.071 (a calibration error measure).
    - Server time about 105ms.
    - Swapping "yes" and "no" labels changed 33% of answers.
    - Confidence of 0.79 on unanswerable questions.
    - Russian dropped 11 points.
  - Substack: a die-roll test gave 83% confidence at 19% accuracy, so treat scores as rankings until you validate them.
  - GitHub issue BillionsBobby/JevRouter#2 (10 tasks): Toolathlon tool-call prediction. Jev serial 38% exact vs DeepSeek V4.1 Flash 24%, at 1.6s vs 8.6s and about 7x cheaper.
  - The independent posts are third-party summaries fetched through a summarizer, so treat exact numbers as approximate.
- Theo (t3.gg) on X:
  - "basically just a DeepSeek 4.1 flash router" (x.com/theo/status/2103701465084919906).
  - He spent about $1,000 benchmarking Jev Router: DeepSWE roughly equal to GPT-6 Astra on low, slightly higher cost, about 5x longer (status/2103774771788108008).
  - Both comments are about Jev Router, not the Decisions model. The tweets themselves would not load (402), so this comes from search snippets.
- I found no HN/Reddit eval numbers beyond commentary that Jev can be "confidently wrong" with a valid value.
- TypeSafe's own failure modes (docs.typesafe.ai/model-jaggedness/jev-1.13):
  - Literal reading.
  - Bad at math, counting and dates.
  - Indirection.
  - Large state full of irrelevant detail.
  - Adversarial content: "State is data... injected instruction... can move the answer".
  - English is best.
  - Noul and Choice outputs are not guaranteed consistent with each other.

**4. Privacy**
- The OpenRouter provider record for TypeSafe (openrouter.ai/api/frontend/v1/all-providers) shows `training:false`, `retainsPrompts:false` and `canPublish:false`.
- TypeSafe privacy policy: "will not train or fine tune any AI/ML models on Input" and will not disclose Input to third parties except service providers.
- TypeSafe direct ZDR is enterprise-only (sales@typesafe.ai). It is closed-source and hosted, so code leaves the machine. Subprocessors are listed at trust.typesafe.ai/subprocessors.
- OpenRouter routing controls:
  - Per-request `provider` field: `data_collection:"deny"`, `allow_fallbacks`, `ignore`.
  - Account-level ZDR enforcement (docs/guides/features/zdr).
- OpenRouter has its own logging and retention policy, separate from the provider's. Only the logging docs' generic text was read; the endpoints/zdr call did not return usable JSON. Check openrouter.ai/settings/privacy and the ZDR list before sending client code.
- Jev Router reportedly runs under ZDR terms and attachments never reach Jev (BigGo summary; the original was unreachable).
- Claude Code hook risk: the cookbook sends the shell command, project path and the agent's description to Jev.

**5. Stability**
- The `/alpha/` endpoint is explicitly alpha. I found no versioning or deprecation policy.
- Aliases move without notice. The response `model` field shows the dated snapshot that served the call.
- OpenRouter's endpoint API reported 100% uptime (30 minutes, 1 day). The provider has no status page URL. OpenRouter's is status.openrouter.ai.
- TypeSafe published no SLA that I found (the eesel review also says none). Rate limits are unstable per TypeSafe.
- Mitigation: prefer `/api/v1/systemone`, which mirrors TypeSafe's schema and is easier to point directly at TypeSafe. Pin `typesafe/jev-1.13`. Keep fail-open-to-prompt behavior.

**6. Official coding-agent cookbook**
Source: openrouter.ai/docs/cookbook/coding-agents/auto-approve-permission-prompts-with-jev
- Method:
  - A Claude Code `PermissionRequest` hook on the `Bash` matcher, timeout 15s.
  - Step 1: a static `RISKY` regex list is checked first, with no Jev call. It covers sudo, shells and eval, `rm -rf`, `git push` and `reset --hard`, publish and deploy commands, `terraform apply`, `kubectl delete`, and `.env`, `.ssh`, `.aws`, `.npmrc` and credentials. `$()`, backticks and `<()` also always prompt.
  - Step 2: two Noul questions.
    - `reversible`: "Every command in `commands` only reads or changes files inside `project` and can be undone with git or by rerunning it. It does not push, publish, deploy, delete files outside the project, change system settings, or send data to a network service."
    - `serves_task`: "Running `commands` is a reasonable next step toward `task`."
    - State is `{commands:[cmd], project:cwd, task: tool_input.description}`. Claude Code supplies no user task, so the agent's own description stands in for it.
  - Step 3: allow only if every score is at least `APPROVE_AT=0.9`. The hook prints `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}`. Otherwise it prints nothing and the normal prompt appears.
- Fail-safe:
  - Network error, non-2xx, 8s abort, a malformed response or a score outside 0 to 1 all return undefined, which means the normal prompt.
  - The hook only sees requests that would already prompt. Deny rules still win.
  - "Treat the risk list, not the threshold, as the security boundary."
- Reported results (2026-09-21):
  - `bun test src/utils/date.test.ts` scored 0.93 and 0.95 and was approved.
  - `bun add left-pad` scored 0.45 and 0.09 and prompts.
  - `npx wrangler deploy` scored 0.04 and 0.07 and prompts.
  - Cost is about $0.0000168 per call. Scores drift by a few hundredths between runs.
  - No false-approve rate is published.
- Other cookbooks: Gate Agent Tool Calls (approve, block or review, under $0.0001 per call), Jev-Verified Cascade (same zero wrong answers as running the frontier model on everything, at about 7% of cost, on 50 questions), Classify and Tag Text at Scale.
- A third-party repo, `leepokai/jev-guard`, wraps Jev as a guard for coding agents. I did not review it.
- TypeSafe repo `typesafe-ai/skills` issue #5 discusses a shell-command approval gate.

**7. SDKs**
- Via TypeSafe: npm `@typesafe-ai/sdk` (0.6.0) and PyPI `typesafe-sdk` (0.7.2).
  - `new TypeSafeClient({apiKey, baseURL:'https://openrouter.ai/api'}).systemOne({model:'jev-1.13',state,questions})`
  - Python: `TypeSafeClient(api_key=..., base_url=...).system_one(...)`
- OpenRouter SDKs: npm `@openrouter/sdk` (1.4.9) with `openRouter.alpha.decisions.create({decisionsRequest:{model,state,questions}})`, PyPI `openrouter`, and Go `github.com/OpenRouterTeam/go-sdk`.

**Files fetched to** `<scratch path>` (raw doc markdown, kept for reference).