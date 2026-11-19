# Snippet verdict fairness audit

**34 of 54 sampled verdicts disagreed with manual review on v1; 12 of 54 disagreed after the fixes.** False “broken” calls fell from 32 to 12; the other two errors in v1 were runnable snippets incorrectly skipped for a lint-only opt-out.

| SDK | Reviewed | Wrong before | Wrong after |
| --- | ---: | ---: | ---: |
| openai-python | 23 | 18 | 2 |
| httpx | 18 | 14 | 10 |
| pydantic | 13 | 2 | 0 |
| **Total** | 54 | 34 | 12 |

## Method and decisions

- Ran 175 Python snippets (59 OpenAI, 34 HTTPX, 82 Pydantic) from the Markdown files listed below, using Sentinel's Docker runner and a 60-second per-snippet timeout. Source links are pinned to the checked-out revisions. The baseline is Sentinel v1 (`4d10738`).
- Hand-labeled 54 of the baseline broken, skipped, and credential-blocked snippets by reading the source and surrounding prose, then replayed the same snippet IDs in the same environment. This is a targeted error review, not a random sample or a leaderboard score.
- Both sides used the same configured pin `openai[aiohttp]==3.55.1` for the two examples with documented aiohttp setup. V1 ignored the extras pin and failed before credential lookup; the fixed runner installs it and reaches the credential check. Other examples used registry resolution (observed SDK releases: OpenAI 3.55.1, HTTPX 0.28.1, Pydantic 2.14.3).
- Credential and fragment decisions now depend on the terminal exception and specific evidence, not traceback source text or an arbitrary undefined name. Pydantic's explicit negative-example opt-outs are still honored. Regression fixtures cover shell-only Python fences and intentional errors with a matching exception in the adjacent output fence; neither new pattern contributed to the measured improvement.
- The remaining 12 disagreements include fragments with no earlier binding, an illustrative dependency, a placeholder path, and sandbox DNS failures for an external demo host. These still need review before publication. A blanket rule for those errors could hide genuine broken examples. “Not evaluated” is also now distinct from a measured 0% for an empty, failed, or incomplete run.

<details>
<summary>Line-by-line judgments and measured results</summary>

### openai/openai-python

