# Lifetime confidence + runtime death epoch：HugeTLB 分配评估

## 结论

**分类与碎片控制方向 GO；端到端性能方向 INCONCLUSIVE。**

用户提出的验证关系是正确的：静态侧给出 lifetime class 与 confidence，
runtime 以对象的分配 epoch 和释放 epoch 生成实际生存期标签，再计算
TP/TN/FP/FN。置信度阈值把 5% 对称误差下的已分类失败率从 **4.993%**
降到 **0.581%**，覆盖率为 **86.012%**；把 FP-heavy 下的已分类失败率
从 **3.746%** 降到 **0.430%**，覆盖率为 **87.007%**。将 abstention
产生的 `Unknown` 也计入实际放置后，端到端失败率分别为 **3.995%** 和
**2.874%**。

epoch-cohort 在独立的 rolling-overlap 实验中，把关键释放窗口的 HugeTLB
retained byte-epochs 降低 **50%**，把 `target + reset` 窗口降低 **33.33%**，
把完整三阶段周期降低 **20%**。其分配/释放 lifecycle 成本增加
**35.224%**，bootstrap 95% 区间为 **[+34.002%, +36.436%]**。这个结果
支持“epoch 隔离减少跨阶段页钉住”的碎片机制，同时把优化 fast path 和真实
应用收益保留为下一道证据门槛。

## 机制：静态推测、runtime truth、物理编排

```text
exact (callsite, type_id, module_id)
  -> profile-v2: {Ephemeral | LongLived, confidence 1..100}
  -> confidence threshold
       accepted -> lifetime_hint 1/2
       abstain  -> Unknown(0) -> existing allocator
  -> lifetime/size/alignment extent + exact 64 KiB identity region
  -> explicit phase advance
  -> death epoch truth + TP/TN/FP/FN
  -> optional epoch-cohort 2 MiB extent isolation
```

静态层使用 `unialloc-lifetime-profile-v2`。每条 profile 记录精确
`(callsite, type_id, module_id)`、二分类 lifetime 和 `1..=100` confidence。
编译器阈值接受高置信条目，低置信条目以
`profile_below_confidence_threshold` abstain；缺失、类型/模块 guard 不匹配、
重复或非法条目也进入 `Unknown`。v1 profile 保持兼容，并按 confidence 100
处理。

runtime 层在每个 routed allocation generation 上记录 `birth_epoch`。释放发生
在同一 epoch 时，实际类别为 ephemeral；跨过至少一个显式 phase boundary
时，实际类别为 long-lived。预测为 long-lived 表示 positive，真实 HugeTLB
backing 表示 placement positive，因此 runtime 同时报告 predictor confusion
matrix 和 actual-placement confusion matrix。moved realloc 结束旧 generation
并创建新 generation；in-place realloc 保留原 birth epoch。

epoch-cohort 策略继续使用 lifetime class 决定 HugeTLB/ordinary backing，并要求
不同 birth epoch 使用不同 2 MiB extent。旧 epoch 的可用 region 会从当前分配
列表中脱离，完整空 extent 立即 unmap。这个约束把“静态对象类别”扩展成
“类别 + 生命周期阶段”的物理页编排信号。

## 统计口径

- **对象计数**表示 allocation generations。一次 moved realloc 贡献旧、新两个
  generation；in-place realloc 仍为同一个 generation。
- **runtime byte confusion matrix** 使用 allocator 的 rounded arena slot bytes。
  该口径对应实际页占用粒度。
- **effective placement byte confusion matrix** 使用 requested payload bytes。
  该口径把 `Unknown` fallback 也纳入端到端放置质量。
- **coverage** = static classified generations / all allocation generations。
- **selective success/failure** 只在已分类集合上计算；success = `(TP+TN)/decided`。
- **end-to-end placement** 覆盖全部 generations。`Unknown` short fallback 计 TN，
  `Unknown` long fallback 计 FN。
