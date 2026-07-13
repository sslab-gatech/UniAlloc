# UniAlloc PhD Qualifier Presentation Blueprint

> 目的：把 UniAlloc 讲成一个可以被委员会检验的研究论证，而不是论文目录、功能清单或当前工程进度汇报。
>
> 推荐版本：**45 分钟排练目标、48 分钟硬上限 + 12--15 分钟问答，31 张主幻灯片 + 19 张备份页**。
>
> 若学院明确要求“完整主讲 60 分钟，问答另计”，使用本文的 53--55 分钟扩展版，仍保留约 5 分钟缓冲。

## 1. 一句话结论

### 推荐标题

**Beyond Size: Compiler-Assisted, Retargetable Memory Allocation for Rust**

副标题可以使用：

**UniAlloc as a Semantic Interface Between the Compiler, Allocation Policy, and Platform**

### 全场唯一主论点

> **The conventional Rust allocation boundary exposes layout but not language-level object semantics. UniAlloc carries trusted compiler-derived semantics through an optional channel, uses them for deployable policies, and separates those policies from platform-specific mechanisms.**

中文理解：传统 Rust allocation boundary 暴露 layout 和运行时上下文，却不暴露编译器掌握的 type/module 等语言级语义。UniAlloc 在明确的 trusted-metadata 与 coverage 假设下，用可选 compiler-to-allocator 通道传递这些语义，以此驱动可选择的策略，并把策略与平台机制解耦。

这比“UniAlloc 是一个更快/更安全/支持很多 feature 的 allocator”更适合作为 qualifier thesis，因为它同时给出：

1. 一个明确的系统接口缺口；
2. 一个可反驳的设计命题；
3. 三组可以分别检验的证据；
4. 清楚的假设、限制与下一步研究问题。

## 2. Qualifier 的成功标准与时间选择

Georgia Tech 对 qualifier 的公开描述强调研究准备度、研究深度、创造力，以及能否通过口试继续解释工作；MIT 的公开 qualifier 指南也强调逻辑、基础、方法选择、批判性思考和技术讨论，而不是把它当成普通 conference talk。因此本报告应当证明“我能提出、设计、评估并批判一个研究问题”，而不只是“我做了一个 allocator”。参考：

