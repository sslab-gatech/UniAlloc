# Lifetime-guided Transparent Hugepage allocation：设计与评估

## 决策

**eager lifetime-guided THP：GO，适合作为默认部署候选与 qualifier/paper mechanism point。**

**runtime-validated epoch collapse：MECHANISM GO，当前实现的 lifecycle 与瞬时内存代价过高。**

UniAlloc 现在把 lifetime placement policy 与物理 page backend 分离。相同的
Rust `(callsite, type_id, module_id, lifetime, confidence)` 信号可以选择 2 MiB
显式 HugeTLB，也可以选择 Linux anonymous THP。正式 80-process 配对实验中，
long-only eager THP 相对系统默认 allocator 提升 dependent-touch **12.69%**，
bootstrap 95% 区间为 **[+5.58%, +18.94%]**；相对同样 lifetime packing 的
`MADV_NOHUGEPAGE` control 提升 **8.45%**，区间为 **[+2.39%, +13.65%]**。
steady THP backing coverage 为 **100%**，且无需应用预留 HugeTLB pool。

runtime death epoch 也确实让静态 hint 更有用：5% 对称分类误差下，延迟到 phase
boundary 再 collapse 将已分类集合的 placement failure 从 **4.990%** 降到
**1.263%**，降低 **74.68%**，并把 false-positive placement 清零；confidence
版本从 **0.582%** 降到 **0.151%**，降低 **74.13%**。同步 collapse 带来
**57.79%** allocator lifecycle overhead 和 **41.50%** 最大有效驻留内存增长，
因此当前推荐 eager long-only THP，epoch validation 保留为研究机制与后续异步化目标。

本证据为 synthetic paired allocator evaluation，`claim_grade=false`。真实 Rust
应用的 profile coverage、held-out classifier calibration、吞吐和 tail latency 仍需单独验证。

## 机制

### Page backend 与 lifetime policy 正交

新增接口：

```rust
pub enum LifetimePageBackend {
    ExplicitHugeTLB,
    TransparentHugepage,
}

lifetime_hugepage_configure_with_backend(policy, backend)
lifetime_hugepage_backend()
```

原有 `lifetime_hugepage_configure(policy)` 保持 explicit HugeTLB 行为，已有调用与
compiler integration 继续可复现。THP 实验显式选择
`LifetimePageBackend::TransparentHugepage`。

| Placement policy | THP 行为 | 用途 |
|---|---|---|
| `SegregatedHugepage` | classified objects 进入 2 MiB-aligned anonymous VMA + `MADV_HUGEPAGE` | all-THP upper control |
| `LongLivedHugepage` | accepted long objects 使用 eager THP；ephemeral 使用 `MADV_NOHUGEPAGE` ordinary extent | 推荐部署候选 |
| `EpochCohortHugepage` | long candidate 初始 `MADV_NOHUGEPAGE`；跨 epoch 存活且 occupancy ≥50% 后执行 `MADV_HUGEPAGE` + `MADV_COLLAPSE` | runtime validation mechanism |
| `SegregatedOrdinary` | 所有实验 extent 使用 `MADV_NOHUGEPAGE` | page-size causal control |

50% occupancy 是本 feasibility study 预注册的阈值。它表达“只 promotion 足够密集且
已经跨 phase 存活的 extent”，未声称是通用最优值。实现没有 allocator-side prefault；
probe 的正常对象写入提供 resident workload，避免通过额外触页人为抬高 RSS。

### runtime validation 如何修正静态 hint

```text
static exact identity + lifetime class + confidence
  -> eager path: accepted-long VMA becomes THP-eligible immediately
  -> validated path: accepted-long VMA starts NOHUGEPAGE
       -> object survives explicit epoch boundary
       -> extent live-slot occupancy >= 50%
       -> MADV_HUGEPAGE
       -> MADV_COLLAPSE
       -> point-in-time confirmed placement
  -> death epoch supplies actual short/long truth
  -> predictor and physical-placement TP/TN/FP/FN
```

false-long objects 在第一个 boundary 前已经释放，因此 delayed collapse 阻止它们
进入 hugepage-positive placement。低 occupancy survivor 继续保持普通页；它们进入
placement FN 与 low-occupancy skip 指标。这个策略在正式 trace 中把全部
false-positive placements 过滤为 0，同时让 recall 从静态 **95.016%** 轻微降到
runtime-confirmed **94.946%**。

## 证据口径

