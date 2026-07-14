#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <time.h>
#include <unistd.h>

#ifndef PR_SET_THP_DISABLE
#define PR_SET_THP_DISABLE 41
#endif
#ifndef PR_GET_THP_DISABLE
#define PR_GET_THP_DISABLE 42
#endif

/*
 * Allocator-neutral large-page workload.
 *
 * Every allocator receives the same malloc/free trace from the same binary.
 * Long- and short-lived 4 KiB objects are interleaved, short-lived objects are
 * released, and a dependent pointer chase measures the retained working set.
 * Process memory is sampled outside every timed interval so /proc parsing does
 * not contaminate the latency measurements.
 */

typedef struct
{
  size_t objects;
  size_t slot_bytes;
  size_t passes;
  size_t warmup_passes;
  size_t waves;
  unsigned settle_ms;
  uint64_t seed;
} config_t;

typedef struct
{
  size_t rss_kib;
  size_t rss_anon_kib;
  size_t anonymous_kib;
  size_t anon_huge_kib;
  size_t hugetlb_kib;
  bool status_ok;
  bool rollup_ok;
} memory_kib_t;

static void usage(const char* program)
{
  fprintf(
    stderr,
    "usage: %s [--objects N] [--slot-bytes N] [--passes N] "
    "[--warmup-passes N] [--waves N] [--settle-ms N] [--seed N]\n",
    program);
}

static bool parse_size(const char* raw, size_t* value)
{
  char* end = NULL;
  errno = 0;
  unsigned long long parsed = strtoull(raw, &end, 10);
  if (errno != 0 || end == raw || *end != '\0' || parsed > SIZE_MAX)
    return false;
  *value = (size_t)parsed;
  return true;
}

static bool parse_u64(const char* raw, uint64_t* value)
{
  char* end = NULL;
  errno = 0;
  unsigned long long parsed = strtoull(raw, &end, 10);
  if (errno != 0 || end == raw || *end != '\0')
    return false;
  *value = (uint64_t)parsed;
  return true;
}

static bool parse_config(int argc, char** argv, config_t* config)
{
  *config = (config_t){
    .objects = 131072,
    .slot_bytes = 4096,
    .passes = 16,
    .warmup_passes = 2,
    .waves = 2,
    .settle_ms = 25,
    .seed = UINT64_C(20260714),
  };
  for (int index = 1; index < argc; index++)
  {
    if (index + 1 >= argc)
      return false;
    const char* option = argv[index++];
    const char* value = argv[index];
    size_t parsed_size = 0;
    if (strcmp(option, "--seed") == 0)
    {
      if (!parse_u64(value, &config->seed))
        return false;
    }
    else
    {
      if (!parse_size(value, &parsed_size))
        return false;
      if (strcmp(option, "--objects") == 0)
        config->objects = parsed_size;
      else if (strcmp(option, "--slot-bytes") == 0)
        config->slot_bytes = parsed_size;
      else if (strcmp(option, "--passes") == 0)
        config->passes = parsed_size;
      else if (strcmp(option, "--warmup-passes") == 0)
        config->warmup_passes = parsed_size;
      else if (strcmp(option, "--waves") == 0)
        config->waves = parsed_size;
      else if (strcmp(option, "--settle-ms") == 0 && parsed_size <= UINT32_MAX)
        config->settle_ms = (unsigned)parsed_size;
      else
        return false;
    }
  }
  return config->objects >= 4 && config->objects % 2 == 0 &&
    config->slot_bytes >= 128 && config->passes > 0 && config->waves > 0;
}

static uint64_t monotonic_ns(void)
{
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &now) != 0)
  {
    perror("clock_gettime");
    exit(2);
  }
  return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

static uint64_t xorshift64(uint64_t* state)
{
  uint64_t value = *state;
  if (value == 0)
    value = UINT64_C(0x9e3779b97f4a7c15);
  value ^= value << 13;
  value ^= value >> 7;
  value ^= value << 17;
  *state = value;
  return value;
}