| Source | Before | After | Expected | Reason |
| --- | --- | --- | --- | --- |
| [README.md:30](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L30) | runtime_error | needs_credentials | needs_credentials | Full client example needs a key. |
| [README.md:50](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L50) | runtime_error | needs_credentials | needs_credentials | Uses the default credential lookup. |
| [README.md:81](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L81) | runtime_error | needs_credentials | needs_credentials | Requires a Kubernetes service-account token file. |
| [README.md:175](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L175) | needs_credentials | needs_credentials | needs_credentials | Needs the mTLS certificate from the environment. |
| [README.md:232](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L232) | runtime_error | skipped | skipped | Vision call assumes a client set up earlier. |
| [README.md:252](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L252) | runtime_error | needs_credentials | needs_credentials | Client construction is blocked before the image placeholder. |
| [README.md:316](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L316) | runtime_error | needs_credentials | needs_credentials | Run with the documented aiohttp extra; then needs a key. |
| [README.md:448](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L448) | skipped | skipped | skipped | Partial realtime session with an explicit ellipsis. |
| [README.md:517](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L517) | syntax_error | skipped | skipped | Pagination fragment uses earlier client and an async context. |
| [README.md:531](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L531) | syntax_error | skipped | skipped | Alternative pagination fragment, same setup. |
| [README.md:582](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L582) | runtime_error | runtime_error | skipped | File-like upload starts from caller-provided audio_bytes. |
| [README.md:734](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L734) | syntax_error | skipped | skipped | Request-ID fragment uses earlier client and an async context. |
| [README.md:749](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L749) | syntax_error | skipped | skipped | Exception-handling variation relies on earlier async client setup. |
| [README.md:855](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L855) | runtime_error | skipped | skipped | Demonstrates inspecting a previous response object. |
| [README.md:903](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L903) | runtime_error | skipped | skipped | Shows streaming on an existing client. |
| [README.md:975](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L975) | runtime_error | skipped | skipped | One-line customization example with an existing client and placeholder args. |
| [README.md:989](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L989) | needs_credentials | needs_credentials | needs_credentials | Uses an mTLS certificate from the environment. |
| [README.md:1027](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L1027) | needs_credentials | needs_credentials | needs_credentials | Async mTLS example has the same prerequisite. |
| [README.md:1087](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L1087) | skipped | skipped | skipped | Template with explicit ellipsis. |
| [README.md:1123](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L1123) | runtime_error | needs_credentials | needs_credentials | Needs Azure API credentials. |
| [README.md:1190](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/README.md?plain=1#L1190) | runtime_error | skipped | skipped | Bedrock profile variant reuses imports from the preceding baseline. |
| [httpx2.md:60](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/httpx2.md?plain=1#L60) | runtime_error | runtime_error | skipped | Example CA bundle is explicitly a placeholder pathname. |
| [httpx2.md:192](https://github.com/openai/openai-python/blob/0d50c9daece1a4a0f4920f64b5ff90846b4e2050/httpx2.md?plain=1#L192) | runtime_error | needs_credentials | needs_credentials | Run with documented aiohttp extra; then needs a key. |

### encode/httpx

| Source | Before | After | Expected | Reason |
| --- | --- | --- | --- | --- |
| [docs/compatibility.md:19](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/compatibility.md?plain=1#L19) | runtime_error | runtime_error | skipped | Option comparison assumes an existing client and URL. |
| [docs/compatibility.md:25](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/compatibility.md?plain=1#L25) | runtime_error | runtime_error | skipped | Constructor comparison assumes HTTPX is imported. |
| [docs/compatibility.md:33](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/compatibility.md?plain=1#L33) | runtime_error | runtime_error | skipped | Requests comparison uses implicit import and kwargs. |
| [docs/compatibility.md:39](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/compatibility.md?plain=1#L39) | runtime_error | runtime_error | skipped | HTTPX counterpart uses implicit import and kwargs. |
| [docs/compatibility.md:136](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/compatibility.md?plain=1#L136) | skipped | skipped | skipped | Streaming sketch contains a body placeholder. |
| [docs/quickstart.md:505](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/quickstart.md?plain=1#L505) | runtime_error | skipped | skipped | Continues from the earlier REPL import. |
| [docs/quickstart.md:515](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/quickstart.md?plain=1#L515) | runtime_error | skipped | skipped | Continues from the earlier REPL import. |
| [docs/quickstart.md:528](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/quickstart.md?plain=1#L528) | runtime_error | skipped | skipped | Continues from the earlier REPL import. |
| [docs/quickstart.md:538](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/quickstart.md?plain=1#L538) | runtime_error | skipped | skipped | Continues from the earlier REPL import. |
| [docs/troubleshooting.md:44](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/troubleshooting.md?plain=1#L44) | skipped | skipped | skipped | Proxy troubleshooting sketch has ellipsis. |
| [docs/advanced/authentication.md:99](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/authentication.md?plain=1#L99) | runtime_error | runtime_error | skipped | Class example assumes HTTPX is in scope; earlier REPL examples also use it without an import. |
| [docs/advanced/authentication.md:112](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/authentication.md?plain=1#L112) | runtime_error | runtime_error | skipped | Class variant likewise assumes HTTPX is in scope. |
| [docs/advanced/authentication.md:130](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/authentication.md?plain=1#L130) | skipped | skipped | skipped | Auth implementation sketch intentionally omits bodies. |
| [docs/advanced/authentication.md:153](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/authentication.md?plain=1#L153) | skipped | skipped | skipped | Partial auth implementation with ellipsis. |
| [docs/advanced/authentication.md:220](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/authentication.md?plain=1#L220) | missing_dependency | missing_dependency | skipped | Uses a clearly illustrative sync_only_library for auth hooks. |
| [docs/advanced/clients.md:151](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/clients.md?plain=1#L151) | runtime_error | runtime_error | skipped | One-line Request example assumes HTTPX is already in scope. |
| [docs/advanced/clients.md:188](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/clients.md?plain=1#L188) | runtime_error | runtime_error | skipped | Run blocked on name resolution for external demo download host; not evidence about HTTPX. |
| [docs/advanced/clients.md:211](https://github.com/encode/httpx/blob/7cff5f79bc78649bbcd3fd9aa7a74c86e0f63016/docs/advanced/clients.md?plain=1#L211) | runtime_error | runtime_error | skipped | Same external demo endpoint, with a different progress renderer. |

### pydantic/pydantic

| Source | Before | After | Expected | Reason |
| --- | --- | --- | --- | --- |
| [docs/migration.md:74](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L74) | skipped | skipped | skipped | Author explicitly opts out of execution. |
| [docs/migration.md:96](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L96) | skipped | skipped | skipped | Placeholder import path is explicitly opted out. |
| [docs/migration.md:111](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L111) | skipped | skipped | skipped | Version-specific migration illustration is explicitly opted out. |
| [docs/migration.md:198](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L198) | skipped | skipped | skipped | Intentional V1/V2 comparison is explicitly opted out. |
| [docs/migration.md:677](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L677) | skipped | pass | pass | Lint-only exclusion; self-contained, catches its intentional validation error. |
| [docs/migration.md:878](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/migration.md?plain=1#L878) | skipped | skipped | skipped | Removed V1 API is shown only to contrast its replacement. |
| [docs/errors/usage_errors.md:518](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L518) | skipped | skipped | skipped | Type alias is explicitly opted out. |
| [docs/errors/usage_errors.md:524](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L524) | skipped | skipped | skipped | Invalid aliases deliberately demonstrate the error. |
| [docs/errors/usage_errors.md:800](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L800) | skipped | skipped | skipped | Partial method signature gallery is explicitly opted out. |
| [docs/errors/usage_errors.md:983](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L983) | skipped | skipped | skipped | Intentional invalid annotation is explicitly opted out. |
| [docs/errors/usage_errors.md:995](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L995) | skipped | pass | pass | Working annotation alternative; only linting is opted out. |
| [docs/errors/usage_errors.md:1013](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L1013) | skipped | skipped | skipped | Intentional schema error and explicit test opt-out. |
| [docs/errors/usage_errors.md:1034](https://github.com/pydantic/pydantic/blob/328b88b52694a64ecd05f3acc92f2b6a160bc2ee/docs/errors/usage_errors.md?plain=1#L1034) | skipped | skipped | skipped | Intentional dataclass field error and explicit test opt-out. |

</details>