- runtime 的 predictor matrix 覆盖进入实验 arena 且 death epoch 可观测的
  generations。delayed-free 与 non-cohort mixed-epoch region 进入独立 exclusion
  计数。正式主实验和 rolling 实验的 exclusion 均为 0。

真实部署中的 runtime truth 需要覆盖 profile 候选集合。可执行路径是先用阈值
0 或 shadow instrumentation 收集每个候选 site 的 death-epoch 分布，再校准
class/confidence，最后提高阈值。当前实现提供 aggregate runtime confusion
matrix；per-site 在线更新仍是后续工作。合成 probe 额外知道 `Unknown` 的生成
标签，因此能报告完整的 effective-placement matrix。

## 实验设置

### 分类、内存与访问实验

| 项目 | 配置 |
|---|---|
| Host / pinning | AMD EPYC 9354；CPU 15；NUMA node 0 |
| HugeTLB pool | 4,096 × 2 MiB = 8 GiB；所有 claim-bearing case 要求真实 HugeTLB 与零 fallback |
| 每次进程 | 262,144 个同时存活的 4 KiB 对象；50% long；8 个 exact types / truth class |
| Phase trace | 一个 persistent-long cohort，3 个 ephemeral waves；每次共 524,288 allocation generations |
| Scan | 4 warmup passes；64 measured dependent-pointer passes |
| 重复 | 每个 case 10 个 fresh processes；同一 triplet 的 prediction trace digest 完全配对 |
| Confidence | threshold 80；正确结果 confidence 90；错误结果 confidence 60；10% confidence/outcome overlap |
| Error arms | 5% symmetric：long→short 与 short→long 各 5%；FP-heavy：short→long 5%、long→short 0% |

10% confidence/outcome overlap 会让一部分正确预测 abstain，也会让一小部分错误预测
通过阈值。它构造了带噪但有信息量的 synthetic confidence；置信度仍含 10% 的
outcome-overlap 噪声。

### Rolling epoch-cohort 实验

| 项目 | 配置 |
|---|---|
| Host / pinning | CPU 10；NUMA node 0；真实 HugeTLB required |
| Geometry | 单一 exact identity；4 KiB slots；large cohort 496 objects / 31 regions；bridge 256 objects / 16 regions |
| Pair | 普通 `long-huge` 与 `epoch-cohort` 使用同一 seed、trace digest 和 live-set geometry |
| 周期 | overlap → 释放 large 后的 target → reset；5 warmup + 20 measured cycles |
| 重复 | 30 个 randomized fresh-process pairs；10,000 次 bootstrap |

该 geometry 让 bridge cohort 与前后 large cohort 发生可控重叠。两种策略拥有相同
live byte-epochs；它们的差异来自跨 epoch extent reuse 与 pinning。

## 分类成功率与失败率

下面的 FP/FN 是 10 次运行汇总后的 allocation-generation 计数。`confidence`
和 `confidence+epoch` 使用完全相同的预测 trace，因此分类数字相同；epoch 只改变
物理 extent cohort。

| Error model | 策略 | Coverage | Selective success | Selective failure | Selective precision | Selective recall | End-to-end success | End-to-end failure | End-to-end recall | FP / FN |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 5% symmetric | binary | 100.000% | 95.007% | 4.993% | 86.376% | 95.012% | 95.007% | 4.993% | 95.012% | 196,420 / 65,374 |
| 5% symmetric | confidence | 86.012% | 99.419% | 0.581% | 98.281% | 99.415% | 96.005% | 3.995% | 85.514% | 19,605 / 6,596 |
| 5% symmetric | confidence + epoch | 86.012% | 99.419% | 0.581% | 98.281% | 99.415% | 96.005% | 3.995% | 85.514% | 19,605 / 6,596 |
| FP-heavy 5% | binary | 100.000% | 96.254% | 3.746% | 86.967% | 100.000% | 96.254% | 3.746% | 100.000% | 196,420 / 0 |
| FP-heavy 5% | confidence | 87.007% | 99.570% | 0.430% | 98.365% | 100.000% | 97.126% | 2.874% | 89.999% | 19,605 / 0 |
| FP-heavy 5% | confidence + epoch | 87.007% | 99.570% | 0.430% | 98.365% | 100.000% | 97.126% | 2.874% | 89.999% | 19,605 / 0 |