Linux 将 THP promotion/demotion 交给内核，并提供 `MADV_HUGEPAGE`、
`MADV_NOHUGEPAGE` 与 `MADV_COLLAPSE` 控制；完整语义见
[Linux Transparent Hugepage documentation](https://docs.kernel.org/admin-guide/mm/transhuge.html)。
本评估使用以下严格边界：

- `MADV_HUGEPAGE` success 表示 VMA eligibility；实际 backing 由进程
  `/proc/self/smaps_rollup` 的 `AnonHugePages` 证明。
- `/proc/vmstat` 的 `thp_fault_alloc` 与 `thp_collapse_alloc` 是 system-wide
  corroboration，单独使用时不提供进程归因。
- claim-bearing THP 与 ordinary control 要求 host mode 为 `madvise`，并要求
  `smaps_rollup` 可读取、可解析。
- eager THP 要求全部 steady THP-backend VMA 的 backing coverage ≥95%。
- epoch THP 要求全部 steady collapse-confirmed extent 的 backing coverage ≥95%；
  主动跳过的 sparse candidate 单独报告为 backend-VMA coverage。
- collapse accounting 要求 `eligible = attempts + advice failures` 且
  `attempts = successes + collapse failures`。
- effective resident memory 为 `VmRSS + HugetlbPages`，采样 initial peak、
  post-first-epoch、每个 live-wave peak 与 steady state，并取最大值。
- allocator lifecycle 包含首次同步 epoch promotion；`/proc` evidence read 时间
  单独统计并从 lifecycle 中扣除。

显式 HugeTLB 使用 persistent reserved pool；THP 使用普通 anonymous memory 的动态
promotion。HugeTLB pool 语义见
[Linux HugeTLB documentation](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html)。

## 实验设置

| 项目 | 配置 |
|---|---|
| Host | 2-socket AMD EPYC 9354；64 cores / 128 logical CPUs |
| Kernel | Linux 6.8.0-111-generic x86_64 |
| THP | global `madvise`；defrag `madvise`；2 MiB PMD size |
| Pinning | CPU 15；NUMA memory node 0 |
| HugeTLB control | 4,096 × 2 MiB persistent pages；所有 explicit samples 要求 zero fallback |
| Process workload | 262,144 initial 4 KiB objects；50% long；8 exact types / truth class |
| Trace | persistent-long cohort + 3 ephemeral waves；524,288 allocation generations |
| Prediction noise | false-long 5%；false-short 5%；confidence outcome overlap 10% |
| Confidence | threshold 80；correct 90；error 60；Unknown rate 0 |
| Touch | 4 warmup passes；64 dependent-pointer measured passes |
| Matrix | 8 cases × 10 fresh processes = 80 samples；repeat 内 randomized order |
| Pairing | 同 repeat 使用相同 seed 与 prediction trace digest |

八个 cases：

1. `system-default`
2. `ordinary-no-thp`
3. `all-thp`
4. `static-long-thp`
5. `confidence-thp`
6. `static-epoch-thp`
7. `confidence-epoch-thp`
8. `explicit-hugetlb`

复现：

```bash
PYTHONHASHSEED=0 python3 \
  evaluation/scripts/lifetime_thp_allocator_experiment.py \
  --run-id lifetime-thp-final-20260714 \
  --objects 262144 --slot-bytes 4096 --types-per-truth 8 \
  --long-fraction 0.5 --false-long-rate 0.05 --false-short-rate 0.05 \
  --confidence-threshold 80 --correct-confidence 90 --error-confidence 60 \
  --confidence-overlap-rate 0.10 --unknown-rate 0.0 \
  --ephemeral-waves 3 --warmup-passes 4 --passes 64 --repeats 10 \
  --numa-node 0 --cpu 15 --seed 20260714 --timeout 900
```

Tracked evidence：

- `docs/evidence/lifetime-thp-final-20260714/manifest.json`
- `docs/evidence/lifetime-thp-final-20260714/samples.jsonl`
- `docs/evidence/lifetime-thp-final-20260714/summary.json`
- `docs/evidence/lifetime-thp-final-20260714/verification.txt`
- `docs/evidence/lifetime-thp-final-20260714/host-before.txt`
- `docs/evidence/lifetime-thp-final-20260714/host-after.txt`

## 实际 backing 与 THP 需求

| Case | Median AnonHugePages delta | Steady strict coverage | THP collapse | 说明 |
|---|---:|---:|---:|---|
| `all-thp` | 1,028 MiB | 100% | — | all classified placements use THP |
| `static-long-thp` | 514 MiB | 100% | — | long-only eager THP |
| `confidence-thp` | 442 MiB | 100% | — | low-confidence objects abstain |
| `static-epoch-thp` | 512 MiB | 100% confirmed；99.61% steady VMA | 256/256 median，0 failures | sparse candidates stay 4 KiB |
| `confidence-epoch-thp` | 440 MiB | 100% confirmed；99.55% steady VMA | 220/220 median，0 failures | confidence + runtime survival |
| `ordinary-no-thp` | 0 | 100% no-THP gate | — | `MADV_NOHUGEPAGE` control |
| `explicit-hugetlb` | 0 AnonHugePages | real HugeTLB；0 fallback | — | deterministic control |

long-only eager THP 相对 all-THP 将 THP footprint 从 **1,028 MiB** 降到
**514 MiB，减少 50%**。confidence 进一步降到 442 MiB；这部分额外降低来自
abstention coverage tradeoff。全局 HugeTLB pool 在实验前后都为 4,096 free
pages，THP arms 本身没有消耗 persistent pool。

## 分类与 runtime-confirmed placement

所有 static/epoch/explicit cases 使用完全相同的 paired prediction trace。表中
classification rate 面向已分类 generation；confidence coverage 明确显示 abstention。

| Policy | Classification coverage | Static success / failure | Runtime placement success / failure | Runtime precision / recall |
|---|---:|---:|---:|---:|
| static + explicit HugeTLB | 100% | 95.010% / 4.990% | 95.010% / 4.990% | 86.388% / 95.016% |
| static + epoch THP | 100% | 95.010% / 4.990% | **98.737% / 1.263%** | **100% / 94.946%** |
| confidence + epoch THP | 86.013% | 99.418% / 0.582% | **99.849% / 0.151%** | **100% / 99.398%** |

static trace 的 median false-positive prediction 为 19,623 generations；epoch THP
的 runtime placement FP 为 **0**。confidence trace 的 median static FP 为
1,955.5；runtime placement FP 同样为 **0**。低 occupancy skip 与 false-short
classification 形成少量 FN，因此 precision 提升到 100%，recall 保持接近原静态值。

这个结果支持用户提出的设计：静态分析给出 lifetime/confidence，runtime 以 death
epoch 验证实际生存期，然后把验证结果用于物理 page promotion。runtime truth 依赖
有语义的 phase boundary；服务 workload 需要 request、transaction 或时间窗口定义。

## 性能与内存

bootstrap 区间基于 10 个 paired repeats。正数 improvement 表示 target 更好；表内
resident 使用完整 wave peak 口径。

### 推荐路径：static long-only eager THP

| Baseline | Dependent touch | Allocator lifecycle | Max effective resident | Steady effective resident |
|---|---:|---:|---:|---:|
| system default | **+12.69%** `[+5.58%, +18.94%]` | **+3.58%** `[+2.52%, +4.66%]` | +0.11% | **+8.46%** `[+7.75%, +9.09%]` |
| ordinary no-THP | **+8.45%** `[+2.39%, +13.65%]` | **+28.32%** `[+27.71%, +28.95%]` | -0.15% | -0.13% |
| explicit HugeTLB | -0.73% `[-6.86%, +5.03%]` | **+1.69%** `[+1.10%, +2.23%]` | approximately equal | approximately equal |

THP 与 explicit HugeTLB 的 touch 区间跨过零，二者在本 trace 的访问性能相当。
THP 保留动态 ordinary-memory semantics，免去应用侧 persistent pool provisioning。
relative to ordinary control 的 touch 与 lifecycle 改善确认了 large-page translation/
fault mechanism；steady resident 基本相同，说明 page backend 本身没有创造
fragmentation reduction。

static long-only 与 ordinary control 都保留 **937 MiB** arena capacity 和
**425 MiB** steady slack。lifetime/identity packing 决定 extent reclaim 与碎片；
THP 决定 backing 与 translation behavior。

### confidence eager THP

confidence eager THP 相对 ordinary control 的 dependent-touch 区间为
`[-7.63%, +6.44%]`，直接 latency 结果开放；allocator lifecycle 增加 **4.68%**。
steady effective resident 减少 **28.64%**，arena retained 从 937 MiB 降到
520 MiB，slack 从 425 MiB 降到 79.4 MiB。这个内存结果主要来自 confidence
abstention 降低错误 cohort contamination；abstained objects 回到现有 allocator。

### runtime-validated epoch THP

| Case vs ordinary no-THP | Dependent touch | Allocator lifecycle | Max effective resident | Steady effective resident |
|---|---:|---:|---:|---:|
| static epoch THP | **+11.94%** `[+3.17%, +19.45%]` | **-57.79%** | **-41.50%** | -0.69% |
| confidence epoch THP | **+13.62%** `[+9.37%, +19.09%]` | **-50.94%** | **-8.38%** | **+27.98%** |

这里的负数表示 overhead / resident growth。static epoch 的 median first promotion
为 170.36 ms；confidence epoch 为 155.04 ms。同步 `MADV_COLLAPSE` 发生在 arena
lock 内。epoch-cohort 还阻止新 wave 复用旧 cohort extent，使 static epoch 的
median max effective resident 达到 **1,470 MiB**，普通 control 为约 **1,041 MiB**。

runtime validation 显著提高 placement precision，当前同步实现拥有明确成本。高收益
后续方向是把 surviving extent 放入 promotion queue，在 phase boundary 只做状态
切换，由后台线程批量 collapse；同时允许 safe canonical-bucket reuse 或设置内存
budget，限制 cohort isolation 的 wave peak。

## 与 closest prior work 的边界

| 系统 | 已建立的机制 | 本实现的可陈述增量 |
|---|---|---|
| [LLAMA, ASPLOS 2020](https://colinraffel.com/publications/asplos2020learning.pdf) | allocation-context lifetime prediction、多 lifetime classes、2 MiB placement、动态误预测恢复 | exact Rust identity/profile binding、confidence abstention、death-epoch confusion matrix、THP advice/backing evidence contract |
| [TEMERAIRE, OSDI 2021](https://www.usenix.org/system/files/osdi21-hunter.pdf) | fullness/longest-free-range hugepage packing、tail donation、subrelease | semantic lifetime/confidence/epoch 作为 packing 输入；survival-gated physical promotion |
| [TCMalloc warehouse-scale study, ASPLOS 2024](https://people.csail.mit.edu/delimitrou/papers/2024.asplos.memory.pdf) | short/long span separation in dedicated hugepage sets；fleet-scale throughput/memory/TLB evaluation | compiler-to-allocator Rust contract与 runtime placement validation；当前证据为单机 synthetic allocator trace |

prior work 的 workload、allocator、fragmentation denominator 与硬件不同，直接计算
“超过多少”缺少共同基线。本实验可以陈述自身 controls：long-only THP 相对 all-THP
减少 50% THP footprint；相对 ordinary control 提升 8.45% dependent touch；
runtime epoch validation 将 selective placement failure 降低约 74%。跨论文数字保留
为背景定位。

## 可 defend 的 claim

- UniAlloc 的 lifetime policy 可以选择 production allocator path 上的实际 anonymous
  THP backend，同时保持 exact identity、fallback 与 symmetric deallocation provenance。
- eager long-only THP 在该 host 的 80-sample paired trace 中达到 100% steady backing，
  将 THP footprint 减半，并相对 ordinary no-THP control 提升 dependent touch。
- static lifetime + runtime death epoch 能把 false-long predictions 留在普通页，清零
  本 trace 的 false-positive THP placement。
- confidence 与 runtime validation 分别控制 classifier contamination 与 physical
  promotion，二者的 coverage、success/failure、precision/recall 均可闭合。
- advice、point-in-time collapse 与 steady actual backing 使用独立指标；memory、timing
  和 byte-epoch accounting 全部通过严格 gate。

## 开放边界与推荐

- profile confidence 与 error model 是 synthetic；真实 Rust workload 需要 training /
  held-out validation 和 per-site calibration。
- 当前 workload 固定 4 KiB slot、单 NUMA node、显式 phase boundary；多 size class、
  cross-thread service trace 与长期 THP split behavior仍待验证。
- `/proc/vmstat` 是 system-wide corroboration；进程归因来自 smaps。实验期间主机有
  unrelated background work，CPU pinning、case randomization、paired seeds 与 bootstrap
  区间降低了噪声影响。
- 50% promotion threshold 需要 25%/50%/75% sweep；完整策略还需要 memory budget 与
  collapse latency budget。
- eager THP 作为下一阶段主路径：

```rust
lifetime_hugepage_configure_with_backend(
    LifetimeHugepagePolicy::LongLivedHugepage,
    LifetimePageBackend::TransparentHugepage,
)
```

qualifier/paper 的核心表述应使用 **Rust compiler/runtime contract for
lifetime-guided THP placement and survival-validated promotion**。部署结果使用 eager
THP；runtime epoch 结果用于证明 hint 可以通过在线验证变成更精确的 physical
placement，异步 promotion 是下一项工程优化。