- [Georgia Tech Ph.D. CS Qualifier Exam Information](https://www.cc.gatech.edu/phd-cs-qualifier-exam-information)
- [MIT MechE Qualifying Exam Presentation](https://mitcommlab.mit.edu/meche/commkit/qualifying-exam-presentation/)
- [MIT NSE Doctoral Qualifying Exam Presentation](https://mitcommlab.mit.edu/nse/commkit/doctoral-qualifying-exam-presentation/)

### 两种可执行节奏

| 场景 | 主讲 | 问答/缓冲 | 建议 |
|---|---:|---:|---|
| 一小时是整个 exam slot，或委员会可能中途提问 | 45 min target；48 min ceiling | 12--15 min | **默认使用**；准备 42 min 可剪裁版 |
| 学院明确要求约一小时 uninterrupted talk，问答另计 | 53--55 min | 5--7 min | 加入第 6 节的 5 张扩展页 |

不要排练到 59:30。委员会中途追问、切换 backup slide、设备问题都会吃掉时间。

## 3. Research question、假设与贡献

### Research question

> **Can a Rust allocator use compiler-visible heap-object semantics without breaking existing programs, and can the same allocator runtime be retargeted across userspace, kernels, and constrained systems?**

论文依据：`../rust-alloc-paper/intro.tex:217-224`。

这里的 “without breaking existing programs” 只能作为论文原问题的 shorthand；主讲必须用 Slide 12 的四维 contract 将其限定为 paired toolchain 下 supported paths 的 source/execution compatibility，而不是 universal binary ABI、FFI 或 cross-rustc compatibility。

### 三个可检验假设

| 假设 | 要证明什么 | 不需要证明什么 |
|---|---|---|
| **H1 — Semantic availability** | 配套 compiler/runtime 能把对象语义传到 allocator；supported common paths 不需 source annotation，unknown requests 有 conventional execution path | 所有 allocation site 都有语义，或存在 universal binary/FFI compatibility |
| **H2 — Policy usefulness** | 至少一个代表性策略能利用语义改变 allocator 的行为，并有可量化的 cost/boundary | UniAlloc 消灭 UAF、普遍更快或对所有 workload 都更省内存 |
| **H3 — Retargetability** | policy、cache/zone/backend、metadata layout、PAL 之间有可复用边界；retargeting 是 semantic channel 是否为 reusable systems contract 的 generality test | H3 自身证明 security benefit、移植零工作量，或当前源码的所有平台 claim 已完成复现 |

每个假设章节最后固定使用两句话：

1. **This evidence supports ...**
2. **It does not establish ...**

### 三项贡献；不要把 feature 列表当贡献列表

1. **Semantic allocation API**：可选 metadata 参数，同时保留传统 allocation path。
2. **Compiler-assisted extraction**：修改 rustc/core allocation path，让 `Box<T>`、`Vec<T>` 等自动产生 metadata，而不是要求手工 annotation。
3. **Retargetable allocator runtime**：把 semantic policy 与 platform memory acquisition、cache primitive、metadata layout 分开。

论文依据：`../rust-alloc-paper/intro.tex:246-272`。Type isolation、metadata segregation、hugepages、PAC 等应作为证明架构能力的 **case studies**，而不是各自宣称为独立 thesis。

## 4. 31 张主幻灯片：逐页可执行 Outline

规则：所有标题都写成委员会应当记住的结论句，而不是写成 “Background”“Design”“Evaluation” 等名词。

下列逐页预算合计 **45:00**；45--48 分钟之间的 3 分钟不分配给任何 slide，只作为 transition、短中断和设备缓冲。

### A. Hook and gap — 0:00--8:00（Slides 1--6）

| # | 建议英文标题 | 这一页只完成什么 | 最省事、最清楚的图 | 时间/追问 |
|---:|---|---|---|---|
| 1 | **Beyond Size: Compiler-Assisted, Retargetable Memory Allocation for Rust** | 说姓名、题目和一句 thesis；不要讲履历 | 标题 + 一个 `type → metadata → allocator policy` 箭头 | 1:00；不接问题 |
| 2 | **The conventional Rust allocation boundary omits language-level semantics.** | 直接给 thesis 和三项贡献的预告 | 三层横条：Compiler semantics / Policy / Platform | 1:30；委员会立即知道主张 |
| 3 | **Same-size reuse can turn a temporal bug into type confusion.** | 建立全场 running example：A free 后被同 size 的 B 占据 | 4 格 UAF 动画；只显示两种类型和同一 slot | 1:30；准备“这是否阻止所有 UAF？” |
| 4 | **Rust preserves type semantics until the allocation boundary discards them.** | 展示 `Box<T>`/`Vec<T>` 有 `T`，`GlobalAlloc` 最终只见 `Layout` | 左侧 typed object，右侧 `size + align`，中间语义被灰掉 | 1:30；这是全场最重要 gap 图 |
| 5 | **The conventional Rust API exposes layout, not language-level type or module semantics.** | 用最少背景说明 allocator 还会使用 thread/address/history，但 API 缺少语言级语义 | cache → zone → backend 图，旁边标出 available inputs | 1:15；避免“只看 size”的绝对表述 |
| 6 | **Prior systems obtain policy inputs from runtime state, manual classes, or fixed hardening mechanisms.** | 用 nearest-neighbor 定位 snmalloc/mimalloc/Temeraire 与 Slitter/hardened malloc/Scudo，再突出 UniAlloc 的 compiler-derived input | 三行表：metadata source / policy / deployment | 1:15；准备 novelty 问题 |

**段落转场：** “The bottleneck is not another free-list optimization; it is the information boundary.”

### B. Question, criteria, and answer — 8:00--12:00（Slides 7--9）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 7 | **Retargetability tests whether the semantic channel is a reusable contract rather than a point integration.** | 把原 RQ 拆为两个相连问题：语义兼容性与 abstraction generality；H3 不直接证明 security benefit | RQ-A → stable contract → RQ-B | 1:15 |
| 8 | **Three hypotheses make the thesis falsifiable.** | 给出 H1/H2/H3 和各自成功标准 | 三列 hypothesis → measurement | 1:30；告诉委员会后面如何判断成功 |
| 9 | **UniAlloc answers with an API, compiler extraction, and a reusable runtime.** | 给出三项贡献，明确 features 是 case studies | 三个编号块，颜色贯穿全场 | 1:15 |

### C. H1: Semantic availability without abandoning compatibility — 12:00--23:00（Slides 10--16）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 10 | **The security scope trusts compiler/runtime metadata and separates only covered cross-class reuse.** | 明确 attacker、TCB 和 non-goals：trusted metadata；内部 cache lookup-key collision 会做 exact identity check，但 spoofed/identical metadata、compiler type-ID collision、same-type、fallback、metadata corruption 仍是边界 | TCB 边界 + “Protects / Does not protect” | 1:30；先于委员会指出限制 |
| 11 | **UniAlloc separates semantic input, allocation policy, and platform mechanism.** | 给出全系统 mental model | 把 `fig/overview.pdf` 重画为三条横向 lane，并 progressive reveal | 2:00；原图只放 backup |
| 12 | **Optional metadata preserves supported source-level execution, not universal ABI compatibility.** | 解释 `AllocationMetadata`、semantic API、`GlobalAlloc` fallback，并预告四维 compatibility contract | 两条路径 + source/toolchain/FFI/ABI 四行表 | 1:30；准备 ABI/compatibility |
| 13 | **Compiler extraction removes source annotations from common Rust allocation paths.** | 展示 `T` 如何通过 optimized MIR rewrite 进入 metadata ABI | `Box<T>` → MIR → metadata ABI → allocator，4 个节点 | 2:00；准备 rustc fragility |
| 14 | **Correct semantics require pairing allocation, reallocation, drop, unwind, and thread transfer.** | 用 actual-rustc 证据区分 direct neutral delegation、allocation-side recovery 与 provider observability boundary | object lifecycle 状态图；三条 pairing lane | 1:30；准备 cross-thread 问题 |
| 15 | **Fallback preserves execution when semantics are absent, but it also bounds protection.** | 把兼容性与安全 coverage 放在同一张图上 | Coverage 圆：typed/known vs unknown/fallback | 1:30；不要把 fallback 说成安全覆盖 |
| 16 | **H1 is supported by source-bound probes and an instrumented real application, with version and coverage limits.** | H1 小结：actual-rewrite probes 与 Oxipng integration 支持 feasibility；不证明全面 coverage、unmodified-app deployment 或稳定 ABI | `Supports / Does not establish` 两个框 | 1:00；给证据 badge |

关键实现依据：

- `unialloc/src/alloc_api/type_isolation.rs` 的 `AllocationMetadata`：metadata fields/flags 与 unknown 状态。
- 同文件的 `SemanticAlloc` trait、`impl SemanticAlloc for RustAllocator` 与 `__unialloc_semantic_scope_push*`/`__unialloc_semantic_scope_pop`：semantic alloc/dealloc/realloc API 与 scope ABI。
- `unialloc/src/cache/mod.rs` 的 `unsafe impl GlobalAlloc for RustAllocator`：semantic path 与普通 `GlobalAlloc` fallback。
- `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs` 的 `record_or_rewrite_semantic_scope_candidates`、`push_semantic_scope_pop_block` 与 `record_or_rewrite_semantic_ownership_transfers`：metadata ABI、scope push/original call/pop、ownership transfer 与 unwind cleanup。行号只在 deck freeze 前刷新一次，symbol 是长期入口。
- `docs/allocator-mir-and-backend-validation.md:38-84,170-300`：real rustc-driver 与 bounded functionality probes；不要把这些叫 performance evidence。
- `94b2523d...` source-bound presentation snapshot（source digest `56a912ef...`）：direct path 观察到 36 个 actual rewrites、typed runtime `84/84`；semantic-scope path 观察到 116 个 rewrites、28 个 drop rewrites、typed runtime `161/161`；cross-thread path 观察到 4 个 hints、3 个 recovery matches、0 mismatch。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-current-head-94b2523d8823-20260712T060330Z/`。开发 HEAD 已继续前进，因此它是精确绑定的 recent functionality evidence，不应再称 live-HEAD universal coverage 或 performance evidence。
- `4715d46...` clean-HEAD compiler-driven type-isolation probe：普通 `Box<T>` 源码没有手工 metadata/allocator ABI；real rustc-driver 实际应用 21 个 semantic-scope 与 4 个 Drop rewrite（该 probe 没有 supported direct allocator-call replacement candidate），为两个 same-layout Rust types 产生 distinct compiler-derived IDs。Hosted 与 `fixed_heap` 各单次 PASS：wrong-type reuse 被阻止、producer identity 4/4 完整取回自己的地址，target-type drop/deallocation scope 为 `0/0`，因此该生命周期必须使用 allocation-side recovery；corrupt slots 与 recovery mismatch 均为 0。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/current-source-typeiso-oxipng-4715d46-20260712a/`。它支持 bounded compiler-derived identity → runtime isolation path，不支持 universal UAF prevention、same-key/collision/spoofing/fallback coverage 或 performance claim。
- `3d08399...` actual-rustc `Box<[T], A> -> Vec<T, A>` ownership-transfer probe：exact DefId + structural type proof 将普通 Rust ownership transfer 实际改写到 `__unialloc_semantic_box_slice_into_vec`，保留 pointer/payload，只把 live recovery `type_id` 从 Box owner rebind 到 distinct compiler-derived Vec identity；module、flags、hints 与 allocation callsite 仍绑定原始 allocation。`python3 tools/unialloc-rustc-pass/test_mir_box_slice_into_vec_rebind.py` 单次 PASS：wrong-type non-reuse、same-Vec-type exact reuse、typed alloc/dealloc `3/3`，raw fallback、recovery mismatch、corrupt slots 均为 `0`。独立 runtime regression `box_slice_into_vec_rebind_rejects_memory_tagged_record_without_mutation` 验证 memory-tagged record fail closed、保留旧 identity 且不发生 mutation。这只是一项 bounded functional probe，不支持 universal coverage、performance 或论文百分比 claim。
- `9c04871...` wrapped actual-rustc ownership-transfer probe：普通 Rust helper 接收 `Result<Option<Box<[u8]>>, u8>`，经 `?`、`Option::expect` 与显式 move 后调用 `into_vec`；audit 对 direct 与 wrapped 两条 transfer candidate 精确应用 `2/2`。wrapped runtime 保留 pointer/payload，阻止 wrong-type reuse、允许 same-Vec-type reuse，recovery mismatch 为 `0`。这是 bounded Result/Option passthrough 与 ownership-transfer 功能证据，不是通用容器 coverage 或性能 claim。
- `718aab9...` cross-thread actual-rustc `Box<[u8]> -> Vec<u8>` probe：普通 Rust 程序在 main 分配 Box，move 到 distinct worker 后在那里调用 `into_vec`；该 lane 显式配置 cross-thread recovery placement policy，并非 automatic escape-inference test。audit candidate/applied 为 `1/1`，runtime transfer attempted/applied/rejected 为 `1/1/0`。pointer/payload 跨线程保留，wrong Box identity 不复用、same Vec identity 精确复用；fallback alloc/dealloc、raw alloc/dealloc/realloc-without-metadata、mismatch、corrupt、dropped 均为 `0`。权威入口：`tools/unialloc-rustc-pass/test_mir_cross_thread_box_slice_into_vec_rebind.py`。这是 bounded functional/safety evidence，不是 universal coverage、performance 或论文百分比。
- `718aab9...` actual-rustc `String::with_capacity -> String::into_bytes -> Vec<u8>` probe：exact non-generic helper candidate/applied 为 `1/1`，compiler-derived String/Vec IDs 分别为 `11507945832468554002 / 13513741751600386252`。runtime `1/1/0`，pointer/capacity/payload 保留，wrong String identity 不复用、same Vec identity 精确复用，fallback/raw/mismatch/corrupt/dropped 全为 `0`。`unialloc/tests/string_into_bytes_rebind.rs` 的独立 safety regression 另验证 wrong expected ID、memory-tagged source、missing record 均 fail closed，aggregate 为 `1 applied / 3 rejected`，hosted 与 `fixed_heap` 各 `1/1` PASS，不发生 trusted-record mutation、fabricated record 或 mismatch。权威入口：`tools/unialloc-rustc-pass/test_mir_string_into_bytes_rebind.py` 与该 integration test。这是 bounded functional/safety evidence，不是 universal coverage、performance 或论文百分比。
- `99762a9...` actual-rustc `Vec<T, A> -> Box<[T], A>` shrink-aware ownership-transfer probe：exact structural match 对 exact-capacity 与 spare-capacity 两个 candidate 精确应用 `2/2`。单次 runtime 中，`capacity == len` 保留 pointer/payload 且没有 allocation/deallocation/cache lifecycle event；spare shrink 的 pointer 移动但 payload 保留。transfer attempted/applied/rejected 为 `2/2/0`：wrong Vec identity 不复用 Box storage、same-Box identity 可复用，且 old spare Vec identity 可复用释放的旧 storage；fallback alloc/dealloc、raw alloc/dealloc/realloc-without-metadata、mismatch、corrupt 均为 `0`。独立 reviewer 首轮发现 rejected wrong/tagged shrink 会丢 source policy，以及 missing record + outer scope 会被 outer/auto 伪 attribution；修复后 wrong/tagged exact/moved 路径保留 source policy/tag，missing + outer + auto 被抑制，RAII guard 在 panic/unwind 后恢复且不消耗 finite compiler stream。hosted 与 `fixed_heap` focused tests 各 `3/3`。这是 bounded functional/safety evidence，不是 universal coverage、performance 或论文百分比。
- `5eb25f5...` actual-rustc `VecDeque` same-layout isolation：普通 Rust 源码让 Alpha/Beta 两个同 layout element type 获得 distinct nonzero compiler identities。Hosted 与 `fixed_heap` 各单次 PASS：4 个 Alpha 与 4 个 Beta 地址集合不相交，随后 4 个 Alpha 精确取回原集合；typed alloc/dealloc 为 `12/12`、cache hit `4`，fallback/raw/mismatch/corrupt/dropped 全为 `0`。这是 Slide 14 的 bounded container lifecycle 和 Slide 17 的 covered-path reuse evidence，不是 universal container coverage、安全证明或性能结果。
- `c02baa6...` exact `VecDeque` capacity owner：current 与 pinned nightly 的 actual `RUSTC_WRAPPER` 对 std-owned `with_capacity` 和 `reserve_exact` 实际应用外层 ring-buffer identity。单次 probe 为 typed alloc/dealloc `4/4`、grow alloc/dealloc `1/1`，wrong-type cache-hit delta `0`、exact-type delta `1`，地址精确回收；fallback/raw/mismatch/corrupt 全为 `0`。`push_back`、custom same-name helper 与 multi-owner `Drop` 保持 fail closed。这只支持 exact capacity-path functional feasibility，不是容器全覆盖或性能 claim。
- `32c3c5b...` P0 ownership-transfer pairing：actual-rustc `Vec<T,A> -> IntoIter<T,A>` 唯一 candidate 应用 `1/1`，runtime `1/1/0`，保留 pointer/payload、阻止 wrong-Vec reuse、允许 exact-IntoIter reuse，implicit Drop mismatch `0`；`String -> Box<str>` exact/spare 两条 candidate 应用 `2/2`，runtime `2/2/0`，覆盖 pointer-preserving exact rebind 与 moved shrink，wrong-String non-reuse、same-Box reuse 和 old-String old-storage recovery 均成立。对应 fail-closed regression 覆盖 wrong/tagged/missing 等输入，hosted/fixed 均 PASS，fallback/raw/mismatch/corrupt/dropped 为 `0`。这是 Slide 14 的 ownership-consuming pairing 证据，不是所有标准库 conversion 或稳定 rustc ABI 的证明。
- `2e3c0e2...a7b5f75...` actual-rustc 三 lane deterministic replay：同 class `Layout` shrink 的 alloc 建立 nonzero identity，realloc/dealloc 以全零 type/module/flags/hints strict-neutral delegation 继承该 identity；`63 -> 57`、align 64 的 pointer/layout/payload 均有效，realloc typed alloc/dealloc `1/1`、final typed dealloc `1`，fallback/mismatch/corrupt 均 `0`。Partial-coverage lane 中，seed/recover helper 各有一个 actual `Vec::with_capacity` scope、target helper Drop rows 为 `0`，因此明确由 allocation-side recovery 配对；raw Clone alloc/dealloc 恰为 `1/1`，不能取得 protected address，随后 supported `Vec` 精确取回。Generic helper runtime typed-dealloc/fallback-dealloc/cache-insert 为 `4/0/4`，但 `optimized_mir` 未暴露独立 generic helper row，所以不虚构 generic-skip。证据精确绑定 source `2e3c0e2`、validator `a7b5f75`，`collector_runs=3`、`reruns=0`，只重放 preserved raw；这是功能证据，不是 benchmark、百分比或 universal coverage claim。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-actual-rustc-2e3c0e2-a7b5f75-20260712/`。
- `7096fc6...37ea7cd...` compiler-driven `Vec` transfer-before-growth probe：creator thread 用普通 `Vec<ProducerPayload>` 完成 capacity-1 allocation 后先转移到 distinct worker；worker 在 growth 前核对 pointer/capacity/payload，再执行 capacity `1 → 8` 的 typed realloc 与最终 Drop。Hosted 与 `fixed_heap` 在 clean `37ea7cd` 各单次 PASS：audit 有 `2` 个 function-bound direct positive-control replacement、`11` 个 semantic-scope、`4` 个 Drop rewrite、`0/0` unsolved；allocation/growth type id 相同，growth typed alloc/dealloc `1/1`、worker Drop typed dealloc `1`、raw realloc/dealloc 与 old-metadata fallback `0`，wrong-type reuse blocked、same-type recovery true、mismatch/corrupt `0/0`。Direct control 在 lifecycle snapshots 后独立运行；这是 bounded actual-rewrite/realloc/drop/thread-transfer/isolation 功能证据，不是 universal container coverage 或 performance claim。
- `97c7aae...d394f19...` nested unwind actual-rewrite probe：inner `Vec::extend_from_slice` 的 Clone panic 通过真实 MIR cleanup 只弹出 inner scope，runtime 必须恢复仍然 active 的 outer `Box` scope（depth `1`），outer return 后再回到 depth `0`；随后 `Box` allocation/Drop 使用同一个 nonzero compiler-derived type id。Hosted 与 `fixed_heap` 各一次 PASS：typed alloc/dealloc `1/1`、fallback `0`、mismatch/corrupt `0`；audit 为 `8` enter/exit、`8` Drop、`5` unwind pops、`0` unsolved。由于 workspace dev profile 是 `panic=abort`，probe subprocess 明确局部设置 `CARGO_PROFILE_DEV_PANIC=unwind`；这是 bounded unwind-pairing evidence，不是所有 panic/MIR shape 的通用证明。
- `52a7002...88c35fd...` Clone classifier current-rustc ICE 修复与 fail-closed 边界：plain `Clone::clone` 只在结果中有单一 supported heap owner 时降低；nonheap Clone 被标记为 skipped、ambiguous owner 与 raw-pointer wrapper 保持 unsolved、仍含 type/const params 的 const-generic Clone 保持 unresolved，避免对 `TypingEnv::fully_monomorphized()` 做会 ICE 的 Copy query。`88c35fd` 只加入 exact `indexmap::map::IndexMap` / `indexmap::set::IndexSet` heap-container identity，使用 normalized exact def-path match，不放宽到任意 custom ADT；PngData、Headers 与 crossbeam Sender 仍按边界 fail-closed。
- `dd30004...` supported plain-Clone positive + ambiguous negative：真实 optimized-MIR 对普通 `Option<Vec<ProducerPayload>>::clone` 应用恰好一条 semantic-scope rewrite；Producer type/module 为 `11653960357981974603 / 13835860698770440193`，同布局 Consumer 使用 distinct type `17450045950661180065`。运行时 Option Clone typed alloc/dealloc/cache-hit/cache-insert 为 `1/1/1/1`、fallback/raw 为 `0`，精确取回 Producer protected address 且不取得 Consumer address；ambiguous `Result<Vec<ProducerPayload>, String>::clone` 仍为一条 fail-closed row 与 raw alloc/dealloc `1/1`。这是单一 supported Clone callsite + 单一 ambiguous control 的 source-bound 功能证据，不是全 Clone/全应用 coverage 或性能证据。
- `1956350...` Cargo multi-crate allowlist regression：POSIX 临时 fixture 的 selected bin 与 path dependency 均真实编译执行，输出精确为 `target=7 dependency=11 sum=18`；dependency 经 compiler shim，selected target 由 rustc-driver 内部处理。测试要求唯一 target audit/log、`selected_value` 的 actual semantic rewrite，以及 dependency 零 MIR rows；exact unittest `1/1` PASS。它只证明该 target/dependency non-interference path，不证明任意 dependency graph、direct allocator-call coverage、runtime isolation 或性能。
- `374d455...` ambiguous Clone fallback regression：普通 `Result<Vec<ProducerPayload>, String>::clone` 在真实 rustc audit 中恰有一条 ambiguous/fail-closed row，且不得出现 applied/planned semantic scope；hosted 与 `fixed_heap` 各一次独立复验均观察到 typed Clone allocation `0`、raw fallback alloc/dealloc `1/1`、正确且独立的 clone buffer、recovery mismatch `0`、corrupt slots `0`。这证明该 bounded unsupported path 的 conventional execution 安全退化，不把 fallback 写成 type-isolation coverage。
- `f8612de...b5b70ed...` `Layout` fallback provenance regression：真实 rustc-driver 对 `Layout::new::<[u64; 4]>().align_to(64).expect(...)` 实际改写，并在 runtime 观察到相同 nonzero compiler-derived identity 的 `32B/align64` alloc/dealloc；`align_to(3).unwrap_or_else(|_| Layout::new::<[u8; 37]>())` 必须改用 unknown-object direct-callsite fallback identity，不能继承源 `[u64; 4]` identity。Clean `b5b70ed` 单次 PASS：两个 identity 不同、typed alloc/dealloc `2/1`、mismatch/corrupt `0/0`，并严格核对 dealloc alignment `64`。它只证明这两个 bounded Result/Layout shape，不是 universal transformer coverage 或性能证据。
- `addd743...` instrumented Oxipng integration：在 `dea2321...` (`v4.0.3`) 的 detached copy 中加入 UniAlloc dependency/global allocator、runtime counters、symbol-visibility hook 和有限 build plumbing（`lock_api`、`[workspace]`与更新后的 `Cargo.lock`）；保存的 source/build patch 不包含生成的 `Cargo.lock` diff。该历史 run 在真实 Oxipng library/binary MIR 上实际应用 6 个 allocator-call replacements、846 个 semantic scopes 与 532 个 Drop rewrites，剩余 9 个 semantic unsolved、0 个 Drop unsolved。对一个 pinned PNG invocation，功能运行返回 0，输出 SHA-256 与先前 clean harness 相同。instrumented `main` 中的 recording window 观察到 `1058/1067` typed allocation events，即 `9915 bp` counter-truncated coverage（直接比率约 `99.16%`），fallback `9`，type-isolation corrupt slots `0`；pre-main 和 post-snapshot events 不在该 denominator 中。这是 exact `nightly-2022-07-01` 上的 source-bound historical functional evidence；不是 unmodified-app、whole-process coverage、general output equivalence、live-HEAD、object coverage 或 performance claim。
- `e466831...` pre-multi-owner-fix Oxipng runtime-class smoke：固定 Oxipng v4.0.3 只 build 一次、功能运行一次，输出 SHA 精确匹配；target-crate audit 为 `6` direct rewrites、`848` semantic scopes、`535` Drop rewrites、semantic unresolved `4`、Drop unresolved `0`。runtime 完整保存 `140/140` 个 type-class rows，typed allocation events 为 `1058/1067`，并将 5 个 compiler identities 绑定到 runtime lifecycle rows。它是精确 source-bound 的较早功能证据，但后续 `532435a` 修复了 aggregate Drop first-owner 误归因，所以不能把这里较高的 applied Drop 数继续当作当前安全 coverage。
- `9c74b95...6aae903...` safer-Drop + injected Oxipng address oracle：`9c74b95` source snapshot/pass 对 265 个 multi-owner Drop rows 明确 fail closed，只应用 278 个可表达的 Drop rows；另有 853 semantic scopes、6 direct rewrites、4 semantic fail-closed rows、131 个完整 runtime rows和 2 个自然 lifecycle matches。正确性修复后没有自然 same-layout pair，因此该项只作 diagnostic。单独标记的 injected oracle 使用 actual MIR 产生的两个同 module、同 `64B/align8` identity：producer type `15719177160194310719` alloc=2/hit=1，wrong type `11520851239810895908` alloc=1；地址关系为 producer `4349034560`、wrong `4349034624`、producer recovery `4349034560`，executed producer/wrong Drop identities 为 `2/1`，mismatch/corrupt/dropped 为 `0/0/0`，PNG hash 匹配。两次 actual build/run 都成功，但 collector 分别暴露“要求未执行 cleanup row 有 runtime row”和“强制自然 pair”两个具体 validator 缺陷；修复后只对 preserved raw 离线 replay，未执行第三次 run。Artifact SHA-256 `debb31038ab3912bdd07260de7facd394f8152ba086a4054e9b098b68b3aab0a`。它只支持一个 injected、compiler-identity-bound 地址序列，不是自然 Oxipng 隔离覆盖、全程序/全地址保证、安全证明或性能证据。
- `26051b9...` direct-local ownership hardening：`_local` scope 只允许 unprojected owner、单一 acyclic normal path 与 exact `Drop` 的 zero-alias proof；borrow/ref/raw pointer、copy/move、call argument、projection、overwrite、branch/loop/early exit 任一出现即令整个 same-type candidate group recovery-backed，raw `SizeAlign`/`exchange_malloc` 也始终 recovery-backed。actual two-crate `RUSTC_WRAPPER` regression 把 hidden `&mut owner` 传给 dependency 中的 `mem::replace`：fail-first 曾出现 typed `1/1`、fallback `1/1`、`raw_dealloc_no_metadata=1`；修复后 hidden lane 为 typed alloc/dealloc `1/2`、fallback alloc/dealloc `1/0`、raw `0`，positive aggregate 为 typed `4/4`、fallback `0/0`、raw `0`。这是 bounded conservative ownership proof，不是 general escape analysis。
- `15d892e...` **pre-ownership-hardening** Oxipng v4.0.3 functional run：单次成功 build/run 的 actual MIR audit 为 843 semantic scopes、278 Drop rewrites、265 multi-owner Drop fail-closed、2 semantic fail-closed、6 direct rewrites和 131 个 runtime rows。`PngData::clone` 的两个具体 site 都只把 `Vec<u8>` 认作 allocation owner 并实际应用；`Headers` 因多 owner、`Sender` 因 pass 无法绑定 dependency version 而 fail closed。853→843 是移除 standalone `Arc`/`Rc` handle Clone 假阳性 scope 后的语义精化，不是 coverage regression。功能输出 SHA-256 为 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`，injected address oracle 通过。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-arc-vec-15d892e-20260712/`。它不能 rebinding 到 `26051b9` 之后的源码，也不证明 universal compiler coverage 或 publication-grade performance。
- `af342f7...3dc1039...` **post-ownership-hardening Oxipng v4.0.3 one-shot**：应用 build/run 精确绑定 code-bearing source `af342f7e26dc4a5e132acc18d7f7a450009e6517`，两者 return code 均为 `0`，输出 SHA-256 精确匹配 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`。静态 target-crate MIR 分母分别为 6 个 direct rewrites、843 个 semantic-scope rewrites、278 个 Drop rewrites，以及 267 个明确 fail-closed candidates（265 multi-owner Drop + 2 semantic）；1121 个 applied semantic/Drop rows 中 23 个满足 `_local` zero-alias proof、1098 个保守使用 recovery。运行窗口另报 `1061/1070` typed allocation events、9 个 fallback allocations、129 个完整 type rows、0 dropped events、0 corrupt slots；`9915 bp` 只是这个动态事件 counter 的诊断性比率。Injected oracle 的地址序列为 producer `4379656256`、wrong type `4379656320`、same-type recovery `4379656256`，wrong-type 不复用、same-type 精确复用，oracle mismatch before/after 为 `0/0`、corrupt 为 `0`。全 workload 另有 `recovery_identity_mismatches=67`：runtime 均使用 allocation-time recorded identity 做 fail-closed correction，所以这是 `recovery_corrected_non_exact` compiler attribution，明确阻止 whole-app exact pairing claim；它与 267 个静态 fail-closed candidates 不是同一分母。`3dc1039` 修复 validator 将 bounded oracle 与后续 workload counter 解耦，并只离线重放 preserved artifacts，**没有重跑应用**。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-af342f7-20260712a/posthoc-preserved-run-validation.json`。这是一次性功能/诊断证据，不是 benchmark、universal coverage 或 publication-grade performance evidence。
- `04b8108...` **post-Oxipng compiler fail-closed hardening**：non-Clone receiver call 只检查首个 MIR receiver，factory/constructor 只检查 destination；对选定 receiver/destination 的现有 bounded supported-owner scan 必须恰好得到一个 owner，多 owner 或 unresolved 均 fail closed，不再继承任意 argument 或 textual return identity。真实 `RUSTC_WRAPPER` regression 对 `(Vec<u8>, String)` factory 与 `Vec<String>::resize` 各要求唯一 ambiguous audit row、零 applied/planned scope，常规程序结果保持正确；positive direct-local probe、pinned pass compile 与 15/15 unit tests 通过。该提交发生在 `af342f7` one-shot 之后，因此旧 Oxipng counts 不得 rebinding；该 `04b8108` hardening round 没有重跑 Oxipng。
- `2ff8770...` **recovery layout/auth fail-closed hardening**：TLS 与 process-visible recovery lookup 区分 `Missing / Mismatched / Exact`。live pointer 的合法但错误 layout/auth 在 FFI 与 `SemanticAlloc` dealloc 上均先于 stats/cache/delayed/raw path 被拒绝并保留 exact record；conservative/recovery-backed FFI single/split realloc ABI wrappers 同样在 copy/dealloc 前返回 null，payload 与 record 保持，exact retry 成功。两个新 regression、邻近 recovery tests、独立 review 与包含 652 个 UniAlloc tests、430 个 std-bench tests 的 full pre-commit suite 通过；这是 current-source correctness evidence，不是 exploit corpus 或性能结论。
- `0026dfe...` **active recovery-scope P0 hardening**：fail-first 中，scope 前创建的 raw pointer 被错误归因并进入 delayed-free（`occupied_slots=1`，应为 `0`）。修复后 `Missing` 走 unknown/raw fallback、释放并精确记录一次 fallback；moved raw realloc 保留 prefix、为 replacement 建立新 recovery identity，并只记录一次旧 raw release；`Exact` 使用已记录 identity，`Mismatched` fail closed 且不消费 record，允许 exact retry。Hosted 与 `fixed_heap` focused filters 各 `2/2` PASS；这是 correctness evidence，不是性能结论。
- `427583b...` **earlier hidden/consumed-owner P0 hardening + source-bound Oxipng one-shot**：actual-`RUSTC_WRAPPER` fail-first regressions 中，hidden custom ADT 与 consumed by-value factory/receiver 的冲突路径各从 mismatch `1` 收紧为 audit-only fail-closed、mismatch `0`；same-owner factory/receiver positive controls 仍实际 rewrite 且 mismatch `0`。当时的 instrumented Oxipng v4.0.3 在 pinned `nightly-2022-07-01` 上一次 build、一次功能运行均 PASS，输出 SHA-256 匹配；source `scoped_status=""`，scoped fingerprint 为 `5f36c0a7f1bad4284071cd3a8f6d50bb7a894282e5f76726e6f2b095d5bc49e8`，pass-source SHA-256 为 `aedef38625f6096e3f5875b35f3d89f839709ac3277d7c79e4df6756da8a1373`。target-crate audit 为 6 direct、383 semantic、320 Drop applied 和 581 fail-closed；runtime 为 64 type rows、corrupt `0`。bounded injected address oracle PASS（wrong-type 不复用、same-type 复用、oracle mismatch/corrupt `0/0`），但 whole run 有 13 次 recovery correction，因此仍是 `recovery_corrected_non_exact`，不是 whole-app exact pairing。13 不能归因或 rebinding 成旧 `af342f7` 67 次 correction 的已修复子集；这只是 pinned instrumented functional/diagnostic evidence，不是 unmodified app、benchmark、性能或 publication-grade coverage claim。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-427583b-20260712a/oxipng-realapp-repro-summary.json`。
- `572bfab...` **capacity-only Vec outer-owner coverage**：仅对 `reserve/reserve_exact/try_reserve/try_reserve_exact/shrink_to/shrink_to_fit`，且 receiver ADT path 精确为 `std::vec::Vec`/`alloc::vec::Vec` 时，使用 direct outer `Vec` identity；`resize/extend/push/clone_from/Drop/factory` 保持完整 owner-graph 或 consumed-owner fail-closed。current-rustc `Vec<String>` 与 same-layout `Vec<Vec<u8>>` 获得 distinct nonzero compiler/runtime IDs，wrong-type 不复用、same-type 精确复用并有 1 次 cache hit，mismatch/corrupt 为 `0/0`；`Vec<String>::resize` 仍 ambiguous。`427583b` artifact 早于该提交，不能 rebinding 到 `572bfab`；后续 current-content run 单独列于下方。这不是性能证据。
- `576df61...9bb9f8d...f8f0d90...` **source-bound Oxipng v4.0.3 run at `576df61`**：首次 pinned old-nightly build 暴露 `GenericArg::as_type` 不兼容；最小 cfg adapter 后以 byte-identical 内容提交为 `9bb9f8d`，独立 review 为 `APPROVE`，old-nightly compile、current actual-rustc Box probe 与 embedded tests `16/16` 均 PASS。功能 collection 发生于 HEAD `576df61`、adapter 尚为稳定 uncommitted 内容时；`9bb9f8d` 与 collection 的 60 个 scoped inputs 全部一致。`f8f0d90` 后为 58/60，唯一差异是 post-run summarizer 及其 test，compiler pass 与 allocator/runtime inputs 仍 byte-identical；这不表示 full working tree clean。Oxipng build/run return `0`、输出 hash 精确匹配；audit 为 6 direct + 383 semantic + 320 Drop rewrites，并在 real functions 中观察到 6 个 actual `Box<[u8]> -> Vec<u8>` ownership-transfer rewrites，另有 575 个 fail-closed candidates。runtime 为 typed allocations `904/1070`、64 rows、dropped/corrupt `0/0`；injected wrong-type non-reuse/same-type reuse oracle PASS，但 whole-run 13 次 correction 使 pairing 仍为 `recovery_corrected_non_exact`。Enriched artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-576df61-20260712c-enriched/`，summary SHA-256 `cd14bf7bdcbff8fe99d5fc6d97888ce2065050c527b423baa63b856857bdd43b`；它由 preserved raw audits 确定性重放，精确记录 6 candidates/6 applied/6 selected rows，且与 6 direct rewrites 分开计数，**没有重跑应用**。原 `...20260712b` summary SHA `054b...` 保持不变。这是一次功能运行，无 timing loop，不支持 performance、论文百分比、universal 或 natural-app isolation coverage。
- `6d955c0...38b8b59...` **source-bound Oxipng ownership-transfer run at `38b8b59`**：`6d955c0` 保留 optimized `vec!` 路径中 immediate `Box<[T; N]>` allocation owner，`38b8b59` 识别实际 `into_vec` callsite 的 optimized storage markers。成功 run 在 start/end 均绑定 HEAD `38b8b59c9748691d07b0ac9c0ad7c6adf94396cf`、pass SHA-256 `c2c83bec49001c0b40d045a32daaed10d4094afb7eea2415685670a756fe6d10` 与 60-file scoped fingerprint `8609ff8ff261f27998779613eefb739ddfcc0c682ac1a76ddefaf9dadbd2eb29`；静态 transfer candidates/applied 为 `6/6`，动态 workload delta 为 attempted/applied/rejected `1/1/0`。实际执行点为 `png::PngData::output`，旧 owner 是 `Box<[u8; 8]>`，basis 为 `exact_immediate_box_array_unsize`，随后 pointer-preserving rebind 到 `Vec<u8>`；功能输出 SHA-256 为 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`。成功 artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-38b8b59-20260712d-success/`，summary SHA-256 `292c8ff8479887f4bfa90fc58b48ce60326e27f89a9f26baf7ac50cce1a0e113`。此前 `.../oxipng-ownership-runtime-6d955c0-20260712c-failed-after-first-repair/` 保持历史失败状态：功能进程虽返回 0，但动态 transfer 为 `1/0/1`，不得写成 PASS。这是单次 bounded diagnostic functional evidence，不是 whole-program coverage、benchmark、性能或论文百分比。
- `a51960d...` **historical presentation bundle at `a51960d`**：source HEAD `a51960d92a7c72deabaf25fc23e985c3b26c09a5`、scoped fingerprint `f23eda54db834f2a81475ea84f28d70a55a54fb5502c379d7b36dc355a6e1d8f`。一次 pinned instrumented build/run 均 PASS，输出 SHA `565f253e...`；static audit 将 6 direct、383 scope、320 Drop、12/12 ownership-transfer candidate/applied 和 575 fail-closed 分开；dynamic transfer 为 `3/1/2`。Injected oracle wrong-type non-reuse、same-type reuse 且 mismatch/corrupt `0/0`；whole-run 13 次 corrected mismatch 单列，因此 pairing 仍是 `recovery_corrected_non_exact`。Full cargo 同一 source 通过 660 UniAlloc unit + 430 std-bench tests。Artifact summary SHA `bc6f32805e58cb021223dde2e01a91887cbe36e653f5ff73257823349a12e685`。Slide 16 应使用这一 source-bound bundle，而不是跨 revision rebinding 旧 counts；它仍不是 natural-app universal isolation、whole-program coverage、安全证明或性能证据。
- `ed188ca...4cd0d7f...997e840...` **post-bundle type-isolation safety and compiler attribution hardening**：新增 String-to-Vec ownership transfer 后跨线程 Drop 的 hosted/fixed-heap regression，要求 payload 保持、旧 String identity 不复用、精确 Vec identity 可复用且 mismatch 为 0；exact `Vec::with_capacity` destination 现在只归因到 nested `Vec<Vec<u8>>` 的 outer Vec backing，current/legacy actual-rustc probe 均通过；exact `Result<T,E>` factory 只允许 `Ok(T)` 作为返回 allocation identity，`Err(E)` 仅作为 fail-closed hazard。Result A/B/C actual-rustc probe 分别验证 Err-only 不产生 scope、`Ok(Vec)` 实际 rewrite、Ok/Err owner 冲突保持 ambiguous，current/legacy 与 clean-tree Clone/Layout gates 均 PASS，pre-commit full suite 为 660+430。`997e840` 后没有重跑 Oxipng，因此不得声称旧 bundle 的 13 次 correction 已归零，也不得把旧 counts 重新绑定到新 HEAD。
- `681398e...` **borrowed slice-iterator hazard-only coverage**：只在 by-value hazard scan 中把来自 `core` 且 DefPath 精确为 `slice::Iter`/`IterMut` 的 borrowed iterator 视为 non-owner，使普通 Rust `input.iter().copied().collect::<Vec<u8>>()` 在 current 与 `nightly-2022-07-01` actual-rustc probe 中获得实际 Vec semantic-scope rewrite。runtime 证明 payload 正确、wrong String identity 不复用、exact Vec identity 可复用，transfer 为 `2/2/0`，fallback/raw/mismatch/corruption 均为 `0`；custom raw-pointer iterator 仍 unresolved，含 `IterMut` 与 `IntoIter` 的 Zip Drop 仍为 `2` 条 unresolved、`0` applied，general/Drop/Clone/transfer scans 未扩大。Full pre-commit suite 为 660+430。该提交后未重跑 Oxipng，不得把历史 458-row unresolved 分母或旧 bundle counts rebinding 到新 HEAD；这是一条 bounded actual-rewrite 与隔离效果证据，不是 universal coverage 或性能证据。
- `9240fc6...` **memory-tagged ownership-transfer P0 closure**：fail-first 的普通 Rust `String::into_bytes` 在 policy flags `129` 下虽有 actual MIR rewrite，却只有 runtime transfer `1/0/1`，并出现旧 String identity 复用原地址、新 Vec identity 不复用。修复后 recovery auth 与 matching software memory-tag auth 只替换 `type_id` 并一起提交；TLS/global fast+overflow 全表做 `0/1/>1` exact classification，duplicate、cross-domain duplicate、layout/metadata/auth 不一致均 fail closed，recovery commit 失败时 tag 回滚。current 与 `nightly-2022-07-01` actual-rustc probe 均为 `1/1/0`，payload/pointer/capacity 保持，wrong String non-reuse、exact Vec reuse、tag cleanup 均 PASS，fallback/raw/mismatch/corruption 为 `0`；hosted/fixed-heap、local/global、rollback/duplicate regressions及独立 review PASS，full suite 为 662+430。证据只覆盖 pointer-preserving `String -> Vec<u8>` actual-rewrite contract；不证明所有 ownership transfer、非法并发线性化、Oxipng 最新 HEAD coverage 或性能。
- `2b33401...` **cross-thread tagged composition closure**：把上述 runtime contract 与 cross-thread recovery 合并到同一条普通 Rust actual-rustc 路径：主线程创建 `String`，worker 调用未手写 metadata 的 `String::into_bytes`，scope rows 同时携带 flags `129` 与 placement `32768`。current 与 `nightly-2022-07-01` 各一次功能运行均为 transfer `1/1/0`，payload/pointer/capacity 保持，wrong String non-reuse、exact Vec reuse、tag cleanup reuse 均 PASS，fallback/raw/mismatch/corrupt/dropped 为 `0`；独立 hosted/fixed-heap 手写边界 regression 各 `1/1` PASS，full pre-commit suite 为 662+430。transfer audit 的 placement 来自 explicit manual policy；这不是自动 escape-analysis、真实外部应用、benchmark、universal coverage 或性能证据。
- `46d5aaa...` **latest source-bound Oxipng one-shot before module-id hardening**：一次 pinned Oxipng v4.0.3 instrumented build/run 均返回 `0`，输出 SHA `565f253e...` 匹配。target-crate audit 为 6 direct、367 semantic、320 Drop、12/12 ownership-transfer candidate/applied，并明确保留 529 个 fail-closed rows；runtime 为 59 type rows、whole-run mismatch `0`、corrupt/dropped `0/0`，dynamic transfer `3/1/2`，injected oracle wrong-type non-reuse / same-type reuse PASS。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-46d5aaa-20260712e/`，summary SHA `f5b8ad7c2fbb0a9f28ccbe052ac7dca6d7c40c98c3482c61d09b6fefcc4e274b`。这是单次功能/诊断 evidence，无 timing loop；不支持 natural-app universal isolation、whole-program coverage 或性能 claim。
- `0704852...` **multi-crate module isolation closure**：此前 external crates 共用固定 module id；现在 compiler pass 优先用 crate name + rustc `-C metadata`，无 metadata 时用 canonical primary input，再以完整 rustc argv 兜底。两个不同 Cargo package 故意使用相同 rustc crate name、相同源码与同一 compiler type id `13297006753675728434`，actual-rustc run 必须得到两个不同 module ids、wrong-module non-reuse、same-module reuse，mismatch/corrupt `0/0`；两个无 metadata 的 direct-rustc 同名 crate control 也必须分离。current/legacy pass compile、738 allocator + 432 std-bench functional tests与 independent review 均 PASS。该提交晚于 `46d5aaa` Oxipng run，不能把新 module-id 算法 rebinding 到旧 application counts；64-bit hash 仍受 trusted-metadata/collision boundary 约束，且不是性能证据。
- `eadfa9c...` **lifetime-hint cache-key safety regression**：固定 type/module/flags/placement，只改变显式 lifetime hint；`0x11` 释放的地址不得被 `0x22` 复用，返回 `0x11` 时必须精确取回原地址。Hosted 与 `fixed_heap` 各 `1/1` PASS，fallback allocation/deallocation、recovery mismatch、corrupt slot 均为 `0`。这是手工 metadata 生命周期上的 covered-path allocator evidence，不是 compiler-derived lifetime coverage、universal isolation 或性能证据。
- `a379f23...` **placement-hint cache-key safety regression**：固定 type/module/flags/lifetime/layout，只改变显式 placement hint；`0x21` 释放的地址不得被 `0x22` 复用，返回 `0x21` 时必须精确取回原地址。Hosted 与 `fixed_heap` 各 `1/1` PASS，fallback allocation/deallocation、recovery mismatch、corrupt slot 均为 `0`。这是手工 metadata 生命周期上的 covered-path allocator evidence，不是 automatic compiler placement inference、universal isolation 或性能证据。
- `05d18be...` **current-thread duplicate-quarantine fail-stop**：指针进入 delayed-free TLS quarantine 后，第二次 dealloc 即使清空 occupancy hint、且省略 `FLAG_DELAYED_FREE` 试图走 raw/compiler fast path，也会在 recovery consumption、stats、cache mutation 或 raw free 前 fail-stop；quarantine/accounting 不变且 type cache 不被污染。Hosted/fixed-heap focused、邻近 delayed-free `11/11`、full suite `663+430` 与 independent verifier 均 PASS。边界仅为 current-thread TLS quarantine ownership；不证明无 memory-tagging 的 cross-thread duplicate detection，也不是性能证据。
- `ded36de...` **actual-rustc `Box<str> -> String` ownership pairing**：普通 Rust `Box<str, Global>::into_string` 的唯一 candidate 实际应用 `1/1`，runtime transfer 为 `1/1/0`；compiler-derived Box/String identity 均非零且不同，pointer/payload/length/capacity 保持，旧 Box identity 不复用、精确 String identity 复用，fallback/raw/mismatch/corrupt/dropped 均为 `0`。Hosted/fixed-heap regressions覆盖 accepted、wrong-old-ID、missing record 和 authenticated memory-tagged 路径；邻近 current actual-rustc transfer probes、embedded pass tests `16/16`、legacy pass compile、full suite `663+430` 与独立 review 均 PASS。它是 ordinary-Rust actual rewrite 与 bounded isolation-effect evidence，不是 external-app、universal coverage、benchmark 或性能 claim；没有重跑或 rebinding Oxipng。
- `e6dc7d6...` **process-visible cross-thread delayed-free ownership**：8 shards × 32 slots 的 bounded registry 在 memory-tag validation、recovery consumption、stats/cache mutation、copy/in-place realloc 和 raw free 前发布 pending/quarantined pointer ownership；panic-before-TLS-publication 由 RAII rollback，release/eviction/valid thread-exit 认证后 unregister。真实 `GlobalAlloc::dealloc/realloc`、raw entry、different-alignment move 和 explicit `SemanticAlloc` same-class realloc 均在触碰 pointer 前 fail-stop。Hosted/fixed-heap delayed-free filters 各 `15/15`，full workspace 为 `668+430`，independent verifier APPROVE。容量满或 oversized 走 authenticated immediate release，不制造 hidden TLS owner。边界是“完成 process-visible registration 后”的 pointer ownership，不是通用并发 double-free 证明或性能证据。
- `5408c04...` **actual-rustc `CString::into_bytes_with_nul -> Vec<u8>` ownership pairing**：普通 Rust 唯一 candidate 实际应用 `1/1`，runtime transfer `1/1/0`；258-byte pointer/payload/capacity 保持，compiler-derived CString/Vec identities distinct nonzero，旧 CString identity 不复用、精确 Vec identity 复用，fallback/raw/mismatch/corrupt/dropped 全 `0`。Hosted/fixed-heap direct regressions各 `1/1`，覆盖 accepted、wrong-old-ID、missing record 与 authenticated memory-tagged 路径，independent review APPROVE。它只证明 exact `CString -> Vec<u8, Global>` bounded actual rewrite/isolation effect；不支持其他 CString API、custom allocator、external-app、benchmark 或性能 claim。
- `f007c7b...` **realistic multi-module actual-`RUSTC_WRAPPER` application**：一个生成的 Cargo 应用跨 `ingest/transform/storage/handoff` 模块执行，要求 4 条实际 allocation scope、4 个 distinct nonzero callsites、3 个 distinct nonzero String/Vec/Box type IDs，以及 `String::into_bytes` transfer `1/1`。两个 runtime oracle 分别验证 String→Vec 和 same-layout Box/Vec 的 wrong-identity non-reuse / exact-identity reuse；fallback/raw/mismatch/corrupt/dropped 为 `0`。同一次 run 不提供 manual placement：与真实 `thread::spawn(move || ...)` 同 MIR body 的 Vec 自动得到 placement `0x8000` / `auto_cross_thread_escape`，local control 为 placement `0` / `default`；cross-to-local 不复用，两类各自精确复用，窗口 typed alloc/dealloc/hit/insert 为 `3/4/2/4`。这是一个 generated multi-module app 的 bounded actual-rewrite/placement-isolation regression，不是 arbitrary external app、whole-program、universal/natural-app isolation 或性能证据。
- `38dfe17...522c7f5...` **pinned-nightly CString compatibility + exact `str` split coverage**：`38dfe17` 对 paper-pinned pre-release 1.64 nightly 恢复 `alloc_c_string` feature gate，同时不把已稳定 gate 带到 current rustc。`522c7f5` 只在 by-value hazard scan 中把 exact `std/core::str::Split` 与 `SplitInclusive` 视为 borrowed non-owner；current/pinned actual-rustc regression 对两条 `collect::<Vec<&str>>()` 各实际应用一条 scope，wrong-type 不复用、exact type 复用，custom raw-pointer iterator 仍 fail closed，fallback/raw/mismatch/corrupt 全 `0`。不支持任意 iterator、任意 rustc ABI、coverage percentage 或性能 claim。
- `41205d3...` **delayed-free test-state cleanup**：只在 regression tests 中加入 `SemanticStateCleanup`，并在测试暂时取出 delayed-free slot 时用 `PendingGlobalDelayedFreeOwnership` 保持 process-visible ownership，避免 test-order/state leakage；不据此主张新的 production behavior 或性能结果。
- `41205d3...` **current-source Oxipng v4.0.3 one-shot**：pinned `nightly-2022-07-01` build/run return code 为 `0/0`，输出 SHA-256 `565f253ed6a0ffd51eefa1a25ca1ad217287d19a0777c8271c6686192a1988ff`。target-crate audit 为 direct/scope/Drop `6/369/320`、static transfer `12/12`、fail-closed `527`；runtime transfer `3/1/2`、typed `873/1070`、fallback `197`、58 rows、corrupt/dropped `0/0`，injected wrong-type non-reuse / same-type reuse PASS。whole-run mismatch 为 `1`，状态明确是 `recovery_corrected_non_exact`，所以不能称 whole-app exact pairing。Artifact：`.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-41205d3-20260712a/`，summary SHA-256 `c95808092c197b01399d4723e52e47c92478c8dafd5c498148fa70560c2dc7f4`。与旧 `46d5aaa` summary 的 `367/529` 直接算术对照仅为 exact `+2` applied scope / `-2` fail-closed，且与两个实际 `Split<char> -> Vec<&str>` rows 一致；这是 direct inference，不是 timing、performance、coverage percentage、whole-program/universal 或 natural-app isolation claim。所有数字只绑定 `41205d3`，不得跨 revision rebinding。
- `e243779...` **PAC × hugepage policy composition regression**：同一 type/module/layout 的 ordinary `type-isolated + PAC` policy 与 `type-isolated + PAC + hugepage-metadata` policy 不得交叉消费 cache entry，随后两种 policy 各自必须精确复用；hosted 与 `fixed_heap` targeted test 各 `1/1` PASS，fallback allocation、identity mismatch、PAC/software-auth failure 与 corrupt slot 均为 `0`。测试要求实际经过 hardware PAC 或安全 software fallback 的 sign/verify，但在当前主机没有真实 PAC/hugepage backing 时只证明安全 fallback 和 policy-domain identity，不证明硬件路径或性能。
- `5d0822a...` **cross-thread authenticated five-policy composition regression**：同一条 allocator lifecycle 组合 type isolation、memory tagging、delayed free、hugepage metadata、PAC 与 process-visible cross-thread recovery。foreign worker 在错误 Drop identity 下释放时，allocation-time authoritative metadata 必须胜出且只消费一次 recovery/tag；duplicate free 在 stats/cache/registry mutation 前 fail-stop，quarantine release 后只发布正确 hugepage-domain cache entry，ordinary domain 与错误 identity 均 miss，exact identity/domain 才可复用。Hosted 与 `fixed_heap` targeted 各 `1/1` PASS，PAC hardware 或安全 software fallback 的 verification 增加且 auth failure 为 `0`；这是 test-only composition coverage，没有发现新的 production defect，也不证明真实 PAC/hugepage hardware 或性能。
- `7ec42d4...` **cross-thread authenticated split-realloc composition regression**：foreign worker 通过 hints split-metadata FFI 提供 stale old identity，同时 allocation-time old policy 组合 type isolation、memory tagging、delayed free、hugepage metadata、PAC 与 process-visible recovery，replacement 使用 distinct ordinary-domain identity。测试要求 old recovery authority 只审计一次 mismatch，old recovery/tag 事务性消费并发布 new recovery/tag，moved-from storage 以 authenticated old hugepage identity quarantine；duplicate old free 在 stats/cache mutation 前 fail-stop，old/new identity 与 domain 均只能精确复用，最终 recovery/tag/quarantine 清空。Hosted/fixed-heap exact 各 `1/1` PASS；没有发现 production defect。该证据不覆盖任意 size/error path、真实 PAC/hugepage hardware 或性能。
- `b3e793e...` **exact `Arc::new` outer-allocation identity**：compiler pass 只在 alloc-crate exact DefId/DefPath、exact `Arc<T, Global>` destination、唯一 `T` 参数与 destination payload 一致时选择 outer `Arc<T>` identity；不扩展到 `ArcLike`、`new_in`、`Rc` 或任意 constructor。Current 与 `nightly-2022-07-01` actual-`RUSTC_WRAPPER` probe 均为 3 次 typed alloc/dealloc、1 次 exact cache hit、3 次 insert，same-layout `Arc<ProducerWithVec>` / `Arc<ConsumerWithBox>` 得到 distinct nonzero IDs、wrong-ID non-reuse、exact-ID reuse，fallback/raw/mismatch/corrupt 全 `0`；custom `ArcLike::new` 保持 ambiguous fail closed。旧 `41205d3` Oxipng artifact 中的 7 个 Arc constructor fail-closed rows 仍只能作为发现该缺口的历史输入；本提交后没有重跑 Oxipng，不能宣称 external-app counts 已改变，也不是 universal Arc safety 或性能证据。
- `1bd0c9d...` **exact `Rc::new` outer-allocation identity**：compiler pass 只在 alloc-crate exact DefId/DefPath、exact `Rc<T, Global>`（current）或 `Rc<T>`（paper-pinned nightly）destination、唯一 `T` 参数与 destination payload 一致时选择 outer `Rc<T>` identity；不扩展到 `RcLike`、`new_cyclic`、`new_in`、`Weak` 或任意 factory。修复前 exact `Rc::new` 在 current/pinned probe 中均为 audit-only ambiguous，typed alloc/dealloc `0/0`、fallback/raw `3/3`，same-layout wrong type 会立即复用地址；修复后两种 toolchain 均为 typed alloc/dealloc `3/3`、cache hit/insert `1/3`、wrong-ID non-reuse、exact-ID reuse，fallback/raw/mismatch/corrupt 全 `0`。这是 bounded actual-rewrite 与隔离效果证据；未重跑 Oxipng，不支持 universal Rc safety、cross-thread 或性能 claim。
- `715ba13...` **exact `HashMap::with_capacity` outer-table identity**：compiler pass 只接受 std-crate exact DefId/DefPath、exact `HashMap<K, V, RandomState[, Global]>` destination 与唯一 `usize` argument；不扩展到 hashbrown、HashSet、IndexMap、`with_hasher`、`with_capacity_and_hasher`、`new_in` 或本地同名 constructor。Current 与 `nightly-2022-07-01` actual-`RUSTC_WRAPPER` probe 对 same-geometry、但 key 内分别含 Vec/Box nested owner 的两个 HashMap 产生 distinct nonzero outer IDs；两边均为 typed alloc/dealloc `3/3`、wrong-ID cache-hit delta `0`、exact-ID delta `1`、fallback/raw/mismatch/corrupt `0`，custom same-name 保持 ambiguous fail closed。稳定 HashMap 不暴露 deterministic raw-table address，因此该 oracle 只证明本次运行的 identity-directed cache selection，不证明 universal address behavior、external-app coverage 或性能。
- `39c827b...` **exact `HashSet::with_capacity` outer-table identity**：compiler pass 只接受 std-crate exact DefId/DefPath、exact `HashSet<T, RandomState[, Global]>` destination 与唯一 `usize` argument；不扩展到 `with_capacity_and_hasher`、`with_hasher`、allocator-specific constructor、hashbrown、IndexSet 或本地同名 helper。修复前 current/pinned actual wrapper 均把 outer HashSet 与 nested Vec/Box element owner 判为 ambiguous，typed alloc/dealloc `0/0`、fallback/raw `3/3`；修复后两种 toolchain 均为 typed alloc/dealloc `3/3`、cache hit/insert `1/3`、wrong-ID hit `0`、exact-ID hit `1`，fallback/raw/mismatch/corrupt 全 `0`。这是 bounded actual-rewrite 与 cache-selection evidence；稳定 HashSet 不暴露 deterministic raw-table address，因此不支持 universal address behavior、external-app coverage 或性能 claim。
- `8bc2809...6700ca1...` **plain type-cache hash-collision fail-closed**：fail-first tests 强制 inline 与 linked plain cache 使用相同 64-bit cache key 和相同 type id、但不同 module/flags/lifetime/placement；旧实现会返回 foreign pointer。修复后两个路径都保存并精确比较不含 callsite 的 compact allocator-visible identity，linked colliding identities 在 bounded probe table 中占不同 slot。Hosted/fixed-heap collision tests 各 `2/2` PASS，扩大 type-cache family 为 `54/54` 与 `50/50`；64-bit 下代价是约 `+1040 B/thread` TLS。它只闭合 runtime cache-key collision，不能防止完全相同 metadata 的 spoofing/compiler type-id collision、probe exhaustion、UAF 或 hash DoS，也没有性能 claim。
- `d87d5e0...25d316c...` **semantic-cache footprint reduction with saturation repair**：hosted cold bucket depth `8→4`，memory-tag/recovery fast tiers `256→128`，empty records 转为 demand-zero representation；`fixed_heap` 保留原容量。独立 review 确定性触发了 matching bucket 满后错误溢出到邻框的问题，`25d316c` 修复为 matching depth/per-bucket/aggregate saturation 直接 bypass，而 distinct hash collision 继续 bounded probe。Hosted `308/308`、fixed-heap `277/277` type-isolation filters 与新增 512 KiB aggregate-cap no-replacement regression PASS。另一个 source-bound 64-thread diagnostic 以 `318b66c` 为 baseline、`25d316c` 为 current，各 3 次交错测量；ready/peak/idle/max RSS median 从 `9.578/66.969/71.188/71.922 MiB` 变为 `7.500/64.953/69.141/69.484 MiB`，但 max range overlap。它只支持方向性 regression evidence，不得生成论文百分比。
- `2e3bc4c...` **plain linked-cache bounded-probe exhaustion regression**：4 个 distinct exact identities 以强制相同 lookup key 填满 bounded probe window，后续 8 个 colliders 必须 fail closed；测试要求 rejected push 不写 node header、不改 64 个 slots/retained-byte accounting，lookup 不返回 foreign pointer，且 4 个 retained pointer 最终只能由 exact owner 取回。Hosted/fixed-heap targeted 各 `1/1`、type-cache family `55/55` 与 `51/51` PASS。该 test 没有复现新的 production defect，只支持单线程内部 collision/exhaustion 的 fail-closed 行为；不证明 hash-DoS、compiler type-ID 唯一性、identical-metadata spoofing、跨线程或性能性质。
- `19ffb71...` **latest accepted Oxipng v4.0.3 actual rewrite/isolation one-shot**：前一次并行 collection 因 `type_isolation.rs` 在运行中改变而被 source-binding gate 正确拒绝；修复提交后冻结 claim-bearing source，再执行且仅执行一次 build + 一次 functional invocation，无 retry/timing。start/end HEAD 均为 `19ffb710752466a140067034650190dfdad60328`，scoped status 为空，fingerprint 为 `13f72a5a...`；build/run `0/0`，输出 SHA `565f253e...`。target-crate direct/scope/Drop 为 `6/310/320`，transfer candidate/applied/selected 为 `12/12/12` 且 selected identities 全部非零，fail-closed semantic/Drop 为 `516/119`（117 multi-owner Drop）。runtime typed alloc/dealloc `871/860`、fallback `199/160`、transfer `3/1/2`、53 rows、corrupt/dropped `0/0`；injected wrong-type non-reuse / exact-type reuse PASS。whole-run mismatch `1`，状态仍为 `recovery_corrected_non_exact`，不能称 whole-app exact pairing。Artifact acceptance SHA `efa66ec1...`。这是 instrumented pinned application 的 bounded functional/diagnostic evidence，不是 benchmark、性能、universal/natural-app isolation 或 whole-program coverage claim。
- `9a02767...ab98075...` **current-source factory provenance + lifecycle fail-closed hardening**：任意 local/platform/dependency factory 只因返回 `Vec`、`String` 或 `Result` 不再获得 caller-side typed attribution；没有 exact constructor/body allocation proof 就只保留 audit row。新增 exact `Box::new` 与 `String::with_capacity` matcher 需要 alloc/std DefId/path、exact destination 与 argument shape，custom same-name/allocator-specific 路径仍 fail closed。Current 与 `nightly-2022-07-01` opaque dependency probe 的 direct/Result typed allocation 均为 `0`，exact `Vec::with_capacity` control 仍为 typed alloc/dealloc `1/1`。`ab98075` 又修正 end-to-end security validator：current toolchain 逐行接受 86 个 audited fail-closed rows（其中 opaque `producer_box/consumer_box` 为 `8/4`），同时 exact inner scope 保持 typed alloc/dealloc `12/12`、wrong-type non-reuse、exact-type reuse、mismatch/corrupt `0/0`。Nested-unwind probe 现在以 exact outer `Vec::extend` receiver 为证据：inner `Vec::extend_from_slice` panic cleanup 后 depth 恢复到 `1`，outer return 后为 `0`，outer allocation/Drop identity 配对；direct-local hidden replacement 若没有 allocation recovery record，later non-local Drop 必须保持 raw，不能从 surrounding scope 伪造 typed attribution。这是 bounded actual-rewrite/fail-closed evidence，不是 arbitrary factory completeness 或 universal unwind proof。
- `3ccd464...` **current-source metadata-segregated collision/tamper fail-stop**：每个 occupied entry 的 keyed structural authenticator 绑定 lookup key、完整 callsite-agnostic allocator-visible identity、policy、pointer、layout、optional PAC/software auth 与 metadata。Inline/materialized 强制 key collision 只有 exact identity 才可命中；protection/auth downgrade、identity/policy、pointer（含 null）、size/align 篡改均 fail stop，full-bucket replacement/retained-byte projection 在读取 eviction candidate 前先认证。Hosted/fixed-heap collision、structural-tamper、full-bucket projection、metadata-segregated 与 footprint regressions PASS，且 entry footprint 不增加。它只支持 bounded internal cache integrity，不证明 compiler type-ID 唯一、任意 metadata corruption、UAF 消除或性能。
- `79d0184...61233b6...` **recovery-layout 与 Unix TLS publication fail-closed**：`GlobalAlloc::dealloc` 遇到 live recovery record 但 caller `Layout` 不精确匹配时，在 raw/fallback/cache mutation 前拒绝并保留 record，exact-layout retry 才消费并按原 identity/layout 发布。Unix TLS 先成功建立 pthread destructor ownership 并写入 pthread slot，再发布 fast Rust TLS pointer；注入 save failure 时 pointer 保持不可见并交回 reclamation。两者是 bounded correctness evidence，不是 forged-pointer、Windows FLS 或性能结论。
- `08a1bbf...` **exact `String::from(immutable &str)` actual rewrite**：matcher 只接受 exact core `From::from` DefId、exact alloc `String` destination 与唯一 immutable `&str` source；其他 `From`/source shape 与任意 String factory 保持 audit-only fail closed。Current 与 `nightly-2022-07-01` actual-wrapper 均为 typed alloc/dealloc `3/3`、wrong-type non-reuse、exact reuse，fallback/raw/mismatch/corrupt 全 `0`。这是 bounded functional evidence，不是 universal String、external-app 或性能 claim；Oxipng 未重跑，`19ffb71` 仍 stale 且不可 rebinding。
- `c477339...6640305...95d3d8a...cfd887e...` **current platform/recovery/generated-app closure**：Redox `--tests` 与 Linux `--lib --tests` type-check PASS，`mincore` 仅在支持目标编译且 Linux/Darwin residency-byte ABI 可移植；Redox runtime 仍缺外部 linker/runner。Cross-thread `GlobalAlloc` wrong-layout 在 raw/cache mutation 前拒绝并保留 record，exact retry 才消费。Current generated multi-module actual wrapper 对 exact `Box<[u8]>` PASS：Box/Vec wrong-type non-reuse、exact Box reuse、raw/fallback/mismatch/corrupt `0`；`Box<[String]>` 保持 audit-only。仅为 bounded functional evidence，无 universal app、论文百分比或性能 claim；Oxipng `19ffb71` 仍 stale 且不 rebinding。
- `465234c...504ed10...` **custom ADT discovery 与 fail-closed ownership boundary**：`465234c` 的 positive-only 尝试扩大了 `Buffer { bytes: Vec<u8> }` destination owner discovery，但独立 review 用“返回既有 `Buffer`，函数内部仅分配/释放无关 `String`”的 actual-wrapper 负例测得 mismatch `1`，因此该版本不能作为安全 rewrite 证据。`504ed10` 修复后，custom aggregate factory、纯 passthrough 与带无关 allocation 的 passthrough 都只保留 unresolved audit row；Current 与 `nightly-2022-07-01` 仍实际 rewrite 精确的内部 `Vec::with_capacity`、无关 `String::with_capacity` 及 backing `Vec` Drop，runtime typed alloc/dealloc `2/2`、fallback/raw/mismatch `0`，两个 passthrough mismatch delta 均为 `0`。multi-owner 仍为 ambiguous audit-only，raw-pointer unresolved audit-only，borrowed/`PhantomData` 不 lower 或保持 unresolved。该证据证明控制层捕获并修复了一次真实误归因；不证明 arbitrary wrapper allocation provenance、external-app coverage 或性能。

Slide 14 用 `5eb25f5`、`32c3c5b`、`ed188ca...2b33401`、`0704852`、`f007c7b`、`9a02767`、`08a1bbf` 与 `cfd887e` 说明“ordinary Rust source → exact/fail-closed MIR provenance → allocation/transfer/Drop/unwind pairing → bounded reuse effect”；不要把候选行或代码存在当 actual rewrite。Slide 15/16 必须把四种证据分开：**static compiler rows**（candidate/applied/fail-closed）、**runtime transfer/allocation events**（各自窗口和 denominator）、**bounded isolation oracle**（明确执行的 adversarial lifecycle）和 **whole-run recovery corrections**（requested identity 与 allocation-time record 的非精确配对）。四个 denominator/时间窗口不得互换；Slide 16 以 `19ffb71` bundle 作为最新 accepted source-bound real-app actual rewrite、applied transfer gate 与 bounded oracle，且必须同时展示 whole-run mismatch `1` / `recovery_corrected_non_exact`。该 artifact 早于 current development source（例如 `c477339`、`6640305`、`95d3d8a` 与 `cfd887e`），所以只能标记 historical/source-bound，不能称 current-source 或 rebinding；`41205d3...`、`46d5aaa...`、`a51960d...`、`38b8b59...`、`576df61...9bb9f8d...`、`427583b` 与 `af342f7` 同样只保留各自 revision 的历史证据。地址级 effect 只能引用明确标记的 injected oracle 或独立 adversarial lifecycle regression，不能从自然 Oxipng counter 推导；whole-run correction 也不能被 oracle 的零 delta 隐藏。

Slide 16/B18 只使用下面这一张 denominator/status 表，不把不同 revision 或窗口相加：

| Evidence surface | Exact result | Status / boundary |
|---|---:|---|
| Oxipng static direct / scope / Drop | `6 / 310 / 320` | latest accepted external-app bundle at historical source `19ffb71`; target-crate audited rows, stale to current HEAD |
| Oxipng static ownership transfer | `12 / 12 / 12` candidate/applied/selected | same bundle; selected identities are nonzero; static rewrite gate only |
| Oxipng runtime typed / fallback allocations | `871 / 199` of `1070` | recording-window events; diagnostic, not object coverage |
| Oxipng runtime ownership transfer | `3 / 1 / 2` attempted/applied/rejected | dynamic execution observed; separate denominator |
| Oxipng whole-run recovery mismatch | `1` | `recovery_corrected_non_exact`; prevents whole-app exact-pairing claim |
| C002 compiler functional coverage | `430 / 430`, `99.851437%` | G001 read-only freeze digest `235549b2...`; historical/freeze-bound PASS, not live G002 rebinding |
| Current performance percentage | `N/A` | full reproduction deferred; reduced runs are diagnostic only |

#### Slide 12 必须定义的 compatibility contract

| 维度 | 可以主张 | 不可以主张 |
|---|---|---|
| **Application source** | 支持的 `Box<T>`/`Vec<T>` 等路径不要求应用手工 annotation；metadata absent 时 conventional path 可执行 | 所有 Rust allocation sites 都获得语义 |
| **Toolchain** | 配套 rustc/core rewrite 与 runtime ABI 能传递 metadata | 任意未修改 rustc、任意未来 rustc 版本都自动兼容 |
| **Custom allocator / FFI / unsafe path** | 能进入 conventional path 的请求可 fallback | 这些路径得到 semantic protection；完全绕过 UniAlloc 的 custom allocator 仍由 UniAlloc 管理 |
| **Binary ABI** | 本工作展示 prototype integration 的执行兼容性 | 跨 compiler/runtime version 的稳定 binary ABI；这仍是下一阶段 contract 问题 |

### D. H2: Semantic policy case study — 23:00--31:00（Slides 17--21）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 17 | **Distinct trusted allocator-visible identities separate ordinary cross-class reuse.** | 回到 Slide 3：只对 covered requests、distinct trusted exact identities 与 ordinary reuse path 做有限主张 | before/after slot 图；把 trust/coverage 写在图上 | 1:45；内部 lookup-key collision 已 exact-check；identical/spoofed metadata 与 compiler type-ID collision 仍开放 |
| 18 | **Type isolation and quarantine defend different exploitation steps.** | 与 Scudo/quarantine 做机制层对比，而不是泛化性能胜负 | 横轴 time、纵轴 identity 的 2×2 | 1:15；可叠加，不必二选一 |
| 19 | **The runtime uses a hashed semantic identity whose trust and stability are part of the contract.** | 展示 64-bit allocator-visible key 结合 type/module/policy/lifetime/placement；解释 callsite 不参与 reuse key | identity-key 拼图 + collision/spoofing boundary | 1:30；准备 type-ID collision/stability |
| 20 | **Per-type caching trades reuse separation for retention and fragmentation.** | 主动讨论 memory cost、empty slab、bounded TLS cache 和 workload sensitivity | 标注 `CONCEPTUAL` 的机制图，或真实 historical per-workload RSS 点；不要画伪定量曲线 | 1:45；避免“没有 memory overhead” |
| 21 | **H2 supports policy feasibility, not universal security or optimality.** | H2 小结：type isolation 改变 reuse rule；coverage、same-type reuse、内存与性能均是边界 | `Supports / Does not establish` | 1:45；可在中途提问版缩为 1:00 |

关键实现依据：

- `unialloc/src/alloc_api/type_isolation.rs:347-353,4454-4486`：64-bit hashed allocator-visible identity；这不是 collision-free cryptographic type identity。
- `unialloc/src/alloc_api/type_isolation.rs:5432-5501`：typed vs fallback classification。
- `unialloc/src/alloc_api/type_isolation.rs:6492-6611,8444-8484,8554-8604`：ordinary matching-key cache 的 allocation/deallocation/recovery。
- `unialloc/src/cache/thread_cache.rs:22-38,1576-1655`、`unialloc/src/zone.rs:15-43,83-165`：hot path 和 retention bounds。
- `../rust-alloc-paper/intro.tex:274-286`：论文明确的 defense-in-depth 与 coverage 限制。
- `94b2523d...` source-bound H2 snapshot：type-id cache separation、cross-thread recovery metadata、moved-realloc corrupt-tag transaction 三项精确 regression tests 均 `1/1`，semantic metadata probe 也通过。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h2-20260712T0604Z/audit.json`。该 snapshot 支持当时源码的 lifecycle 与 routing correctness；live HEAD 的新增安全 regression 另列如下，二者都不支持论文性能百分比。
- `3acbd6d...` adversarial reuse regression：4 个同 layout 对象经 process-visible cross-thread recovery 在同一 worker TLS cache 中释放；consumer 与 producer 的 module/flags/lifetime/placement 完全相同、只有 `type_id` 不同，consumer 不得获得任一 producer 地址，而 producer identity 随后必须无重复地取回全部 4 个地址。Hosted 与 `fixed_heap` 各 `1/1` PASS；证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-cross-thread-security-3acbd6d/`。这仍是 covered-path mechanism test，不是 universal UAF/exploit-success 证明。
- `ca9462e...` mismatch/quarantine regression：allocation 带 process-visible cross-thread recovery、type isolation 与 delayed-free；foreign thread 同时处在错误 Drop identity 下。测试要求 global recovery 记录只能消费一次、错误 identity 不得取消 quarantine 或看到该地址、释放 quarantine 后地址只能进入 allocation-side cache。Hosted 与 `fixed_heap` 各 `1/1` PASS。它把 cross-thread、mismatched metadata、delayed-free 和 cache poisoning 四个条件放进同一条 adversarial lifecycle，但仍不代表 arbitrary forged metadata 或完整 exploit corpus。
- `13807b3...` memory-tagged cross-thread regression：带 global recovery 与 memory tag 的 allocation 在 foreign thread 遇到错误 Drop identity 后，正确 recovery identity 只能 quarantine 一次；重复 typed free 必须 fail-stop，不能二次入队或污染错误 type cache。该测试闭合了 memory-tag side table、cross-thread recovery、mismatch、quarantine 与 duplicate-free 的组合路径，但不等于 hardware tagging、任意 forged metadata 或完整 exploit coverage。
- `27fc0c8...` split-realloc stale-identity regression：两个公开 split-metadata FFI 都必须以 allocation-completion recovery record 作为 old-object 权威 identity，而不能让调用者提供的 stale old metadata 授权同地址 relabel。一个 parameterized test 在循环内覆盖普通与 hints ABI，hosted 与 fixed-heap 配置各 `1/1` PASS；测试同时要求错误请求只记录一次 mismatch、committed move 以权威 old identity 配对、payload 保留、old storage 进入 recorded delayed-free/type-cache domain，而 requested new metadata 只绑定 replacement。Pre-commit 全仓测试通过；独立复验的 realloc family 为 hosted `46/46`、fixed-heap `48/48` PASS。它闭合的是受 recovery record 保护的两个 public split FFI，不等于 metadata 不可伪造或所有 custom allocator ABI 都受保护。
- `2eea36f...` cross-thread type-changing realloc regression：old recovery record 必须被精确消费、replacement 必须发布 new identity 并保留 payload；old buffer 在 delayed-free quarantine 中不得被 new type 观察，释放后只能被 old type 回收，cleanup 后不得留下 recovery record。Hosted `696/696` 与 fixed-heap `582/582` full suites PASS。它补上 realloc、跨线程 recovery、quarantine 与 type-cache routing 的组合不变量，但不代表 forged metadata 或所有应用 realloc path。
- `48cdfcf...ae923c6...` cross-thread realloc policy-domain regression：old hugepage-policy identity 与 replacement ordinary metadata-segregated identity 必须分别进入正确 cache key，old recovery 只消费一次且 payload 保留。Hosted direct snapshot 进一步证明 old pointer 在 physical hugepage side-cache、replacement 在 ordinary inline cache；`fixed_heap` 明确断言两种 policy 都映射到 ordinary physical domain，因此该配置只主张 policy-key/identity separation，不主张物理 hugepage 分域。两种配置 focused test 各 `1/1` PASS；不是 compiler coverage 或性能证据。
- `6fd22fb...` checked semantic snapshot ABI regressions：semantic-stats、fallback-attribution 与 metadata-validation 三个 checked snapshots 对 null/undersized buffer fail closed，exact/oversized buffer 返回正确字段；hosted 与 `fixed_heap` focused tests 各 `3/3` PASS。它保护 probe/platform 使用的 size-negotiated C inspection contract，但不等于外部平台 runtime evidence。
- `7096fc6...37ea7cd...` `Vec` transfer-before-growth adversarial lifecycle：同 layout 的 producer/consumer element 都是 64 bytes，但 compiler-derived identity 必须不同；producer 的 allocation 在 creator thread，pointer/capacity/payload 转移后由 worker 完成 typed realloc 与 Drop，再验证 wrong-type non-reuse、same-type exact recovery、zero mismatch/corruption。Hosted 与 `fixed_heap` 在 clean `37ea7cd` 各一次通过。它补上跨线程 realloc 发生在转移之后的 real Rust container path，但仍只覆盖该 bounded Vec lifecycle。
- `c426a2f...` Arc+Vec multi-owner worker-drop evidence：companion 从 validated raw target rewrite 与 runtime type rows 重算，不接受 summary 自证；function-bound Arc/Vec 使用 distinct nonzero identities 与同一 module，唯一 multi-owner closure skip 满足完整契约，两种 identity 的 allocation/deallocation 均为 exact `1/1`，recovery mismatch 为 `0`。这只闭合 bounded Arc+Vec worker-drop pairing，不是完整 escape analysis、universal container coverage 或性能证据；artifact 位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/cross-thread-multi-owner-pairing-c426a2f-20260712/`。
- `909a7ad...68749b9...` realloc-to-zero identity retirement：带 process-visible recovery 的 old identity 在 foreign thread 以 distinct same-layout requested type realloc 到零；测试要求 aligned sentinel、old record 精确消费、无 replacement/stale slow path、wrong-type miss、old-type one-shot recovery 与 payload 保留。Hosted/fixed-heap focused 各 `1/1` PASS、mismatch/corrupt `0/0`。该 test 未启用 PAC，明确不提供 PAC failure evidence；只支持 ordinary TLS identity-retirement/cache-routing 不变量。
- `9ca5a10...` memory-tagged realloc-to-zero retirement：local 与 process-visible recovery 两条路径都必须返回 aligned sentinel、移除 old memory-tag record、精确消费 recovery identity 一次；第二次 dealloc 必须在 cache/raw free 前 panic，且 side-cache snapshot 不变。Hosted/fixed-heap focused 各 `2/2` PASS。它只覆盖 `FLAG_MEMORY_TAGGING` 的 fail-stop double-free，不是 hardware memory tagging、任意 stale pointer 或性能证据。
- `422c91f...` cross-thread overflow realloc failure invariant：creator 发布 old recovery identity 与 payload，foreign worker 用 distinct requested type 和 `usize::MAX` 发起 invalid-layout realloc；必须返回 null 且 old record/count、payload、validation、cache/delayed-free 状态不变，随后 normal dealloc 精确消费 old identity 一次，new type miss、old type one-shot recovery。Hosted/fixed-heap focused 各 `1/1` PASS；stats disabled 且未请求 PAC，因此不作 PAC 或性能主张。
- `6603460...` auto-metadata lifecycle hardening：disable/reconfigure 不再清除 live allocation recovery records；global、thread-local、layout-derived policy 都保留 allocation-time exact metadata/generation，长寿 worker 在 generation 变化后从新 compiler-ID stream 的第一个 ID lazy restart。pre-enable raw dealloc 与 unrecorded old-pointer realloc/move 不得继承当前 auto policy；控制变更与 record publication overlap 也有并发 regression。Hosted `stats,type_isolation` suite `720/720`、fixed-heap suite `606/606` PASS。该证据闭合 policy-generation identity 生命周期，不是跨线程 local-only recovery API 的扩张，也不是性能结论。
- `4dc6814...` recovery matcher 把两个 hashed-key 比较收紧为 allocator-visible fields 的 exact comparison，消除了 recovery agreement 的 hash-collision false match，并少做两次 identity hash。5+5 次同机 probe 的方向性 median 为 `3.497 ms → 2.366 ms`，但冷启动范围很宽；只能作为 diagnostic direction，不得作为论文性能百分比。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-exact-recovery-4dc6814/`。
- `4937f4f...` hot-path optimization：`auto_metadata_allocations_exhausted()` 先检查 sticky exhaustion atomic，只在 consuming stream 已 exhausted 时读取 `AUTO_METADATA_CONFIG`，从 layout-derived/cyclic auto-metadata 的常见 alloc/dealloc gate 删除一次 `RwLock` read；独立审查确认 generation reset 与 revalidation 语义不变，targeted type-isolation tests `194/194` PASS。严格只做一组 pre 与一组 post 的同 leaf diagnostic：`13.0/30.97 ns`（off/on）到 `12.92/29.09 ns`；variant 明确是 `layout-derived-size-align`、`compiler_site stream=none`、`semantic_policy.ready=false`。因此只把 `-1.88 ns` 视为保留该优化的方向性信号，不报告稳定百分比、统计结论、compiler-attributed cost 或论文结果。
- `1228f71...` plain-cache accounting/cap repair：首次 cold growth 在 reset/untrusted 后扫描 64 slots 建立 retained-byte aggregate，健康 push/pop 后 O(1) 更新；corruption/accounting repair 将 aggregate 标为 untrusted，下一次 growth 只做一次 bounded rebuild。Inline 与 cold slots 现在共同受 512 KiB cap，空 inline slot 也不能绕过 aggregate headroom。Regression 覆盖 exact cap、push/pop、inline/cold replacement、drain 与 corruption rebuild。单次同条件 micro diagnostic（24 distinct identities、同 64-byte layout、单线程、2.4M typed operations）为 `72.338 -> 54.808 ns/op`（`-24.233%`），两边均为 1.2M hit、1.2M insert、0 bypass；这只有方向性，**没有 median/range**，不得作为 general-app、paper、publication-grade 或稳定百分比 claim。Slide 20 可讲 O(1) accounting 与 512 KiB bound；性能数字只放 speaker note/backup 并带此边界。

Slide 10/17 的精确 threat model：attacker 可以触发 temporal bug 和 heap grooming；TCB 信任 compiler/runtime 产生或受信 semantic caller 提供的 metadata，并假设输入 metadata 未被伪造。对 covered requests，若 allocator-visible identities distinct，ordinary cross-class reuse 被分开；plain inline/linked cache 即使发生内部 64-bit lookup-key collision，也要求 exact callsite-agnostic identity 匹配。`3ccd464` 进一步让 metadata-segregated cache 的 occupied-entry structure、exact identity 与 full-bucket eviction projection 在使用前经过 keyed authentication，因此这些 bounded internal tamper regressions fail stop；这不等于 arbitrary-memory-corruption protection。**完全相同或伪造的 input metadata、compiler type-ID collision、same-type reuse、cache 外 metadata corruption、unknown/fallback/custom-allocator path 仍不在该有限保证内。**

### E. H3: Retargetability is an architectural boundary — 31:00--36:00（Slides 22--24）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 22 | **Retargetability comes from stable boundaries, not from feature flags alone.** | 解释 policy、cache/zone/backend、metadata allocator、PAL 各自责任 | 接口边界图；标出 reused vs adapted | 1:45；准备“这只是 cfg 吗？” |
| 23 | **Hosted and constrained targets reuse the policy while changing memory acquisition.** | 对比 mmap/VirtualAlloc 与 fixed-heap；说明 target 仍需 adapter | 两列 deployment recipe | 1:45；不要说 zero-porting |
| 24 | **The paper reported five retargeting environments; current functional readiness and external validation remain separate.** | 用 historical badge 展示论文五环境结果；另列 G002 的 macOS PASS、既有 Windows 基础功能证据与新增 Wine 10 FLS runtime `3/3` PASS、fixed-heap/hosted current smoke、current-source Redox build/codegen/ABI PASS、历史 artifact-hash-bound Redox runtime evidence（未捕获 source revision），以及 Rust-for-Linux/BlogOS current-source no_std final-link contracts PASS；三者当前 target runtime 仍分别依赖外部 runner/assets | 五行平台矩阵：paper report / reused layer / adapted layer / current functional status | 1:30；完成 H3，再引出 evidence tiers |

关键实现依据：

- `unialloc/src/lib.rs:129-150`：Windows `VirtualAlloc`、Darwin/Linux/Unix `mmap`、fixed-heap selection。
- `unialloc/Cargo.toml:63-95`：fixed heap、alternate slab backend、hugepage、type isolation、metadata segregation、PAC/MTE/MPK/guard/quarantine 等配置面。
- `unialloc/src/sc/mod.rs:1-24`、`unialloc/src/sc/backend.rs:1-18`：separate-metadata/bitmap backend 是真实 backend choice，不只是命名 flag。
- `../rust-alloc-paper/sys.tex:52-114`：paper architecture decomposition。
- `94b2523d...` source-bound H3 smoke：fixed-heap `small_heap` 与 hosted 4-thread allocator workload 均通过、无 compiler warning；fixed-heap semantic C ABI 观察到 typed alloc/dealloc pairing，hosted workload 只证明 global allocator/thread-cache/platform path。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h3-20260712T060354Z-94b2523d8823/`。hosted 普通 Cargo run 没有 MIR rewrite，因此其 `typed_allocations=0` 不得被误读为 H1 failure；开发 HEAD 已前进，不能称这份 snapshot 为 current-HEAD。
- `f5c4935...` 只闭合 PAL 的对称 FLS accessor（`FlsSetValue`/`FlsGetValue`）；`df5f449...` 才把 production `GlobalTcache` ownership 改为 fiber-local。`f238f10...` 进一步处理 registration/save failure 与 teardown：current-owner callback 做 full drain；`DeleteFiber(B)` 在 A current 时先 narrow-drain OS-thread-shared retained semantic caches，再回收 B，同时保留 live recovery/tag records、active scopes 与 compiler cursor；temporary bind/clear 失败则 fail safe by leak，而不制造 UAF/double free。Host retained-drain test `1/1`、thread-cache filter `69/69`、Windows GNU cross-target type-check/cross-build 与 Zig-linked test executable no-run 均 PASS。使用 `0bd84c1...` 引入的 Wine 10 runner，HEAD `38b8b59...` fresh cross-build 的三个完整模块路径 exact lifecycle tests 均 `1/1`，合计 `3/3` PASS；exe SHA-256 为 `7d4e060d7fc08a32134776f30e45550a8a4561373e15e5bcf077844115c8379b`。Durable transcripts、source hashes、Wine image/runner identity 位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-38b8b59-20260712b/summary.json`（SHA-256 `d44dbe74b7a5fd30776185ee5be4e7f7254eed2e3be5b5ec9bf08f086835a5a2`）。更早 Wine 8 缺失 `bcryptprimitives.dll` 是 runner dependency blocker（missing），不是 allocator failure。该证据只支持 bounded Wine functional path，不等于 native Windows universality 或性能证据。
- `b3f2cad...`（主线等价提交 `2199624...`）current-source Redox contract：真实 `x86_64-unknown-redox` target 上的 allocator library 与 `small_heap` example check PASS，生成的 object 被验证为 ELF64 little-endian x86-64 `ET_REL`，`llvm-nm` 验证 constrained fixed-heap/metadata/semantic-stats/boot C ABI symbol set 无缺失。证据位于 `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/redox-current-source-b3f2cad-20260712/`。这只闭合 current-source build/codegen/interface contract；当前源码的最终 link/run 仍需 redoxer/QEMU，因此 external runtime validation 是 missing，不是 allocator functional failure。`docs/c007-redox-boot-evidence.md` 中 2026-07-07 的 real target transcript 绑定了 binary/image/config/emulator hashes，但 capture 没有记录 Git commit 或 source digest；它只能称为 historical artifact-hash-bound runtime evidence，不能 rebinding 到当前 HEAD。
- `00a187b...a581cb4...` BlogOS local contract：独立 `x86_64-unknown-none` no_std final crate 连接 boot heap publication、受初始化状态保护的 `#[global_allocator]`、真实 `Box` allocation/deallocation、panic 与 allocation-error halt handler；linked ELF 与 allocator/handler symbols 检查 PASS。`a581cb4` 又在同一 no_std 状态机上加入 `5/5` host regressions，覆盖 first publish、same-range idempotence、different-range rejection、failed retry 与 concurrent waiter；final ELF SHA 为 `dcb668f...d994b`。真实 BlogOS image/bootloader/QEMU 仍是 external validation missing；这是 behavior/wiring/build evidence，不是 boot/runtime 或性能证据。
- `bd9d927...` Rust-for-Linux force-link closure：`rust_bench.rs` 显式消费 Makefile 传入的 `--extern unialloc=...`，避免只通过 `extern "C"` bridge 时 rlib 被最终 crate 丢弃。checked-in no_std final-link regression 复用真实 `unialloc_bridge.rs`，linked ELF 的 fixed-heap init/extend/ready、alloc/dealloc/realloc 与 semantic snapshot symbols `11/11` 存在；独立负向对照移除 force-link import 后得到对应 undefined-symbol link failure。真实 kernel module load/run 仍需外部 Rust-for-Linux kernel tree/runner；无 runtime/performance claim。
- `315cfc4...` constrained-platform ABI parity：同一 no_std final crate 在编译期比较 UniAlloc runtime 与 Rust-for-Linux bridge 的 5 个 ABI version、record size/alignment 和全部 75 个字段 offset；C11 header 也对同 5 records 的全部 75 个 offset 做 static assertion。Rust layout 在 `x86_64-unknown-none` final crate 中检查；C header 只由本机 64-bit Apple clang（`arm64-apple-darwin25.5.0`）检查，并非实际 Rust-for-Linux kernel compiler。Focused contract 与 independent review PASS；这是 layout/build evidence，不是 kernel load/runtime 或性能。

### F. Evaluation and evidence boundary — 36:00--42:00（Slides 25--28）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 25 | **The evaluation asks feasibility, cost, coverage, and retargeting as separate questions.** | 先讲 RQ→metric→baseline→threat，而不是先贴结果 | 四行 evaluation matrix | 1:30；标出 historical methodology |
| 26 | **The paper reported less than 2% average performance difference in its tested aggregate, with workload-dependent memory retention.** | 历史结果 1：写清 tested baselines/workloads/aggregation；多数内存 comparable，但 Collections/Rust-Redis peak 更高 | 两个 takeaway；exact comparison table 放 B14/B16 | 1:30；页脚写 paper-reported historical |
| 27 | **The paper reported 5--14% type-isolation slowdown and 72.17% object coverage under its original setup.** | 只回答 H2 的 cost + coverage；metadata segregation、hugepage、PAC 全部移到 backup | 两个 number tiles + coverage boundary；不要混入其他 features | 1:30；不要说 current reproduction |
| 28 | **Current functional evidence is source-bound; full paper-performance reproduction is intentionally deferred.** | 展示四层 evidence ladder 和 G001→G002 状态；这是诚信页，不是道歉页 | Historical / Current probe / Historical partial record / Deferred claim 四阶梯 | 1:30；委员会可能从这里深挖方法 |

Slide 25 必须说清原论文方法：`nightly-2021-08-04`；每项六次，报告后五次的 geometric mean；UniAlloc 为 normalization baseline；MPK/MTE simulation 不进入 performance claims。还要主动说明：该方法没有单独给出 uncertainty/confidence interval/significance analysis，六次运行与 geometric mean 本身不能代替统计不确定性。依据：`../rust-alloc-paper/eval.tex:63-91`。

Slide 26 必须在 B14/B16 列出原论文的六个 baselines：tcmalloc、glibc `malloc`、mimalloc、jemalloc、snmalloc、Scudo；UniAlloc optional features 关闭，baselines 使用 default settings（`eval.tex:93-107`）。论文正文只写 “On average, UniAlloc differs by less than 2%”；若没有从原始数据重新确认 aggregation axis，就只能称为 **the paper's tested aggregate**，不能暗示这是每个 workload、每个 baseline 的上界。

Slide 26--27 的数字只能使用以下句式：

> **The paper reported ... under its original toolchain and methodology. These results are historical reference results, not yet a current-source claim-grade reproduction.**

原论文数字位置：

- default performance：`../rust-alloc-paper/eval.tex:168-175`；
- workload-dependent peak memory：`../rust-alloc-paper/eval.tex:193-210`；
- type isolation and coverage：`../rust-alloc-paper/eval.tex:261-303`；
- backup only：metadata segregation **4% speedup**（`eval.tex:233-252`）、PAC **1--3% slowdown** 与 hugepage **2--4% speedup**（`eval.tex:314-344`）；
- historical retargeting：`../rust-alloc-paper/eval.tex:380-455`。

#### Slide 28 的四层 evidence ladder

| Evidence tier | 能支持的表述 | 不能支持的表述 | 本次审查状态 |
|---|---|---|---|
| **Paper-reported historical** | 原 prototype 在原 toolchain/hardware 下曾报告某结果 | 当前源码已经重现 | 可讲，但必须标 historical |
| **Current implementation/probe** | 当前机制/路径能够构建或运行；某个 bounded probe 观察到某行为 | 完整 performance、coverage 或 platform claim | 有大量机制证据 |
| **Historical source-bound record (partial)** | 记录曾绑定只读 G001 freeze 且自身验证通过 | allocator comparison、完整 cell 或完整 claim | 20 条已接受记录；campaign 已停止，全部仅作 historical/diagnostic |
| **Current source-bound claim (complete)** | 当前源码、完整 required evidence、provenance 和门槛共同支持 claim | — | performance claims 被显式 deferred；不能标 pass 或 fail |

状态快照（`2026-07-12`）必须区分 **只读 G001 evidence freeze**、**当前 G002 开发树** 和 **deferred paper-performance work**：

1. 只读历史 freeze 位于 `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2`，HEAD `0df377b2...`，权威 digest `235549b228dec87334abc90c91c0b4cc8bbf6dfb1c2d5ebaed00d8a97746b451`，clean。
2. G001 formal campaign 在 20/1008 accepted records 后因用户显式目标变更停止；状态是 `stopped_by_explicit_user_objective_change`。这 20 条记录不得用于论文性能百分比或 allocator comparison。
3. G001 原性能目标没有完成，因此保持 incomplete 并标记 superseded；当前 active goal 是 implementation-first G002。不要把 superseded 写成 complete。
4. G002 开发树继续变化；制作和排练时用 `git rev-parse HEAD` 读取 live HEAD，不在主 deck 固化短期 commit。当前 evidence 重点是 actual MIR rewrite、type-isolation lifecycle、fixed-heap/hosted runtime 和平台 adapter 功能。
5. C002 作为 functional/compiler evidence；C006 只讲 PAC functionality，cost deferred；C007 是 platform-functionality backlog。C001/C003/C004/C005 与 C006 performance percentage 均为 `deferred_by_explicit_user_scope_change`，不是 pass 或 fail。

因此主讲中的安全句式是：

> **Full paper performance reproduction was intentionally deferred after 20 source-bound historical records. Current claims are limited to functional and mechanism evidence; reduced benchmark numbers are diagnostic only, and no publication-grade percentage claim is made from them.**

主 Slide 28 只显示这一稳定结论和四层 ladder；live HEAD、probe artifact、freeze digest 与 stop record 放在 B18/speaker notes，并在答辩当天刷新。

不要比较这 20 条 historical records 的 raw timing，也不要把 diagnostic smoke、plan readiness 或 incomplete timing records 称为 performance conclusion。相关入口：

- `evaluation/results/claim_check_current.json`
- `evaluation/results/overclaim_worklist.json`
- `evaluation/results/paper_performance_gap_plan.json`（historical/deferred）
- `evaluation/results/platform_matrix_audit.json`
- `docs/evaluation-gap-analysis.md`（开头的 supersession note 优先于历史 queue）
- `.omx/handoff/g001-performance-campaign-stop-user-objective-change-20260712T030350Z.json`
- `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2/evaluation/raw/source-freeze-required-bound-plan-235549b2-20260711a/final-verification.json`
- `.omx/ultragoal/ledger.jsonl`（G001 supersession、G002 functional evidence 和 current-source probe 入口）

在正式答辩前重新生成这页的 live HEAD 和 current probe summaries；历史 freeze digest 与 20-record stop status 保持不变。

### G. Judgment, agenda, and close — 42:00--45:00（Slides 29--31）

| # | 建议英文标题 | 这一页只完成什么 | 图 | 时间/追问 |
|---:|---|---|---|---|
| 29 | **The scientific contribution is a semantic allocation contract bounded by trust, coverage, and deployment.** | 汇总 contribution、alternatives、limitations；不要把结论降格为 replication status | 3 行 contract / demonstrated use / boundary | 1:15 |
| 30 | **The next falsifiable question is the minimal stable identity contract under partial coverage and FFI.** | dissertation agenda：identity stability/collision/TCB、partial coverage、FFI/cross-language、exploit corpus；完整 source-bound matrix 是 validation infrastructure | 研究问题 → discriminating experiment → possible outcome | 1:15 |
| 31 | **Preserve semantics, separate policy, and make evidence provenance explicit.** | 三句话收尾，回到 Slide 2；进入 Q&A | 三项贡献 + 同一颜色；不要放新数据 | 0:30 |

推荐 closing 三句：

1. **UniAlloc shows that compiler-known heap semantics do not have to disappear at the allocation boundary.**
2. **Those semantics can drive policies without binding the policies to one platform mechanism.**
3. **The next scientific question is the minimal stable identity contract that remains useful under partial coverage, FFI, and adversarial conditions.**

## 5. 42 分钟中断可剪裁版本

委员会中途提问时，不要加速讲完所有页。按下列顺序逐项剪，预计达到 42 分钟就停止；后两项只用于更长中断，不必全部执行：

1. Slide 18（type isolation vs quarantine）移到 backup；Slide 17 口头补一句。
2. Slide 20（memory retention）压缩到 45 秒；详细图移到 backup。
3. Slide 23（两类 target）压缩为 Slide 22 的一个 build animation。
4. Slide 26--27 合并为一张“historical result ranges”；保留 Slide 28 的 evidence boundary。
5. Slide 30 只讲前两个 next steps。

绝对不要剪：Slides 2、4、7--9、10--16、17、21、24、25、28、29、31。它们构成完整论证链。

## 6. 如果必须主讲 53--55 分钟：增加 5 张扩展页

把下列页插入主 deck；不要用更多 feature 页填时间。

| 插入位置 | 扩展页标题 | 价值 | 约时 |
|---|---|---|---:|
| Slide 5 后 | **Allocation separates a hot path from refill and raw-memory acquisition.** | 更详细解释 cache/zone/backend，方便非 allocator 委员 | 1:30 |
| Slide 13 后 | **The MIR transformation preserves the original call and its cleanup behavior.** | 用一个真实但简化的 before/after MIR example 证明 compiler work 不是口号 | 1:45 |
| Slide 19 后 | **Allocation and deallocation must recover the same semantic identity.** | 解释 direct-local、recovery、cross-thread 的 invariant | 1:30 |
| Slide 23 后 | **Retargeting is a recipe of PAL, concurrency, and metadata choices.** | 对比 hosted、kernel、fixed heap 的实际 adaptation surface | 1:30 |
| Slide 25 后 | **A claim is only as current as its source binding and complete matrix.** | 解释 repetitions、geomean、raw evidence、fingerprint 和 fail-closed gate | 1:30 |

五张扩展页的内容预算是 7:45；加到 45 分钟主线后约为 52:45，转场和一次短中断后应在 53--55 分钟结束。不要把 60 分钟全部占满。

## 7. Slide 制作系统：让制作更快、问答更轻松

### 7.1 固定视觉语法

全 deck 只使用四种语义颜色：

- **蓝色**：compiler/semantic information；
- **橙色**：allocation policy；
- **绿色**：platform/backend/PAL；
- **灰色**：conventional fallback 或不在当前 claim 范围内。

每张 architecture/mechanism 页都沿用这些颜色。委员会看到颜色就知道当前讨论位于哪一层。

### 7.2 Evidence badge

所有结果页右下角必须有一个 badge：

- `PAPER-REPORTED HISTORICAL`
- `CURRENT FUNCTIONALITY PROBE`
- `HISTORICAL SOURCE-BOUND RECORD — PARTIAL`
- `CURRENT SOURCE-BOUND CLAIM — COMPLETE`
- `PLANNED / INCOMPLETE`

在 current claim closure 前，不要使用 `CURRENT SOURCE-BOUND CLAIM — COMPLETE`。已停止的 G001 records 只能使用 `HISTORICAL SOURCE-BOUND RECORD — PARTIAL`，不得贴 current badge。数字下方同时写 toolchain/hardware、N、baseline 和 source digest/date；信息太长就链接到 backup 页。

### 7.3 每页最小模板

每页 speaker notes 固定写五行：

```text
Takeaway: one sentence
45-second path: what I will say first
Likely question: the most probable interruption
Short answer: 20-30 seconds
Deep answer: backup slide number
```

这会同时降低制作成本和问答切换成本。

### 7.4 现有图的使用策略

| 原图 | 主 deck 处理 | 理由 |
|---|---|---|
| `../rust-alloc-paper/fig/overview.pdf` | **重画**为 3-lane progressive architecture；原图放 backup | 内容全面但主讲时过密 |
| `../rust-alloc-paper/fig/bg-alloc.pdf` | 重画成 cache→zone→backend→PAL | 原图适合论文，不适合口头教学 |
| `default-perf.pdf`、`perf-type.pdf` | 主 deck 只重画与 Slides 26--27 对应的 aggregate/per-workload takeaway；原图放 backup | 多 baseline bar chart 难以在 30 秒内读懂 |
| `metadata-separation.pdf`、`hugepage.pdf`、`perf-pac.pdf` | backup only；标注 historical direction：4% speedup、2--4% speedup、1--3% slowdown | 它们不是主论证 H2 所需证据，且当前 claim 未重现 |
| Windows/macOS result figures | paper figures 只作 backup/historical；G002 functional status 另列 | 不把历史图当作 live five-platform validation；current functional PASS、current probe 与 external validation gap 必须分栏 |

原则：**一张幻灯片只让委员会比较一个维度。** 不要把论文 screenshot 或段落粘到幻灯片。

### 7.5 Claim ledger

制作 deck 时维护下列小表；每一张 claim slide 都必须有一行：

| Slide | Claim | Evidence tier | Source | Assumption | Does not prove | Backup |
|---:|---|---|---|---|---|---:|
| 16 | real compiler-to-runtime path exists | historical source-bound app evidence + current bounded probes | accepted `19ffb71...` Oxipng bundle + `9a02767...` provenance/unwind probes + `f007c7b...` realistic multi-module | Oxipng counts stay bound to historical source; tested toolchains; separate static/runtime denominators | current-source external-app counts/universal coverage/unmodified-app deployment/stable ABI/whole-app exact pairing | B3--B4/B18 |
| 21 | ordinary cross-class reuse is separated for distinct trusted identities | current adversarial regression + implementation | `3acbd6d...`, `5eb25f5...`, `1228f71...`, `8bc2809...`, `6700ca1...`, `2e3bc4c...`, `3ccd464...` tests + type-isolation code | covered path, trusted exact identity; bounded internal segregated-entry integrity | universal memory safety/identical or spoofed input metadata/compiler type-ID collision/arbitrary corruption protection | B7--B10 |
| 24 | paper reported five environments and runtime has retargeting boundaries | historical + current functional probes | paper eval + PAL/fixed heap + platform artifacts | tested adapter/path | zero-porting/current five-platform aggregate closure | B13/B17 |
| 26--27 | original prototype observed reported ranges | historical | paper eval | original setup | current reproduction | B14--B16 |
| 28 | paper-performance reproduction was explicitly deferred while functional work continues | current audit snapshot | G001 stop handoff + G002 probes | exact source binding and evidence tier | mechanism is absent or deferred claims failed | B18 |

## 8. Q&A 方法：先直接回答，再展开证据边界

统一使用：

> **Claim → Mechanism → Evidence → Boundary → Next discriminating test**

- **20 秒版**：直接 yes/no + boundary。
- **90 秒版**：完整五步。
- **3 分钟版**：切到一个 backup slide，再给 alternative/tradeoff。

不知道时不要猜：

> **I do not yet have evidence for X. The current implementation establishes Y under assumption Z. The discriminating experiment would be W.**

这比模糊扩大 claim 更能显示 qualifier 所需的研究判断力。

### 最可能的委员会问题与安全回答骨架

| 问题 | 第一句直接回答 | 必须补的边界/backup |
|---|---|---|
| **1. Novelty 相对 mimalloc、snmalloc、Temeraire、Scudo/hardened malloc 是什么？** | UniAlloc 的核心新输入是 compiler-provided semantics，以及自动 extraction 和 policy/mechanism separation；不是另一个 size-class free list。 | 说明不同工作可互补；不要宣称所有机制首次出现。B1/B8 |
| **2. 为什么不完全交给 compiler 或 Rust type system？** | Compiler 提供语义，但 allocator 控制 physical reuse；unsafe、FFI、unsound API 最终仍会表现为 heap reuse。 | Defense-in-depth，不是 type-system replacement。B9 |
| **3. 为什么是 Rust，不从 C/C++ 开始？** | Rust 的 `Box<T>`、`Vec<T>` 和集中化 allocation path 让研究能先隔离 interface question。 | Cross-language generality 尚未证明。B3 |
| **4. Type isolation 精确保证什么？** | 对 covered requests，若 trusted allocator-visible metadata distinct，ordinary cross-class reuse 被分开；plain-cache 的内部 64-bit lookup-key collision 还会再做 exact identity check，metadata-segregated occupied entries 在 reuse/accounting/eviction projection 前做 keyed structural authentication。 | 完全相同/伪造 input metadata、compiler type-id collision、cache 外 corruption、uncovered/fallback 均不在有限保证内；它不消灭 UAF。B7/B9 |
| **5. 为什么不用 quarantine？** | Quarantine 约束 reuse time；type isolation 约束 reuse identity，防御不同步骤且可组合。 | 不要做超出 tested configuration 的性能胜负结论。B8 |
| **6. `type_id` 如何唯一、稳定且避免 collision？** | 当前 runtime 使用 64-bit hashed allocator-visible identity；stable compiler-level identity、collision policy 与 trust contract 仍需明确化。 | 不要把 helper hash 说成 cryptographic、collision-free 或跨编译稳定保证。B9 |
| **7. 为什么 reuse identity 不包含 callsite？** | Allocation 和 drop 可能来自不同 callsite；把 callsite 放入 identity 会破坏同一对象类别的合法配对。 | Callsite 仍可用于 provenance/policy，但不应默认成为 reuse key。B9 |
| **8. ABI/API 变化为何不破坏现有程序？** | 在配套 toolchain 的 supported paths 上不需应用 source annotation；unknown metadata 可走 conventional fallback。 | 这不是跨 rustc binary ABI、任意 custom allocator/FFI 或 universal semantic coverage 的保证。B1--B3 |
| **9. realloc、drop、unwind、跨线程 deallocation 如何匹配？** | Actual-rustc probe 中 alloc 建立 identity，realloc/dealloc 可用 strict-neutral metadata 委托 recovery；split FFI 若同时收到 stale explicit old metadata，则 allocation-completion record 对 old object 保持权威，而 requested new identity 只绑定 replacement。当 partial/generic helper Drop 未被 provider 暴露时，allocation-side recovery 完成配对。独立 nested-unwind probe 还验证 inner cleanup 只弹出 inner scope、恢复 depth-1 outer scope，outer return 后再回到 depth 0。 | `Vec`、Layout shrink、split FFI、partial/generic 与 nested unwind 各是 bounded lifecycle；provider 未暴露 generic helper row，所以不能虚构 generic-skip，也不是完整 escape analysis。B4 |
| **10. MIR pass 会不会随 rustc 版本变化而脆弱？** | 会，这是 compiler integration 的明确 maintenance cost。 | 讲稳定 ABI/contract 与 versioned regression suite 的下一步。B3 |
| **11. Per-type/per-thread cache 会不会导致内存爆炸？** | 会增加 retention/fragmentation 风险；当前设计用 bounded caches/empty-slab controls 缓解，而不是消除。 | 展示历史异常 workload 与 current footprint controls。B10/B11 |
| **12. “Retargetable” 是否只是 `cfg`/feature flags？** | 不是；复用的是 semantic policy 和 allocator pipeline，适配的是 PAL、raw memory、concurrency 和 metadata layout。 | 仍然不是 zero-porting；每个 target 需要真实运行证据。B12/B17 |
| **13. 如何证明真的用了 hugepage，而不是 ordinary-page fallback？** | Current HEAD 的 hugepage/ordinary domain 与 fallback tests 为 `17/17`，但本机 direct probe 没有观察到 real hugepage backing；macOS 返回 `KERN_INVALID_ARGUMENT`，因此 backing 仍是 missing。 | Domain separation/fallback PASS 不等于 mapping/backing PASS；需要合适 host 和与当前三对象 side-cache materialization 一致的 fresh probe。B13 |
| **14. PAC 当前到底验证了什么？** | Current HEAD 验证了 allocator PAC metadata 的安全 software fallback 与 typed side-cache reuse；独立 `no_std` consumer contract 隔离了 std-only dev-dependencies，并允许用 `rust-src` 构建真实 arm64e allocator runtime probe。 | external ABI evidence 不能代替 allocator runtime；只有 source-bound arm64e `no_std` probe 才能支持 hardware functional evidence，且 C006 cost/percentage 仍 deferred。B12 |
| **15. 72.17% 的 denominator 是什么？是当前数字吗？** | 原论文表述为标准 Rust `alloc` benchmark 中“72.17% of objects”；它不是当前 source-bound 已闭合数字。 | 若 raw evidence 未定义 event/object denominator，不自行改名；给原方法、fallback 与 current audit。B14/B18 |
| **16. 为什么现在会看到 99.851437% coverage？** | `430/430` 是 G001 freeze-bound functional coverage evidence，不是性能。G002 的旧 `576df61...9bb9f8d...` one-shot 保留其精确 denominator；新的 `38b8b59...` current-source one-shot 单独报告静态 ownership transfer `6/6` 与动态 `1/1/0`，不能把它们换算或并入旧百分比。 | 先看 source digest、denominator、actual-rewrite/dynamic-execution evidence 和 evidence tier；不要跨 revision rebinding，也不要写成 performance claim。B18 |
| **17. Evaluation 是否公平？** | 需要相同 workload、baseline、配置、重复运行、明确 normalization、raw provenance 和 source binding 才能比较。 | 原论文旧 toolchain/hardware、simulation，以及没有单独 uncertainty/significance analysis 的限制必须主动说明。B14--B16 |
| **18. Security benefit 真正测量了吗？** | 当前已有同 layout、跨线程 recovery、不同 trusted `type_id` 的 adversarial reuse regression，证明 covered cache path 的 cross-type address reuse 被阻断；plain cache 另有强制 lookup-key collision regression，但还不是系统性 exploit-success study。 | Same-type、fallback、identical/spoofed metadata、compiler type-ID collision 与真实 exploit corpus 尚未覆盖；下一步测 reuse-success rate 与 attacker capabilities。B19 |
| **19. 当前源码支持五个平台吗？** | G002 已有 macOS functional PASS、Windows FLS Wine 10 runtime `3/3` PASS、current-source Redox build/codegen/ABI PASS、Rust-for-Linux 与 BlogOS current-source no_std final-link contracts PASS、历史 artifact-hash-bound Redox target runtime evidence（未捕获 source revision），以及 current fixed/hosted smoke；当前 Redox runtime、Rust-for-Linux kernel load/run 和 BlogOS boot validation 仍依赖外部 runner/assets。 | 使用 `0bd84c1` 引入的 runner，`38b8b59` fresh Windows cross-build 在 Wine 10 上闭合 A-current/B-delete、A-null/B-populated 与 current-owner exit 三个 bounded lifecycle；早期 Wine 8 缺 DLL 只是 runner blocker。local link contract 与 Wine 证据都不等于 native/current-HEAD 五平台实机闭合。B17/B18 |
| **20. 最重要的 dissertation 下一步是什么？** | 找出在 partial coverage、FFI 与 adversarial metadata 下仍有用的最小 stable identity contract。 | Source-bound matrix、exploit corpus 与跨平台实验是检验该问题的基础设施；随后扩展 cross-language/policy automation。B19 |

### 高风险措辞：不要说

- “UniAlloc **prevents UAF**.”
- “UniAlloc **proves Rust memory safety**.”
- “The 64-bit semantic key is **collision-free, unforgeable, or stable across compilers**.”
- “Fallback proves **binary ABI and FFI compatibility**.”
- “The current version **runs on five platforms**.”
- “Current coverage **is 72.17%**” 或 “**is 99.85%**” 而不说明 denominator、digest 与 evidence tier。
- “PAC 的 **1--3% overhead 已由当前源码复现**” 而只有 hardware functionality probe、没有完整 cost matrix。
- “Hugepages **are used**” 而没有 backing/mapping evidence。
- “All tests pass, therefore the paper claims are reproduced.”
- “Retargeting requires no platform work.”
- “The allocator has no overhead.”

## 9. 19 张备份页：按问题概率排序

编号直接使用 `B1`--`B19`，并在主讲 speaker notes 中写明跳转页。为降低首轮制作成本：**B1--B10 是第一轮必须完成；B11--B19 是第二轮/appendix pass。**

| Backup | 标题/内容 | 主要回答 |
|---:|---|---|
| B1 | `GlobalAlloc` vs semantic alloc/dealloc/realloc signatures | compatibility/ABI |
| B2 | `AllocationMetadata` fields、flags、unknown/fallback state | metadata semantics |
| B3 | rustc optimized-MIR rewrite before/after | compiler automation/fragility |
| B4 | alloc→realloc→drop→unwind→cross-thread lifecycle | pairing/recovery |
| B5 | 原始完整 `overview.pdf` | architecture details |
| B6 | Cargo feature/configuration matrix and invalid combinations | configurability/test burden |
| B7 | Trusted hashed-key cache invariant and ordinary matching path | exact guarantee |
| B8 | Type isolation vs quarantine/Scudo | alternative mechanism |
| B9 | Type-ID stability、collision、same-type limitation、threat model | security boundary |
| B10 | Thread cache、zone、empty slab、RSS/fragmentation controls | memory overhead |
| B11 | Metadata layouts: in-band/segregated/compressed/hybrid | locality/security/footprint tradeoff |
| B12 | PAC/MTE/MPK/guard/quarantine: hardware vs software vs simulation | feature evidence |
| B13 | Hugepage mapping/fallback and fixed-heap/PAL adapters | backing/retargeting |
| B14 | Original benchmark suite、baselines、hardware、toolchain | method validity |
| B15 | Six runs、discard first、geomean、normalization | statistics |
| B16 | Original per-workload plots; aggregate only after raw view | outliers/fairness |
| B17 | Platform-by-platform adapter and evidence matrix | retargeting claim |
| B18 | Current source fingerprint、C001--C007、worklist、gap plan | provenance/current status |
| B19 | Future experiment design: stable identity contract、exploit corpus、source-frozen matrix、FFI frontend | dissertation direction |

## 10. Opening、transition 与 closing 脚本

### 60 秒 opening（可逐字排练）

> Allocators can observe layout and runtime state, but the conventional Rust allocation API does not expose language-level information such as type or module context. Rust already knows that information when many heap objects are created. My research question is whether a paired compiler/runtime can carry those semantics without requiring source annotations on supported paths, and whether the resulting policy boundary remains reusable across userspace, kernels, and constrained systems. UniAlloc explores that question through an optional semantic API, compiler-assisted extraction, and a retargetable allocator runtime. I will show what this architecture enables, its trust and compatibility contract, and what the evidence does and does not establish.

### 三个关键 transition

1. **Gap → design**： “If the missing resource is semantic information, the first design question is how to carry it without making compatibility conditional.”
2. **Design → policy**： “A semantic channel matters only if it changes a meaningful allocator decision; type-isolated reuse is the representative case study.”
3. **Policy → evaluation**： “The right evaluation is therefore not one benchmark number; it is a set of separate tests for feasibility, cost, coverage, and retargeting.”

### 30 秒 closing

> UniAlloc’s central result is that heap semantics do not have to disappear at the allocator boundary. A compiler-assisted, optional channel can expose trusted semantics while preserving a conventional fallback, and a policy/mechanism separation can reuse the allocator across different deployment environments. The next falsifiable question is the minimal stable identity contract that remains useful under partial coverage, FFI, and adversarial conditions; the source-bound evaluation matrix is how we will test that contract, not the contribution itself.

## 11. 排练与完成标准

### 四轮排练

1. **逻辑排练，不计时**：每页只说一句 takeaway；若两页 takeaway 相同，合并。
2. **45 分钟排练**：记录每个 section 的实际时间，不要只看总时间；48 分钟只是硬上限。
3. **中断排练**：请一人随机在 Slides 4、13、17、22、28 打断；练习 20 秒回答后回到论证链。
4. **42 分钟排练**：按第 5 节剪页，验证没有丢失 H1/H2/H3 的 closure。

MIT 的实践指南建议为每页写一句 takeaway 并向不同技术背景的人排练；这正是本模板把每页写成 answer-title 的原因。参考：[Practical Advice for Preparing Your Qualifying Exam Presentation](https://mitcommlab.mit.edu/meche/2021/04/20/practical-advice-for-preparing-your-qualifying-exam-presentation/)。

### 主 deck 完成定义

- [ ] 31 张主幻灯片排练目标为 45 分钟，任何一次完整排练都不超过 48 分钟。
- [ ] 每一张标题都是完整 claim sentence。
- [ ] 每个数字都有 evidence badge、baseline、N、toolchain/hardware 和 date/digest。
- [ ] H1/H2/H3 各有一张 `Supports / Does not establish` 小结。
- [ ] Historical paper result 与 current implementation/current claim-grade evidence 视觉上不可混淆。
- [ ] 19 张 backup 页可在 10 秒内跳转。
- [ ] 能在 20 秒和 90 秒两种长度回答表中 20 个问题。
- [ ] Opening 60 秒、closing 30 秒可不看稿完成。
- [ ] 答辩前重新生成 Slide 28/B18 的 live HEAD、H1/H2/H3 functional probe summary 和 deferred-claim status。

## 12. 最小 source map

以下是制作 slides 时优先打开的文件；不要从聊天记录复制事实。

| 目的 | 权威入口 |
|---|---|
| Research question/contributions/scope | `../rust-alloc-paper/intro.tex:217-286` |
| Design principles | `../rust-alloc-paper/rethink.tex:1-57` |
| Paper architecture | `../rust-alloc-paper/sys.tex:52-114` |
| Related-work/novelty boundary | `../rust-alloc-paper/discuss.tex:6-66` |
| Original evaluation method/results | `../rust-alloc-paper/eval.tex:63-91,168-210,233-303,314-344,380-455` |
| Runtime/platform selection | `unialloc/src/lib.rs:89,129-150` |
| Semantic metadata/API | `unialloc/src/alloc_api/type_isolation.rs` (`AllocationMetadata`, semantic alloc/dealloc/realloc) |
| GlobalAlloc semantic/fallback routing | `unialloc/src/cache/mod.rs` (`unsafe impl GlobalAlloc for RustAllocator`) |
| Compiler MIR rewrite | `tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs` |
| Vec realloc/isolation actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_vec_realloc_identity_probe.rs`、`tools/unialloc-rustc-pass/test_mir_vec_realloc_identity_probe.py` |
| Cross-thread Box-to-Vec actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_cross_thread_box_slice_into_vec_rebind.py` |
| String-to-Vec actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_string_into_bytes_rebind.py`、`unialloc/tests/string_into_bytes_rebind.rs` |
| Vec-to-boxed-slice actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_vec_into_boxed_slice_rebind.py`；runtime regressions `vec_into_boxed_slice_transfers_exact_and_moved_shrink_identities`、`vec_into_boxed_slice_rejection_preserves_exact_source_policy`、`vec_into_boxed_slice_missing_record_suppresses_outer_and_auto_attribution` |
| VecDeque same-layout actual-rewrite/isolation probe | `tools/unialloc-rustc-pass/test_mir_vecdeque_same_layout_type_isolation.py` (`5eb25f5`; hosted/fixed one-shot functional evidence) |
| VecDeque capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_vecdeque_capacity_outer_owner.py` (`c02baa6`; current/pinned exact capacity paths, fail-closed negatives, functional only) |
| Rc outer-owner actual-rewrite/isolation probe | `tools/unialloc-rustc-pass/test_mir_rc_new_outer_owner.py` (`1bd0c9d`; current/pinned functional evidence, exact constructor boundary) |
| HashMap capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_hashmap_with_capacity_outer_owner.py` (`715ba13`; current/pinned functional evidence, cache-counter oracle only) |
| HashSet capacity outer-owner actual-rewrite probe | `tools/unialloc-rustc-pass/test_mir_hashset_with_capacity_outer_owner.py` (`39c827b`; current/pinned functional evidence, cache-counter oracle only) |
| Multi-crate same-type-id module isolation probe | `tools/unialloc-rustc-pass/test_mir_multicrate_module_isolation.py` (`0704852`; Cargo metadata plus no-metadata direct-rustc fallback; functional only) |
| Lifetime-hint cache isolation probe | `unialloc/tests/type_isolation_lifetime_hint.rs` (`eadfa9c`; hosted/fixed-heap manual metadata lifecycle; functional only) |
| Placement-hint cache isolation probe | `unialloc/tests/type_isolation_placement_hint.rs` (`a379f23`; hosted/fixed-heap manual metadata lifecycle; functional only) |
| Plain type-cache forced-key-collision regressions | `unialloc/src/alloc_api/type_isolation.rs` (`8bc2809`/`6700ca1` exact identity + `2e3bc4c` bounded-probe exhaustion; hosted/fixed functional only) |
| Metadata-segregated collision/tamper fail-stop | `unialloc/src/alloc_api/type_isolation.rs` (`3ccd464`; forced inline/materialized exact-identity collisions, structural discriminator/null-pointer tamper, and authenticated full-bucket projection; hosted/fixed functional only) |
| Cross-thread authenticated policy composition regression | `unialloc/src/alloc_api/type_isolation.rs` (`5d0822a`; exact test `cross_thread_authoritative_policy_composition_quarantines_and_reuses_exact_identity`; hosted/fixed functional only) |
| Cross-thread authenticated split-realloc composition regression | `unialloc/src/alloc_api/type_isolation.rs` (`7ec42d4`; exact test `cross_thread_split_realloc_preserves_authenticated_old_and_new_identities`; hosted/fixed functional only) |
| Custom ADT destination discovery / fail-closed provenance | `tools/unialloc-rustc-pass/test_mir_non_generic_adt_destination_owner.py` (`465234c` unsafe positive-only attempt superseded by `504ed10`; current/pinned actual wrapper, exact internal constructors + Drop only) |
| Delayed-free duplicate-quarantine regression | `unialloc/src/alloc_api/type_isolation.rs` (`05d18be`; current-thread TLS quarantine, stale-mask and omitted-flag bypass; hosted/fixed-heap functional only) |
| Vec-to-IntoIter and String-to-Box-str pairing probes | `tools/unialloc-rustc-pass/test_mir_vec_into_iter_rebind.py`、`tools/unialloc-rustc-pass/test_mir_string_into_boxed_str_rebind.py`、`unialloc/tests/string_into_boxed_str_rebind.rs` (`32c3c5b`) |
| Box-str-to-String actual-rewrite/safety probe | `tools/unialloc-rustc-pass/test_mir_boxed_str_into_string_rebind.py`、`unialloc/tests/boxed_str_into_string_rebind.rs` (`ded36de`; current-rustc functional only) |
| Supported/ambiguous Clone actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_ambiguous_clone_fallback_probe.rs`、`tools/unialloc-rustc-pass/test_mir_ambiguous_clone_fallback_probe.py` |
| Cargo multi-crate target allowlist regression | `tools/unialloc-rustc-pass/test_mir_wrapper_target_allowlist.py` |
| Nested unwind actual-rewrite probe | `unialloc/src/bin/rustc_driver_mir_semantic_scope_unwind_probe.rs`、`tools/unialloc-rustc-pass/test_mir_semantic_scope_unwind_probe_runner.py` |
| Arbitrary dependency-factory fail-closed provenance | `tools/unialloc-rustc-pass/test_mir_dependency_factory_provenance_fail_closed.py` (`9a02767`; current/pinned actual wrapper, exact control only) |
| Clone classifier fail-closed fixture | `tools/unialloc-rustc-pass/fixtures/mir_clone_candidate_classification.rs`、`tools/unialloc-rustc-pass/test_mir_type_isolation_security_probe.py` |
| Bounded current mechanism validation | `docs/allocator-mir-and-backend-validation.md` |
| Current actual-rustc identity/recovery replay | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/type-isolation-actual-rustc-2e3c0e2-a7b5f75-20260712/validation-summary.json` |
| Earlier presentation bundle at `a51960d` | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-type-isolation-a51960d-20260712T202813Z/summary.json`（HEAD `a51960d92a7c72deabaf25fc23e985c3b26c09a5`；scoped fingerprint `f23eda54...`；Oxipng build/run PASS；6/383/320 direct/scope/Drop；static transfer `12/12`；dynamic `3/1/2`；oracle wrong-type non-reuse/same-type reuse、mismatch/corrupt `0/0`；whole-run corrected mismatch `13`、fail-closed `575`；660 UniAlloc + 430 std-bench PASS；summary SHA `bc6f32805e58cb021223dde2e01a91887cbe36e653f5ff73257823349a12e685`） |
| Earlier source-bound H1 actual-rewrite evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-current-head-94b2523d8823-20260712T060330Z/identity-hash-manifest.json`（historical to exact source; do not rebind） |
| External Rust application rewrite/isolation evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-typeiso-current-576df61-20260712c-enriched/oxipng-realapp-repro-summary.json`（collection HEAD `576df61` + stable adapter，byte-identical commit `9bb9f8d`；`f8f0d90` 后为 58/60 scoped hashes，仅 post-run summarizer/test 改变；one-shot build/run；6/383/320 applied + 6 exact Box-to-Vec transfer candidates/applied/selected rows、575 fail-closed、runtime `904/1070`/64 rows、13 recovery-corrected non-exact、bounded oracle PASS；summary SHA-256 `cd14bf7bdcbff8fe99d5fc6d97888ce2065050c527b423baa63b856857bdd43b`；enrichment 确定性重放 preserved raw，program rerun=false，direct count 独立）；这不表示 full working tree clean，也不是 performance/natural-app universal isolation evidence。原 `...20260712b` 与历史 `427583b`、`af342f7` artifacts 均保持 append-only，不能跨 revision rebinding/归因。 |
| Earlier source-bound external Rust functional run | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-typeiso-46d5aaa-20260712e/oxipng-realapp-repro-summary.json`（HEAD `46d5aaa...`；pass SHA `52ab1465...`；one-shot build/run；output SHA `565f253e...`；6/367/320 direct/scope/Drop；transfer static `12/12`、dynamic `3/1/2`；529 fail-closed；59 runtime rows；whole-run mismatch `0`；oracle PASS；summary SHA `f5b8ad7c...`）；该 run 早于 `0704852` module-id hardening，不得跨 revision rebinding；无 timing/performance claim。 |
| Latest accepted source-bound external Rust functional run | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-current-head-19ffb71-20260712-one-shot/acceptance.json`（claim-bearing source `19ffb710...`；pinned Oxipng v4.0.3 与 `nightly-2022-07-01` one-shot build/run `0/0`；output SHA `565f253e...`；direct/scope/Drop `6/310/320`；static transfer candidate/applied/selected `12/12/12`、runtime `3/1/2`；fail-closed semantic/Drop `516/119`；53 runtime rows；injected wrong-type non-reuse / same-type reuse PASS；whole-run mismatch `1` 且状态为 `recovery_corrected_non_exact`；acceptance SHA `efa66ec1...`）。该 artifact 早于 current development source（latest code-bearing commit `3ccd464`），只能支持 `19ffb71` revision 的 bounded functional/diagnostic claim；不得 rebinding，也不支持 whole-app exact pairing、universal coverage、current-source external-app counts 或 performance。 |
| Current-source external Rust ownership-transfer execution | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/oxipng-ownership-runtime-38b8b59-20260712d-success/oxipng-realapp-repro-summary.json`（HEAD `38b8b59c...`，pass SHA `c2c83b...`，scoped fingerprint `8609ff...`，static transfer `6/6`，dynamic `1/1/0`，`png::PngData::output` old owner `Box<[u8; 8]>` / `exact_immediate_box_array_unsize`，output SHA `565f...`，summary SHA `292c8f...`）；此前 `...6d955c0...failed-after-first-repair` artifact 保留为 `1/0/1` 历史失败。单次 diagnostic functional only；非 whole-program、benchmark 或性能。 |
| Source-bound H2 lifecycle evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h2-20260712T0604Z/audit.json`、`sha256sums.txt` |
| Source-bound H3 fixed/hosted smoke evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/presentation-h3-20260712T060354Z-94b2523d8823/sha256-manifest.json` |
| Current-source Wine 10 FLS lifecycle evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/windows-wine10-fls-38b8b59-20260712b/summary.json`（fresh Windows GNU cross-build；exact tests `3/3`；exe SHA `7d4e060d...`；Wine image、runner、source-input hashes 与 transcripts preserved；summary SHA `d44dbe74...`）；bounded Wine functional only，非 native-Windows universality/performance。 |
| Current-source Redox build/codegen/ABI evidence | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/redox-current-source-b3f2cad-20260712/summary.json` |
| BlogOS fixed-heap publication/final-link contract | `tools/blogos-contract/` (`00a187b`, `a581cb4`; no_std ELF/global allocator/boot init/panic handlers + 5 host state-machine regressions; external boot runtime missing) |
| Rust-for-Linux final-crate force-link contract | `tools/rust-for-linux-link-contract/`、`kernel/kernel-modules/benchmarking/rust_bench.rs` (`bd9d927`; real bridge and 11 required symbols; external kernel runtime missing) |
| Constrained-platform ABI layout parity | `tools/rust-for-linux-link-contract/src/abi_layout_contract.rs`、`test_rust_for_linux_link_contract.py` (`315cfc4`; 5 records, 75 offsets/language; local-host C compiler boundary) |
| Cache/footprint controls | `docs/allocator-memory-footprint.md` (`d87d5e0`, `25d316c`; hosted footprint reduction + matching-saturation correctness repair; diagnostic only) |
| PAC functionality vs cost boundary | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/pac-current-head-94b2523d8823-20260712T060647Z/`、`docs/evaluation-toolchains.md:272-280` |
| Hugepage domain/fallback vs backing boundary | `.omx/ultragoal/artifacts/G002-unialloc-functional-correctness-and/hugepage-domain-smoke-20260712a/hugepage-domain-smoke-summary.json`；current domain/fallback PASS，real backing MISSING |
| Current claim status | `evaluation/results/claim_check_current.json` |
| Missing claim requirements | `evaluation/results/overclaim_worklist.json` |
| Deferred paper-performance scope | `evaluation/results/paper_performance_gap_plan.json`、`.omx/handoff/g001-performance-campaign-stop-user-objective-change-20260712T030350Z.json` |
| Platform evidence | `docs/c007-redox-boot-evidence.md`；`evaluation/results/platform_matrix_audit.json` 是 2026-07-10 的 historical/stale aggregate，仍含已修复 Redox blocker 与旧 source digest，不得作为 current aggregate 引用 |
| Historical G001 freeze/partial records | `/Users/hqzhao/Downloads/UniAlloc-G001-freeze-235549b2/evaluation/raw/source-freeze-required-bound-plan-235549b2-20260711a/final-verification.json`；20 accepted records remain historical/diagnostic only |
| Active implementation goal/status | `.omx/ultragoal/goals.json`、`.omx/ultragoal/ledger.jsonl`、live `git rev-parse HEAD` |

---

**最终选择：** 把主 deck 做成“conventional Rust semantic gap → trusted optional compiler channel → bounded representative policy → retargetable boundary → evidence judgment”的单条论证。这样 slide 更容易制作，因为每页只服务一个假设；问答也更轻松，因为所有回答都能回到 H1/H2/H3、compatibility/TCB contract、evidence tier 和明确 boundary。