置信度 abstention 在两个 error arms 中都减少 **90.019%** 的 false positives。
对称 arm 还减少 **89.910%** 的 false negatives。Selective failure 分别相对
降低 **88.364%** 和 **88.528%**；将 abstention 的 long objects 计为实际放置
FN 后，端到端 failure 仍相对降低 **19.985%** 和 **23.279%**。这两组数字把
“分类器更纯”和“系统最终放对页”分开呈现。abstained long generations 进入
ordinary fallback，使端到端 recall 分别从 **95.012%** 降到 **85.514%**、从
**100.000%** 降到 **89.999%**；这是降低 false-positive contamination 时付出的
HugeTLB coverage 代价。

## 内存、碎片与 timing controls

### 标签正确时的完整 baseline matrix

主表中的 confidence cases 使用 10% confidence overlap，因此约 10% 的正确标签
进入 `Unknown` fallback。`binary` 与 `oracle` 在该 arm 中有意采用等价的正确
标签；前者是 error triplet 的基线，后者是 truth ceiling。有效驻留内存为
`VmRSS + HugetlbPages`。

| 策略 | Static coverage | Steady effective resident | Steady arena retained | Steady arena slack | Peak HugeTLB | Alloc ns/generation | Dependent ns/touch |
|---|---:|---:|---:|---:|---:|---:|---:|
| raw default | — | 1,043.13 MiB | — | — | 0 MiB | 1,850.51 | 113.22 |
| ordinary segregated | 100% | 527.75 MiB | 512 MiB | 0 MiB | 0 MiB | 1,877.83 | 93.70 |
| all HugeTLB segregated | 100% | 527.74 MiB | 512 MiB | 0 MiB | 1,024 MiB | 395.14 | 106.84 |
| binary long→HugeTLB | 100% | 527.75 MiB | 512 MiB | 0 MiB | 512 MiB | 1,121.35 | 82.36 |
| confidence | 90.006% | 580.94 MiB | 462 MiB | 1.12 MiB | 462 MiB | 1,239.61 | 101.29 |
| confidence + epoch | 90.006% | 588.27 MiB | 462 MiB | 1.12 MiB | 462 MiB | 1,229.82 | 85.72 |
| oracle long→HugeTLB | 100% | 527.76 MiB | 512 MiB | 0 MiB | 512 MiB | 1,117.32 | 108.39 |

ordinary、all-HugeTLB、binary 和 oracle 相对 raw default 的 steady effective
resident reduction 都约为 **49.4%**。这项共同结果把主要内存收益归因于 lifetime
segregation 与 complete-extent release。binary/oracle 把 peak HugeTLB 从
all-HugeTLB 的 1,024 MiB 降到 512 MiB，体现了 long-only HugeTLB placement。
confidence 将 accepted-long HugeTLB demand 进一步降到 462 MiB，同时让 abstained
long objects 留在现有 allocator；因此 arena retained 更小，而 whole-process
resident memory 高于完全分类的 512 MiB controls。

### 误分类下的 contamination 与碎片

| Error model | 策略 | Steady resident | Steady retained | Steady slack | Peak HugeTLB | Retained byte-epochs |
|---|---|---:|---:|---:|---:|---:|
| 5% symmetric | binary | 952.60 MiB | 937 MiB | 425 MiB | 514 MiB | 2,811 MiB·epochs |
| 5% symmetric | confidence | 679.71 MiB | 520 MiB | 79.41 MiB | 442 MiB | 1,560 MiB·epochs |
| 5% symmetric | confidence + epoch | 687.11 MiB | 520 MiB | 79.41 MiB | 442 MiB | 1,560 MiB·epochs |
| FP-heavy 5% | binary | 553.78 MiB | 538 MiB | 26 MiB | 538 MiB | 1,614 MiB·epochs |
| FP-heavy 5% | confidence | 604.32 MiB | 465 MiB | 4.17 MiB | 465 MiB | 1,395 MiB·epochs |
| FP-heavy 5% | confidence + epoch | 611.68 MiB | 465 MiB | 4.17 MiB | 465 MiB | 1,395 MiB·epochs |