static void shuffle(void** values, size_t length, uint64_t* state)
{
  for (size_t index = length; index > 1; index--)
  {
    size_t other = (size_t)(xorshift64(state) % index);
    void* temporary = values[index - 1];
    values[index - 1] = values[other];
    values[other] = temporary;
  }
}

static bool parse_kib(const char* line, const char* prefix, size_t* value)
{
  size_t prefix_len = strlen(prefix);
  if (strncmp(line, prefix, prefix_len) != 0)
    return false;
  unsigned long long parsed = 0;
  if (sscanf(line + prefix_len, "%llu", &parsed) != 1 || parsed > SIZE_MAX)
    return false;
  *value = (size_t)parsed;
  return true;
}

static memory_kib_t read_memory(void)
{
  memory_kib_t memory = {0};
  char* line = NULL;
  size_t capacity = 0;
  FILE* status = fopen("/proc/self/status", "r");
  bool rss = false, rss_anon = false, hugetlb = false;
  if (status != NULL)
  {
    while (getline(&line, &capacity, status) >= 0)
    {
      rss |= parse_kib(line, "VmRSS:", &memory.rss_kib);
      rss_anon |= parse_kib(line, "RssAnon:", &memory.rss_anon_kib);
      hugetlb |= parse_kib(line, "HugetlbPages:", &memory.hugetlb_kib);
    }
    fclose(status);
  }
  memory.status_ok = rss && rss_anon && hugetlb;

  free(line);
  line = NULL;
  capacity = 0;
  FILE* rollup = fopen("/proc/self/smaps_rollup", "r");
  bool anonymous = false, anon_huge = false;
  if (rollup != NULL)
  {
    while (getline(&line, &capacity, rollup) >= 0)
    {
      anonymous |= parse_kib(line, "Anonymous:", &memory.anonymous_kib);
      anon_huge |= parse_kib(line, "AnonHugePages:", &memory.anon_huge_kib);
    }
    fclose(rollup);
  }
  memory.rollup_ok = anonymous && anon_huge;
  free(line);
  return memory;
}

static size_t effective_resident(memory_kib_t memory)
{
  return memory.rss_kib + memory.hugetlb_kib;
}

static size_t maximum_size(size_t left, size_t right)
{
  return left > right ? left : right;
}

static void settle(unsigned milliseconds)
{
  struct timespec duration = {
    .tv_sec = milliseconds / 1000,
    .tv_nsec = (long)(milliseconds % 1000) * 1000000L,
  };
  while (nanosleep(&duration, &duration) != 0 && errno == EINTR)
  {}
}

static void fault_object(unsigned char* object, size_t bytes, uint64_t tag)
{
  for (size_t offset = 0; offset < bytes; offset += 4096)
    object[offset] = (unsigned char)(tag + offset / 4096);
  object[64] = (unsigned char)(tag ^ (tag >> 8));
  object[bytes - 1] = (unsigned char)(tag >> 8);
}

static bool maps_contain(const char* token)
{
  if (token == NULL || *token == '\0')
    return true;
  FILE* maps = fopen("/proc/self/maps", "r");
  if (maps == NULL)
    return false;
  char* line = NULL;
  size_t capacity = 0;
  bool found = false;
  while (getline(&line, &capacity, maps) >= 0)
  {
    if (strstr(line, token) != NULL)
    {
      found = true;
      break;
    }
  }
  free(line);
  fclose(maps);
  return found;
}

static const char* host_thp_mode(void)
{
  static char result[16] = "unknown";
  FILE* enabled = fopen("/sys/kernel/mm/transparent_hugepage/enabled", "r");
  if (enabled == NULL)
    return result;
  char buffer[256] = {0};
  if (fgets(buffer, sizeof(buffer), enabled) != NULL)
  {
    if (strstr(buffer, "[always]") != NULL)
      strcpy(result, "always");
    else if (strstr(buffer, "[madvise]") != NULL)
      strcpy(result, "madvise");
    else if (strstr(buffer, "[never]") != NULL)
      strcpy(result, "never");
  }
  fclose(enabled);
  return result;
}