在对称误差下，confidence 相对 binary 减少 **44.504%** 的 retained byte-epochs/
steady retained，并减少 **81.315%** 的 steady slack。在 FP-heavy 下，retained
减少 **13.569%**，slack 减少 **83.954%**。FP-heavy 的 steady resident 升高反映
了 `Unknown` 回到现有 allocator 后的 RSS 成本；retained arena 与 HugeTLB
footprint 仍同步下降。这是 confidence abstention 的明确空间权衡。

### Timing 与 PMU

表中的单点 median `ns/touch` 缺少稳定的单调关系。配对 bootstrap 结果同样把
直接 latency 留在开放状态：oracle long→HugeTLB 相对 ordinary-segregated 的
improvement 区间为 **[-15.19%, +2.16%]**；5% symmetric 下
confidence+epoch 相对 binary 的区间为 **[-10.64%, +13.84%]**。这些区间跨过
零，主实验的 timing verdict 为 **INCONCLUSIVE**。

同 seed、CPU 15 的 `perf stat -r 5` whole-process 诊断显示：

| 配对 | cycles | L1 dTLB misses | L2 dTLB misses | 4 KiB reload L2 misses | page faults |
|---|---:|---:|---:|---:|---:|
| ordinary-segregated → correct binary | -6.175% | -20.323% | -80.511% | -85.424% | — |
| 5% symmetric binary → confidence | +23.033% | — | +85.789% | — | +80.635% |

第一行支持 long-only HugeTLB 的 translation mechanism。第二行显示 aggressive
abstention 用更多 ordinary/default pages 换取更低的误放置和页钉住，whole-process
cycles、TLB misses 与 faults 随之上升。PMU 范围包含 allocation、first touch、
warmup、scan、phase release 和 teardown，其角色是方向性机制诊断；主 paired
timing CI 继续承担 latency 判定。TLB events 的平均运行比例约为 62%，perf 已
进行 scaling；page-fault event 的运行比例为 100%。

## Epoch-cohort 的独立增量

persistent-long 主实验中，confidence 与 confidence+epoch 的 allocator-retained、
HugeTLB 和 ordinary byte-epoch counters 完全相同。该 trace 的 long cohort 始终
存活，对 epoch isolation 的额外 reclaim 价值缺乏识别力。rolling-overlap 实验
专门建立了这一识别窗口。

| Window | `long-huge` retained byte-epochs | `epoch-cohort` retained byte-epochs | Reduction | Live byte-epochs change |
|---|---:|---:|---:|---:|
| overlap | 80 MiB·epochs | 80 MiB·epochs | 0% | 0% |
| target：释放上一 large cohort 后 | 80 MiB·epochs | 40 MiB·epochs | **50.00%** | 0% |
| reset | 40 MiB·epochs | 40 MiB·epochs | 0% | 0% |
| target + reset | 120 MiB·epochs | 80 MiB·epochs | **33.33%** | 0% |
| full cycle | 200 MiB·epochs | 160 MiB·epochs | **20.00%** | 0% |

全部 30 个 pairs 的 ratio 相同，prediction trace、参数、live set、checksum 均配对；
runtime 验证 19,296 generations / sample，TP 为 19,296，exclusion、HugeTLB
fallback、mapping failure 与 release failure 均为 0；4,096 页全局池在每次进程
退出后恢复。target 窗口里，普通 long-huge 由 bridge objects 钉住 4 MiB，
epoch-cohort 保留 2 MiB。该差异直接来自 cohort extent isolation。