int main(int argc, char** argv)
{
  config_t config;
  if (!parse_config(argc, argv, &config))
  {
    usage(argv[0]);
    return 2;
  }

  bool disable_requested = false;
  const char* disable_env = getenv("UNIALLOC_EVAL_THP_DISABLE");
  if (disable_env != NULL && strcmp(disable_env, "1") == 0)
  {
    disable_requested = true;
    if (prctl(PR_SET_THP_DISABLE, 1, 0, 0, 0) != 0)
    {
      perror("prctl(PR_SET_THP_DISABLE)");
      return 2;
    }
  }
  int process_thp_disabled = prctl(PR_GET_THP_DISABLE, 0, 0, 0, 0);
  if (process_thp_disabled < 0)
  {
    perror("prctl(PR_GET_THP_DISABLE)");
    return 2;
  }

  const char* preload_token = getenv("UNIALLOC_EVAL_PRELOAD_TOKEN");
  bool preload_observed = maps_contain(preload_token);
  memory_kib_t baseline = read_memory();
  const size_t long_count = config.objects / 2;
  const size_t short_count = config.objects - long_count;
  void** all = (void**)calloc(config.objects, sizeof(void*));
  void** retained = (void**)malloc(long_count * sizeof(void*));
  void** short_lived = (void**)malloc(short_count * sizeof(void*));
  void** wave = (void**)malloc(short_count * sizeof(void*));
  if (all == NULL || retained == NULL || short_lived == NULL || wave == NULL)
  {
    fprintf(stderr, "pointer table allocation failed\n");
    return 2;
  }

  size_t retained_index = 0, short_index = 0;
  uint64_t allocation_start = monotonic_ns();
  for (size_t index = 0; index < config.objects; index++)
  {
    unsigned char* object = (unsigned char*)malloc(config.slot_bytes);
    if (object == NULL)
    {
      fprintf(stderr, "object allocation failed at %zu\n", index);
      return 2;
    }
    all[index] = object;
    if ((index & 1U) == 0)
      retained[retained_index++] = object;
    else
      short_lived[short_index++] = object;
  }
  uint64_t allocation_ns = monotonic_ns() - allocation_start;

  uint64_t fault_start = monotonic_ns();
  for (size_t index = 0; index < config.objects; index++)
    fault_object((unsigned char*)all[index], config.slot_bytes, index + 1);
  uint64_t initial_fault_ns = monotonic_ns() - fault_start;
  settle(config.settle_ms);
  memory_kib_t peak = read_memory();

  uint64_t release_start = monotonic_ns();
  for (size_t index = 0; index < short_count; index++)
  {
    free(short_lived[index]);
    short_lived[index] = NULL;
  }
  uint64_t initial_release_ns = monotonic_ns() - release_start;
  settle(config.settle_ms);
  memory_kib_t steady = read_memory();

  uint64_t rng = config.seed;
  shuffle(retained, long_count, &rng);
  for (size_t index = 0; index < long_count; index++)
  {
    void* next = retained[(index + 1) % long_count];
    memcpy(retained[index], &next, sizeof(next));
  }

  volatile uint64_t checksum = UINT64_C(0xcbf29ce484222325);
  void* cursor = retained[0];
  for (size_t pass = 0; pass < config.warmup_passes; pass++)
  {
    for (size_t index = 0; index < long_count; index++)
    {
      void* next = NULL;
      memcpy(&next, cursor, sizeof(next));
      checksum ^= ((volatile unsigned char*)cursor)[64];
      checksum *= UINT64_C(0x100000001b3);
      cursor = next;
    }
  }
  uint64_t touch_start = monotonic_ns();
  for (size_t pass = 0; pass < config.passes; pass++)
  {
    for (size_t index = 0; index < long_count; index++)
    {
      void* next = NULL;
      memcpy(&next, cursor, sizeof(next));
      checksum ^= ((volatile unsigned char*)cursor)[64];
      checksum *= UINT64_C(0x100000001b3);
      cursor = next;
    }
  }
  uint64_t touch_ns = monotonic_ns() - touch_start;

  uint64_t wave_ns = 0;
  memory_kib_t wave_peak = {0};
  for (size_t wave_index = 0; wave_index < config.waves; wave_index++)
  {
    uint64_t wave_start = monotonic_ns();
    for (size_t index = 0; index < short_count; index++)
    {
      unsigned char* object = (unsigned char*)malloc(config.slot_bytes);
      if (object == NULL)
      {
        fprintf(stderr, "wave allocation failed at %zu\n", index);
        return 2;
      }
      wave[index] = object;
      fault_object(object, config.slot_bytes, config.objects + index + wave_index);
    }
    wave_ns += monotonic_ns() - wave_start;
    settle(config.settle_ms);
    memory_kib_t live_wave = read_memory();
    if (effective_resident(live_wave) > effective_resident(wave_peak))
      wave_peak = live_wave;
    wave_start = monotonic_ns();
    for (size_t index = 0; index < short_count; index++)
    {
      free(wave[index]);
      wave[index] = NULL;
    }
    wave_ns += monotonic_ns() - wave_start;
  }

  uint64_t teardown_start = monotonic_ns();
  for (size_t index = 0; index < long_count; index++)
    free(retained[index]);
  uint64_t teardown_ns = monotonic_ns() - teardown_start;
  free(wave);
  free(short_lived);
  free(retained);
  free(all);
  settle(config.settle_ms);
  memory_kib_t final_memory = read_memory();

  size_t max_effective = maximum_size(effective_resident(peak), effective_resident(steady));
  max_effective = maximum_size(max_effective, effective_resident(wave_peak));
  size_t max_anon_huge = maximum_size(peak.anon_huge_kib, steady.anon_huge_kib);
  max_anon_huge = maximum_size(max_anon_huge, wave_peak.anon_huge_kib);
  size_t max_hugetlb = maximum_size(peak.hugetlb_kib, steady.hugetlb_kib);
  max_hugetlb = maximum_size(max_hugetlb, wave_peak.hugetlb_kib);
  size_t anon_huge_delta = max_anon_huge > baseline.anon_huge_kib
    ? max_anon_huge - baseline.anon_huge_kib
    : 0;
  size_t hugetlb_delta = max_hugetlb > baseline.hugetlb_kib
    ? max_hugetlb - baseline.hugetlb_kib
    : 0;
  size_t peak_anon_delta = peak.anonymous_kib > baseline.anonymous_kib
    ? peak.anonymous_kib - baseline.anonymous_kib
    : 0;
  double thp_coverage = peak_anon_delta == 0
    ? 0.0
    : (double)anon_huge_delta / (double)peak_anon_delta;
  uint64_t total_allocations = (uint64_t)config.objects +
    (uint64_t)config.waves * (uint64_t)short_count;
  uint64_t lifecycle_ns = allocation_ns + initial_fault_ns + initial_release_ns +
    wave_ns + teardown_ns;
  uint64_t touches = (uint64_t)config.passes * (uint64_t)long_count;
  bool memory_ok = baseline.status_ok && baseline.rollup_ok && peak.status_ok &&
    peak.rollup_ok && steady.status_ok && steady.rollup_ok && wave_peak.status_ok &&
    wave_peak.rollup_ok && final_memory.status_ok && final_memory.rollup_ok;
  bool passed = memory_ok && preload_observed && checksum != 0 &&
    (!disable_requested || process_thp_disabled == 1);

  printf(
    "{\"source\":\"cross_allocator_large_page_workload\","
    "\"passed\":%s,\"objects\":%zu,\"slot_bytes\":%zu,"
    "\"long_objects\":%zu,\"short_objects\":%zu,\"passes\":%zu,"
    "\"warmup_passes\":%zu,\"waves\":%zu,\"seed\":%" PRIu64 ","
    "\"host_thp_mode\":\"%s\",\"process_thp_disable_requested\":%s,"
    "\"process_thp_disabled\":%d,\"preload_token_observed\":%s,"
    "\"memory_evidence_complete\":%s,\"allocation_ns\":%" PRIu64 ","
    "\"initial_fault_ns\":%" PRIu64 ",\"initial_release_ns\":%" PRIu64 ","
    "\"wave_ns\":%" PRIu64 ",\"teardown_ns\":%" PRIu64 ","
    "\"lifecycle_ns\":%" PRIu64 ",\"total_allocations\":%" PRIu64 ","
    "\"lifecycle_ns_per_allocation\":%.9f,\"touch_ns\":%" PRIu64 ","
    "\"touches\":%" PRIu64 ",\"ns_per_touch\":%.9f,"
    "\"baseline_rss_kib\":%zu,\"baseline_anonymous_kib\":%zu,"
    "\"baseline_anon_huge_kib\":%zu,\"baseline_hugetlb_kib\":%zu,"
    "\"peak_rss_kib\":%zu,\"peak_anonymous_kib\":%zu,"
    "\"peak_anon_huge_kib\":%zu,\"peak_hugetlb_kib\":%zu,"
    "\"steady_rss_kib\":%zu,\"steady_anonymous_kib\":%zu,"
    "\"steady_anon_huge_kib\":%zu,\"steady_hugetlb_kib\":%zu,"
    "\"wave_peak_rss_kib\":%zu,\"wave_peak_anonymous_kib\":%zu,"
    "\"wave_peak_anon_huge_kib\":%zu,\"wave_peak_hugetlb_kib\":%zu,"
    "\"final_rss_kib\":%zu,\"final_anon_huge_kib\":%zu,"
    "\"final_hugetlb_kib\":%zu,\"max_effective_resident_kib\":%zu,"
    "\"anon_huge_delta_kib\":%zu,\"hugetlb_delta_kib\":%zu,"
    "\"peak_thp_coverage\":%.9f,\"checksum\":\"%016" PRIx64 "\"}\n",
    passed ? "true" : "false",
    config.objects,
    config.slot_bytes,
    long_count,
    short_count,
    config.passes,
    config.warmup_passes,
    config.waves,
    config.seed,
    host_thp_mode(),
    disable_requested ? "true" : "false",
    process_thp_disabled,
    preload_observed ? "true" : "false",
    memory_ok ? "true" : "false",
    allocation_ns,
    initial_fault_ns,
    initial_release_ns,
    wave_ns,
    teardown_ns,
    lifecycle_ns,
    total_allocations,
    (double)lifecycle_ns / (double)total_allocations,
    touch_ns,
    touches,
    (double)touch_ns / (double)touches,
    baseline.rss_kib,
    baseline.anonymous_kib,
    baseline.anon_huge_kib,
    baseline.hugetlb_kib,
    peak.rss_kib,
    peak.anonymous_kib,
    peak.anon_huge_kib,
    peak.hugetlb_kib,
    steady.rss_kib,
    steady.anonymous_kib,
    steady.anon_huge_kib,
    steady.hugetlb_kib,
    wave_peak.rss_kib,
    wave_peak.anonymous_kib,
    wave_peak.anon_huge_kib,
    wave_peak.hugetlb_kib,
    final_memory.rss_kib,
    final_memory.anon_huge_kib,
    final_memory.hugetlb_kib,
    max_effective,
    anon_huge_delta,
    hugetlb_delta,
    thp_coverage,
    checksum);
  return passed ? 0 : 1;
}