代价同样清晰：epoch-cohort allocation time 增加 **46.692%**，release time 增加
**19.408%**，合并 lifecycle ns/object 增加 **35.224%**。显式 epoch advance 的
相对成本约为 7.90×；60 次 measured advances 的绝对中位总时间为 13.77 µs。
当前实现为全局锁、固定表的研究路径；`FRAGMENTATION-GO` 适用于机制，
`INCONCLUSIVE` 适用于性能收益。

## 与 closest prior work 的边界

| 系统 | 已建立的机制 | 本实验的可陈述增量 | 数字比较边界 |
|---|---|---|---|
| [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf) | allocation-context lifetime prediction、多时间尺度 lifetime classes、2 MiB lifetime placement 与动态反馈/误预测恢复 | Rust exact `(callsite,type,module)` profile binding、compiler metadata continuity、confidence abstention，以及 runtime death-epoch confusion matrix | LLAMA 使用生产服务代码与 trace；本实验使用固定几何和合成置信度。两个实验的 fragmentation/accuracy 数字拥有不同 workload 与 denominator。 |
| [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf) | 依据 size、page fullness 和 longest-free-range 状态进行 Hugepage packing、tail donation 与 subrelease | semantic lifetime/confidence/epoch 作为 size/fullness 之外的编排输入，并以 rolling cohort 隔离阶段间 pinning | TEMERAIRE 在生产 allocator 与多应用上评估；本实验是单 size class 的 allocator microprobe。直接“抢多少”比例缺乏共同基线。 |

因此，paper point 应定位为 **Rust compiler/runtime contract for validated
lifetime-guided hugepage orchestration**。lifetime-aware hugepage placement、
动态 error recovery 与 fullness-aware packing 已由 prior work 建立。本实验提供的
新证据是：精确 Rust site 能携带 class + confidence，runtime death epoch 能量化
其正确性，confidence 能控制污染，epoch 能控制跨阶段物理页复用。

## 可 defend 的陈述与开放边界

### 可 defend

- runtime death epoch 为显式 phase workload 提供可闭合的 predictor/placement
  TP/TN/FP/FN；所有正式样本的 confusion 与 allocator accounting closure 通过。
- 置信度阈值在 5% 合成误差下显著提高 selective accuracy，并在计入 abstention
  后继续降低端到端 placement failure。
- confidence 在两类误差下减少 false-positive contamination、retained arena
  capacity 和 slack。
- epoch-cohort 在相同 live set 的 rolling trace 中减少 target-window 与 full-cycle
  retained byte-epochs。
- 所有 claim-bearing mappings 使用真实 2 MiB HugeTLB，fallback 为 0，实验前后
  persistent pool 均为 4,096 free pages。

### 开放边界

- profile confidence 是 synthetic calibration，当前数据尚未来自真实 Rust
  workload 训练与 held-out validation。
- runtime epoch truth 依赖有语义的 phase boundary。服务型 workload 需要请求、
  transaction、GC-like epoch 或时间窗口定义。
- 当前矩阵固定 4 KiB slot、单机 NUMA pinning 和有限 exact types；多 size class、
  跨线程 cache、并发 phase 与长期服务 trace 仍需评估。
- explicit HugeTLB `munmap` 把页面返还 persistent pool；返还 ordinary Linux RAM
  需要 pool shrink、surplus 或 THP 路径。
- rolling microprobe 已测得 +35.224% lifecycle cost。真实应用吞吐、tail latency、
  RSS/PSS、HugeTLB coverage 与 PMU 联合收益决定最终性能结论。

## 证据来源

- `docs/evidence/lifetime-confidence-epoch-final-20260714/main-summary.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/main-samples.jsonl`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/rolling-summary.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/rolling-samples.jsonl`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/perf/perf5-manifest.json`
- `docs/evidence/lifetime-confidence-epoch-final-20260714/perf/perf5-*.stdout`

最强的 presentation 顺序是：**静态 class+confidence → runtime death-epoch truth
→ selective 与 end-to-end 两套错误率 → contamination/slack reduction → rolling
epoch reclaim → 明确的 lifecycle cost 与真实-workload gate。**
